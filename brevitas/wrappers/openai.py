"""
Wraps openai.OpenAI so chat.completions.create() can apply content-preserving cache
optimization before forwarding to OpenAI/DeepSeek. Quality-affecting retrieval is opt-in.

Usage:
    import openai, brevitas
    client = brevitas.wrap(openai.OpenAI(api_key="sk-..."))
    response = client.chat.completions.create(model="gpt-4o", messages=[...])
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from .._compress import report_usage
from ..labels import resolve_labels
from ..receipts import canonical_provider, count_request_tokens, normalize_usage
from ..session import BrevitasSession
from token_efficiency_model.lossless.engine import optimize_request, record_usage
from token_efficiency_model.lossless.router import BrevitasRouter


class _ProviderRouterPool:
    """Keep incompatible provider/model economics out of one learned router."""
    def __init__(self, seed: BrevitasRouter | None = None) -> None:
        self._routers: dict[tuple[str, str], BrevitasRouter] = {}
        if seed is not None:
            self._routers[(seed.provider, seed.model or "")] = seed

    def get(self, provider: str, model: str) -> BrevitasRouter:
        key = (provider, model or "")
        router = self._routers.get(key)
        if router is None:
            router = BrevitasRouter(provider=provider, model=model or "")
            self._routers[key] = router
        return router


def _router_for(source: Any, provider: str, model: str) -> BrevitasRouter:
    if isinstance(source, _ProviderRouterPool):
        return source.get(provider, model)
    if isinstance(source, BrevitasRouter) and source.provider == provider:
        source.model = model or source.model
        return source
    pool = getattr(source, "_brevitas_router_pool", None)
    if pool is None:
        pool = _ProviderRouterPool(source if isinstance(source, BrevitasRouter) else None)
        try:
            setattr(source, "_brevitas_router_pool", pool)
        except Exception:
            pass
    return pool.get(provider, model)


class _BrevitasCompletions:
    def __init__(self, completions_obj: Any, session: BrevitasSession,
                 router: BrevitasRouter) -> None:
        self._orig = completions_obj
        self._session = session
        self._router = router

    def _invoke(self, method: str, *, messages: list[dict], model: str = "",
                **kwargs: Any) -> Any:
        sid = kwargs.pop("_brevitas_session", self._session.session_id)
        labels = resolve_labels(kwargs.pop("_brevitas_meta", None))
        provider = canonical_provider("openai", model)
        router = _router_for(self._router, provider, model)
        body = {"messages": deepcopy(list(messages)), "model": model, **deepcopy(kwargs)}
        baseline = count_request_tokens(body, "chat.completions")
        original = deepcopy(body)
        try:
            meta = optimize_request(body, provider, router, sid)   # in-place
        except Exception:
            body = original
            meta = {"strategy": "passthrough:optimizer_error",
                    "response_faithful": True}
        compressed = count_request_tokens(body, "chat.completions")
        strategy = meta.get("strategy", "passthrough")
        response = getattr(self._orig, method)(**body)
        if body.get("stream"):
            return _MeteredOpenAIStream(response, provider, model, baseline, compressed,
                                        self._session, router, sid, labels,
                                        "chat.completions", strategy)
        try:
            text = response.choices[0].message.content or ""
            self._session.record_response(text)
            usage = _model_dict(response.usage)
            receipt = normalize_usage(usage, provider)
            record_usage(usage, provider, router, sid)
            report_usage(provider, model, baseline, compressed, self._session,
                         pipeline=labels["pipeline"], agent=labels["agent"],
                         run_id=labels["run_id"], usage_raw=usage, strategy=strategy,
                         metadata={**labels, **receipt.as_dict(), "operation": "chat.completions",
                                   "cache_attributable": bool(meta.get("cache_attributable"))})
        except Exception:
            # A successful provider response must never fail because optional
            # metering/reporting encountered an unexpected receipt shape or outage.
            pass
        self._session.advance()
        return response

    def create(self, *, messages: list[dict], model: str = "", **kwargs: Any) -> Any:
        return self._invoke("create", messages=messages, model=model, **kwargs)

    def parse(self, *, messages: list[dict], model: str = "", **kwargs: Any) -> Any:
        return self._invoke("parse", messages=messages, model=model, **kwargs)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._orig, name)
        if name in ("with_raw_response", "with_streaming_response"):
            return _BrevitasCompletions(value, self._session, self._router)
        return value


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
        self._active = None

    def __iter__(self):
        try:
            for chunk in (self._active or self._stream):
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
        try:
            receipt = normalize_usage(self._usage, self._provider)
            if receipt.total_tokens:
                record_usage(self._usage, self._provider, self._router, self._sid)
                report_usage(self._provider, self._model, self._baseline, self._compressed,
                    self._session, pipeline=self._labels["pipeline"], agent=self._labels["agent"],
                    run_id=self._labels["run_id"], usage_raw=self._usage, strategy=self._strategy,
                    metadata={**self._labels, **receipt.as_dict(), "operation": self._operation,
                              "is_stream": True})
        except Exception:
            pass
        self._session.advance()

    def __enter__(self):
        if hasattr(self._stream, "__enter__"):
            self._active = self._stream.__enter__()
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

    def _prepare(self, model: str, input: Any, kwargs: dict) -> tuple:
        labels = resolve_labels(kwargs.pop("_brevitas_meta", None))
        sid = kwargs.pop("_brevitas_session", self._session.session_id)
        provider = canonical_provider("openai", model)
        router = _router_for(self._router, provider, model)
        body = {"model": model, "input": input, **kwargs}
        baseline = count_request_tokens(body, "responses")
        strategy = "passthrough"
        cache_attributable = False
        if isinstance(input, list) and all(isinstance(item, dict) and "role" in item for item in input):
            temporary = {"model": model, "messages": deepcopy(list(input)),
                         "_brevitas_operation": "responses"}
            try:
                meta = optimize_request(temporary, provider, router, sid)
                body["input"] = temporary["messages"]
                for cache_field in ("prompt_cache_key", "prompt_cache_options"):
                    if cache_field in temporary:
                        body[cache_field] = temporary[cache_field]
                strategy = meta.get("strategy", "passthrough")
                cache_attributable = bool(meta.get("cache_attributable"))
            except Exception:
                body["input"] = deepcopy(input)
                strategy = "passthrough:optimizer_error"
        compressed = count_request_tokens(body, "responses")
        return (body, provider, router, labels, sid, baseline, compressed,
                strategy, cache_attributable)

    def create(self, *, model: str = "", input: Any = None, **kwargs: Any) -> Any:
        (body, provider, router, labels, sid, baseline, compressed,
         strategy, cache_attributable) = self._prepare(model, input, kwargs)
        response = self._orig.create(**body)
        if body.get("stream"):
            return _MeteredOpenAIStream(response, provider, model, baseline, compressed,
                                        self._session, router, sid, labels,
                                        "responses", strategy)
        try:
            usage = _model_dict(getattr(response, "usage", {}))
            receipt = normalize_usage(usage, provider)
            if receipt.total_tokens:
                record_usage(_router_compatible_usage(receipt), provider, router, sid)
                report_usage(provider, model, baseline, compressed, self._session,
                    pipeline=labels["pipeline"], agent=labels["agent"], run_id=labels["run_id"],
                    usage_raw=usage, strategy=strategy,
                    metadata={**labels, **receipt.as_dict(), "operation": "responses",
                              "cache_attributable": cache_attributable})
        except Exception:
            pass
        self._session.advance()
        return response

    def stream(self, *, model: str = "", input: Any = None, **kwargs: Any) -> Any:
        (body, provider, router, labels, sid, baseline, compressed,
         strategy, _cache_attributable) = self._prepare(model, input, kwargs)
        manager = self._orig.stream(**body)
        return _MeteredOpenAIStream(manager, provider, model, baseline, compressed,
                                    self._session, router, sid, labels,
                                    "responses", strategy)

    def __getattr__(self, name):
        value = getattr(self._orig, name)
        if name in ("with_raw_response", "with_streaming_response"):
            return _BrevitasResponses(value, self._session, self._router)
        return value


class _BrevitasPlainResource:
    def __init__(self, resource: Any, session: BrevitasSession, router: BrevitasRouter,
                 operation: str) -> None:
        self._orig, self._session, self._router = resource, session, router
        self._operation = operation

    def create(self, *, model: str = "", **kwargs: Any) -> Any:
        labels = resolve_labels(kwargs.pop("_brevitas_meta", None))
        sid = kwargs.pop("_brevitas_session", self._session.session_id)
        provider = canonical_provider("openai", model)
        router = _router_for(self._router, provider, model)
        body = {"model": model, **kwargs}
        baseline = count_request_tokens(body, self._operation)
        response = self._orig.create(**body)
        if body.get("stream"):
            return _MeteredOpenAIStream(response, provider, model, baseline, baseline,
                                        self._session, router, sid, labels,
                                        self._operation, "passthrough")
        try:
            usage = _model_dict(getattr(response, "usage", {}))
            receipt = normalize_usage(usage, provider)
            if receipt.total_tokens:
                record_usage(_router_compatible_usage(receipt), provider, router, sid)
                report_usage(provider, model, baseline, baseline, self._session,
                    pipeline=labels["pipeline"], agent=labels["agent"], run_id=labels["run_id"],
                    usage_raw=usage, strategy="passthrough",
                    metadata={**labels, **receipt.as_dict(), "operation": self._operation})
        except Exception:
            pass
        self._session.advance()
        return response

    def __getattr__(self, name):
        return getattr(self._orig, name)


def _router_compatible_usage(receipt):
    return {"prompt_tokens": receipt.input_tokens,
            "prompt_tokens_details": {
                "cached_tokens": receipt.cached_input_tokens,
                "cache_write_tokens": receipt.cache_write_tokens,
            },
            "completion_tokens": receipt.output_tokens}


class BrevitasOpenAIClient:
    def __init__(self, client: Any, session: BrevitasSession | None = None,
                 router_pool: _ProviderRouterPool | None = None) -> None:
        self._client = client
        self._session = session or BrevitasSession()
        self._router = router_pool or _ProviderRouterPool()
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

    def with_options(self, *args: Any, **kwargs: Any):
        return BrevitasOpenAIClient(
            self._client.with_options(*args, **kwargs),
            session=self._session, router_pool=self._router,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _MeteredAsyncOpenAIStream(_MeteredOpenAIStream):
    async def __aiter__(self):
        try:
            async for chunk in (self._active or self._stream):
                usage = getattr(chunk, "usage", None)
                response = getattr(chunk, "response", None)
                usage = usage or (getattr(response, "usage", None) if response else None)
                if usage:
                    self._usage = _model_dict(usage)
                yield chunk
        finally:
            await self._finish_async()

    async def _finish_async(self):
        if self._done:
            return
        self._done = True
        try:
            receipt = normalize_usage(self._usage, self._provider)
            if receipt.total_tokens:
                record_usage(self._usage, self._provider, self._router, self._sid)
                await asyncio.to_thread(
                    report_usage, self._provider, self._model, self._baseline,
                    self._compressed, self._session, self._labels["pipeline"],
                    self._labels["agent"], self._labels["run_id"], self._usage,
                    self._strategy,
                    {**self._labels, **receipt.as_dict(), "operation": self._operation,
                     "is_stream": True},
                )
        except Exception:
            pass
        self._session.advance()

    async def __aenter__(self):
        if hasattr(self._stream, "__aenter__"):
            self._active = await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        try:
            if hasattr(self._stream, "__aexit__"):
                return await self._stream.__aexit__(*args)
            return None
        finally:
            await self._finish_async()


class _BrevitasAsyncCompletions(_BrevitasCompletions):
    async def _invoke(self, method: str, *, messages: list[dict], model: str = "",
                      **kwargs: Any) -> Any:
        sid = kwargs.pop("_brevitas_session", self._session.session_id)
        labels = resolve_labels(kwargs.pop("_brevitas_meta", None))
        provider = canonical_provider("openai", model)
        router = _router_for(self._router, provider, model)
        body = {"messages": deepcopy(list(messages)), "model": model, **deepcopy(kwargs)}
        baseline = count_request_tokens(body, "chat.completions")
        original = deepcopy(body)
        try:
            meta = optimize_request(body, provider, router, sid)
        except Exception:
            body = original
            meta = {"strategy": "passthrough:optimizer_error", "response_faithful": True}
        compressed = count_request_tokens(body, "chat.completions")
        strategy = meta.get("strategy", "passthrough")
        response = await getattr(self._orig, method)(**body)
        if body.get("stream"):
            return _MeteredAsyncOpenAIStream(
                response, provider, model, baseline, compressed, self._session,
                router, sid, labels, "chat.completions", strategy,
            )
        try:
            text = response.choices[0].message.content or ""
            self._session.record_response(text)
            usage = _model_dict(response.usage)
            receipt = normalize_usage(usage, provider)
            record_usage(usage, provider, router, sid)
            await asyncio.to_thread(
                report_usage, provider, model, baseline, compressed, self._session,
                labels["pipeline"], labels["agent"], labels["run_id"], usage,
                strategy,
                {**labels, **receipt.as_dict(), "operation": "chat.completions",
                 "cache_attributable": bool(meta.get("cache_attributable"))},
            )
        except Exception:
            pass
        self._session.advance()
        return response

    async def create(self, *, messages: list[dict], model: str = "", **kwargs: Any) -> Any:
        return await self._invoke("create", messages=messages, model=model, **kwargs)

    async def parse(self, *, messages: list[dict], model: str = "", **kwargs: Any) -> Any:
        return await self._invoke("parse", messages=messages, model=model, **kwargs)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._orig, name)
        if name in ("with_raw_response", "with_streaming_response"):
            return _BrevitasAsyncCompletions(value, self._session, self._router)
        return value


class _BrevitasAsyncChat:
    def __init__(self, chat_obj: Any, session: BrevitasSession, router: Any) -> None:
        self._orig = chat_obj
        self.completions = _BrevitasAsyncCompletions(chat_obj.completions, session, router)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


class _BrevitasAsyncResponses(_BrevitasResponses):
    async def create(self, *, model: str = "", input: Any = None, **kwargs: Any) -> Any:
        (body, provider, router, labels, sid, baseline, compressed,
         strategy, cache_attributable) = self._prepare(model, input, kwargs)
        response = await self._orig.create(**body)
        if body.get("stream"):
            return _MeteredAsyncOpenAIStream(
                response, provider, model, baseline, compressed, self._session,
                router, sid, labels, "responses", strategy,
            )
        try:
            usage = _model_dict(getattr(response, "usage", {}))
            receipt = normalize_usage(usage, provider)
            if receipt.total_tokens:
                record_usage(_router_compatible_usage(receipt), provider, router, sid)
                await asyncio.to_thread(
                    report_usage, provider, model, baseline, compressed, self._session,
                    labels["pipeline"], labels["agent"], labels["run_id"], usage,
                    strategy,
                    {**labels, **receipt.as_dict(), "operation": "responses",
                     "cache_attributable": cache_attributable},
                )
        except Exception:
            pass
        self._session.advance()
        return response

    def stream(self, *, model: str = "", input: Any = None, **kwargs: Any) -> Any:
        (body, provider, router, labels, sid, baseline, compressed,
         strategy, _cache_attributable) = self._prepare(model, input, kwargs)
        manager = self._orig.stream(**body)
        return _MeteredAsyncOpenAIStream(
            manager, provider, model, baseline, compressed, self._session,
            router, sid, labels, "responses", strategy,
        )

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._orig, name)
        if name in ("with_raw_response", "with_streaming_response"):
            return _BrevitasAsyncResponses(value, self._session, self._router)
        return value


class _BrevitasAsyncPlainResource(_BrevitasPlainResource):
    async def create(self, *, model: str = "", **kwargs: Any) -> Any:
        labels = resolve_labels(kwargs.pop("_brevitas_meta", None))
        sid = kwargs.pop("_brevitas_session", self._session.session_id)
        provider = canonical_provider("openai", model)
        router = _router_for(self._router, provider, model)
        body = {"model": model, **kwargs}
        baseline = count_request_tokens(body, self._operation)
        response = await self._orig.create(**body)
        if body.get("stream"):
            return _MeteredAsyncOpenAIStream(
                response, provider, model, baseline, baseline, self._session,
                router, sid, labels, self._operation, "passthrough",
            )
        try:
            usage = _model_dict(getattr(response, "usage", {}))
            receipt = normalize_usage(usage, provider)
            if receipt.total_tokens:
                record_usage(_router_compatible_usage(receipt), provider, router, sid)
                await asyncio.to_thread(
                    report_usage, provider, model, baseline, baseline, self._session,
                    labels["pipeline"], labels["agent"], labels["run_id"], usage,
                    "passthrough",
                    {**labels, **receipt.as_dict(), "operation": self._operation},
                )
        except Exception:
            pass
        self._session.advance()
        return response


class BrevitasAsyncOpenAIClient:
    def __init__(self, client: Any, session: BrevitasSession | None = None,
                 router_pool: _ProviderRouterPool | None = None) -> None:
        self._client = client
        self._session = session or BrevitasSession()
        self._router = router_pool or _ProviderRouterPool()
        self.chat = _BrevitasAsyncChat(client.chat, self._session, self._router)
        if hasattr(client, "responses"):
            self.responses = _BrevitasAsyncResponses(client.responses, self._session, self._router)
        if hasattr(client, "embeddings"):
            self.embeddings = _BrevitasAsyncPlainResource(
                client.embeddings, self._session, self._router, "embeddings")
        if hasattr(client, "completions"):
            self.completions = _BrevitasAsyncPlainResource(
                client.completions, self._session, self._router, "completions")

    @property
    def session(self) -> BrevitasSession:
        return self._session

    def new_session(self) -> None:
        self.__init__(self._client, BrevitasSession(), self._router)

    def with_options(self, *args: Any, **kwargs: Any):
        return BrevitasAsyncOpenAIClient(
            self._client.with_options(*args, **kwargs),
            session=self._session, router_pool=self._router,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
