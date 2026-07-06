# Brevitas `bvx` — Cofounder Handoff (Brew packaging + test run)

This doc is written for **your Claude** to (1) run and verify the tool locally, then
(2) package it for Homebrew. Everything below is on branch `phase1-native-caching`,
commits `7d8b12a` and `fdf012d`.

---

## 0. What `bvx` is (30-second version)

`bvx` finds every AI API call in a codebase, routes those calls through a **local
Brevitas proxy** that compresses each request **losslessly** and records the savings.

Four commands:
| Command | Does |
|---|---|
| `bvx scan <path>` | Finds all AI API calls; **opens a visual popup** of which files call which providers |
| `bvx install <path>` | Writes `.env.brevitas` routing; `--auto` rewrites hardcoded provider URLs |
| `bvx start` | One command: starts compression engine + proxy, auto-mints a local key |
| `bvx dash <path>` | Local HTML dashboard of calls + live savings |

**Lossless guarantee:** every shipped path (proxy + both SDK wrappers) forces
`lossless=True`. It only removes exact-duplicate sentences — it never drops a fact.
Honest tradeoff: near-zero savings on unique one-off prompts; real savings come from
**provider caching on repeated context** (chatbots, agent fleets).

---

## 1. Test it locally FIRST (before packaging)

```bash
cd <repo-root>            # must be the repo root — the engine is api.server:app
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install numpy uvicorn 'openai>=1.30'   # numpy: needed by token_efficiency_model/lossless

# 1) Scan — a browser popup opens showing which files make API calls
bvx scan .

# 2) Prove lossless (no network, no key): must print "all facts preserved"
python -m brevitas._compress

# 3) One-command stack: starts engine + proxy, mints a local key at ~/.brevitas/key
bvx start        # leave running in one terminal; Ctrl-C to stop
```

In a **second terminal**, fire a real call through the proxy (needs a provider key):

```bash
export DEEPSEEK_API_KEY=sk-...            # a throwaway/test key
python - <<'PY'
from openai import OpenAI
c = OpenAI(api_key=__import__('os').environ['DEEPSEEK_API_KEY'],
           base_url="http://127.0.0.1:4242/openai")   # ← the proxy
r = c.chat.completions.create(model="deepseek-chat",
      messages=[{"role":"user","content":"Say hello in 3 words."}])
print("reply via proxy:", r.choices[0].message.content)
PY
```

Expected: a real reply comes back **through the proxy**. Check recorded savings:

```bash
curl -s http://127.0.0.1:8000/v1/stats -H "X-API-Key: $(cat ~/.brevitas/key)" \
  | python3 -m json.tool | head -20
```

> Note: on a short unique prompt, lossless saves ~0, so `total_calls` may show 0
> (savings are only logged when baseline > compressed). That's expected — it means
> lossless is working, not that routing failed. The reply coming back proves routing.

---

## 2. Known gaps to handle DURING packaging

1. **numpy is a real dependency but not in `pyproject.toml`.**
   `token_efficiency_model/lossless/retrieval.py` imports numpy, and the proxy imports
   from `lossless`. **Add `numpy` to `[project.dependencies]`** or the install breaks.

2. **The engine (`api.server:app`) must run from the repo root.** `bvx start` spawns
   `uvicorn api.server:app` with `cwd=repo_root`. For a brew install where there is no
   "repo root", either: ship the `api/` + `token_efficiency_model/` packages inside the
   wheel, **or** point `bvx start --no-engine --base-url https://<hosted-engine>` at a
   hosted engine. Decide which before the formula.

3. **Python entry point.** `pyproject.toml` already declares:
   ```toml
   [project.scripts]
   brevitas = "brevitas.cli:main"
   bvx      = "brevitas.cli:main"
   ```
   So a `pip install` gives both `brevitas` and `bvx` on PATH.

---

## 3. Homebrew packaging (recommended path)

Because this is a Python app, the cleanest Homebrew route is
`brew install` of a **Python formula** using `Language::Python::Virtualenv`, OR a
single-binary build. Two options:

### Option A — Python virtualenv formula (simplest, matches current code)

Create a tap repo `github.com/brevitas-ai/homebrew-brevitas`, formula `Formula/bvx.rb`:

```ruby
class Bvx < Formula
  include Language::Python::Virtualenv
  desc "Route AI API calls through Brevitas — find, compress (lossless), save"
  homepage "https://github.com/brevitas-ai/brevitas-systems"
  url "https://github.com/brevitas-ai/brevitas-systems/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "<fill after tagging release>"
  license :cannot_represent   # project is Proprietary; adjust to your license policy
  depends_on "python@3.12"

  # `brew update-python-resources Formula/bvx.rb` auto-generates these resource blocks
  # from the tagged sdist's dependencies (httpx, tiktoken, fastapi, uvicorn, pydantic,
  # click, rich, numpy, openai).

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "brevitas", shell_output("#{bin}/bvx --help")
  end
end
```

Then: `brew install brevitas-ai/brevitas/bvx`.

**Blocker to resolve first:** the formula needs the engine code (`api/`,
`token_efficiency_model/`) available at runtime for `bvx start`. Ensure the sdist
includes them (they are top-level packages, so add to `[tool.setuptools.packages.find]`
in `pyproject.toml` — currently it only includes `brevitas*`). Confirm with:
`python -m build` then inspect the wheel contains `api/` and `token_efficiency_model/`.

### Option B — single standalone binary (zero Python-version friction)

Build with PyInstaller/shiv into one file, then a trivial formula drops it in `bin/`.
Best UX (no user Python fights), but the binary must bundle the engine + numpy.

```bash
pip install pyinstaller
pyinstaller --onefile --name bvx --collect-all token_efficiency_model \
  --collect-all api brevitas/__main__.py     # add a brevitas/__main__.py that calls main()
```

---

## 4. Release checklist

- [ ] Add `numpy` to `pyproject.toml` dependencies.
- [ ] Widen `[tool.setuptools.packages.find]` to include `api*` and
      `token_efficiency_model*` so the sdist ships the engine.
- [ ] Decide: bundle engine (local) **or** host engine and use `--no-engine`.
- [ ] `git tag v0.1.0 && push` a release tarball; grab its sha256.
- [ ] Create tap repo `homebrew-brevitas`, add `Formula/bvx.rb`.
- [ ] `brew install --build-from-source ./Formula/bvx.rb` to test locally.
- [ ] `bvx scan .` opens the popup; `bvx start` brings up the stack.

---

## 5. Security notes (do not skip)

- Never commit `.env.local`, `api/.secret_key`, or `api/brevitas.db` — all are
  gitignored; keep them that way.
- The proxy passes the caller's provider key straight through; it is **never** stored
  or logged. `~/.brevitas/key` is a *Brevitas* key (chmod 600), not a provider key.
- **Rotate any API keys that were shared in chat/dev logs before release.**
