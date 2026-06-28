# Archived — pre-lossless logic (do not use)

This folder holds the original lossy token-efficiency stack, moved out of the active import
path on branch `feat/lossless-levers`. It is kept for reference/history only. **Nothing here
is on the live path.**

Why archived: these modules reduced tokens by *lossily* dropping/merging context
(`CommunicationCompressor`, `AdaptiveSemanticSampler`, `SmartContextPruner`) and gated quality
with a fake heuristic (`quality_proxy_score`) — see the review in the repo history. They were
replaced by the faithful, accuracy-first algorithms in `token_efficiency_model/lossless/`.

Contents: `combined_tactics`, `agent_communication_compression`, `adaptive_semantic_sampling`,
`smart_context_pruning`, `custom_protocol`, `task_aware_routing`, `shared_memory_layer`,
`context_store`, `common`, `modes`, `experts`, `optimizers`, `quality`, `experiments`.
