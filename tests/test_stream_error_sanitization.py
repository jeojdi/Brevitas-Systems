import json

from fastapi import HTTPException

from tests.test_backend_contract_repairs import _server_client


class _RecordingLogger:
    def __init__(self):
        self.errors = []

    def error(self, *args):
        self.errors.append(args)


def _events(response):
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_compress_stream_hides_internal_exception_details(tmp_path, monkeypatch):
    server, _, raw_key, client = _server_client(tmp_path, monkeypatch)
    secret = "sk-secret-internal-123"
    private_url = "https://user:password@internal.example/private"
    provider_message = "provider said account acct_sensitive is disabled"
    logger = _RecordingLogger()
    monkeypatch.setattr(server, "logger", logger)
    monkeypatch.setattr(
        server,
        "_compress_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(f"{secret} {private_url} {provider_message}")),
    )

    response = client.post(
        "/v1/compress/stream",
        headers={"X-Brevitas-Key": raw_key},
        json={"messages": ["hello"], "prior_context": [], "lossy": False},
    )
    events = _events(response)

    assert response.status_code == 200
    assert events[-1] == {
        "stage": "error",
        "code": "compression_stream_failed",
        "message": "Compression stream failed",
    }
    serialized = response.text + repr(logger.errors)
    for sensitive in (secret, private_url, provider_message, "acct_sensitive"):
        assert sensitive not in serialized
    assert logger.errors
    assert "RuntimeError" in repr(logger.errors)


def test_playground_provider_error_has_stable_safe_sse(tmp_path, monkeypatch):
    server, _, raw_key, client = _server_client(tmp_path, monkeypatch)
    byok_secret = "sk-byok-never-echo-456"
    upstream_detail = "https://api.provider.invalid?token=leaked provider body secret"
    logger = _RecordingLogger()
    monkeypatch.setattr(server, "logger", logger)
    monkeypatch.setattr(server, "_get_playground_cache", lambda *_args: None)

    def failed_backend(_prompt, _model):
        raise HTTPException(status_code=502, detail=upstream_detail)

    monkeypatch.setattr(
        server,
        "_build_chat_backend",
        lambda *_args, **_kwargs: ("openai", "gpt-4o-mini", failed_backend),
    )

    response = client.post(
        "/v1/playground/stream",
        headers={"X-Brevitas-Key": raw_key},
        json={
            "messages": ["hello"],
            "prior_context": [],
            "lossy": False,
            "byok_provider": "openai",
            "byok_model": "gpt-4o-mini",
            "byok_key": byok_secret,
        },
    )
    events = _events(response)

    assert response.status_code == 200
    assert events[-1] == {
        "stage": "error",
        "code": "provider_stream_failed",
        "message": "Model provider stream failed",
    }
    serialized = response.text + repr(logger.errors)
    assert byok_secret not in serialized
    assert upstream_detail not in serialized
    assert "token=leaked" not in serialized
    assert "HTTPException" in repr(logger.errors)


def test_stream_cancellation_guards_remain_before_error_delivery():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "api/server.py").read_text()
    assert source.count("except _ClientGone:\n            pass") == 2
    assert source.count(
        "except Exception as exc:\n            if cancel_event.is_set():\n                return"
    ) == 2
    assert source.count("cancel_event.set()") >= 2
    assert '"message": str(exc)' not in source
