"""Concurrent same-prefix burst A/B: pathfinder grouping ON vs OFF (DeepSeek).
6 requests share a ~2K-token prefix and fire simultaneously; only the final question
differs. Ungrouped: all race the cache and re-pay the prefix. Grouped: one pathfinder
writes it, 5 siblings read it."""
import asyncio, json, os, sys
import httpx

def _load_env():
    for ln in open("/tmp/brev-wave-a/.env.local"):
        if "=" in ln and not ln.strip().startswith("#"):
            k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip())

PREFIX = open("/tmp/oss-ab/algo_book.txt").read()[:8000]

MODELS = {"deepseek": ("deepseek-chat", "Deepseek_api_key"),
          "openai": ("gpt-4o-mini", "OPENAI_API_KEY")}
PROV = "openai"

async def one(client, i, arm, nonce):
    model, key_env = MODELS[PROV]
    body = {"model": model, "temperature": 0, "max_tokens": 60,
            "messages": [
                {"role": "user", "content": f"[burst {nonce}/{arm}]\n{PREFIX}"},
                {"role": "user", "content": f"Agent {i}: in ONE sentence, what does this text say about data structures? (perspective {i})"}]}
    r = await client.post("http://127.0.0.1:4242/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {os.environ[MODELS[PROV][1]]}"},
                          json=body, timeout=180)
    u = r.json().get("usage", {})
    return (u.get("prompt_tokens", 0),
            (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)

async def main():
    arm, nonce = sys.argv[1], sys.argv[2]
    global PROV
    PROV = sys.argv[3] if len(sys.argv) > 3 else "openai"
    _load_env()
    async with httpx.AsyncClient() as c:
        res = await asyncio.gather(*[one(c, i, arm, nonce) for i in range(6)])
    tot = sum(p for p, _ in res); cc = sum(x for _, x in res)
    for i, (p, x) in enumerate(res):
        print(f"  call{i}: prompt={p} cached={x}")
    print(f"[{arm}] total prompt={tot} cached={cc} ({100*cc//max(1,tot)}%)")

asyncio.run(main())
