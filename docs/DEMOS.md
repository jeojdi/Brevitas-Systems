# Brevitas demos — token savings, before & after

Two self-contained pages that show, with **real provider-reported token counts**, how much
Brevitas saves on two realistic workloads. Nothing is simulated — every number is the actual
usage returned by the LLM provider.

| Page | Port | What it shows |
|------|------|---------------|
| **Chat with a document** | `3010` | Ask a question about a PDF/codebase. Side-by-side: **Without Brevitas** re-sends the whole document; **With Brevitas** routes per question (retrieve a slice, or keep the full doc + provider cache for broad questions). |
| **Coding agent on a repo** | `3011` | Runs a live 8-turn Claude-Code-style agent over this repo's own files. Each turn shows the before/after input tokens and the running token + cost savings. |

## Run it (no install — just clone & run)

```bash
# 1. clone and enter the repo
git clone https://github.com/jeojdi/Brevitas-Systems.git
cd Brevitas-Systems

# 2. install the demo dependencies (no editable install needed)
pip install -r requirements-demo.txt

# 3. add a DeepSeek API key
echo 'DEEPSEEK_API_KEY=sk-your-own-key' >> .env.local

# 4. launch both demo pages
python run_demo.py
```

Then open **http://127.0.0.1:3010** (textbook) and **http://127.0.0.1:3011** (coding agent).

> Already have the repo? Just `git pull` and run steps 2–4. `run_demo.py` imports straight from the
> clone, so there's no `pip install -e` and no `*.egg-info` to cause merge conflicts on pull.

<details><summary>Alternative: full editable install</summary>

```bash
pip install -e ".[all]"
python -m brevitas.demos
```
</details>

- On :3010, click **“use the sample textbook”** to load the bundled, license-clean
  *Algorithms Handbook*, or drag in your own PDF / codebase folder.
- On :3011, click **“Run live agent session”** and watch the 8 turns stream in.

### Options
- `AB_PROVIDER=openai python -m brevitas.demos` — use OpenAI (`gpt-4o-mini`) instead of DeepSeek.
- `BREV_SAMPLE_PDF=/path/to/your.pdf` — use your own document for the “sample” button.

## What the numbers mean (read this before demoing)

- **LLM APIs are stateless** — the model has no memory, so the whole context is re-sent every turn.
  That re-send is the cost Brevitas attacks.
- **Retrieve** (specific question): send only the relevant chunks → large token cut.
- **Full context · cached** (broad “summarize the whole thing” question): keep the full document so
  the answer stays correct, and lean on the provider's prompt cache for the discount.
- **Token savings ≠ cost savings on a caching provider.** DeepSeek caches a stable repeated prefix
  at ~0.26×, so the baseline is already discounted; Brevitas's retrieved context changes each turn
  and misses that cache. The token drop is large; the dollar drop is real but smaller. The pages
  state this honestly.

The bundled sample is intentionally small (~4k tokens) so it loads instantly; upload a larger
document to see the bigger savings percentages.
