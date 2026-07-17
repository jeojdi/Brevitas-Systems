"""Cross-run state persistence (the "cross-run stability" lever, lossless).

Everything the optimizer LEARNS — which prefixes repeat (LCP fingerprints), observed
provider cache-hit rates, learned tokenizer ratios, retrieval keep-fractions, b9
promotion order and do-no-harm locks — lives in process memory, so a proxy restart or
a pipeline re-run starts amnesiac and re-pays the cold-start cost. This module
snapshots that decision state to a small local JSON file and restores it on startup,
so run N+1 of the same workload is recognized immediately.

PRIVACY: the snapshot is content-free by construction — message CONTENT is never
persisted, only SHA-256 hashes, token counts and scalar statistics. (shared_prefix's
canonical-message bookkeeping is deliberately NOT persisted for this reason; layout
only ever reorders messages present in the live request, so it never needs stored
content.) The file stays on the customer's machine, next to their keys.

Strictly lossless: restoring state changes ROUTING DECISIONS (what to cache/promote/
leave alone), never message content. Losing or deleting the file is always safe — the
system just relearns. Corrupt/partial files are ignored (fail-safe cold start).

Enable via BREVITAS_STATE_FILE=<path> (the proxy wires this up automatically).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Dict, Optional

_SESSION_FIELDS = ("msg_hashes", "msg_tokens", "last_ts", "obs_hit", "obs_count",
                   "keep_frac", "last_est", "tok_ratio", "last_strategy", "gap_ewma",
                   "repeat_observations", "cache_read_tokens", "cache_write_tokens",
                   "cache_net_units", "cache_negative_writes", "cache_blocked_until")
_MAX_AGE_S = 7 * 24 * 3600      # snapshots older than a week: cold-start instead


def capture(routers: Dict[str, object]) -> dict:
    """Serialize router registry + shared-prefix pipelines + b9 locks. Content-free."""
    from . import engine
    from .shared_prefix import _default as shared

    out: dict = {"v": 1, "ts": time.time(), "routers": {}, "shared": {}, "b9": {}}
    for key, r in routers.items():
        sessions = {}
        for sid, st in getattr(r._sessions, "_sessions", {}).items():
            sessions[sid] = {f: getattr(st, f) for f in _SESSION_FIELDS}
        out["routers"][key] = {"provider": r.provider, "model": r.model,
                               "sessions": sessions}
    for pid, ps in shared._pipelines.items():
        out["shared"][pid] = {"seen": {h: sorted(a) for h, a in ps.seen.items()},
                              "promo": dict(ps.promo_order),
                              "explicit": sorted(ps.explicit)}
    out["b9"] = {p: dict(st) for p, st in engine._b9_pipes.items()}
    return out


def restore(snapshot: dict, routers: Dict[str, object], router_factory) -> int:
    """Rebuild in-memory state from a snapshot. Returns #sessions restored.

    `router_factory(provider)` builds a fresh BrevitasRouter for a registry key that
    doesn't exist yet. Unknown/malformed entries are skipped (fail-safe)."""
    from . import engine
    from .router import _SessionState
    from .shared_prefix import _PipelineState, _default as shared

    if not isinstance(snapshot, dict) or snapshot.get("v") != 1:
        return 0
    if time.time() - float(snapshot.get("ts", 0)) > _MAX_AGE_S:
        return 0

    n = 0
    for key, rd in (snapshot.get("routers") or {}).items():
        try:
            r = routers.get(key)
            if r is None:
                r = router_factory(rd.get("provider", "openai"))
                routers[key] = r
            r.model = rd.get("model", "") or r.model
            for sid, sd in (rd.get("sessions") or {}).items():
                st = _SessionState()
                for f in _SESSION_FIELDS:
                    if f in sd:
                        setattr(st, f, sd[f])
                r._sessions.setdefault(sid, st)
                n += 1
        except Exception:
            continue
    for pid, pd in (snapshot.get("shared") or {}).items():
        try:
            ps = _PipelineState()
            ps.seen = {h: set(a) for h, a in (pd.get("seen") or {}).items()}
            ps.promo_order = {h: int(i) for h, i in (pd.get("promo") or {}).items()}
            ps.explicit = set(pd.get("explicit") or [])
            shared._pipelines[pid] = ps
        except Exception:
            continue
    for pipe, st in (snapshot.get("b9") or {}).items():
        if isinstance(st, dict) and "locked" in st:
            engine._b9_pipes[pipe] = {"reordered": bool(st.get("reordered")),
                                      "locked": bool(st.get("locked")),
                                      "pre_hit": float(st.get("pre_hit", 0.0)),
                                      "post_hit": float(st.get("post_hit", -1.0)),
                                      "post_n": int(st.get("post_n", 0))}
    return n


def save(path: str, routers: Dict[str, object]) -> bool:
    """Atomic snapshot write (tmp + rename); never raises into the request path."""
    try:
        snap = capture(routers)
        d = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".brevstate-")
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load(path: str, routers: Dict[str, object], router_factory) -> int:
    """Restore from file if present/valid; 0 on any problem (fail-safe cold start)."""
    try:
        with open(path) as f:
            snap = json.load(f)
        return restore(snap, routers, router_factory)
    except Exception:
        return 0
