"""
Wraps anthropic.Anthropic so that messages.create() and messages.stream()
automatically compress context before forwarding to Anthropic.

Usage:
    import anthropic, brevitas
    brevitas.configure(api_key="bvt_...", base_url="http://localhost:8000")
    client = brevitas.wrap(anthropic.Anthropic(api_key="sk-ant-..."))
    response = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024, messages=[...])
"""
from __future__ import annotations

from typing import Any

from .._compress import compress_messages, report_usage
from ..session import BrevitasSession

_PROVIDER = "anthropic"


class _BrevitasMessages:
    def __init__(self, messages_obj: Any, session: BrevitasSession) -> None:
        self._orig = messages_obj
        self._session = session

    def create(self, *, messages: list[dict], model: str = "", **kwargs: Any) -> Any:
        task = kwargs.pop("_brevitas_task", "")
        compressed, baseline, compressed_tok = compress_messages(
            messages, self._session, task=task
        )
        response = self._orig.create(messages=compressed, model=model, **kwargs)
        # Record session context and report billing
        response_text = ""
        if hasattr(response, "content") and response.content:
            block = response.content[0]
            response_text = getattr(block, "text", "")
        self._session.record_response(response_text)
        self._session.advance()
        report_usage(_PROVIDER, model, baseline, compressed_tok, self._session)
        return response

    def stream(self, *, messages: list[dict], model: str = "", **kwargs: Any):
        task = kwargs.pop("_brevitas_task", "")
        compressed, baseline, compressed_tok = compress_messages(
            messages, self._session, task=task
        )
        ctx = self._orig.messages.stream(messages=compressed, model=model, **kwargs)
        report_usage(_PROVIDER, model, baseline, compressed_tok, self._session)
        return ctx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


class BrevitasAnthropicClient:
    def __init__(self, client: Any, session: BrevitasSession | None = None) -> None:
        self._client = client
        self._session = session or BrevitasSession()
        self.messages = _BrevitasMessages(client.messages, self._session)

    @property
    def session(self) -> BrevitasSession:
        return self._session

    def new_session(self) -> None:
        """Start a fresh pipeline run (clears cross-hop context)."""
        self._session = BrevitasSession()
        self.messages = _BrevitasMessages(self._client.messages, self._session)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
