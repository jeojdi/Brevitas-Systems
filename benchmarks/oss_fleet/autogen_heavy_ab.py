"""Heavy AutoGen agent-to-agent A/B — ONE complex problem, ~20 conversational rounds.

Unlike the earlier 4-agent single pass, this is a long multi-agent DEBATE: 5 distinct-role
agents argue/refine one complex system-design problem for ~20 rounds. The shared problem
brief + the growing transcript are re-sent to a distinct-role agent EVERY round — heavy
repetition of a large shared context, which is exactly where provider caching (and our b9
shared-prefix promotion) should bite hard. This is the realistic "lots of agent-to-agent
talking about the same topic" workload.

Routed through the Brevitas proxy on DeepSeek. Baseline vs Brevitas by proxy env
(BREVITAS_PASSTHROUGH); both metered (BREVITAS_METER_FILE). Run once per arm.

Usage: python autogen_heavy_ab.py --arm baseline --rounds 22
       python autogen_heavy_ab.py --arm brevitas --rounds 22
"""
import argparse, os

PORT = int(os.environ.get("BREV_PORT", "4242"))


def _load_env():
    for ln in open("/tmp/brev-wave-a/.env.local"):
        if "=" in ln and not ln.strip().startswith("#"):
            k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True)
    ap.add_argument("--rounds", type=int, default=22)
    args = ap.parse_args()
    _load_env()
    from autogen import AssistantAgent, GroupChat, GroupChatManager, UserProxyAgent

    cfg = {"config_list": [{
        "model": "deepseek-chat",
        "api_key": os.environ["Deepseek_api_key"],
        "base_url": f"http://127.0.0.1:{PORT}/openai/v1",
    }], "cache_seed": None, "temperature": 0.3}

    # One complex problem — rich enough to sustain 20 rounds of genuine debate.
    problem = (
        "COMPLEX DESIGN PROBLEM (shared context for the whole board):\n"
        "Design a globally-distributed, multi-tenant API rate limiter for a platform serving "
        "50k customers and 2M req/s at peak across 6 regions. Requirements: per-tenant and "
        "per-endpoint limits; sliding-window fairness; sub-millisecond decision latency; "
        "survive a full region outage with no global lockout; consistent-enough counters "
        "without a global lock; hot-key / celebrity-tenant handling; graceful degradation "
        "under Redis failure; auditability of every throttle decision for billing disputes; "
        "and a config plane that propagates limit changes in <5s worldwide. Constraints: "
        "budget-conscious (no exotic hardware), must run on commodity cloud + Redis/Kafka, "
        "and the SLA is 99.99%. Discuss trade-offs explicitly; do not hand-wave. " * 4
    )

    def agent(name, role):
        return AssistantAgent(name, llm_config=cfg, system_message=(
            f"You are the {role} on a design review board debating ONE shared problem. "
            f"Engage with the PREVIOUS speakers by name, push back where you disagree, and "
            f"add one concrete, specific contribution per turn from your discipline. Keep "
            f"each turn to ~4-6 sentences. Never say TERMINATE; the chair ends the session."))

    agents = [
        agent("Architect", "Lead Systems Architect"),
        agent("SRE", "Site-Reliability / failure-modes engineer"),
        agent("DataEng", "Data & consistency engineer (Redis/Kafka)"),
        agent("Security", "Security & multi-tenant-isolation reviewer"),
        agent("Skeptic", "Adversarial cost/complexity skeptic"),
    ]
    pm = UserProxyAgent("Chair", human_input_mode="NEVER", code_execution_config=False,
                        max_consecutive_auto_reply=0, default_auto_reply="")

    # Only the 5 assistants rotate (Chair is NOT in the rotation, so it can't terminate
    # the debate early) — they discuss for the full max_round turns.
    gc = GroupChat(agents=list(agents), messages=[], max_round=args.rounds,
                   speaker_selection_method="round_robin")
    mgr = GroupChatManager(groupchat=gc, llm_config=cfg)
    pm.initiate_chat(mgr, message=f"{problem}\n\nBegin the design review. Architect leads.")
    print(f"[{args.arm}] debate done — {len(gc.messages)} messages")


if __name__ == "__main__":
    main()
