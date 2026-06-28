"""Web frontend for 'chat with your PDF' with a live token-savings meter.

A small FastAPI app: upload a PDF/text doc, ask questions against it, and watch the per-turn
and cumulative token/cost savings (the doc is cached by the provider, so turns 2+ are cheap).
Launch with `brevitas chat --web`.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse

from .chat import read_document, _load_key, _base_url


@dataclass
class Session:
    doc: str = ""
    doc_tokens: int = 0
    name: str = ""
    history: List[dict] = field(default_factory=list)
    cum_uncached: float = 0.0
    cum_actual: float = 0.0
    cached_total: int = 0
    client: object = None
    model: str = ""


_SESSIONS: Dict[str, Session] = {}


def create_app(provider: str = "deepseek", api_key: str = "") -> FastAPI:
    from brevitas import BrevitasClient
    from token_efficiency_model.lossless.provider_cache import count_tokens

    app = FastAPI(title="Brevitas Chat", docs_url=None, redoc_url=None)
    key = _load_key(provider, api_key)
    model = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}.get(provider, "gpt-4o-mini")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _HTML.replace("__PROVIDER__", f"{provider} / {model}")

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...), session: str = Form("default")):
        suffix = os.path.splitext(file.filename or "doc")[1] or ".txt"
        with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as tmp:
            tmp.write(await file.read())
            path = tmp.name
        try:
            text = read_document(path)
        except SystemExit as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if not key:
            return JSONResponse({"error": f"No API key for {provider}. Set its env var."}, status_code=400)
        s = Session(doc=text, doc_tokens=count_tokens(text), name=file.filename or "document",
                    client=BrevitasClient(provider=provider, api_key=key, base_url=_base_url(provider)),
                    model=model)
        _SESSIONS[session] = s
        return {"name": s.name, "tokens": s.doc_tokens}

    @app.post("/api/ask")
    async def ask(question: str = Form(...), session: str = Form("default")):
        s = _SESSIONS.get(session)
        if not s or not s.doc:
            return JSONResponse({"error": "Upload a document first."}, status_code=400)
        system = ("Answer using the document below as the source of truth.\n\n=== DOCUMENT ===\n"
                  + s.doc + "\n=== END DOCUMENT ===")
        messages = [{"role": "system", "content": system}] + s.history + [
            {"role": "user", "content": question}]
        try:
            resp, sav = s.client.chat(messages=messages, model=s.model,
                                      session_id=session, max_tokens=500)
            answer = resp.choices[0].message.content
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        s.history += [{"role": "user", "content": question},
                      {"role": "assistant", "content": answer}]
        s.cum_uncached += sav.uncached_cost
        s.cum_actual += sav.actual_cost
        s.cached_total += sav.cached_tokens
        total = round(100 * (1 - s.cum_actual / s.cum_uncached), 1) if s.cum_uncached else 0.0
        return {
            "answer": answer,
            "cached_tokens": sav.cached_tokens,
            "turn_saved_pct": sav.savings_pct,
            "cumulative_saved_pct": total,
            "cached_total": s.cached_total,
            "turns": len(s.history) // 2,
        }

    return app


_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brevitas · Chat with your PDF</title>
<style>
:root{--bg:#0b0f17;--panel:#121826;--line:#1f2937;--accent:#34d399;--accent2:#60a5fa;--text:#e5e7eb;--dim:#9ca3af}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex}
.side{width:300px;border-right:1px solid var(--line);padding:20px;display:flex;flex-direction:column;gap:16px;background:var(--panel)}
.main{flex:1;display:flex;flex-direction:column}
h1{font-size:18px;margin:0}.sub{color:var(--dim);font-size:13px}
.drop{border:1.5px dashed var(--line);border-radius:12px;padding:22px;text-align:center;cursor:pointer;transition:.15s}
.drop:hover{border-color:var(--accent2)}.drop input{display:none}
.meter{background:#0e1422;border:1px solid var(--line);border-radius:12px;padding:14px}
.meter .big{font-size:30px;font-weight:700;color:var(--accent)}
.row{display:flex;justify-content:space-between;font-size:13px;color:var(--dim);margin-top:6px}
.row b{color:var(--text)}
#msgs{flex:1;overflow:auto;padding:24px;display:flex;flex-direction:column;gap:14px}
.m{max-width:760px;padding:12px 14px;border-radius:12px;white-space:pre-wrap}
.me{align-self:flex-end;background:#1d4ed8}.bot{align-self:flex-start;background:#111827;border:1px solid var(--line)}
.tag{align-self:flex-start;font-size:12px;color:var(--dim)}
.bar{display:flex;gap:10px;padding:16px;border-top:1px solid var(--line);background:var(--panel)}
input.q{flex:1;background:#0e1422;border:1px solid var(--line);color:var(--text);border-radius:10px;padding:12px 14px;font-size:15px}
button{background:var(--accent);border:0;color:#06281d;font-weight:700;border-radius:10px;padding:0 18px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.pill{font-size:12px;background:#0e1422;border:1px solid var(--line);border-radius:999px;padding:3px 10px;color:var(--accent)}
</style></head><body>
<div class="side">
  <div><h1>⚡ Brevitas</h1><div class="sub">Chat with your PDF · __PROVIDER__</div></div>
  <label class="drop"><input type="file" id="file" accept=".pdf,.txt,.md">
    <div id="dropText">📄 Drop a PDF / text file<br><span class="sub">or click to upload</span></div>
  </label>
  <div class="meter">
    <div class="sub">Total input-cost saved</div>
    <div class="big" id="saved">—</div>
    <div class="row"><span>This turn</span><b id="turn">—</b></div>
    <div class="row"><span>Tokens from cache</span><b id="cached">0</b></div>
    <div class="row"><span>Turns</span><b id="turns">0</b></div>
    <div class="row"><span>Doc size</span><b id="dtok">—</b></div>
  </div>
  <div class="sub">The document is re-sent each turn, but the provider caches it — so every
  question after the first is much cheaper. The more you ask, the more you save.</div>
</div>
<div class="main">
  <div id="msgs"><div class="tag">Upload a document to begin.</div></div>
  <div class="bar">
    <input class="q" id="q" placeholder="Ask a question about the document…" disabled>
    <button id="send" disabled>Ask</button>
  </div>
</div>
<script>
const $=id=>document.getElementById(id); let ready=false;
function add(cls,txt){const d=document.createElement('div');d.className='m '+cls;d.textContent=txt;$('msgs').appendChild(d);$('msgs').scrollTop=1e9;return d;}
$('file').onchange=async e=>{
  const f=e.target.files[0]; if(!f)return;
  $('dropText').innerHTML='⏳ Reading '+f.name+'…';
  const fd=new FormData();fd.append('file',f);fd.append('session','default');
  const r=await fetch('/api/upload',{method:'POST',body:fd}); const j=await r.json();
  if(j.error){$('dropText').innerHTML='⚠️ '+j.error;return;}
  $('dropText').innerHTML='✅ '+j.name+'<br><span class="sub">'+j.tokens.toLocaleString()+' tokens</span>';
  $('dtok').textContent=j.tokens.toLocaleString()+' tok';
  $('msgs').innerHTML='<div class="tag">Loaded '+j.name+' ('+j.tokens.toLocaleString()+' tokens). Ask away.</div>';
  ready=true;$('q').disabled=false;$('send').disabled=false;$('q').focus();
};
async function ask(){
  const q=$('q').value.trim(); if(!q||!ready)return;
  $('q').value='';$('send').disabled=true; add('me',q);
  const tag=document.createElement('div');tag.className='tag';tag.textContent='thinking…';$('msgs').appendChild(tag);
  const fd=new FormData();fd.append('question',q);fd.append('session','default');
  try{
    const r=await fetch('/api/ask',{method:'POST',body:fd}); const j=await r.json();
    tag.remove();
    if(j.error){add('bot','⚠️ '+j.error);}else{
      add('bot',j.answer);
      $('saved').textContent=j.cumulative_saved_pct+'%';
      $('turn').textContent=Math.round(j.turn_saved_pct)+'% · '+j.cached_tokens.toLocaleString()+' cached';
      $('cached').textContent=j.cached_total.toLocaleString();
      $('turns').textContent=j.turns;
    }
  }catch(e){tag.remove();add('bot','⚠️ '+e);}
  $('send').disabled=false;$('q').focus();
}
$('send').onclick=ask;$('q').addEventListener('keydown',e=>{if(e.key==='Enter')ask();});
</script></body></html>"""
