"""Anthropic system compatibility without changing mid-conversation semantics."""
from __future__ import annotations

from token_efficiency_model.lossless.dropin import BrevitasDropIn


class _FakeMessages:
    def __init__(self, sink): self.sink = sink
    def create(self, **body):
        self.sink.update(body)
        class U:  # minimal anthropic-style usage
            input_tokens = 10; output_tokens = 5
            cache_creation_input_tokens = 0; cache_read_input_tokens = 0
        class R:
            usage = U(); content = [type("B", (), {"type": "text", "text": "ok"})()]
        return R()


class _FakeAnthropic:
    def __init__(self): self.captured = {}; self.messages = _FakeMessages(self.captured)
    __provider__ = "anthropic"


def test_system_role_message_hoisted_to_top_level_system(monkeypatch):
    client = BrevitasDropIn(provider="anthropic", api_key="x")
    fake = _FakeAnthropic()
    monkeypatch.setattr(client, "_route_client", lambda p: fake)
    client.chat(
        messages=[{"role": "system", "content": "You are Warren Buffett."},
                  {"role": "user", "content": "Verdict on NPO?"}],
        model="claude-haiku-4-5-20251001", max_tokens=50)
    body = fake.captured
    # system must be a top-level field; NO system-role message left in messages
    assert "You are Warren Buffett." in (body.get("system") or "")
    assert all(m.get("role") != "system" for m in body["messages"])
    assert body["messages"][-1]["content"] == "Verdict on NPO?"


def test_system_kwarg_and_system_message_merge(monkeypatch):
    client = BrevitasDropIn(provider="anthropic", api_key="x")
    fake = _FakeAnthropic()
    monkeypatch.setattr(client, "_route_client", lambda p: fake)
    client.chat(
        messages=[{"role": "system", "content": "Role A."},
                  {"role": "user", "content": "hi"}],
        model="claude-haiku-4-5-20251001", system="Global policy.", max_tokens=50)
    sysv = fake.captured.get("system") or ""
    assert "Role A." in sysv and "Global policy." in sysv


def test_structured_top_level_system_is_preserved_when_merging(monkeypatch):
    client = BrevitasDropIn(provider="anthropic", api_key="x")
    fake = _FakeAnthropic()
    monkeypatch.setattr(client, "_route_client", lambda p: fake)
    structured = [{"type": "text", "text": "Global policy."}]
    client.chat(
        messages=[{"role": "system", "content": "Role A."},
                  {"role": "user", "content": "hi"}],
        model="claude-haiku-4-5-20251001", system=structured, max_tokens=50)
    assert fake.captured["system"][0] == {"type": "text", "text": "Role A."}
    assert fake.captured["system"][1:] == structured


def test_mid_conversation_system_message_is_not_hoisted(monkeypatch):
    client = BrevitasDropIn(provider="anthropic", api_key="x")
    fake = _FakeAnthropic()
    monkeypatch.setattr(client, "_route_client", lambda p: fake)
    messages = [
        {"role": "user", "content": "Draft an answer."},
        {"role": "system", "content": "Now use the reviewer policy."},
        {"role": "assistant", "content": "Reviewed answer."},
        {"role": "user", "content": "Continue."},
    ]
    client.chat(messages=messages, model="claude-opus-4-8", max_tokens=50)
    assert fake.captured["messages"] == messages
    assert "system" not in fake.captured


def test_directive_only_system_message_is_preserved(monkeypatch):
    client = BrevitasDropIn(provider="anthropic", api_key="x")
    fake = _FakeAnthropic()
    monkeypatch.setattr(client, "_route_client", lambda p: fake)
    directive = {"role": "system", "content": [],
                 "output_config": {"effort": "high"}}
    messages = [directive, {"role": "user", "content": "Continue."}]
    client.chat(messages=messages, model="claude-opus-4-8", max_tokens=50)
    assert fake.captured["messages"] == messages
    assert "system" not in fake.captured
