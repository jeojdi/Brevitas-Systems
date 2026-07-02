"""AutoGen group-chat fleet A/B through Brevitas.

A real AutoGen GroupChat: 4 distinct-role agents (Architect, Engineer, Reviewer,
Documenter) collaborate on one task. The group conversation transcript is the SHARED,
GROWING context re-sent to every agent each turn — the workload where a gateway can
save by caching/stabilizing the shared prefix across distinct-role agents.

All LLM traffic routed at the OpenAI-compatible base_url to the Brevitas proxy. Baseline
vs Brevitas is decided by the proxy env (BREVITAS_PASSTHROUGH); both metered identically
(BREVITAS_METER_FILE). Run this script once per arm (proxy restarted between arms).

Usage: python autogen_fleet_ab.py --arm baseline   (proxy in passthrough mode)
       python autogen_fleet_ab.py --arm brevitas    (proxy optimizing)
"""
import argparse, os, sys

PORT = int(os.environ.get("BREV_PORT", "4242"))


def _load_env():
    for ln in open("/tmp/brev-wave-a/.env.local"):
        if "=" in ln and not ln.strip().startswith("#"):
            k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True)
    ap.add_argument("--rounds", type=int, default=8)
    args = ap.parse_args()
    _load_env()

    from autogen import AssistantAgent, GroupChat, GroupChatManager, UserProxyAgent

    # route EVERYTHING through the Brevitas proxy (OpenAI-compatible), model=deepseek-chat
    cfg = {"config_list": [{
        "model": "deepseek-chat",
        "api_key": os.environ["Deepseek_api_key"],
        "base_url": f"http://127.0.0.1:{PORT}/openai/v1",
    }], "cache_seed": None, "temperature": 0}

    # a shared spec = the big shared context every agent sees, re-sent as the transcript grows
    spec = ("PROJECT SPEC (shared): Build a Python CLI 'todo' app. Requirements: add/list/"
            "done/delete commands; JSON file storage at ~/.todo.json; argparse interface; "
            "each task has id, text, done, created_at; graceful errors; a --help for each "
            "subcommand; unit-testable core separated from CLI. Coding standards: type hints, "
            "docstrings, no external deps beyond stdlib. " * 6)

    architect = AssistantAgent("Architect", llm_config=cfg,
        system_message="You are the Architect. Define modules, data model, and function "
                       "signatures for the shared spec. Be concrete and concise.")
    engineer = AssistantAgent("Engineer", llm_config=cfg,
        system_message="You are the Engineer. Implement the code per the Architect's design "
                       "and the shared spec. Output Python in one code block.")
    reviewer = AssistantAgent("Reviewer", llm_config=cfg,
        system_message="You are the Reviewer. Critique the Engineer's code against the spec; "
                       "list concrete fixes. Be terse.")
    documenter = AssistantAgent("Documenter", llm_config=cfg,
        system_message="You are the Documenter. Write a short README usage section from the "
                       "final design. Terse.")
    user = UserProxyAgent("PM", human_input_mode="NEVER", code_execution_config=False,
                          max_consecutive_auto_reply=0,
                          default_auto_reply="")

    gc = GroupChat(agents=[user, architect, engineer, reviewer, documenter],
                   messages=[], max_round=args.rounds, speaker_selection_method="round_robin")
    mgr = GroupChatManager(groupchat=gc, llm_config=cfg)

    user.initiate_chat(mgr, message=f"{spec}\n\nCollaborate to design, implement, review, "
                       f"and document this app. Architect first.")
    print(f"[{args.arm}] group chat done, {len(gc.messages)} messages")


if __name__ == "__main__":
    main()
