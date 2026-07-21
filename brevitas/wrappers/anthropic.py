"""
Wraps anthropic.Anthropic so messages.create()/stream() can apply content-preserving
cache optimization before forwarding to Anthropic. Quality-affecting retrieval is opt-in.

Usage:
    import anthropic, brevitas
    client = brevitas.wrap(anthropic.Anthropic(api_key="sk-ant-..."))
    response = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024, messages=[...])
"""
from __future__ import annotations

from copy import deepcopy
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
        # The optimizer adds cache metadata in place. SDK callers commonly reuse the
        # same message dictionaries across turns, so always work on a deep copy.
        body = {"messages": deepcopy(list(messages)), "model": model, **deepcopy(kwargs)}
        baseline = count_request_tokens(body, "messages")
        original = deepcopy(body)
        try:
            meta = optimize_request(body, _PROVIDER, self._router, sid)   # in-place
        except Exception:
            # Optimization is optional middleware. Any failure must preserve the
            # provider call exactly and never break the user's request path.
            body = original
            meta = {"strategy": "passthrough:optimizer_error",
                    "response_faithful": True}
        compressed = count_request_tokens(body, "messages")
        return body, sid, labels, baseline, compressed, meta

    def create(self, *, messages: list[dict], model: str = "", **kwargs: Any) -> Any:
        body, sid, labels, baseline, compressed, meta = self._optimize(messages, model, kwargs)
        response = self._orig.create(**body)
        try:
            block = response.content[0]
            self._session.record_response(getattr(block, "text", ""))
            usage = _usage_dict(response.usage)
            record_usage(usage, _PROVIDER, self._router, sid)
            receipt = normalize_usage(usage, _PROVIDER)
            report_usage(_PROVIDER, model, baseline, compressed, self._session,
                         pipeline=labels["pipeline"], agent=labels["agent"],
                         run_id=labels["run_id"], usage_raw=usage,
                         strategy=meta.get("strategy", "passthrough"),
                         metadata={**labels, **receipt.as_dict(), "operation": "messages",
                                   "cache_attributable":
                                       meta.get("cache_control_owner") == "brevitas"})
        except Exception:
            # The provider response is authoritative. Metering/reporting is best-effort
            # middleware and must never turn a successful model call into a failure.
            pass
        self._session.advance()
        return response

    def stream(self, *, messages: list[dict], model: str = "", **kwargs: Any):
        body, sid, labels, baseline, compressed, meta = self._optimize(messages, model, kwargs)
        return _MeteredAnthropicStream(self._orig.stream(**body), model, baseline,
                                       compressed, self._session, self._router, sid,
                                       labels, meta)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


def _usage_dict(value: Any) -> dict:
    if isinstance(value, dict):
        data = dict(value)
    elif hasattr(value, "model_dump"):
        data = value.model_dump()
    else:
        data = {name: getattr(value, name) for name in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
            "output_tokens", "cache_creation") if hasattr(value, name)}
    if hasattr(data.get("cache_creation"), "model_dump"):
        data["cache_creation"] = data["cache_creation"].model_dump()
    return data


class _MeteredAnthropicStream:
    def __init__(self, manager, model, baseline, compressed, session, router, sid, labels, meta):
        self._manager, self._active = manager, None
        self._model, self._baseline, self._compressed = model, baseline, compressed
        self._session = session
        self._router, self._sid, self._labels = router, sid, labels
        self._meta = meta
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
            usage = _usage_dict(message.usage)
            receipt = normalize_usage(usage, _PROVIDER)
            record_usage(usage, _PROVIDER, self._router, self._sid)
            report_usage(_PROVIDER, self._model, self._baseline, self._compressed,
                self._session, pipeline=self._labels["pipeline"], agent=self._labels["agent"],
                run_id=self._labels["run_id"], usage_raw=usage,
                strategy=self._meta.get("strategy", "passthrough"),
                metadata={**self._labels, **receipt.as_dict(), "operation": "messages",
                          "is_stream": True, "cache_attributable":
                              self._meta.get("cache_control_owner") == "brevitas"})
        except Exception:
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
