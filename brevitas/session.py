"""
Multi-hop session tracking.

Keeps a running record of every agent response so the next hop
can reference prior content and benefit from cross-hop deduplication.
"""
import secrets
from typing import Any


class BrevitasSession:
    def __init__(self, session_id: str = ""):
        self.session_id = session_id or ("sess_" + secrets.token_urlsafe(12))
        self._prior_content: list[str] = []
        self.hop_count: int = 0

    def record_response(self, text: str) -> None:
        """Call after each agent response to build cross-hop context."""
        if text:
            self._prior_content.append(text)

    def prior_context(self) -> list[str]:
        """Return all prior agent outputs as context for the next compression call."""
        return list(self._prior_content)

    def advance(self) -> None:
        self.hop_count += 1

    def reset(self) -> None:
        self._prior_content.clear()
        self.hop_count = 0
