"""Lever 3 — delta transmission of near-identical artifacts.

Three published algorithms, implemented faithfully:

1. Myers O(ND) diff  (Myers 1986, "An O(ND) Difference Algorithm and Its Variations")
   Greedy shortest-edit-script over the edit graph; basis of UNIX diff. Used here for
   text/code artifacts to find matched regions (COPY) vs inserted regions (ADD).

2. VCDIFF instruction set  (RFC 3284)
   The delta is a list of ADD(bytes) / COPY(addr,size) / RUN(byte,size) ops over the
   "superstring" U = source S + target-so-far T. COPY may reference already-emitted
   target bytes (periodic-sequence compression).

3. rsync rolling checksum  (Tridgell & Mackerras, TR-CS-96-05, 1996)
   weak rolling sum  a(k,l)=Σ X_i mod M ,  b(k,l)=Σ (l-i+1)X_i mod M ,  s=a+2^16·b ,
   plus a strong hash, to match base blocks at any offset in O(n). Used for large blobs.

Accuracy-first: every delta carries base_hash + target_hash; `apply_delta` re-hashes the
reconstruction and returns None (fail-safe -> full send) on any base/hash mismatch.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_M = 1 << 16


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# 1. Myers O(ND) diff  ->  match/insert moves
# --------------------------------------------------------------------------- #
def myers_moves(a: bytes, b: bytes) -> List[tuple]:
    """Return forward edit moves transforming a -> b.

    Moves are ('match', xa, yb) | ('ins', yb) | ('del', xa), in order. Implements the
    greedy O(ND) algorithm with a per-D trace and backtrack (Myers 1986, Fig. 2 + §4).
    """
    n, m = len(a), len(b)
    maxd = n + m
    if maxd == 0:
        return []
    off = maxd
    v = [0] * (2 * maxd + 1)
    trace: List[List[int]] = []
    found_d = maxd
    for d in range(maxd + 1):
        trace.append(v.copy())
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and v[off + k - 1] < v[off + k + 1]):
                x = v[off + k + 1]          # move down  (insertion in b)
            else:
                x = v[off + k - 1] + 1      # move right (deletion in a)
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1                      # follow the diagonal "snake"
            v[off + k] = x
            if x >= n and y >= m:
                found_d = d
                trace.append(v.copy())      # final state
                return _backtrack(a, b, trace, found_d, off)
    return _backtrack(a, b, trace, found_d, off)


def _backtrack(a: bytes, b: bytes, trace: List[List[int]], d: int, off: int) -> List[tuple]:
    n, m = len(a), len(b)
    x, y = n, m
    moves: List[tuple] = []
    for dd in range(d, 0, -1):
        v = trace[dd]
        k = x - y
        if k == -dd or (k != dd and v[off + k - 1] < v[off + k + 1]):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = v[off + prev_k]
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            moves.append(("match", x - 1, y - 1))
            x -= 1
            y -= 1
        if x == prev_x:
            moves.append(("ins", y - 1))
        else:
            moves.append(("del", x - 1))
        x, y = prev_x, prev_y
    while x > 0 and y > 0:
        moves.append(("match", x - 1, y - 1))
        x -= 1
        y -= 1
    while y > 0:
        moves.append(("ins", y - 1))
        y -= 1
    while x > 0:
        moves.append(("del", x - 1))
        x -= 1
    moves.reverse()
    return moves


def _moves_to_vcdiff(a: bytes, b: bytes, moves: List[tuple]) -> List[dict]:
    """Coalesce Myers moves into VCDIFF COPY/ADD ops (RFC 3284)."""
    ops: List[dict] = []
    i = 0
    nmov = len(moves)
    while i < nmov:
        kind = moves[i][0]
        if kind == "match":
            start_addr = moves[i][1]
            size = 0
            while i < nmov and moves[i][0] == "match" and moves[i][1] == start_addr + size:
                size += 1
                i += 1
            ops.append({"op": "COPY", "addr": start_addr, "size": size})
        elif kind == "ins":
            buf = bytearray()
            while i < nmov and moves[i][0] == "ins":
                buf.append(b[moves[i][1]])
                i += 1
            ops.append({"op": "ADD", "data": bytes(buf)})
        else:  # 'del' -> base bytes simply not copied; no wire op needed
            i += 1
    return _runs(ops)


def _runs(ops: List[dict]) -> List[dict]:
    """Convert long single-byte ADD runs into RUN ops (RFC 3284)."""
    out: List[dict] = []
    for op in ops:
        if op["op"] == "ADD" and len(op["data"]) >= 8 and len(set(op["data"])) == 1:
            out.append({"op": "RUN", "byte": op["data"][0], "size": len(op["data"])})
        else:
            out.append(op)
    return out


# --------------------------------------------------------------------------- #
# 3. rsync rolling-checksum block matching (for large blobs)
# --------------------------------------------------------------------------- #
def _weak(block: bytes) -> Tuple[int, int, int]:
    a = sum(block) % _M
    b = 0
    L = len(block)
    for idx, x in enumerate(block):
        b = (b + (L - idx) * x) % _M
    return a, b, (a + _M * b)


def rsync_ops(base: bytes, target: bytes, block: int = 1024) -> List[dict]:
    """Emit COPY/ADD ops matching `target` against `base` using rsync's rolling sum."""
    n = len(base)
    if n == 0 or len(target) < block:
        return [{"op": "ADD", "data": target}] if target else []

    # signatures of base blocks (non-overlapping)
    sig: Dict[int, List[Tuple[str, int]]] = {}
    for off in range(0, n - block + 1, block):
        blk = base[off : off + block]
        _, _, s = _weak(blk)
        sig.setdefault(s, []).append((_h(blk), off))

    ops: List[dict] = []
    literal = bytearray()
    i = 0
    tlen = len(target)
    a = b = 0
    have_window = False
    while i + block <= tlen:
        if not have_window:
            a, b, s = _weak(target[i : i + block])
            have_window = True
        else:
            s = (a + _M * b)
        matched = False
        if s in sig:
            strong = _h(target[i : i + block])
            for hh, off in sig[s]:
                if hh == strong:
                    if literal:
                        ops.append({"op": "ADD", "data": bytes(literal)})
                        literal = bytearray()
                    ops.append({"op": "COPY", "addr": off, "size": block})
                    i += block
                    have_window = False
                    matched = True
                    break
        if not matched:
            literal.append(target[i])
            # roll window forward by one byte
            out = target[i]
            inb = target[i + block]
            a = (a - out + inb) % _M
            b = (b - block * out + a) % _M
            i += 1
    literal.extend(target[i:])
    if literal:
        ops.append({"op": "ADD", "data": bytes(literal)})
    return _runs(ops)


# --------------------------------------------------------------------------- #
# Delta payload + codec with verification (accuracy-first)
# --------------------------------------------------------------------------- #
@dataclass
class DeltaPayload:
    base_hash: str
    target_hash: str
    method: str                       # "myers" | "rsync" | "full"
    ops: List[dict] = field(default_factory=list)
    full: Optional[bytes] = None      # set when method == "full"

    def wire_size(self) -> int:
        """Approx bytes on the wire (literal payload only; refs are tiny)."""
        if self.method == "full":
            return len(self.full or b"")
        total = 0
        for op in self.ops:
            if op["op"] == "ADD":
                total += len(op["data"])
            elif op["op"] == "RUN":
                total += 2
            else:  # COPY -> a couple ints
                total += 8
        return total


def encode_delta(base: bytes, target: bytes, method: str = "auto",
                 large_threshold: int = 16_384) -> DeltaPayload:
    base_hash, target_hash = _h(base), _h(target)
    if not base:
        return DeltaPayload(base_hash, target_hash, "full", full=target)
    if method == "auto":
        method = "rsync" if len(target) >= large_threshold else "myers"
    if method == "myers":
        ops = _moves_to_vcdiff(base, target, myers_moves(base, target))
    elif method == "rsync":
        ops = rsync_ops(base, target)
    else:
        return DeltaPayload(base_hash, target_hash, "full", full=target)
    return DeltaPayload(base_hash, target_hash, method, ops=ops)


def apply_delta(base: bytes, payload: DeltaPayload) -> Optional[bytes]:
    """Reconstruct target from base + delta, verifying base and result hashes.

    Returns None (fail-safe -> caller must request a full send) if the base doesn't
    match the sender's assumed base, or the reconstruction fails its hash check.
    """
    if payload.method == "full":
        out = payload.full or b""
        return out if _h(out) == payload.target_hash else None

    if _h(base) != payload.base_hash:
        return None  # base drift -> never reconstruct a wrong state silently

    out = bytearray()
    U = base  # COPY addresses index the source (and emitted target via out)
    for op in payload.ops:
        kind = op["op"]
        if kind == "COPY":
            addr, size = op["addr"], op["size"]
            if addr < len(U):
                if addr + size <= len(U):
                    out.extend(U[addr : addr + size])
                else:
                    return None
            else:  # COPY from already-emitted target (RFC 3284 superstring)
                taddr = addr - len(U)
                for j in range(size):
                    if taddr + j >= len(out):
                        return None
                    out.append(out[taddr + j])
        elif kind == "ADD":
            out.extend(op["data"])
        elif kind == "RUN":
            out.extend(bytes([op["byte"]]) * op["size"])
        else:
            return None

    result = bytes(out)
    return result if _h(result) == payload.target_hash else None
