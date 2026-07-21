"""Two on-brand demo pages that visualise Brevitas token savings:

  create_textbook_app()  — "Chat with a document": upload a PDF/codebase, ask questions, and see
                           the per-turn input tokens WITHOUT vs WITH Brevitas (live).
  create_agent_app()     — "Coding agent on a repo": runs a realistic Claude-Code-style agent
                           session over the Brevitas repo live (streamed turn-by-turn) and shows
                           the honest token + cost savings, including the provider-caching caveat.

Both are themed with the real Brevitas brand tokens (public/theme.css): Newsreader headings,
Inter Tight body, JetBrains Mono labels; ink backgrounds, signal-mint = with-Brevitas, oxblood =
baseline. Numbers are always the REAL DeepSeek-reported usage — nothing is fabricated.
"""

from __future__ import annotations

import copy, json, os, tempfile
from dataclasses import dataclass, field
from typing import Dict, List

import urllib.request
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .chat import read_document, _load_key, _base_url
from .webchat import _CODE_EXT

# --------------------------------------------------------------------------- #
# shared brand theme (mirrors public/theme.css)
# --------------------------------------------------------------------------- #
_HEAD = """<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--ink:#06070b;--ink2:#0d1017;--ink3:#151b28;--line:rgba(196,206,229,.2);
--stone:#8891a4;--stone2:#b6bfd3;--bone:#f5f7ff;--boned:#d7ddec;
--bronze:#8ea6ff;--signal:#8dd8c6;--signalglow:rgba(141,216,198,.12);--oxblood:#c98078}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 700px at 70% -10%,rgba(142,166,255,.06),transparent),var(--ink);
color:var(--bone);font-family:'Inter Tight',system-ui,sans-serif;font-feature-settings:"ss01","cv11";min-height:100vh}
.eyebrow{font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:.18em;font-size:11px;color:var(--signal)}
h1{font-family:'Newsreader',Georgia,serif;font-weight:500;letter-spacing:-.02em;margin:.1em 0;font-size:30px}
h2{font-family:'Newsreader',Georgia,serif;font-weight:500;letter-spacing:-.015em;margin:0;font-size:19px}
.serif{font-family:'Newsreader',serif}.mono{font-family:'JetBrains Mono',monospace}
header{padding:22px 34px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
.brand{font-family:'Newsreader',serif;font-size:20px;letter-spacing:-.01em}
.sub{color:var(--stone2);font-size:13px}.dim{color:var(--stone)}
.wrap{max-width:1180px;margin:0 auto;padding:26px 34px}
.hero{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 22px}
.stat{flex:1;min-width:210px;background:linear-gradient(180deg,var(--ink2),var(--ink2));border:1px solid var(--line);border-radius:16px;padding:18px 20px}
.stat .lbl{font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:.14em;font-size:10.5px;color:var(--stone)}
.stat .big{font-family:'Newsreader',serif;font-size:42px;line-height:1.05;margin-top:6px;font-weight:600}
.stat.good .big{color:var(--signal)}.stat .note2{font-size:12px;color:var(--stone2);margin-top:4px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:var(--ink2);border:1px solid var(--line);border-radius:16px;padding:18px;display:flex;flex-direction:column;gap:12px}
.card.base{border-color:rgba(201,128,120,.4)}.card.brev{border-color:rgba(141,216,198,.4);box-shadow:0 0 0 1px var(--signalglow),0 18px 40px rgba(0,0,0,.35)}
.card h2 .tag{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:.12em;padding:3px 9px;border-radius:999px;float:right}
.card.base .tag{background:rgba(201,128,120,.14);color:var(--oxblood)}
.card.brev .tag{background:rgba(141,216,198,.14);color:var(--signal)}
.tokn{font-family:'Newsreader',serif;font-size:34px;font-weight:600}.card.base .tokn{color:var(--oxblood)}.card.brev .tokn{color:var(--signal)}
.bar{height:12px;border-radius:7px;background:#0a0e16;border:1px solid var(--line);overflow:hidden}
.bar>i{display:block;height:100%;transition:width .6s cubic-bezier(.16,1,.3,1)}
.card.base .bar>i{background:linear-gradient(90deg,var(--oxblood),#a8625a)}.card.brev .bar>i{background:linear-gradient(90deg,var(--signal),#5fae9b)}
.ans{background:#0a0e16;border:1px solid var(--line);border-radius:10px;padding:11px;font-size:13px;color:var(--boned);max-height:210px;overflow:auto;white-space:pre-wrap;line-height:1.5}
input,button{font-family:inherit}
.drop{border:1.4px dashed var(--line);border-radius:14px;padding:20px;text-align:center;cursor:pointer;transition:.2s;background:var(--ink2)}
.drop:hover{border-color:var(--bronze)}
.askbar{display:flex;gap:10px;margin:16px 0}
input.q{flex:1;background:var(--ink2);border:1px solid var(--line);border-radius:11px;color:var(--bone);padding:13px 15px;font-size:15px}
input.q:focus{outline:none;border-color:var(--bronze);background:rgba(142,166,255,.06)}
button.go{background:var(--signal);color:#04110b;border:0;border-radius:11px;padding:0 24px;font-weight:600;cursor:pointer;font-size:14px}
button.go:disabled{opacity:.45;cursor:default}
.btn2{background:transparent;color:var(--bronze);border:1px solid var(--line);border-radius:11px;padding:10px 16px;cursor:pointer;font-size:13px}
.turns{display:flex;flex-direction:column;gap:8px;margin-top:8px}
.trow{display:grid;grid-template-columns:30px 1fr 1fr;gap:12px;align-items:center;background:var(--ink2);border:1px solid var(--line);border-radius:12px;padding:10px 14px;opacity:0;transform:translateY(6px);transition:.4s}
.trow.in{opacity:1;transform:none}.trow .n{font-family:'JetBrains Mono',monospace;color:var(--stone);font-size:12px}
.trow .q{font-size:12.5px;color:var(--stone2);grid-column:2/4;margin-bottom:2px}
.mini{font-family:'JetBrains Mono',monospace;font-size:12px}.mini.base{color:var(--oxblood)}.mini.brev{color:var(--signal)}
.note{font-size:12.5px;color:var(--stone2);line-height:1.7;margin-top:18px;border-left:2px solid var(--line);padding-left:14px}
.note b{color:var(--boned)}
@media(max-width:780px){.cols,.trow{grid-template-columns:1fr}}
</style>"""


def _doc_header(title, subtitle):
    return (f'<header><span class="brand">Brevitas</span>'
            f'<span class="eyebrow">{title}</span>'
            f'<span class="sub" style="margin-left:auto">{subtitle}</span></header>')


# --------------------------------------------------------------------------- #
# realistic coding-agent session (real files, real DeepSeek, streamed)
# --------------------------------------------------------------------------- #
_AGENT_SYSTEM = ("You are a senior engineer working inside the Brevitas repo via tools. Answer "
                 "using the file contents already opened in this conversation. Be concise.")
_AGENT_TURNS = [
    ("Where does the router decide which optimization lever to use?", [("router.py", False)]),
    ("Walk me through how the retrieval lever selects which chunks to keep.",
     [("api_adapter.py", False), ("retrieval.py", False)]),
    ("How does the engine apply the router's decision to a request body?", [("engine.py", False)]),
    ("Is the DeepSeek cache discount in the cost model correct?", [("provider_cache.py", False)]),
    ("Cross-check: does the router's discount match provider_cache's rate?", []),
    ("Add a comment to engine.py documenting the fail-safe order of the levers.",
     [("engine.py", True)]),
    ("Given that edit, is the compress lever reachable in the engine flow?", [("task_router.py", False)]),
    ("Summarize the full decision flow across router, engine and provider_cache.", []),
]


def _agent_base():
    import token_efficiency_model.lossless as L
    return os.path.dirname(L.__file__) + "/"


def _readfile(name, edit=False):
    txt = open(_agent_base() + name).read()
    if edit:
        txt = txt.replace("# cache_only / passthrough",
                          "# cache_only / passthrough (fail-safe order: retrieve -> compress -> cache)")
    return f"[Opened {name}]\n```python\n{txt}\n```"


def run_agent_session(provider, key, model):
    """Generator: yields one dict per turn (real DeepSeek usage) then a final summary dict."""
    from brevitas import BrevitasClient
    from token_efficiency_model.lossless.provider_cache import savings_from_usage

    def call_raw(messages):
        body = {"model": model, "messages": messages, "max_tokens": 350, "temperature": 0.2}
        req = urllib.request.Request(_base_url(provider).rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=180).read())
        return d["choices"][0]["message"]["content"], d.get("usage", {})

    client = BrevitasClient(provider=provider, api_key=key, base_url=_base_url(provider))
    context = [{"role": "system", "content": _AGENT_SYSTEM}]
    t_raw_in = t_brev_in = 0
    t_raw_cost = t_brev_cost = 0.0
    for i, (q, files) in enumerate(_AGENT_TURNS, 1):
        opened = []
        for name, edited in files:
            context.append({"role": "user", "content": _readfile(name, edited)})
            opened.append(name + (" (edited)" if edited else ""))
        context.append({"role": "user", "content": q})

        raw_ans, raw_usage = call_raw(context)
        raw_s = savings_from_usage(raw_usage, provider)
        raw_in = int(raw_usage.get("prompt_tokens", 0))

        brev_resp, sav = client.chat(messages=copy.deepcopy(context), model=model,
                                     session_id="agent-demo", max_tokens=350)
        brev_in = int(getattr(brev_resp.usage, "prompt_tokens", 0) or 0)
        brev_ans = brev_resp.choices[0].message.content
        strat = (sav.cache_placement or {}).get("strategy", "cache_only")
        context.append({"role": "assistant", "content": raw_ans})

        t_raw_in += raw_in; t_brev_in += brev_in
        t_raw_cost += raw_s.actual_cost; t_brev_cost += sav.actual_cost
        yield {"turn": i, "of": len(_AGENT_TURNS), "q": q, "opened": opened,
               "raw_in": raw_in, "raw_cached": raw_s.cached_tokens, "brev_in": brev_in,
               "strategy": strat, "raw_ans": raw_ans[:700], "brev_ans": brev_ans[:700],
               "tok_drop": round(100 * (1 - brev_in / raw_in), 1) if raw_in else 0,
               "tot": {"raw_in": t_raw_in, "brev_in": t_brev_in,
                       "tok_drop": round(100 * (1 - t_brev_in / t_raw_in), 1) if t_raw_in else 0,
                       "cost_drop": round(100 * (1 - t_brev_cost / t_raw_cost), 1) if t_raw_cost else 0}}
    yield {"done": True}


# --------------------------------------------------------------------------- #
# textbook session store
# --------------------------------------------------------------------------- #
@dataclass
class _Doc:
    doc: str = ""
    tokens: int = 0
    name: str = ""
    model: str = ""
    client: object = None
    off_hist: List[dict] = field(default_factory=list)
    on_hist: List[dict] = field(default_factory=list)
    off_in: int = 0
    on_in: int = 0
    off_cost: float = 0.0
    on_cost: float = 0.0


_DOCS: Dict[str, _Doc] = {}
_INSTRUCTION = ("You are a senior engineer. Answer using the provided document as the source of "
                "truth. If it isn't covered, say so.")


def _doc_messages(doc, hist, q):
    return ([{"role": "system", "content": _INSTRUCTION},
             {"role": "user", "content": "=== DOCUMENT ===\n" + doc + "\n=== END DOCUMENT ==="}]
            + hist + [{"role": "user", "content": q}])


# --------------------------------------------------------------------------- #
# app factories
# --------------------------------------------------------------------------- #
def create_textbook_app(provider="deepseek", api_key=""):
    from brevitas import BrevitasClient
    from brevitas.webchat import _raw_chat
    from token_efficiency_model.lossless.provider_cache import count_tokens

    app = FastAPI(title="Brevitas · Document demo", docs_url=None, redoc_url=None)
    key = _load_key(provider, api_key)
    model = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}.get(provider, "gpt-4o-mini")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _TEXTBOOK_HTML.replace("__PROV__", f"{provider} / {model}")

    @app.post("/api/upload")
    async def upload(files: List[UploadFile] = File(...), session: str = Form("default")):
        parts, names = [], []
        for f in files:
            raw = await f.read()
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext == ".pdf":
                with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as tmp:
                    tmp.write(raw); p = tmp.name
                try:
                    txt = read_document(p)
                finally:
                    os.unlink(p)
            elif ext in _CODE_EXT or not ext:
                txt = raw.decode("utf-8", "ignore")
            else:
                continue
            parts.append(f"// FILE: {f.filename}\n{txt}"); names.append(f.filename)
        if not parts:
            return JSONResponse({"error": "No readable PDF/text/code found."}, status_code=400)
        if not key:
            return JSONResponse({"error": f"No API key for {provider}."}, status_code=400)
        doc = "\n\n".join(parts)
        _DOCS[session] = _Doc(doc=doc, tokens=count_tokens(doc),
                              name=names[0] if len(names) == 1 else f"{len(names)} files",
                              model=model,
                              client=BrevitasClient(provider=provider, api_key=key, base_url=_base_url(provider)))
        return {"name": _DOCS[session].name, "tokens": _DOCS[session].tokens}

    @app.post("/api/sample")
    def sample(session: str = Form("default")):
        if not key:
            return JSONResponse({"error": f"No API key for {provider}."}, status_code=400)
        # Default: the bundled, license-clean Algorithms Handbook (ships with the repo so the demo
        # works with zero setup). Override with BREV_SAMPLE_PDF to point at your own PDF/text.
        override = os.environ.get("BREV_SAMPLE_PDF")
        try:
            if override and os.path.exists(override):
                txt, name = read_document(override), os.path.basename(override)
            else:
                p = os.path.join(os.path.dirname(__file__), "samples", "algorithms_handbook.md")
                txt, name = open(p, encoding="utf-8").read(), "Algorithms Handbook (sample)"
        except Exception as e:
            return JSONResponse({"error": f"sample load failed: {e}"}, status_code=500)
        _DOCS[session] = _Doc(doc=txt, tokens=count_tokens(txt), name=name, model=model,
                              client=BrevitasClient(provider=provider, api_key=key, base_url=_base_url(provider)))
        return {"name": _DOCS[session].name, "tokens": _DOCS[session].tokens}

    @app.post("/api/ask")
    async def ask(question: str = Form(...), session: str = Form("default")):
        s = _DOCS.get(session)
        if not s or not s.doc:
            return JSONResponse({"error": "Load a document first."}, status_code=400)
        try:
            off_msgs = _doc_messages(s.doc, s.off_hist, question)
            (off_ans, off_cached, _u, off_act, _s, _o, off_in, off_comp) = _raw_chat(provider, s.model, key, off_msgs)
            on_msgs = _doc_messages(s.doc, s.on_hist, question)
            resp, sav = s.client.chat(messages=on_msgs, model=s.model, session_id=session, max_tokens=500)
            on_ans = resp.choices[0].message.content
            on_in = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
            strat = (sav.cache_placement or {}).get("strategy", "cache_only")
            retr = sav.retrieval_optimized_tokens if sav.retrieval_applied else None
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        s.off_hist += [{"role": "user", "content": question}, {"role": "assistant", "content": off_ans}]
        s.on_hist += [{"role": "user", "content": question}, {"role": "assistant", "content": on_ans}]
        s.off_in += off_in; s.on_in += on_in; s.off_cost += off_act; s.on_cost += sav.actual_cost
        return {"off": {"answer": off_ans, "in": off_in, "cached": off_cached},
                "on": {"answer": on_ans, "in": on_in, "strategy": strat, "retr": retr},
                "turn_drop": round(100 * (1 - on_in / off_in), 1) if off_in else 0,
                "tot": {"off_in": s.off_in, "on_in": s.on_in,
                        "tok_drop": round(100 * (1 - s.on_in / s.off_in), 1) if s.off_in else 0,
                        "cost_drop": round(100 * (1 - s.on_cost / s.off_cost), 1) if s.off_cost else 0,
                        "turns": len(s.on_hist) // 2}}

    return app


def create_agent_app(provider="deepseek", api_key=""):
    app = FastAPI(title="Brevitas · Coding-agent demo", docs_url=None, redoc_url=None)
    key = _load_key(provider, api_key)
    model = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}.get(provider, "gpt-4o-mini")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _AGENT_HTML.replace("__PROV__", f"{provider} / {model}")

    @app.get("/api/run")
    def run():
        if not key:
            return JSONResponse({"error": f"No API key for {provider}."}, status_code=400)

        def gen():
            try:
                for rec in run_agent_session(provider, key, model):
                    yield f"data: {json.dumps(rec)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


# --------------------------------------------------------------------------- #
# HTML — textbook
# --------------------------------------------------------------------------- #
_TEXTBOOK_HTML = """<!doctype html><html><head><title>Brevitas · Chat with a document</title>""" + _HEAD + """</head><body>
""" + _doc_header("Document demo", "__PROV__ — same question, both ways, live token count") + """
<div class="wrap">
  <div class="eyebrow">Scenario 01 — Chat with a document</div>
  <h1>Re-sending a whole document is the default. Brevitas sends only what the question needs.</h1>

  <div class="drop" id="drop" style="margin:18px 0">
    <b id="dropText">Drop a PDF / codebase, or click to upload</b>
    <div class="sub" style="margin-top:6px">or <a href="#" id="sampleLink" style="color:var(--bronze)">use the sample textbook</a></div>
    <input type="file" id="file" multiple hidden></div>

  <div class="hero">
    <div class="stat good"><div class="lbl">Provider input tokens avoided vs control</div><div class="big" id="tokDrop">—</div>
      <div class="note2" id="tokDetail">load a document & ask a question</div></div>
    <div class="stat good"><div class="lbl">Observed cost delta</div><div class="big" id="costDrop">—</div>
      <div class="note2">sequential demo · not isolated attribution</div></div>
    <div class="stat"><div class="lbl">Turns / context</div><div class="big" id="turns">0</div>
      <div class="note2"><span id="dtok">—</span> tokens in the document</div></div>
  </div>

  <div class="askbar">
    <input class="q" id="q" placeholder="Ask a question about the document…" disabled>
    <button class="go" id="send" disabled>Ask both</button></div>

  <div class="cols">
    <div class="card base"><h2>Without Brevitas<span class="tag">re-sends whole doc</span></h2>
      <div class="tokn" id="offIn">—</div><div class="sub">input tokens sent this turn</div>
      <div class="bar"><i id="offBar" style="width:0"></i></div><div class="ans" id="offAns">—</div></div>
    <div class="card brev"><h2>With Brevitas<span class="tag" id="onTag">retrieval</span></h2>
      <div class="tokn" id="onIn">—</div><div class="sub" id="onSub">input tokens sent this turn</div>
      <div class="bar"><i id="onBar" style="width:0"></i></div><div class="ans" id="onAns">—</div></div>
  </div>
  <div class="note">LLM APIs are <b>stateless</b> — the model has no memory, so the whole document must be
  replayed every turn for it to “see” it. Brevitas routes <b>per question</b>: a specific lookup uses
  <b>retrieval</b> (sends just the relevant chunks → big token savings), while a broad “summarize / rank
  the whole book” question keeps the <b>full book in context</b> and leans on provider <b>caching</b> for
  the discount — because a retrieved slice can’t answer a whole-document question. Watch the tag on the
  right switch between <b>retrieve</b> and <b>full book · cached</b>. All numbers are real DeepSeek usage.</div>
</div>
<script>
const $=id=>document.getElementById(id);let ready=false;
$('drop').onclick=e=>{if(e.target.id!=='sampleLink')$('file').click()};
$('sampleLink').onclick=async e=>{e.preventDefault();e.stopPropagation();await load('/api/sample',new FormData());};
$('file').onchange=async e=>{const fd=new FormData();for(const f of e.target.files)fd.append('files',f);fd.append('session','default');await load('/api/upload',fd);};
async function load(url,fd){
  fd.append('session','default');$('dropText').textContent='Reading…';
  const r=await fetch(url,{method:'POST',body:fd});const j=await r.json();
  if(j.error){$('dropText').textContent='⚠ '+j.error;return;}
  $('dropText').innerHTML='✓ '+j.name+' · '+j.tokens.toLocaleString()+' tokens';
  $('dtok').textContent=j.tokens.toLocaleString();ready=true;$('q').disabled=false;$('send').disabled=false;$('q').focus();
}
async function ask(){
  const q=$('q').value.trim();if(!q||!ready)return;
  $('send').disabled=true;$('q').disabled=true;$('offAns').textContent='…';$('onAns').textContent='…';$('offIn').textContent='…';$('onIn').textContent='…';
  const fd=new FormData();fd.append('question',q);fd.append('session','default');
  try{const r=await fetch('/api/ask',{method:'POST',body:fd});const j=await r.json();
    if(j.error){$('offAns').textContent='⚠ '+j.error;return;}
    const o=j.off,n=j.on,t=j.tot;
    $('offIn').textContent=o.in.toLocaleString();$('onIn').textContent=n.in.toLocaleString();
    $('offBar').style.width='100%';$('onBar').style.width=Math.max(2,100*n.in/o.in)+'%';
    $('onSub').textContent='input tokens sent this turn · −'+j.turn_drop+'% vs left';
    const lever=n.strategy==='retrieve'?('retrieve · '+(n.retr?n.retr.toLocaleString()+' tok':'slice'))
      :(n.strategy==='cache_only'?'full book · cached':n.strategy);
    $('onTag').textContent=lever;
    $('offAns').textContent=o.answer;$('onAns').textContent=n.answer;
    $('tokDrop').textContent='−'+t.tok_drop+'%';$('tokDetail').textContent=t.on_in.toLocaleString()+' vs '+t.off_in.toLocaleString()+' tokens';
    $('costDrop').textContent='−'+t.cost_drop+'%';$('turns').textContent=t.turns;
  }catch(e){$('offAns').textContent='⚠ '+e;}
  $('q').value='';$('send').disabled=false;$('q').disabled=false;$('q').focus();
}
$('send').onclick=ask;$('q').addEventListener('keydown',e=>{if(e.key==='Enter')ask();});
</script></body></html>"""


# --------------------------------------------------------------------------- #
# HTML — coding agent
# --------------------------------------------------------------------------- #
_AGENT_HTML = """<!doctype html><html><head><title>Brevitas · Coding agent</title>""" + _HEAD + """</head><body>
""" + _doc_header("Coding-agent demo", "__PROV__ — live 8-turn agent over the Brevitas repo") + """
<div class="wrap">
  <div class="eyebrow">Scenario 02 — Coding agent on a repo</div>
  <h1>A real agent keeps every file it opens in context. Stateless APIs re-send all of it, every turn.</h1>
  <p class="sub" style="max-width:700px">Claude-Code-style baseline: the agent opens real files from this repo and the whole
  growing context is re-sent each turn. Brevitas routes <b>per turn</b> — retrieve for specific questions, full context (cached)
  for broad ones. Runs live on DeepSeek, turn by turn.</p>

  <div style="margin:16px 0"><button class="go" id="run">▶ Run live agent session (8 turns)</button>
    <span class="sub" id="status" style="margin-left:12px"></span></div>

  <div class="hero">
    <div class="stat good"><div class="lbl">Provider input tokens avoided vs control</div><div class="big" id="tokDrop">—</div>
      <div class="note2" id="tokDetail">press run</div></div>
    <div class="stat good"><div class="lbl">Observed cost delta</div><div class="big" id="costDrop">—</div>
      <div class="note2">sequential demo · not isolated attribution</div></div>
    <div class="stat"><div class="lbl">Turn</div><div class="big" id="turn">0/8</div>
      <div class="note2" id="ctx">growing context re-sent each turn</div></div>
  </div>

  <div class="sub" id="qline" style="margin:8px 0 12px;min-height:18px">Press run — each turn appears here, before &amp; after.</div>

  <div class="cols">
    <div class="card base"><h2>Without Brevitas<span class="tag">re-sends whole context</span></h2>
      <div class="tokn" id="offIn">—</div><div class="sub">input tokens sent this turn</div>
      <div class="bar"><i id="offBar" style="width:0"></i></div><div class="ans" id="offAns">—</div></div>
    <div class="card brev"><h2>With Brevitas<span class="tag" id="onTag">—</span></h2>
      <div class="tokn" id="onIn">—</div><div class="sub" id="onSub">input tokens sent this turn</div>
      <div class="bar"><i id="onBar" style="width:0"></i></div><div class="ans" id="onAns">—</div></div>
  </div>

  <div class="turns" id="turns" style="margin-top:18px"></div>

  <div class="note" id="note">Brevitas routes <b>per turn</b> (watch the With-Brevitas tag): a specific question uses
  <b>retrieve</b> (sends only the relevant context); a broad “summarize the whole flow” turn keeps the <b>full context · cached</b>
  so the answer stays correct. The honest catch on cost: the baseline’s <b>stable growing prefix</b> is cached by DeepSeek (cheap
  re-sends), while Brevitas’ retrieved context <b>changes each turn</b> and misses that cache — so the <b>cost</b> saving is
  smaller than the <b>token</b> saving. The win is fewer tokens & lower latency; the dollar win is real but modest on a
  strongly-caching provider. All numbers are live DeepSeek usage.</div>
</div>
<script>
const $=id=>document.getElementById(id);
$('run').onclick=()=>{
  $('run').disabled=true;$('turns').innerHTML='';$('status').textContent='running… ~30s, live API calls';
  $('tokDrop').textContent='—';$('costDrop').textContent='—';$('offAns').textContent='…';$('onAns').textContent='…';
  $('offIn').textContent='…';$('onIn').textContent='…';
  const es=new EventSource('/api/run');
  es.onmessage=ev=>{const d=JSON.parse(ev.data);
    if(d.error){$('status').textContent='⚠ '+d.error;es.close();$('run').disabled=false;return;}
    if(d.done){$('status').textContent='✓ done — '+($('turns').children.length)+' turns';es.close();$('run').disabled=false;return;}
    const lever=d.strategy==='retrieve'?('retrieve · '+d.brev_in.toLocaleString()+' tok')
      :(d.strategy==='cache_only'?'full ctx · cached':d.strategy);
    // big before/after cards = current turn
    $('qline').innerHTML='<b>Turn '+d.turn+'/'+d.of+'</b> — '+d.q+(d.opened.length?' <span class="dim">· opened '+d.opened.join(', ')+'</span>':'');
    $('offIn').textContent=d.raw_in.toLocaleString();$('onIn').textContent=d.brev_in.toLocaleString();
    $('offBar').style.width='100%';$('onBar').style.width=Math.max(2,100*d.brev_in/d.raw_in)+'%';
    $('onTag').textContent=lever;$('onSub').textContent='input tokens sent this turn · −'+d.tok_drop+'% vs left';
    $('offAns').textContent=d.raw_ans;$('onAns').textContent=d.brev_ans;
    // hero totals
    $('turn').textContent=d.turn+'/'+d.of;$('ctx').textContent='strategy: '+d.strategy;
    $('tokDrop').textContent='−'+d.tot.tok_drop+'%';
    $('tokDetail').textContent=d.tot.brev_in.toLocaleString()+' vs '+d.tot.raw_in.toLocaleString()+' tokens';
    $('costDrop').textContent='−'+d.tot.cost_drop+'%';
    // compact running log
    const row=document.createElement('div');row.className='trow';
    row.innerHTML='<div class="n">T'+d.turn+'</div>'
      +'<div class="q">'+d.q+'</div>'
      +'<div class="mini base">without · '+d.raw_in.toLocaleString()+' tok</div>'
      +'<div class="mini brev">with · '+d.brev_in.toLocaleString()+' tok &nbsp;−'+d.tok_drop+'% &nbsp;<span style="color:var(--stone)">['+d.strategy+']</span></div>';
    $('turns').appendChild(row);requestAnimationFrame(()=>row.classList.add('in'));
  };
  es.onerror=()=>{es.close();$('run').disabled=false;if(!$('status').textContent.startsWith('✓'))$('status').textContent='connection closed';};
};
</script></body></html>"""


# --------------------------------------------------------------------------- #
# one-command launcher:  python -m brevitas.demos
# --------------------------------------------------------------------------- #
def serve_demos(provider: str = "deepseek", textbook_port: int = 3010, agent_port: int = 3011):
    """Launch both demo pages: textbook (chat with a document) and coding-agent (live repo run).

    Reads the provider API key from the environment (e.g. DEEPSEEK_API_KEY) or a .env.local in the
    working directory. Set AB_PROVIDER to switch providers; BREV_SAMPLE_PDF to use your own sample.
    """
    import threading
    import uvicorn

    tb = create_textbook_app(provider)
    ag = create_agent_app(provider)
    print(f"\n  Brevitas demos — provider: {provider}")
    print(f"  • Textbook (chat with a document):  http://127.0.0.1:{textbook_port}")
    print(f"  • Coding agent (live repo run):     http://127.0.0.1:{agent_port}\n")

    def _serve(app, port):
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    threading.Thread(target=_serve, args=(tb, textbook_port), daemon=True).start()
    _serve(ag, agent_port)


if __name__ == "__main__":
    serve_demos(provider=os.environ.get("AB_PROVIDER", "deepseek"))
