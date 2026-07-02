"""AutoGen analysis-fleet A/B over a BIG shared context (codebase or algorithms book).

The workload where cache-aware b9 should shine: a large shared document that multiple
distinct-role agents all analyze. Each agent's distinct system prompt sits first, so the
big shared context is stuck behind it and the provider can't cache it across agents —
until b9 v2 (BREVITAS_AUTO_SHARED_PREFIX=1) promotes it to a cacheable leading prefix,
but ONLY if the provider isn't already caching it (cache-aware gate).

Scenario 1 (codebase): shared context = real source files from the Brevitas repo.
Scenario 2 (book): shared context = text from the algorithms textbook PDF.

Routed through the Brevitas proxy; arm decided by proxy env (PASSTHROUGH). Metered.
Usage: python autogen_analysis_ab.py --scenario codebase --arm baseline
"""
import argparse, os, glob

PORT = int(os.environ.get("BREV_PORT", "4242"))
REPO = "/tmp/brev-wave-a"


def _load_env():
    for ln in open(f"{REPO}/.env.local"):
        if "=" in ln and not ln.strip().startswith("#"):
            k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip())


def codebase_context() -> str:
    files = [
        "token_efficiency_model/lossless/router.py",
        "token_efficiency_model/lossless/provider_cache.py",
        "token_efficiency_model/lossless/engine.py",
        "token_efficiency_model/lossless/shared_prefix.py",
    ]
    parts = []
    for rel in files:
        try:
            src = open(f"{REPO}/{rel}").read()
        except OSError:
            continue
        parts.append(f"### FILE: {rel}\n```python\n{src}\n```")
    return "\n\n".join(parts)


def book_context() -> str:
    txt = open("/tmp/oss-ab/algo_book.txt").read()
    return txt[:48000]   # ~12K tokens of the algorithms textbook


SCENARIOS = {
    "codebase": {
        "context": codebase_context,
        "intro": "SHARED CODEBASE (analyze together):\n",
        "agents": [
            ("Architect", "Lead architect. Assess module design and coupling in the shared codebase."),
            ("Security", "Security reviewer. Find injection/SSRF/secret-handling risks in the shared codebase."),
            ("Performance", "Performance engineer. Find hot paths / O(n^2) / re-encoding costs in the shared codebase."),
            ("TestEng", "Test engineer. Identify untested edge cases and propose tests for the shared codebase."),
        ],
        "kickoff": "Review this codebase together. Architect leads; each of you contributes from your discipline.",
    },
    "book": {
        "context": book_context,
        "intro": "SHARED TEXTBOOK EXCERPT (data structures & algorithms):\n",
        "agents": [
            ("Instructor", "CS instructor. Explain the key ideas in the shared textbook excerpt clearly."),
            ("Student", "Curious student. Ask sharp clarifying questions about the shared excerpt."),
            ("Examiner", "Examiner. Pose exam questions grounded ONLY in the shared excerpt."),
            ("Editor", "Technical editor. Flag anything unclear or incorrect in the shared excerpt."),
        ],
        "kickoff": "Study this textbook excerpt together. Instructor leads; keep it grounded in the text.",
    },
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True, choices=list(SCENARIOS))
    ap.add_argument("--arm", required=True)
    ap.add_argument("--rounds", type=int, default=8)
    # cache-isolation nonce: without it, arm A warms the provider's prefix cache for a
    # byte-identical arm B (measured: brevitas call0 hit 10112 cached tokens straight
    # from the baseline run) and the A/B comparison lies. A per-RUN nonce at the very
    # front of the context makes each run's prefix unique to the provider cache while
    # keeping both arms of the SAME run comparable (same nonce given via env).
    ap.add_argument("--nonce", default=os.environ.get("AB_NONCE", ""))
    args = ap.parse_args()
    _load_env()
    from autogen import AssistantAgent, GroupChat, GroupChatManager, UserProxyAgent

    sc = SCENARIOS[args.scenario]
    ctx = sc["context"]()
    if args.nonce:
        ctx = f"[run-id {args.nonce}/{args.arm}]\n{ctx}"
    cfg = {"config_list": [{"model": "deepseek-chat", "api_key": os.environ["Deepseek_api_key"],
                            "base_url": f"http://127.0.0.1:{PORT}/openai/v1"}],
           "cache_seed": None, "temperature": 0}   # temp=0 for a cleaner comparison

    def mk(name, role):
        return AssistantAgent(name, llm_config=cfg, system_message=(
            f"You are the {name}: {role} Engage with prior speakers by name; add one "
            f"concrete, specific point per turn grounded ONLY in the shared context. "
            f"~4-6 sentences. Never say TERMINATE."))

    agents = [mk(n, r) for n, r in sc["agents"]]
    pm = UserProxyAgent("Lead", human_input_mode="NEVER", code_execution_config=False,
                        max_consecutive_auto_reply=0, default_auto_reply="")
    gc = GroupChat(agents=list(agents), messages=[], max_round=args.rounds,
                   speaker_selection_method="round_robin")
    mgr = GroupChatManager(groupchat=gc, llm_config=cfg)
    pm.initiate_chat(mgr, message=f"{sc['intro']}{ctx}\n\n{sc['kickoff']}")
    print(f"[{args.scenario}/{args.arm}] done — {len(gc.messages)} messages, "
          f"~{len(ctx)//4} ctx tokens")


if __name__ == "__main__":
    main()
