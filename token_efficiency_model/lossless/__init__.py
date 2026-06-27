"""Lossless token-saving levers for Brevitas, each implementing a published algorithm.

Lever 2 — content-addressed dedup:
  * IPFS content addressing + Merkle DAG  (Benet 2014, arXiv:1407.3561)
  * LBFS content-defined chunking w/ Rabin fingerprints (Muthitacharoen et al., SOSP 2001)

See token_efficiency_model/lossless/content_store.py.
"""

from .content_store import ContentStore, RabinChunker, cid

__all__ = ["ContentStore", "RabinChunker", "cid"]
