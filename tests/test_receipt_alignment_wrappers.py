from types import SimpleNamespace
from copy import deepcopy
import asyncio


def test_openai_wrapper_reports_local_delta_and_actual_strategy(monkeypatch):
    import brevitas.wrappers.openai as wrapper
    from brevitas.session import BrevitasSession
    from token_efficiency_model.lossless.router import BrevitasRouter

    captured = {}

    def optimize(body, *_args, **_kwargs):
        body["messages"] = body["messages"][-1:]
        return {"strategy": "retrieve"}

    monkeypatch.setattr(wrapper, "optimize_request", optimize)
    monkeypatch.setattr(wrapper, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrapper, "report_usage", lambda *args, **kwargs: captured.update(
        args=args, kwargs=kwargs))

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=1_000, completion_tokens=10),
    )
    client = SimpleNamespace(create=lambda **_kwargs: response)
    messages = [
        {"role": "system", "content": "stable context " * 100},
        {"role": "user", "content": "question"},
    ]
    wrapped = wrapper._BrevitasCompletions(
        client, BrevitasSession(), BrevitasRouter(provider="openai"))
    wrapped.create(messages=messages, model="gpt-4o-mini")

    baseline, compressed = captured["args"][2:4]
    assert baseline > compressed
    assert compressed != 1_000  # provider receipt is an anchor, not the local delta
    assert captured["kwargs"]["strategy"] == "retrieve"


def test_anthropic_wrapper_preserves_cache_tiers_and_attribution(monkeypatch):
    import brevitas.wrappers.anthropic as wrapper
    from brevitas.session import BrevitasSession
    from token_efficiency_model.lossless.router import BrevitasRouter

    captured = {}
    monkeypatch.setattr(wrapper, "optimize_request", lambda *_args, **_kwargs: {
        "strategy": "cache_only", "cache_control_owner": "brevitas",
    })
    monkeypatch.setattr(wrapper, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrapper, "report_usage", lambda *args, **kwargs: captured.update(
        args=args, kwargs=kwargs))

    usage = SimpleNamespace(model_dump=lambda: {
        "input_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 50,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 10,
            "ephemeral_1h_input_tokens": 40,
        },
        "output_tokens": 5,
    })
    response = SimpleNamespace(
        content=[SimpleNamespace(text="ok")], usage=usage,
    )
    client = SimpleNamespace(create=lambda **_kwargs: response)
    wrapped = wrapper._BrevitasMessages(
        client, BrevitasSession(), BrevitasRouter(provider="anthropic"))
    wrapped.create(messages=[{"role": "user", "content": "question"}],
                   model="claude-sonnet-4-6")

    metadata = captured["kwargs"]["metadata"]
    assert captured["args"][2] == captured["args"][3]
    assert captured["kwargs"]["strategy"] == "cache_only"
    assert metadata["cache_write_5m_tokens"] == 10
    assert metadata["cache_write_1h_tokens"] == 40
    assert metadata["cache_attributable"] is True


def test_anthropic_wrapper_never_mutates_caller_messages(monkeypatch):
    import brevitas.wrappers.anthropic as wrapper
    from brevitas.session import BrevitasSession
    from token_efficiency_model.lossless.router import BrevitasRouter

    seen = {}
    def optimize(body, *_args, **_kwargs):
        body["messages"][0]["content"] = [{
            "type": "text", "text": "stable",
            "cache_control": {"type": "ephemeral"},
        }]
        return {"strategy": "cache_only"}

    monkeypatch.setattr(wrapper, "optimize_request", optimize)
    monkeypatch.setattr(wrapper, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrapper, "report_usage", lambda *_args, **_kwargs: None)
    response = SimpleNamespace(
        content=[SimpleNamespace(text="ok")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client = SimpleNamespace(create=lambda **kwargs: seen.update(kwargs) or response)
    messages = [{"role": "user", "content": "stable"}]
    before = deepcopy(messages)
    wrapper._BrevitasMessages(
        client, BrevitasSession(), BrevitasRouter(provider="anthropic")
    ).create(messages=messages, model="claude-sonnet-4-6")

    assert messages == before
    assert seen["messages"] != messages


def test_openai_async_client_and_helpers_stay_wrapped(monkeypatch):
    import brevitas
    import brevitas.wrappers.openai as wrapper

    calls = []
    monkeypatch.setattr(wrapper, "report_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrapper, "record_usage", lambda *_args, **_kwargs: None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )

    class AsyncCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return response

    completions = AsyncCompletions()
    completions.with_raw_response = AsyncCompletions()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        with_options=lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=AsyncCompletions())),
    )
    wrapped = brevitas.wrap(client)
    result = asyncio.run(wrapped.chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hello"}],
        stream=True, stream_options={"include_usage": False},
    ))
    assert isinstance(result, wrapper._MeteredAsyncOpenAIStream)
    assert calls[0]["stream_options"] == {"include_usage": False}
    assert isinstance(wrapped.chat.completions.with_raw_response,
                      wrapper._BrevitasAsyncCompletions)
    assert isinstance(wrapped.with_options(timeout=1), wrapper.BrevitasAsyncOpenAIClient)


def test_openai_wrapper_uses_separate_provider_routers(monkeypatch):
    import brevitas.wrappers.openai as wrapper

    monkeypatch.setattr(wrapper, "report_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrapper, "record_usage", lambda *_args, **_kwargs: None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )
    resource = SimpleNamespace(create=lambda **_kwargs: response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=resource))
    wrapped = wrapper.BrevitasOpenAIClient(client)
    wrapped.chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "one"}])
    wrapped.chat.completions.create(
        model="deepseek-chat", messages=[{"role": "user", "content": "two"}])
    assert {provider for provider, _model in wrapped._router._routers} == {
        "openai", "deepseek"}


def test_sync_wrappers_fail_open_when_usage_reporting_fails(monkeypatch):
    import brevitas.wrappers.anthropic as anthropic_wrapper
    import brevitas.wrappers.openai as openai_wrapper
    from brevitas.session import BrevitasSession
    from token_efficiency_model.lossless.router import BrevitasRouter

    def reporting_failure(*_args, **_kwargs):
        raise RuntimeError("meter unavailable")

    monkeypatch.setattr(openai_wrapper, "report_usage", reporting_failure)
    monkeypatch.setattr(openai_wrapper, "record_usage", lambda *_args, **_kwargs: None)
    openai_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )
    openai_resource = SimpleNamespace(create=lambda **_kwargs: openai_response)
    wrapped_openai = openai_wrapper._BrevitasCompletions(
        openai_resource, BrevitasSession(), BrevitasRouter(provider="openai"))
    assert wrapped_openai.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hello"}]
    ) is openai_response

    monkeypatch.setattr(anthropic_wrapper, "report_usage", reporting_failure)
    monkeypatch.setattr(anthropic_wrapper, "record_usage", lambda *_args, **_kwargs: None)
    anthropic_response = SimpleNamespace(
        content=[SimpleNamespace(text="ok")],
        usage=SimpleNamespace(input_tokens=2, output_tokens=1),
    )
    anthropic_resource = SimpleNamespace(create=lambda **_kwargs: anthropic_response)
    wrapped_anthropic = anthropic_wrapper._BrevitasMessages(
        anthropic_resource, BrevitasSession(), BrevitasRouter(provider="anthropic"))
    assert wrapped_anthropic.create(
        model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hello"}]
    ) is anthropic_response


def test_async_openai_wrapper_fails_open_when_usage_reporting_fails(monkeypatch):
    import brevitas.wrappers.openai as wrapper
    from brevitas.session import BrevitasSession
    from token_efficiency_model.lossless.router import BrevitasRouter

    def reporting_failure(*_args, **_kwargs):
        raise RuntimeError("meter unavailable")

    monkeypatch.setattr(wrapper, "report_usage", reporting_failure)
    monkeypatch.setattr(wrapper, "record_usage", lambda *_args, **_kwargs: None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )

    class AsyncResource:
        async def create(self, **_kwargs):
            return response

    wrapped = wrapper._BrevitasAsyncCompletions(
        AsyncResource(), BrevitasSession(), BrevitasRouter(provider="openai"))
    result = asyncio.run(wrapped.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hello"}]))
    assert result is response


def test_openai_streams_fail_open_when_usage_reporting_fails(monkeypatch):
    import brevitas.wrappers.openai as wrapper
    from brevitas.session import BrevitasSession
    from token_efficiency_model.lossless.router import BrevitasRouter

    def reporting_failure(*_args, **_kwargs):
        raise RuntimeError("meter unavailable")

    monkeypatch.setattr(wrapper, "report_usage", reporting_failure)
    monkeypatch.setattr(wrapper, "record_usage", lambda *_args, **_kwargs: None)
    labels = {"pipeline": "", "agent": "", "run_id": ""}
    usage = SimpleNamespace(prompt_tokens=2, completion_tokens=1)
    chunks = [SimpleNamespace(usage=usage)]
    stream = wrapper._MeteredOpenAIStream(
        chunks, "openai", "gpt-4o-mini", 2, 2, BrevitasSession(),
        BrevitasRouter(provider="openai"), "sync", labels,
        "chat.completions", "passthrough",
    )
    assert list(stream) == chunks

    class AsyncChunks:
        def __aiter__(self):
            async def generate():
                yield SimpleNamespace(usage=usage)
            return generate()

    async_stream = wrapper._MeteredAsyncOpenAIStream(
        AsyncChunks(), "openai", "gpt-4o-mini", 2, 2, BrevitasSession(),
        BrevitasRouter(provider="openai"), "async", labels,
        "chat.completions", "passthrough",
    )

    async def consume():
        return [chunk async for chunk in async_stream]

    async_chunks = asyncio.run(consume())
    assert len(async_chunks) == 1
