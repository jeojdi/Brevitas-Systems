"""Lever 2 — content-addressed shared memory with sub-document dedup.

Two published algorithms, implemented faithfully:

1. IPFS content addressing + Merkle DAG  (Benet 2014, arXiv:1407.3561)
   "a high-throughput content-addressed block storage model, with content-addressed
   hyper links ... a generalized Merkle DAG." Each object is named by the hash of its
   bytes; identical content -> identical name -> stored once; references are verifiable
   by re-hashing (self-certification).

2. LBFS content-defined chunking via Rabin fingerprints (Muthitacharoen et al., SOSP 2001)
   "When the low-order 13 bits of a region's fingerprint equal a chosen value, the region
   constitutes a breakpoint. Assuming random data, the expected chunk size is 2^13 = 8 KB
   (plus the 48-byte breakpoint window)." Editing one region changes only the local
   chunk(s); every other chunk's hash is unchanged -> only changed chunks need transfer.

The point for Brevitas: when multiple agents carry near-identical context/artifacts, the
shared store keeps one copy per unique chunk and every reference is a content id, not bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# --------------------------------------------------------------------------- #
# IPFS-style content identifier (self-certifying name = hash of the bytes)
# --------------------------------------------------------------------------- #
def cid(data: bytes, bits: int = 128) -> str:
    """Content identifier = SHA-256 of the bytes, truncated to `bits` bits.

    SHA-256 (not the repo's old 40-bit sha1[:10]); 128 bits keeps the birthday
    bound astronomically safe for any realistic store size. The id IS the identity,
    exactly as in IPFS / Git object models.
    """
    if bits % 4 != 0:
        raise ValueError("bits must be a multiple of 4")
    return "b" + hashlib.sha256(data).hexdigest()[: bits // 4]


# --------------------------------------------------------------------------- #
# LBFS content-defined chunking (Rabin-fingerprint sliding window)
# --------------------------------------------------------------------------- #
class RabinChunker:
    """Split a byte string into content-defined chunks (LBFS, SOSP 2001).

    A polynomial rolling fingerprint is maintained over a sliding `window` of bytes
    (LBFS uses a Rabin fingerprint over GF(2); a Rabin-Karp polynomial hash mod a large
    prime is the standard practical equivalent and yields the same content-defined
    boundary property). A boundary ("breakpoint") is declared when the low `avg_bits`
    bits of the fingerprint equal `magic`, giving an expected chunk size of 2^avg_bits.
    `min_size`/`max_size` bound chunk lengths to avoid pathological cases (also from LBFS).
    """

    _BASE = 257
    _MOD = (1 << 61) - 1  # large Mersenne prime

    def __init__(
        self,
        window: int = 48,
        avg_bits: int = 13,
        min_size: int = 2048,
        max_size: int = 65536,
        magic: int = 0x78,
    ) -> None:
        if window < 1:
            raise ValueError("window must be >= 1")
        if min_size <= window:
            # boundary test only fires once the window is full; keep min > window
            min_size = window + 1
        self.window = window
        self.mask = (1 << avg_bits) - 1
        self.min_size = min_size
        self.max_size = max(max_size, min_size + 1)
        self.magic = magic & self.mask
        # BASE^(window-1) mod MOD, used to evict the byte leaving the window
        self._top = pow(self._BASE, window - 1, self._MOD)

    def split(self, data: bytes) -> List[bytes]:
        n = len(data)
        if n == 0:
            return []
        if n <= self.min_size:
            return [data]

        chunks: List[bytes] = []
        start = 0
        h = 0  # rolling fingerprint of the current window, reset at each chunk start
        i = 0
        while i < n:
            b = data[i]
            if i - start < self.window:
                h = (h * self._BASE + b) % self._MOD
            else:
                out = data[i - self.window]
                h = ((h - out * self._top) * self._BASE + b) % self._MOD

            cur_len = i - start + 1
            boundary = cur_len >= self.min_size and (h & self.mask) == self.magic
            if boundary or cur_len >= self.max_size:
                chunks.append(data[start : i + 1])
                start = i + 1
                h = 0
            i += 1

        if start < n:
            chunks.append(data[start:])
        return chunks


# --------------------------------------------------------------------------- #
# Content-addressed store with Merkle-DAG manifests
# --------------------------------------------------------------------------- #
@dataclass
class StoreStats:
    blocks_stored: int = 0
    bytes_stored: int = 0           # unique bytes actually held
    bytes_logical: int = 0          # total bytes referenced (incl. duplicates)
    bytes_transferred: int = 0      # new bytes that had to be sent on the last put

    @property
    def dedup_ratio(self) -> float:
        """Fraction of logical bytes avoided by dedup (1.0 = everything deduped)."""
        if self.bytes_logical <= 0:
            return 0.0
        return 1.0 - (self.bytes_stored / self.bytes_logical)


@dataclass
class ContentStore:
    """Content-addressed block store. `put_artifact` chunks (LBFS) and stores each chunk
    once (IPFS). A manifest is itself a content-addressed Merkle node listing child cids.
    `get_artifact` reconstructs and re-verifies (self-certification)."""

    chunker: RabinChunker = field(default_factory=RabinChunker)
    blocks: Dict[str, bytes] = field(default_factory=dict)
    manifests: Dict[str, dict] = field(default_factory=dict)
    stats: StoreStats = field(default_factory=StoreStats)

    # -- write -------------------------------------------------------------- #
    def put_artifact(self, data: bytes) -> str:
        """Store an artifact; return its manifest cid (a Merkle root over chunk cids)."""
        chunks = self.chunker.split(data)
        child_ids: List[str] = []
        new_bytes = 0
        for c in chunks:
            cidv = cid(c)
            if cidv not in self.blocks:
                self.blocks[cidv] = c
                self.stats.blocks_stored += 1
                self.stats.bytes_stored += len(c)
                new_bytes += len(c)
            child_ids.append(cidv)

        manifest = {"type": "blob", "size": len(data), "chunks": child_ids}
        # the manifest's name is the hash of its canonical encoding -> Merkle DAG node
        manifest_bytes = repr(manifest).encode("utf-8")
        root = cid(manifest_bytes)
        self.manifests[root] = manifest

        self.stats.bytes_logical += len(data)
        self.stats.bytes_transferred = new_bytes
        return root

    # -- read --------------------------------------------------------------- #
    def get_artifact(self, root: str) -> Optional[bytes]:
        """Reconstruct an artifact from its manifest, verifying every chunk.

        Returns None (fail-safe signal) if any referenced chunk is missing or any
        chunk fails its content-hash check — callers must then fall back to full send.
        """
        manifest = self.manifests.get(root)
        if manifest is None:
            return None
        parts: List[bytes] = []
        for chash in manifest["chunks"]:
            block = self.blocks.get(chash)
            if block is None:
                return None  # silent-loss guard: missing chunk -> fail-safe
            if cid(block) != chash:
                return None  # corruption guard (self-certification)
            parts.append(block)
        out = b"".join(parts)
        if len(out) != manifest["size"]:
            return None
        return out

    def has(self, root: str) -> bool:
        return root in self.manifests
