"""Concurrent same-prefix burst on ANTHROPIC (Haiku): pathfinder ON vs OFF.
6 requests share a ~4.7K-token doc block (above Haiku's 4096 cache minimum) inside one
user message (alternating-role constraint) + a distinct final question block."""
import asyncio, os, sys
import httpx

def _load_env():
    for ln in open("/tmp/brev-wave-a/.env.local"):
        if "=" in ln and not ln.strip().startswith("#"):
            k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip())

PREFIX = open("/tmp/oss-ab/algo_book.txt").read()[:20000]   # ~4.7K tokens

async def one(client, i, arm, nonce):
    body = {"model": "claude-haiku-4-5-20251001", "temperature": 0, "max_tokens": 60,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": f"[burst {nonce}/{arm}]\n{PREFIX}"},
                {"type": "text", "text": f"Agent {i}: ONE sentence — what does this text "
                                         f"say about data structures? (angle {i})"}]}]}
    r = await client.post("http://127.0.0.1:4242/v1/messages",
                          headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                   "anthropic-version": "2023-06-01"},
                          json=body, timeout=180)
    u = r.json().get("usage", {})
    return (u.get("input_tokens", 0), u.get("cache_creation_input_tokens", 0) or 0,
            u.get("cache_read_input_tokens", 0) or 0)

async def main():
    arm, nonce = sys.argv[1], sys.argv[2]
    _load_env()
    async with httpx.AsyncClient() as c:
        res = await asyncio.gather(*[one(c, i, arm, nonce) for i in range(6)])
    fr = sum(f for f, _, _ in res); w = sum(x for _, x, _ in res); rd = sum(x for _, _, x in res)
    for i, (f, x, r2) in enumerate(res):
        print(f"  call{i}: fresh={f} write={x} read={r2}")
    usd = (fr + 1.25 * w + 0.10 * rd) / 1e6
    print(f"[{arm}] fresh={fr} write={w} read={rd} input_cost=${usd:.6f}")

asyncio.run(main())
