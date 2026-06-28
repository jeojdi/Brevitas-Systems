import json
import zlib
import base64
from typing import Any, Dict, List


class AgentProtocol:
    FIELD_MAP = {
        "task_id": "t",
        "model": "m",
        "summary": "s",
        "context_refs": "c",
        "instructions": "i",
        "priority": "p",
        "base_state_id": "b",
        "delta_ops": "d",
        "ack_id": "a",
        "rehydrate_policy": "r",
        "wire_mode": "w",
        "is_delta": "x",
    }

    def encode(self, payload: Dict[str, Any], mode: str = "compact", wire_mode: str = "json") -> str:
        if mode == "raw-json":
            serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            return self._to_wire(serialized, wire_mode)
        compact = {}
        for key, value in payload.items():
            mapped = self.FIELD_MAP.get(key, key)
            compact[mapped] = value
        serialized = json.dumps(compact, separators=(",", ":"), ensure_ascii=False)

        # For human-consumable prompts, add a short readable header to guide model behavior
        if wire_mode != "binary":
            header = (
                "### TOKEN-EFFICIENT PAYLOAD ###\n"
                "# compact fields: t=task_id, m=model, s=summary, c=context_refs, i=instructions, p=priority\n"
                "# b=base_state_id, d=delta_ops, a=ack_id, r=rehydrate_policy, w=wire_mode, x=is_delta\n"
            )
            return header + serialized

        return self._to_wire(serialized, wire_mode)

    def _to_wire(self, serialized: str, wire_mode: str) -> str:
        if wire_mode != "binary":
            return serialized

        compressed = zlib.compress(serialized.encode("utf-8"), level=9)
        return "bin:" + base64.b64encode(compressed).decode("utf-8")

    def _from_wire(self, encoded: str) -> str:
        if not encoded.startswith("bin:"):
            return encoded

        payload = encoded[4:]
        decoded = base64.b64decode(payload.encode("utf-8"))
        return zlib.decompress(decoded).decode("utf-8")

    def decode(self, encoded: str) -> Dict[str, Any]:
        normalized = self._from_wire(encoded)
        data = json.loads(normalized)
        reverse_map = {v: k for k, v in self.FIELD_MAP.items()}
        expanded = {}
        for key, value in data.items():
            expanded[reverse_map.get(key, key)] = value
        return expanded

    def build_payload(
        self,
        task_id: str,
        model: str,
        summary: str,
        context_refs: List[str],
        instructions: str,
        priority: float,
        base_state_id: str = "",
        delta_ops: List[Dict[str, Any]] = None,
        ack_id: str = "",
        rehydrate_policy: str = "on-miss",
        wire_mode: str = "json",
        is_delta: bool = False,
    ) -> Dict[str, Any]:
        if delta_ops is None:
            delta_ops = []
        return {
            "task_id": task_id,
            "model": model,
            "summary": summary,
            "context_refs": context_refs,
            "instructions": instructions,
            "priority": round(priority, 3),
            "base_state_id": base_state_id,
            "delta_ops": delta_ops,
            "ack_id": ack_id,
            "rehydrate_policy": rehydrate_policy,
            "wire_mode": wire_mode,
            "is_delta": is_delta,
        }
