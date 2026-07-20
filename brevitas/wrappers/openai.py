"""
Wraps openai.OpenAI so chat.completions.create() applies LOSSLESS token savings
(auto-router: provider caching vs retrieval) before forwarding to OpenAI/DeepSeek.

Usage:
    import openai, brevitas
    client = brevitas.wrap(openai.OpenAI(api_key="sk-..."))
    response = client.chat.completions.create(model="gpt-4o", messages=[...])
"""
from __future__ import annotations

from typing import Any

from .._compress import report_usage
from ..labels import resolve_labels
from ..receipts import canonical_provider, count_request_tokens, normalize_usage
from ..session import BrevitasSession
from token_efficiency_model.lossless.engine import optimize_request, record_usage
from token_efficiency_model.lossless.router import BrevitasRouter


class _BrevitasCompletions:
    def __init__(self, completions_obj: Any, session: BrevitasSession,
                 router: BrevitasRouter) -> None:
        self._orig = completions_obj
        self._session = session
        self._router = router

    def create(self, *, messages: list[dict], model: str = "", **kwargs: Any) -> Any:
        sid = kwargs.pop("_brevitas_session", self._session.session_id)
        labels = resolve_labels(kwargs.pop("_brevitas_meta", None))
        provider = canonical_provider("openai", model)
        body = {"messages": list(messages), "model": model, **kwargs}
        baseline = count_request_tokens(body, "chat.completions")
        meta = optimize_request(body, provider, self._router, sid)   # in-place
        compressed = count_request_tokens(body, "chat.completions")
        strategy = meta.get("strategy", "passthrough")
        if body.get("stream") and provider in ("openai", "deepseek"):
            body.setdefault("stream_options", {}).setdefault("include_usage", True)
        response = self._orig.create(**body)
        if body.get("stream"):
            return _MeteredOpenAIStream(response, provider, model, baseline, compressed,
                                        self._session, self._router, sid, labels,
                                        "chat.completions", strategy)
        try:
            text = response.choices[0].message.content or ""
            self._session.record_response(text)
            usage = _model_dict(response.usage)
            receipt = normalize_usage(usage, provider)
            s = record_usage(usage, provider, self._router, sid)
            report_usage(provider, model, baseline, compressed, self._session,
                         pipeline=labels["pipeline"], agent=labels["agent"],
                         run_id=labels["run_id"], usage_raw=usage, strategy=strategy,
                         metadata={**labels, **receipt.as_dict(), "operation": "chat.completions"})
        except (AttributeError, IndexError):
            pass
        self._session.advance()
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


class _BrevitasChat:
    def __init__(self, chat_obj: Any, session: BrevitasSession, router: BrevitasRouter) -> None:
        self._orig = chat_obj
        self.completions = _BrevitasCompletions(chat_obj.completions, session, router)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


def _model_dict(value: Any) -> dict:
    if isinstance(value, dict):
        data = dict(value)
    elif hasattr(value, "model_dump"):
        data = value.model_dump()
    else:
        data = {name: getattr(value, name) for name in (
            "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens",
            "prompt_tokens_details", "input_tokens_details", "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens") if hasattr(value, name)}
    for name in ("prompt_tokens_details", "input_tokens_details"):
        if hasattr(data.get(name), "model_dump"):
            data[name] = data[name].model_dump()
    return data


class _MeteredOpenAIStream:
    def __init__(self, stream, provider, model, baseline, compressed, session, router, sid,
                 labels, operation, strategy):
        self._stream, self._provider, self._model = stream, provider, model
        self._baseline, self._compressed = baseline, compressed
        self._session, self._router = session, router
        self._sid, self._labels, self._operation, self._strategy = (
            sid, labels, operation, strategy)
        self._usage = {}
        self._done = False

    def __iter__(self):
        try:
            for chunk in self._stream:
                usage = getattr(chunk, "usage", None)
                response = getattr(chunk, "response", None)
                usage = usage or (getattr(response, "usage", None) if response else None)
                if usage:
                    self._usage = _model_dict(usage)
                yield chunk
        finally:
            self._finish()

    def _finish(self):
        if self._done:
            return
        self._done = True
        receipt = normalize_usage(self._usage, self._provider)
        if receipt.total_tokens:
            record_usage(self._usage, self._provider, self._router, self._sid)
            report_usage(self._provider, self._model, self._baseline, self._compressed,
                self._session, pipeline=self._labels["pipeline"], agent=self._labels["agent"],
                run_id=self._labels["run_id"], usage_raw=self._usage, strategy=self._strategy,
                metadata={**self._labels, **receipt.as_dict(), "operation": self._operation,
                          "is_stream": True})
        self._session.advance()

    def __enter__(self):
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self._stream.__exit__(*args) if hasattr(self._stream, "__exit__") else None
        finally:
            self._finish()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _BrevitasResponses:
    def __init__(self, responses_obj: Any, session: BrevitasSession, router: BrevitasRouter) -> None:
        self._orig, self._session, self._router = responses_obj, session, router

    def create(self, *, model: str = "", input: Any = None, **kwargs: Any) -> Any:
        labels = resolve_labels(kwargs.pop("_brevitas_meta", None))
        sid = kwargs.pop("_brevitas_session", self._session.session_id)
        provider = canonical_provider("openai", model)
        body = {"model": model, "input": input, **kwargs}
        baseline = count_request_tokens(body, "responses")
        strategy = "passthrough"
        if isinstance(input, list) and all(isinstance(item, dict) and "role" in item for item in input):
            temporary = {"model": model, "messages": list(input)}
            meta = optimize_request(temporary, provider, self._router, sid)
            body["input"] = temporary["messages"]
            strategy = meta.get("strategy", "passthrough")
        compressed = count_request_tokens(body, "responses")
        response = self._orig.create(**body)
        if body.get("stream"):
            return _MeteredOpenAIStream(response, provider, model, baseline, compressed,
                                        self._session, self._router, sid, labels,
                                        "responses", strategy)
        usage = _model_dict(getattr(response, "usage", {}))
        receipt = normalize_usage(usage, provider)
        if receipt.total_tokens:
            record_usage(_router_compatible_usage(receipt), provider, self._router, sid)
            report_usage(provider, model, baseline, compressed, self._session,
                pipeline=labels["pipeline"], agent=labels["agent"], run_id=labels["run_id"],
                usage_raw=usage, strategy=strategy,
                metadata={**labels, **receipt.as_dict(), "operation": "responses"})
        self._session.advance()
        return response

    def __getattr__(self, name):
        return getattr(self._orig, name)


class _BrevitasPlainResource:
    def __init__(self, resource: Any, session: BrevitasSession, router: BrevitasRouter,
                 operation: str) -> None:
        self._orig, self._session, self._router = resource, session, router
        self._operation = operation

    def create(self, *, model: str = "", **kwargs: Any) -> Any:
        labels = resolve_labels(kwargs.pop("_brevitas_meta", None))
        sid = kwargs.pop("_brevitas_session", self._session.session_id)
        provider = canonical_provider("openai", model)
        body = {"model": model, **kwargs}
        baseline = count_request_tokens(body, self._operation)
        response = self._orig.create(**body)
        if body.get("stream"):
            return _MeteredOpenAIStream(response, provider, model, baseline, baseline,
                                        self._session, self._router, sid, labels,
                                        self._operation, "passthrough")
        usage = _model_dict(getattr(response, "usage", {}))
        receipt = normalize_usage(usage, provider)
        if receipt.total_tokens:
            record_usage(_router_compatible_usage(receipt), provider, self._router, sid)
            report_usage(provider, model, baseline, baseline, self._session,
                pipeline=labels["pipeline"], agent=labels["agent"], run_id=labels["run_id"],
                usage_raw=usage, strategy="passthrough",
                metadata={**labels, **receipt.as_dict(), "operation": self._operation})
        self._session.advance()
        return response

    def __getattr__(self, name):
        return getattr(self._orig, name)


def _router_compatible_usage(receipt):
    return {"prompt_tokens": receipt.input_tokens,
            "prompt_tokens_details": {"cached_tokens": receipt.cached_input_tokens},
            "completion_tokens": receipt.output_tokens}


class BrevitasOpenAIClient:
    def __init__(self, client: Any, session: BrevitasSession | None = None) -> None:
        self._client = client
        self._session = session or BrevitasSession()
        self._router = BrevitasRouter(provider="openai")
        self.chat = _BrevitasChat(client.chat, self._session, self._router)
        if hasattr(client, "responses"):
            self.responses = _BrevitasResponses(client.responses, self._session, self._router)
        if hasattr(client, "embeddings"):
            self.embeddings = _BrevitasPlainResource(client.embeddings, self._session, self._router, "embeddings")
        if hasattr(client, "completions"):
            self.completions = _BrevitasPlainResource(client.completions, self._session, self._router, "completions")

    @property
    def session(self) -> BrevitasSession:
        return self._session

    def new_session(self) -> None:
        self._session = BrevitasSession()
        self.chat = _BrevitasChat(self._client.chat, self._session, self._router)
        if hasattr(self._client, "responses"):
            self.responses = _BrevitasResponses(self._client.responses, self._session, self._router)
        if hasattr(self._client, "embeddings"):
            self.embeddings = _BrevitasPlainResource(self._client.embeddings, self._session, self._router, "embeddings")
        if hasattr(self._client, "completions"):
            self.completions = _BrevitasPlainResource(self._client.completions, self._session, self._router, "completions")

    def close(self) -> None:
        """Deterministically close the wrapped SDK's connection pool."""
        self._client.close()

    def __enter__(self) -> "BrevitasOpenAIClient":
        enter = getattr(self._client, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *args: Any) -> Any:
        exit_method = getattr(self._client, "__exit__", None)
        if exit_method is not None:
            return exit_method(*args)
        self.close()
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
