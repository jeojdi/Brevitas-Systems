"""Incremental-context conversational session A/B — the realistic consumer pattern.

The shape most Brevitas customers actually have: a conversational assistant where the
user ADDS DATA, talks about it, adds MORE data, talks again — 5 cycles. Each cycle:
  turn A: "here is new data (part i): <~2K tokens>"  -> model acknowledges
  turn B: question about the new data                -> answer
  turn C: follow-up question across ALL data so far  -> answer
15 calls per provider per arm; the conversation history (system + all data + all turns)
is re-sent on every call, growing from ~2K to ~12K+ tokens — the repeating-prefix shape.

Both arms go through the Brevitas proxy (arm chosen by proxy env: PASSTHROUGH=1 baseline
vs optimized), metered identically from real provider usage (BREVITAS_METER_FILE).
Providers: anthropic (needs OUR cache markers), openai, deepseek (auto-cache). temp=0.
A per-run nonce in the first data chunk keeps arms cache-isolated.
"""
import argparse
import os
import time

REPO = "/tmp/brev-wave-a"
PORT = int(os.environ.get("BREV_PORT", "4242"))


def _load_env():
    for ln in open(f"{REPO}/.env.local"):
        if "=" in ln and not ln.strip().startswith("#"):
            k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip())


def data_chunks(n=5):
    txt = open("/tmp/oss-ab/algo_book.txt").read()
    step = len(txt) // n
    return [txt[i * step:(i + 1) * step] for i in range(n)]


SYSTEM = ("You are a meticulous data-analysis assistant. The user will progressively "
          "share parts of a technical document and ask questions. Answer concisely "
          "(<=6 sentences), grounded ONLY in the shared material.")

QUESTIONS = [
    ("What is the main topic introduced in the newest part?",
     "Across everything shared so far, list the key concepts covered in one line each."),
    ("Summarize the most important definition or claim in the newest part.",
     "How does the newest part relate to the earlier parts?"),
    ("What example or illustration does the newest part use?",
     "Which single concept so far is most central, and why?"),
    ("What would a student find hardest in the newest part?",
     "Give a 3-bullet running summary of the whole document so far."),
    ("What is one limitation or caveat in the newest part?",
     "If you had to give the whole shared document a title, what would it be?"),
]

MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
}


def call_openai_style(client, model, messages):
    r = client.chat.completions.create(model=model, messages=messages,
                                       max_tokens=300, temperature=0)
    return r.choices[0].message.content or ""


def call_anthropic(client, model, system, messages):
    r = client.messages.create(model=model, system=system, messages=messages,
                               max_tokens=300, temperature=0)
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def run_provider(prov, arm, nonce, chunks):
    print(f"--- {prov} ({MODELS[prov]}) arm={arm}", flush=True)
    if prov == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],
                                     base_url=f"http://127.0.0.1:{PORT}", max_retries=2)
        history = []          # anthropic: system passed separately
    else:
        import openai
        key = os.environ["OPENAI_API_KEY"] if prov == "openai" else os.environ["Deepseek_api_key"]
        client = openai.OpenAI(api_key=key, base_url=f"http://127.0.0.1:{PORT}/openai/v1",
                               max_retries=2)
        history = [{"role": "system", "content": SYSTEM}]

    def ask(text):
        history.append({"role": "user", "content": text})
        for attempt in (1, 2, 3):
            try:
                if prov == "anthropic":
                    out = call_anthropic(client, MODELS[prov], SYSTEM, history)
                else:
                    out = call_openai_style(client, MODELS[prov], history)
                break
            except Exception as e:
                if attempt == 3:
                    raise
                print(f"    retry {attempt}: {type(e).__name__}: {e}", flush=True)
                time.sleep(4)
        history.append({"role": "assistant", "content": out})
        time.sleep(0.4)
        return out

    for i, chunk in enumerate(chunks):
        tag = f"[session {nonce}/{arm}]\n" if i == 0 else ""
        ask(f"{tag}NEW DATA (part {i + 1} of {len(chunks)}):\n{chunk}\n\n"
            f"Acknowledge receipt and note in one sentence what this part covers.")
        q1, q2 = QUESTIONS[i]
        ask(q1)
        ask(q2)
        print(f"  cycle {i + 1}/5 done", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True)
    ap.add_argument("--nonce", default=os.environ.get("AB_NONCE", ""))
    ap.add_argument("--providers", default="anthropic,openai,deepseek")
    ap.add_argument("--cycles", type=int, default=5)
    args = ap.parse_args()
    _load_env()
    chunks = data_chunks(args.cycles)
    for prov in args.providers.split(","):
        run_provider(prov.strip(), args.arm, args.nonce, chunks)
    print(f"[{args.arm}] all providers done", flush=True)


if __name__ == "__main__":
    main()
