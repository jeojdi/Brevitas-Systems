from types import SimpleNamespace


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
