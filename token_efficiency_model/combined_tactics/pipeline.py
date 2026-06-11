from typing import Callable, Dict, List, Optional, Any

from ..agent_communication_compression import CommunicationCompressor
from ..adaptive_semantic_sampling import AdaptiveSemanticSampler
from ..common.metrics import estimate_tokens, estimate_tokens_many, quality_proxy_score, savings_pct
from ..common.types import PipelineResult, TaskPacket
from ..custom_protocol import AgentProtocol
from ..shared_memory_layer import SharedMemoryLayer
from ..smart_context_pruning import SmartContextPruner
from ..task_aware_routing import TaskAwareRouter


class TokenEfficientPipeline:
    def __init__(
        self,
        model_backend: Optional[Callable[[str, str], str]] = None,
        memory_persistence_path: str = "",
        quality_floor: float = 0.98,
        savings_target: float = 70.0,
        max_tuning_attempts: int = 3,
    ):
        self.router = TaskAwareRouter()
        self.protocol = AgentProtocol()
        self.memory = SharedMemoryLayer(persistence_path=memory_persistence_path)
        self.model_backend = model_backend or self._default_model_backend
        self.quality_floor = quality_floor
        self._turn_count = 0
        
        # Initialize adaptive semantic sampler for advanced context selection
        self.semantic_sampler = AdaptiveSemanticSampler(
            budget=4,
            relevance_weight=0.35,
            frequency_weight=0.25,
            recency_weight=0.20,
            entropy_weight=0.20,
            novelty_weight=0.40,
        )
        # Target savings (%) the pipeline will try to achieve by tuning
        self.savings_target = savings_target
        self.max_tuning_attempts = max_tuning_attempts

    def _dynamic_sampling_policy(self, packet: TaskPacket, prune_budget: int) -> Dict[str, float]:
        context_size = len(packet.prior_context)
        complexity = max(0.0, min(1.0, packet.complexity))
        urgency = max(0.0, min(1.0, packet.urgency))
        context_load = min(1.0, (context_size + len(packet.incoming_messages)) / 24.0)

        adaptive_budget = int(
            round(
                prune_budget
                + (2.0 * complexity)
                + (1.0 * context_load)
                - (0.5 * urgency)
            )
        )
        adaptive_budget = max(2, min(max(prune_budget, 2) + 3, adaptive_budget))

        relevance_weight = 0.30 + 0.35 * complexity
        frequency_weight = 0.20 + 0.30 * context_load
        recency_weight = 0.20 + 0.35 * urgency
        entropy_weight = 0.15 + 0.20 * (1.0 - context_load)
        novelty_weight = 0.22 + 0.45 * context_load

        return {
            "budget": float(adaptive_budget),
            "relevance_weight": relevance_weight,
            "frequency_weight": frequency_weight,
            "recency_weight": recency_weight,
            "entropy_weight": entropy_weight,
            "novelty_weight": novelty_weight,
            "context_load": context_load,
        }

    def _default_model_backend(self, prompt: str, model_name: str) -> str:
        return f"[{model_name}] simulated response to: {prompt[:120]}"

    def process_task(
        self,
        task_text: str,
        incoming_messages: List[str],
        prior_context: List[str],
        task_id: str = "task-001",
        complexity: float = 0.5,
        urgency: float = 0.5,
        compression_level: int = 2,
        prune_budget: int = 5,
        protocol_mode: str = "compact",
        delta_mode: str = "off",
        delta_aggressiveness: int = 1,
        wire_mode: str = "json",
        *,
        must_keep_facts: Optional[List[str]] = None,
    ) -> PipelineResult:
        packet = TaskPacket(
            task_id=task_id,
            task_text=task_text,
            incoming_messages=incoming_messages,
            prior_context=prior_context,
            complexity=complexity,
            urgency=urgency,
        )
        return self.run(
            packet,
            compression_level,
            prune_budget,
            protocol_mode,
            delta_mode=delta_mode,
            delta_aggressiveness=delta_aggressiveness,
            wire_mode=wire_mode,
            must_keep_facts=must_keep_facts,
        )

    def _build_state_values(
        self,
        packet: TaskPacket,
        summary: str,
        context_refs: List[str],
        route_model: str,
    ) -> Dict[str, Any]:
        return {
            "task_text": packet.task_text,
            "summary": summary,
            "context_refs": context_refs,
            "route_model": route_model,
            "urgency": round(packet.urgency, 3),
            "complexity": round(packet.complexity, 3),
        }

    def run(
        self,
        packet: TaskPacket,
        compression_level: int,
        prune_budget: int,
        protocol_mode: str,
        delta_mode: str = "off",
        delta_aggressiveness: int = 1,
        wire_mode: str = "json",
        must_keep_facts: Optional[List[str]] = None,
    ) -> PipelineResult:
        baseline_tokens = estimate_tokens(packet.task_text)
        baseline_tokens += estimate_tokens_many(packet.incoming_messages)
        baseline_tokens += estimate_tokens_many(packet.prior_context)

        # Helper to evaluate a given compression/prune configuration
        def evaluate_config(compression_lvl: int, prune_bgt: int):
            compressor = CommunicationCompressor(level=compression_lvl)
            compressed_msgs, compression_stats = compressor.compress_messages(packet.incoming_messages)

            policy = self._dynamic_sampling_policy(packet, prune_bgt)
            self.semantic_sampler.relevance_weight = policy["relevance_weight"]
            self.semantic_sampler.frequency_weight = policy["frequency_weight"]
            self.semantic_sampler.recency_weight = policy["recency_weight"]
            self.semantic_sampler.entropy_weight = policy["entropy_weight"]
            self.semantic_sampler.novelty_weight = policy["novelty_weight"]

            sampled_context, sampling_metrics = self.semantic_sampler.sample(
                contexts=packet.prior_context,
                task_text=packet.task_text,
                adaptive_budget=int(policy["budget"]),
            )

            pruner = SmartContextPruner(budget=max(1, int(prune_bgt * 0.8)))
            pruned_context, pruning_scores = pruner.prune(packet.task_text, sampled_context)

            inline_chunks, context_refs = self.memory.materialize_or_reference(pruned_context + compressed_msgs)

            summary = compressed_msgs[0] if compressed_msgs else packet.task_text[:120]
            current_values = self._build_state_values(packet, summary, context_refs, route.model_name)

            # Build payload either delta or full (without changing memory yet)
            base_state_id_local = self.memory.latest_state_id()
            can_use_delta_local = (delta_mode != "off") and bool(base_state_id_local) and self.memory.has_state(base_state_id_local)

            if can_use_delta_local:
                delta_ops = self.memory.compute_delta(base_state_id_local, current_values)
                if delta_aggressiveness >= 3:
                    delta_ops = delta_ops[: max(1, len(delta_ops) // 2)]
                payload_local = self.protocol.build_payload(
                    task_id=packet.task_id,
                    model=route.model_name,
                    summary=summary,
                    context_refs=context_refs,
                    instructions="",
                    priority=packet.urgency,
                    base_state_id=base_state_id_local,
                    delta_ops=delta_ops,
                    ack_id=base_state_id_local,
                    rehydrate_policy="on-miss",
                    wire_mode=wire_mode,
                    is_delta=True,
                )
            else:
                payload_local = self.protocol.build_payload(
                    task_id=packet.task_id,
                    model=route.model_name,
                    summary=summary,
                    context_refs=context_refs,
                    instructions=packet.task_text,
                    priority=packet.urgency,
                    base_state_id=base_state_id_local,
                    delta_ops=[],
                    ack_id="",
                    rehydrate_policy="full",
                    wire_mode=wire_mode,
                    is_delta=False,
                )

            protocol_payload_local = self.protocol.encode(payload_local, mode=protocol_mode, wire_mode=wire_mode)

            steady_state_tokens_local = estimate_tokens(protocol_payload_local)
            cold_start_tokens_local = estimate_tokens(protocol_payload_local) + estimate_tokens_many(inline_chunks)
            optimized_tokens_local = cold_start_tokens_local if self._turn_count == 0 else steady_state_tokens_local + estimate_tokens_many(inline_chunks)
            savings_local = savings_pct(baseline_tokens, optimized_tokens_local)

            compression_strength_local = (compression_lvl - 1) / 2.0
            prune_strength_local = max(0.0, min(1.0, 1.0 - (prune_bgt / max(1, len(packet.prior_context) or 1))))
            quality_local = quality_proxy_score(compression_strength_local, prune_strength_local, route.route_fit)

            return {
                "compressed_messages": compressed_msgs,
                "compression_stats": compression_stats,
                "sampled_context": sampled_context,
                "sampling_metrics": sampling_metrics,
                "pruned_context": pruned_context,
                "pruning_scores": pruning_scores,
                "inline_chunks": inline_chunks,
                "context_refs": context_refs,
                "base_state_id": base_state_id_local,
                "protocol_payload": protocol_payload_local,
                "steady_state_tokens": steady_state_tokens_local,
                "cold_start_tokens": cold_start_tokens_local,
                "optimized_tokens": optimized_tokens_local,
                "savings": savings_local,
                "quality": quality_local,
            }
        # Determine routing once (used by evaluation helper)
        # initial lightweight context_load estimate
        init_context_load = min(1.0, (len(packet.prior_context) + len(packet.incoming_messages)) / 15.0)
        route = self.router.route(
            {
                "complexity": packet.complexity,
                "urgency": packet.urgency,
                "context_load": init_context_load,
            }
        )

        # tuning loop: try to reach savings_target by adjusting compression/prune
        attempts = 0
        best_eval = None
        cur_compression = compression_level
        cur_prune = prune_budget

        # model-specific conservative caps
        is_small_model = "llama" in route.model_name and "large" not in route.model_name

        while attempts < self.max_tuning_attempts:
            eval_res = evaluate_config(cur_compression, cur_prune)

            # track best
            if best_eval is None or eval_res["savings"] > best_eval["savings"]:
                best_eval = eval_res

            if eval_res["savings"] >= self.savings_target and eval_res["quality"] >= self.quality_floor:
                # target met with acceptable quality
                break

            # If quality is below floor, stop tuning to avoid rehydrations
            if eval_res["quality"] < self.quality_floor:
                break

            # otherwise, increase compression/pruning conservatively depending on model
            if is_small_model:
                cur_compression = min(cur_compression + 1, 2)
                cur_prune = min(cur_prune + 1, max(1, len(packet.prior_context)))
            else:
                cur_compression = min(cur_compression + 1, 3)
                cur_prune = min(cur_prune + 2, max(1, len(packet.prior_context)))

            attempts += 1

        # Use best_eval to proceed with actual model call and snapshot save
        final = best_eval or evaluate_config(compression_level, prune_budget)

        protocol_payload = final["protocol_payload"]
        inline_chunks = final["inline_chunks"]
        compression_stats = final.get("compression_stats")
        sampling_metrics = final.get("sampling_metrics", {})
        pruning_scores = final.get("pruning_scores", {})
        context_refs = final.get("context_refs", [])

        # If quality below floor, force a full rehydrate payload (safety)
        rehydration_events = 0
        if final["quality"] < self.quality_floor:
            rehydration_events += 1
            payload = self.protocol.build_payload(
                task_id=packet.task_id,
                model=route.model_name,
                summary=packet.task_text[:120],
                context_refs=context_refs,
                instructions=packet.task_text,
                priority=packet.urgency,
                base_state_id=self.memory.latest_state_id(),
                delta_ops=[],
                ack_id="",
                rehydrate_policy="force-full",
                wire_mode=wire_mode,
                is_delta=False,
            )
            protocol_payload = self.protocol.encode(payload, mode=protocol_mode, wire_mode=wire_mode)

        prompt = f"{protocol_payload}\nINLINE_CONTEXT={inline_chunks}"
        model_response = self.model_backend(prompt, route.model_name)

        # Persist snapshot only after a successful model call
        new_state_id = self.memory.save_snapshot(packet.task_id, self._build_state_values(packet, packet.task_text[:120], context_refs, route.model_name))
        self._turn_count += 1

        return PipelineResult(
            routed_model=route.model_name,
            baseline_tokens=baseline_tokens,
            optimized_tokens=final["optimized_tokens"],
            steady_state_tokens=final["steady_state_tokens"],
            cold_start_tokens=final["cold_start_tokens"],
            savings_pct=final["savings"],
            quality_proxy=final["quality"],
            protocol_payload=protocol_payload,
            model_response=model_response,
            debug={
                "compression": {
                    "original_tokens": compression_stats.original_tokens if compression_stats else None,
                    "compressed_tokens": compression_stats.compressed_tokens if compression_stats else None,
                },
                "adaptive_sampling": {
                    "sampled_count": sampling_metrics.get("sampled_count", 0),
                    "total_count": sampling_metrics.get("total_count", 0),
                    "average_relevance": sampling_metrics.get("average_relevance", 0.0),
                    "average_importance": sampling_metrics.get("average_importance", 0.0),
                    "diversity_score": sampling_metrics.get("diversity_score", 0.0),
                    "average_novelty_gain": sampling_metrics.get("average_novelty_gain", 0.0),
                    "anchors_preserved": sampling_metrics.get("anchors_preserved", 0),
                    "policy_budget": int(self._dynamic_sampling_policy(packet, cur_prune)["budget"]),
                    "policy_context_load": self._dynamic_sampling_policy(packet, cur_prune)["context_load"],
                },
                "pruning_scores": pruning_scores,
                "inline_chunks_count": len(inline_chunks),
                "context_refs_count": len(context_refs),
                "cache_hit_rate": 1 if delta_mode != "off" and bool(self.memory.latest_state_id()) and self.memory.has_state(self.memory.latest_state_id()) else 0,
                "rehydration_events": rehydration_events,
                "delta_mode": delta_mode,
                "wire_mode": wire_mode,
                "state_id": new_state_id,
                "base_state_id": final.get("base_state_id", ""),
                "pruned_context": final.get("pruned_context", []),
                "compressed_messages": final.get("compressed_messages", []),
                "inline_chunks": inline_chunks,
                "tuning_attempts": attempts,
                "savings_target": self.savings_target,
                "target_reached": final["savings"] >= self.savings_target,
            },
        )
