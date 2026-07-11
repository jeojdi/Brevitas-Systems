"""
Wraps anthropic.Anthropic so messages.create()/stream() apply LOSSLESS token savings
(auto-router: cache_control breakpoints vs retrieval) before forwarding to Anthropic.

Usage:
    import anthropic, brevitas
    client = brevitas.wrap(anthropic.Anthropic(api_key="sk-ant-..."))
    response = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024, messages=[...])
"""
from __future__ import annotations

from typing import Any

from .._compress import report_usage
from ..labels import resolve_labels
from ..receipts import count_request_tokens, normalize_usage
from ..session import BrevitasSession
from token_efficiency_model.lossless.engine import optimize_request, record_usage
from token_efficiency_model.lossless.router import BrevitasRouter

_PROVIDER = "anthropic"


class _BrevitasMessages:
    def __init__(self, messages_obj: Any, session: BrevitasSession,
                 router: BrevitasRouter) -> None:
        self._orig = messages_obj
        self._session = session
        self._router = router

    def _optimize(self, messages, model, kwargs):
        sid = kwargs.pop("_brevitas_session", self._session.session_id)
        labels = resolve_labels(kwargs.pop("_brevitas_meta", None))
        body = {"messages": list(messages), "model": model, **kwargs}
        baseline = count_request_tokens(body, "messages")
        optimize_request(body, _PROVIDER, self._router, sid)   # lossless, in-place
        return body, sid, labels, baseline

    def create(self, *, messages: list[dict], model: str = "", **kwargs: Any) -> Any:
        body, sid, labels, baseline = self._optimize(messages, model, kwargs)
        response = self._orig.create(**body)
        try:
            block = response.content[0]
            self._session.record_response(getattr(block, "text", ""))
            u = response.usage
            fresh = getattr(u, "input_tokens", 0)
            write = getattr(u, "cache_creation_input_tokens", 0)
            read = getattr(u, "cache_read_input_tokens", 0)
            usage = {"input_tokens": fresh, "cache_creation_input_tokens": write,
                     "cache_read_input_tokens": read,
                     "output_tokens": getattr(u, "output_tokens", 0)}
            record_usage(usage, _PROVIDER, self._router, sid)
            receipt = normalize_usage(usage, _PROVIDER)
            report_usage(_PROVIDER, model, baseline, receipt.input_tokens, self._session,
                         pipeline=labels["pipeline"], agent=labels["agent"],
                         run_id=labels["run_id"], usage_raw=usage, strategy="native_cache",
                         metadata={**labels, **receipt.as_dict(), "operation": "messages"})
        except (AttributeError, IndexError):
            pass
        self._session.advance()
        return response

    def stream(self, *, messages: list[dict], model: str = "", **kwargs: Any):
        body, sid, labels, baseline = self._optimize(messages, model, kwargs)
        return _MeteredAnthropicStream(self._orig.stream(**body), model, baseline,
                                       self._session, self._router, sid, labels)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


class _MeteredAnthropicStream:
    def __init__(self, manager, model, baseline, session, router, sid, labels):
        self._manager, self._active = manager, None
        self._model, self._baseline, self._session = model, baseline, session
        self._router, self._sid, self._labels = router, sid, labels
        self._done = False

    def __enter__(self):
        self._active = self._manager.__enter__()
        return self

    def __exit__(self, *args):
        try:
            self._finish()
        finally:
            return self._manager.__exit__(*args)

    def _finish(self):
        if self._done or self._active is None:
            return
        self._done = True
        try:
            message = self._active.get_final_message()
            usage_obj = message.usage
            usage = usage_obj.model_dump() if hasattr(usage_obj, "model_dump") else {
                name: getattr(usage_obj, name, 0) for name in (
                    "input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens")}
            receipt = normalize_usage(usage, _PROVIDER)
            record_usage(usage, _PROVIDER, self._router, self._sid)
            report_usage(_PROVIDER, self._model, self._baseline, receipt.input_tokens,
                self._session, pipeline=self._labels["pipeline"], agent=self._labels["agent"],
                run_id=self._labels["run_id"], usage_raw=usage, strategy="native_cache",
                metadata={**self._labels, **receipt.as_dict(), "operation": "messages",
                          "is_stream": True})
        except (AttributeError, TypeError):
            pass
        self._session.advance()

    def __iter__(self):
        return iter(self._active or self._manager)

    def __getattr__(self, name):
        return getattr(self._active or self._manager, name)


class BrevitasAnthropicClient:
    def __init__(self, client: Any, session: BrevitasSession | None = None) -> None:
        self._client = client
        self._session = session or BrevitasSession()
        self._router = BrevitasRouter(provider="anthropic")
        self.messages = _BrevitasMessages(client.messages, self._session, self._router)

    @property
    def session(self) -> BrevitasSession:
        return self._session

    def new_session(self) -> None:
        """Start a fresh pipeline run (clears cross-hop context)."""
        self._session = BrevitasSession()
        self.messages = _BrevitasMessages(self._client.messages, self._session, self._router)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
