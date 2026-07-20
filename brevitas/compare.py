"""Side-by-side A/B dashboard: ask ONE question, see BOTH paths at once.

Upload a PDF/codebase once. Each question is sent two ways against the same document:
  * WITHOUT Brevitas — the whole document is re-sent every turn (because LLM APIs are stateless;
    the model has no memory, so the full context must be replayed each call).
  * WITH Brevitas — the router retrieves only the relevant chunks and sends those.

The page shows, per turn and cumulatively, the real DeepSeek-reported input tokens each side sent
and the honest cost (with the provider's own caching credited to the no-Brevitas side, so the
comparison is fair). Launch with `python -m brevitas.compare` or via run_compare.py.
"""

from __future__ import annotations

import os
import tempfile
import atexit
from dataclasses import dataclass, field, replace
from typing import Dict, List

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse

from .chat import read_document, _load_key, _base_url
from .resource_bounds import (
    BoundedTTLMap,
    ResourceBounds,
    ResourceLimitExceeded,
    extend_bounded_list,
    require_size,
    serialized_size_bytes,
    safe_close_resource,
    utf8_size,
)
from .webchat import _raw_chat, _CODE_EXT

_INSTRUCTION = ("You are a senior engineer. Answer using the provided document as the "
                "source of truth. If the document does not cover it, say so.")


@dataclass
class CmpSession:
    doc: str = ""
    doc_tokens: int = 0
    name: str = ""
    model: str = ""
    client: object = None
    off_history: List[dict] = field(default_factory=list)
    on_history: List[dict] = field(default_factory=list)
    off_in: int = 0
    on_in: int = 0
    off_cost: float = 0.0
    on_cost: float = 0.0
    off_out: int = 0
    on_out: int = 0


_BOUNDS = ResourceBounds.from_env()


def _cmp_session_size(value: CmpSession) -> int:
    return (utf8_size(value.doc) + serialized_size_bytes(value.off_history)
            + serialized_size_bytes(value.on_history) + 2048)


def _copy_cmp_session(value: CmpSession) -> CmpSession:
    return replace(
        value, off_history=list(value.off_history), on_history=list(value.on_history)
    )


def _close_cmp_session(value: CmpSession) -> None:
    if value.client is not None:
        safe_close_resource(value.client)


_CMP: BoundedTTLMap[str, CmpSession] = BoundedTTLMap(
    ttl_s=_BOUNDS.demo_session_ttl_s,
    max_entries=_BOUNDS.demo_max_sessions,
    max_value_bytes=_BOUNDS.demo_max_session_bytes,
    max_total_bytes=min(256 * 1024 * 1024,
                        _BOUNDS.demo_max_sessions * _BOUNDS.demo_max_session_bytes),
    sizer=_cmp_session_size,
    copier=_copy_cmp_session,
    snapshotter=_copy_cmp_session,
    on_remove=_close_cmp_session,
    resource_key=lambda value: value.client if value.client is not None else value,
)
atexit.register(_CMP.clear)


def _doc_messages(doc: str, history: List[dict], question: str) -> List[dict]:
    return ([{"role": "system", "content": _INSTRUCTION},
             {"role": "user", "content": "=== DOCUMENT ===\n" + doc + "\n=== END DOCUMENT ==="}]
            + history + [{"role": "user", "content": question}])


def create_compare_app(provider: str = "deepseek", api_key: str = "") -> FastAPI:
    from brevitas import BrevitasClient
    from token_efficiency_model.lossless.provider_cache import count_tokens, savings_from_usage

    app = FastAPI(title="Brevitas A/B", docs_url=None, redoc_url=None)
    app.router.on_shutdown.append(_CMP.clear)
    key = _load_key(provider, api_key)
    model = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}.get(provider, "gpt-4o-mini")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _HTML.replace("__PROVIDER__", f"{provider} / {model}")

    @app.post("/api/upload")
    async def upload(files: List[UploadFile] = File(...), session: str = Form("default")):
        try:
            require_size(session, 256, name="session id", sizer=utf8_size)
        except ResourceLimitExceeded as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        if len(files) > _BOUNDS.request_max_items:
            return JSONResponse({"error": "Too many uploaded files."}, status_code=413)
        parts, names = [], []
        document_bytes = 0
        for f in files:
            raw = await f.read(_BOUNDS.demo_document_max_bytes + 1)
            if len(raw) > _BOUNDS.demo_document_max_bytes:
                return JSONResponse({"error": "Uploaded document is too large."}, status_code=413)
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext == ".pdf":
                with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as tmp:
                    tmp.write(raw); p = tmp.name
                try:
                    try:
                        txt = read_document(p, max_bytes=_BOUNDS.demo_document_max_bytes)
                    except ResourceLimitExceeded as exc:
                        return JSONResponse({"error": str(exc)}, status_code=413)
                finally:
                    os.unlink(p)
            elif ext in _CODE_EXT or not ext:
                txt = raw.decode("utf-8", "ignore")
            else:
                continue
            part = f"// ===== FILE: {f.filename} =====\n{txt}"
            document_bytes += utf8_size(part) + (2 if parts else 0)
            if document_bytes > _BOUNDS.demo_document_max_bytes:
                return JSONResponse({"error": "Combined document is too large."}, status_code=413)
            parts.append(part)
            names.append(f.filename)
        if not parts:
            return JSONResponse({"error": "No readable code/text/PDF files found."}, status_code=400)
        if not key:
            return JSONResponse({"error": f"No API key for {provider}."}, status_code=400)
        doc = "\n\n".join(parts)
        s = CmpSession(doc=doc, doc_tokens=count_tokens(doc),
                       name=names[0] if len(names) == 1 else f"{len(names)} files", model=model,
                       client=BrevitasClient(provider=provider, api_key=key, base_url=_base_url(provider)))
        try:
            _CMP.put(session, s)
        except ResourceLimitExceeded as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        return {"name": s.name, "tokens": s.doc_tokens, "files": len(names)}

    @app.post("/api/ask")
    async def ask(question: str = Form(...), session: str = Form("default")):
        try:
            require_size(session, 256, name="session id", sizer=utf8_size)
            require_size(question, _BOUNDS.session_max_item_bytes,
                         name="question", sizer=utf8_size)
        except ResourceLimitExceeded as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        s = _CMP.get(session)
        if not s or not s.doc:
            return JSONResponse({"error": "Upload a document first."}, status_code=400)
        try:
            # ---- WITHOUT Brevitas: full document re-sent every turn -------------------- #
            off_msgs = _doc_messages(s.doc, s.off_history, question)
            (off_ans, off_cached, off_unc, off_act, off_saved, off_o,
             off_in, off_comp) = _raw_chat(provider, s.model, key, off_msgs)

            # ---- WITH Brevitas: router retrieves relevant chunks ----------------------- #
            on_msgs = _doc_messages(s.doc, s.on_history, question)
            resp, sav = s.client.chat(messages=on_msgs, model=s.model,
                                      session_id=session, max_tokens=500)
            on_ans = resp.choices[0].message.content
            on_in = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
            on_strategy = (sav.cache_placement or {}).get("strategy", "cache_only")
            retr_opt = sav.retrieval_optimized_tokens if sav.retrieval_applied else None
            retr_base = sav.retrieval_baseline_tokens if sav.retrieval_applied else None
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

        def update(current: CmpSession) -> None:
            extend_bounded_list(
                current.off_history,
                [{"role": "user", "content": question},
                 {"role": "assistant", "content": off_ans}],
                max_items=_BOUNDS.demo_history_max_items,
                max_bytes=_BOUNDS.demo_history_max_bytes,
            )
            extend_bounded_list(
                current.on_history,
                [{"role": "user", "content": question},
                 {"role": "assistant", "content": on_ans}],
                max_items=_BOUNDS.demo_history_max_items,
                max_bytes=_BOUNDS.demo_history_max_bytes,
            )
            current.off_in += off_in
            current.on_in += on_in
            current.off_cost += off_act
            current.on_cost += sav.actual_cost
            current.off_out += off_o
            current.on_out += sav.output_tokens

        try:
            updated = _CMP.mutate(session, update)
        except ResourceLimitExceeded as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        if updated is None:
            return JSONResponse({"error": "Session expired; upload again."}, status_code=410)
        s = updated

        tok_drop = round(100 * (1 - on_in / off_in), 1) if off_in else 0.0
        tok_drop_tot = round(100 * (1 - s.on_in / s.off_in), 1) if s.off_in else 0.0
        cost_drop_tot = round(100 * (1 - s.on_cost / s.off_cost), 1) if s.off_cost else 0.0
        return {
            "off": {"answer": off_ans, "in": off_in, "cached": off_cached, "out": off_comp},
            "on": {"answer": on_ans, "in": on_in, "strategy": on_strategy,
                   "retr_optimized": retr_opt, "retr_baseline": retr_base, "out": sav.output_tokens},
            "turn_tok_drop": tok_drop,
            "totals": {"off_in": s.off_in, "on_in": s.on_in, "tok_drop": tok_drop_tot,
                       "off_cost": round(s.off_cost), "on_cost": round(s.on_cost),
                       "cost_drop": cost_drop_tot, "turns": len(s.on_history) // 2},
        }

    @app.delete("/api/session")
    def delete_session(session: str = "default"):
        try:
            require_size(session, 256, name="session id", sizer=utf8_size)
        except ResourceLimitExceeded as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        return {"deleted": _CMP.discard(session)}

    return app


_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brevitas · A/B token comparison</title>
<style>
:root{--bg:#0b0f17;--panel:#121826;--line:#1f2937;--good:#34d399;--bad:#f87171;--blue:#60a5fa;--text:#e5e7eb;--dim:#9ca3af}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}
header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:18px;margin:0}.sub{color:var(--dim);font-size:13px}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
.drop{border:1.5px dashed var(--line);border-radius:12px;padding:18px;text-align:center;cursor:pointer;margin-bottom:14px}
.askbar{display:flex;gap:10px;margin:14px 0}
input.q{flex:1;background:#0e1422;border:1px solid var(--line);border-radius:10px;color:var(--text);padding:12px 14px;font-size:15px}
button{background:var(--good);color:#04110b;border:0;border-radius:10px;padding:0 20px;font-weight:700;cursor:pointer}
button:disabled{opacity:.5}
.headline{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0 16px}
.stat{flex:1;min-width:200px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.stat .big{font-size:30px;font-weight:800}.stat.good .big{color:var(--good)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:10px}
.card.off{border-color:#3a2330}.card.on{border-color:#15352b}
.card h2{font-size:14px;margin:0;display:flex;justify-content:space-between;align-items:center}
.tag{font-size:11px;padding:2px 8px;border-radius:999px}
.tag.off{background:#3a2330;color:var(--bad)}.tag.on{background:#15352b;color:var(--good)}
.tokn{font-size:26px;font-weight:800}.card.off .tokn{color:var(--bad)}.card.on .tokn{color:var(--good)}
.bar{height:10px;border-radius:6px;background:#0e1422;overflow:hidden}
.bar>i{display:block;height:100%}.card.off .bar>i{background:var(--bad)}.card.on .bar>i{background:var(--good)}
.ans{background:#0e1422;border:1px solid var(--line);border-radius:10px;padding:10px;font-size:13px;color:#cbd5e1;max-height:220px;overflow:auto;white-space:pre-wrap}
.row{display:flex;justify-content:space-between;font-size:13px;color:var(--dim)}
.note{font-size:12px;color:var(--dim);margin-top:14px;line-height:1.6}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Brevitas · A/B</h1><span class="sub">__PROVIDER__ — same question, both ways, live token count</span></header>
<div class="wrap">
  <div class="drop" id="drop"><b id="dropText">📄 Drop a PDF / codebase, or click to upload</b>
    <input type="file" id="file" multiple hidden></div>

  <div class="headline">
    <div class="stat good"><div class="sub">Total input tokens saved</div><div class="big" id="tokDrop">—</div>
      <div class="sub" id="tokDetail">upload a document & ask</div></div>
    <div class="stat good"><div class="sub">Total cost saved (incl. provider caching)</div><div class="big" id="costDrop">—</div>
      <div class="sub" id="costDetail">honest, apples-to-apples</div></div>
    <div class="stat"><div class="sub">Turns</div><div class="big" id="turns">0</div>
      <div class="sub">context size: <span id="dtok">—</span> tok</div></div>
  </div>

  <div class="askbar">
    <input class="q" id="q" placeholder="Ask a question about the document…" disabled>
    <button id="send" disabled>Ask both</button>
  </div>

  <div class="cols">
    <div class="card off"><h2>Without Brevitas <span class="tag off">re-sends whole doc</span></h2>
      <div class="tokn" id="offIn">—</div><div class="sub">input tokens sent this turn</div>
      <div class="bar"><i id="offBar" style="width:0"></i></div>
      <div class="ans" id="offAns">—</div></div>
    <div class="card on"><h2>With Brevitas <span class="tag on" id="onTag">retrieval</span></h2>
      <div class="tokn" id="onIn">—</div><div class="sub" id="onSub">input tokens sent this turn</div>
      <div class="bar"><i id="onBar" style="width:0"></i></div>
      <div class="ans" id="onAns">—</div></div>
  </div>

  <div class="note" id="note">⚠️ LLM APIs are <b>stateless</b> — the model has no memory between calls, so the
  whole document must be re-sent every turn for the model to “see” it. Provider caching only discounts
  that; Brevitas instead <b>sends only the relevant chunks</b>. That’s the gap you’ll see below.</div>
</div>
<script>
const $=id=>document.getElementById(id);let ready=false;
$('drop').onclick=()=>$('file').click();
$('file').onchange=async e=>{
  const files=e.target.files;if(!files.length)return;
  $('dropText').textContent='⏳ Reading '+files.length+' file(s)…';
  const fd=new FormData();for(const f of files)fd.append('files',f);fd.append('session','default');
  const r=await fetch('/api/upload',{method:'POST',body:fd});const j=await r.json();
  if(j.error){$('dropText').textContent='⚠️ '+j.error;return;}
  $('dropText').innerHTML='✅ '+j.name+' · '+j.tokens.toLocaleString()+' tokens';
  $('dtok').textContent=j.tokens.toLocaleString();
  ready=true;$('q').disabled=false;$('send').disabled=false;$('q').focus();
};
async function ask(){
  const q=$('q').value.trim();if(!q||!ready)return;
  $('send').disabled=true;$('q').disabled=true;
  $('offAns').textContent='…';$('onAns').textContent='…';$('offIn').textContent='…';$('onIn').textContent='…';
  const fd=new FormData();fd.append('question',q);fd.append('session','default');
  try{
    const r=await fetch('/api/ask',{method:'POST',body:fd});const j=await r.json();
    if(j.error){$('offAns').textContent='⚠️ '+j.error;return;}
    const o=j.off,n=j.on,t=j.totals;
    $('offIn').textContent=o.in.toLocaleString();
    $('onIn').textContent=n.in.toLocaleString();
    $('offBar').style.width='100%';
    $('onBar').style.width=Math.max(2,100*n.in/o.in)+'%';
    $('onSub').textContent='input tokens sent this turn · −'+j.turn_tok_drop+'% vs left';
    $('onTag').textContent=n.strategy+(n.retr_optimized?(' · '+n.retr_optimized.toLocaleString()+' tok'):'');
    $('offAns').textContent=o.answer;$('onAns').textContent=n.answer;
    $('tokDrop').textContent='−'+t.tok_drop+'%';
    $('tokDetail').textContent=t.on_in.toLocaleString()+' vs '+t.off_in.toLocaleString()+' tokens';
    $('costDrop').textContent='−'+t.cost_drop+'%';
    $('costDetail').textContent=t.on_cost.toLocaleString()+' vs '+t.off_cost.toLocaleString()+' cost units';
    $('turns').textContent=t.turns;
  }catch(e){$('offAns').textContent='⚠️ '+e;}
  $('q').value='';$('send').disabled=false;$('q').disabled=false;$('q').focus();
}
$('send').onclick=ask;$('q').addEventListener('keydown',e=>{if(e.key==='Enter')ask();});
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    app = create_compare_app(os.environ.get("AB_PROVIDER", "deepseek"))
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("AB_PORT", "3002")), log_level="warning")
