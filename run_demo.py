#!/usr/bin/env python3
"""Zero-setup launcher for the Brevitas demos — run straight from a fresh clone.

No editable install needed (avoids the `pip install -e` step and the egg-info it generates):

    pip install -r requirements-demo.txt
    python run_demo.py

Reads your provider API key from the environment or a `.env.local` file in this folder
(e.g. a line `DEEPSEEK_API_KEY=sk-...`). Then opens:
    • http://127.0.0.1:3010  — chat with a document
    • http://127.0.0.1:3011  — coding agent on this repo

Switch provider with AB_PROVIDER=openai; use your own sample with BREV_SAMPLE_PDF=/path/to.pdf.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)  # import brevitas/* directly from the clone — no install required

# load KEY=value lines from .env.local so DEEPSEEK_API_KEY etc. are available
_env = os.path.join(ROOT, ".env.local")
if os.path.exists(_env):
    for _line in open(_env):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

try:
    from brevitas.demos import serve_demos
except ModuleNotFoundError as e:
    sys.exit(f"Missing dependency ({e.name}). Run:  pip install -r requirements-demo.txt")

if __name__ == "__main__":
    provider = os.environ.get("AB_PROVIDER", "deepseek")
    canonical = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider, "DEEPSEEK_API_KEY")
    # accept any case variant (e.g. Deepseek_api_key) in env or .env.local
    have_key = any(k.upper() == canonical for k in os.environ)
    if not have_key:
        print(f"⚠  No {canonical} found. Add it to .env.local or export it, then re-run.\n"
              f"   e.g.  echo '{canonical}=sk-...' >> .env.local\n")
    serve_demos(provider=provider)
