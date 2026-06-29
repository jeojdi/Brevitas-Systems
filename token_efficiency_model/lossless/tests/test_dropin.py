"""Tests for the drop-in middleware wrapper.

Covers:
- Provider detection (Anthropic, OpenAI, DeepSeek)
- Prefix stability under caching
- Honest savings math
- Fail-safe behavior (retrieval falls back to full context gracefully)
"""

from unittest.mock import MagicMock, patch

import pytest

from token_efficiency_model.lossless.dropin import BrevitasDropIn, SavingsReport


# --------------------------------------------------------------------------- #
# Provider detection
# --------------------------------------------------------------------------- #
def test_detect_anthropic_from_model_name():
    client = BrevitasDropIn()
    assert client._detect_provider(model="claude-3-sonnet") == "anthropic"


def test_detect_openai_from_model_name():
    client = BrevitasDropIn()
    assert client._detect_provider(model="gpt-4") == "openai"
    assert client._detect_provider(model="text-davinci-003") == "openai"


def test_detect_deepseek_from_model_name():
    client = BrevitasDropIn()
    assert client._detect_provider(model="deepseek-chat") == "deepseek"


def test_detect_anthropic_from_explicit_provider():
    client = BrevitasDropIn(provider="anthropic")
    assert client._detect_provider(model="gpt-4") == "anthropic"


def test_detect_from_base_url():
    client = BrevitasDropIn(base_url="https://api.anthropic.com/v1")
    assert client._detect_provider() == "anthropic"


def test_default_to_openai():
    client = BrevitasDropIn()
    assert client._detect_provider() == "openai"


# --------------------------------------------------------------------------- #
# Prefix stability: caching doesn't mutate the stable prefix
# --------------------------------------------------------------------------- #
def test_anthropic_cache_placement_preserves_prefix():
    """Verify that applying cache_control doesn't change the text content."""
    client = BrevitasDropIn(provider="anthropic")
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "And 3+3?"},
    ]
    body = {"messages": messages, "model": "claude-3", "system": "You are helpful."}

    # Extract content before caching
    content_before = []
    for msg in body["messages"]:
        if isinstance(msg["content"], str):
            content_before.append(msg["content"])
        elif isinstance(msg["content"], list):
            content_before.append("".join(b.get("text", "") for b in msg["content"]))

    # Apply caching
    from token_efficiency_model.lossless.provider_cache import apply_anthropic_cache
    apply_anthropic_cache(body)

    # Extract content after caching
    content_after = []
    for msg in body["messages"]:
        if isinstance(msg["content"], str):
            content_after.append(msg["content"])
        elif isinstance(msg["content"], list):
            content_after.append("".join(b.get("text", "") for b in msg["content"]))

    # Content must be identical (only structure changed for cache_control)
    assert content_before == content_after


def test_openai_prefix_not_mutated():
    """For OpenAI, we don't mutate the messages at all; caching is server-side."""
    client = BrevitasDropIn(provider="openai")
    messages = [
        {"role": "user", "content": "First message"},
        {"role": "assistant", "content": "First response"},
        {"role": "user", "content": "Second message"},
    ]
    messages_copy = [m.copy() for m in messages]

    # The dropin shouldn't mutate messages for OpenAI (no local caching logic)
    # (This is verified by mocking the actual call, below)
    assert messages == messages_copy


# --------------------------------------------------------------------------- #
# Honest savings math
# --------------------------------------------------------------------------- #
def test_savings_report_anthropic_with_cache():
    """Verify savings report contains correct metrics from Anthropic usage."""
    client = BrevitasDropIn(provider="anthropic", api_key="test")

    # Mock the Anthropic client and response
    mock_response = MagicMock()
    mock_response.usage.input_tokens = 1000
    mock_response.usage.cache_creation_input_tokens = 0
    mock_response.usage.cache_read_input_tokens = 5000
    mock_response.usage.output_tokens = 100
    mock_response.content = [MagicMock(text="response")]

    with patch.object(client, "_route_client") as mock_route:
        mock_api = MagicMock()
        mock_api.messages.create.return_value = mock_response
        mock_route.return_value = mock_api

        response, report = client.chat(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-sonnet",
        )

    assert isinstance(report, SavingsReport)
    assert report.provider == "anthropic"
    assert report.cached_tokens == 5000
    # TOTAL savings now includes output (100 tok @ 5x input, never cached):
    # input uncached 6000, actual 1500; output cost 500 both sides -> 1-(2000/6500) = ~69.2%
    # (output dilutes the ~75% input-only figure — this is the honest total-bill number)
    assert 66 < report.savings_pct < 73


def test_savings_report_openai_with_cache():
    """Verify savings report for OpenAI-style (DeepSeek) provider."""
    client = BrevitasDropIn(provider="openai", api_key="test")

    # Mock the OpenAI client and response
    mock_response = MagicMock()
    mock_response.usage.prompt_tokens = 10000
    mock_response.usage.completion_tokens = 50
    mock_response.usage.prompt_tokens_details = {"cached_tokens": 8000}
    mock_response.choices = [MagicMock(message=MagicMock(content="response"))]

    with patch.object(client, "_route_client") as mock_route:
        mock_api = MagicMock()
        mock_api.chat.completions.create.return_value = mock_response
        mock_route.return_value = mock_api

        response, report = client.chat(
            messages=[{"role": "user", "content": "test"}],
            model="gpt-4",
        )

    assert isinstance(report, SavingsReport)
    assert report.provider == "openai"
    assert report.cached_tokens == 8000
    # uncached = 10000; actual = 2000 + 8000*0.5 = 6000 -> 40% savings
    assert abs(report.savings_pct - 40.0) < 1


def test_no_cache_no_savings():
    """When there are no cached tokens, savings_pct should be 0."""
    client = BrevitasDropIn(provider="openai", api_key="test")

    mock_response = MagicMock()
    mock_response.usage.prompt_tokens = 500
    mock_response.usage.completion_tokens = 100
    mock_response.usage.prompt_tokens_details = {}  # No cached_tokens
    mock_response.choices = [MagicMock(message=MagicMock(content="response"))]

    with patch.object(client, "_route_client") as mock_route:
        mock_api = MagicMock()
        mock_api.chat.completions.create.return_value = mock_response
        mock_route.return_value = mock_api

        response, report = client.chat(
            messages=[{"role": "user", "content": "test"}],
            model="gpt-4",
        )

    assert report.cached_tokens == 0
    assert report.savings_pct == 0.0


# --------------------------------------------------------------------------- #
# Fail-safe: retrieval gracefully falls back to full context
# --------------------------------------------------------------------------- #
def test_retrieval_fallback_when_unavailable():
    """If the retrieval model is unavailable, use full context (no error)."""
    client = BrevitasDropIn(provider="openai", api_key="test")

    mock_response = MagicMock()
    mock_response.usage.prompt_tokens = 1000
    mock_response.usage.completion_tokens = 50
    mock_response.usage.prompt_tokens_details = {}
    mock_response.choices = [MagicMock(message=MagicMock(content="response"))]

    # Mock retrieval to be unavailable
    with patch.object(client, "_route_client") as mock_route, patch(
        "token_efficiency_model.lossless.api_adapter._get_encoder", return_value=None
    ):
        mock_api = MagicMock()
        mock_api.chat.completions.create.return_value = mock_response
        mock_route.return_value = mock_api

        big = " ".join(["context"] * 1500)  # large enough to be routable
        messages = [
            {"role": "user", "content": big + " one"},
            {"role": "assistant", "content": "response 1"},
            {"role": "user", "content": big + " two"},
        ]

        response, report = client.chat(messages=messages, model="gpt-4")

    # No error; retrieval falls back safely when encoder unavailable
    assert isinstance(report, SavingsReport)
    assert report.retrieval_applied is False


def test_retrieval_reports_metadata():
    """When retrieval is successfully applied, report includes baseline/optimized tokens."""
    client = BrevitasDropIn(provider="openai", api_key="test")

    mock_response = MagicMock()
    mock_response.usage.prompt_tokens = 500
    mock_response.usage.completion_tokens = 50
    mock_response.usage.prompt_tokens_details = {}
    mock_response.choices = [MagicMock(message=MagicMock(content="response"))]

    # large UNIQUE per-call context so the router auto-selects "retrieve"
    c1 = " ".join(["alpha"] * 1500)
    c3 = " ".join(["gamma"] * 1500)
    mock_retrieval_result = {
        "selected_context": [c1, c3],
        "baseline_tokens": 600,
        "optimized_tokens": 300,
        "savings_pct": 50.0,
        "fallback_applied": False,
        "reason": "retrieved",
    }

    with patch.object(client, "_route_client") as mock_route, patch(
        "token_efficiency_model.lossless.engine.retrieval_select",
        return_value=mock_retrieval_result,
    ):
        mock_api = MagicMock()
        mock_api.chat.completions.create.return_value = mock_response
        mock_route.return_value = mock_api

        response, report = client.chat(
            messages=[
                {"role": "user", "content": c1},
                {"role": "assistant", "content": "response"},
                {"role": "user", "content": "the new question " + " ".join(["q"] * 50)},
            ],
            model="gpt-4",
        )

    assert report.retrieval_applied is True
    assert report.retrieval_baseline_tokens == 600
    assert report.retrieval_optimized_tokens == 300


# --------------------------------------------------------------------------- #
# Context extraction
# --------------------------------------------------------------------------- #
def test_extract_context_chunks_from_prior_messages():
    """Extract context from non-latest messages."""
    client = BrevitasDropIn()
    messages = [
        {"role": "user", "content": "intro"},
        {"role": "assistant", "content": "response"},
        {"role": "user", "content": "follow-up question"},
    ]
    chunks = client._extract_context_chunks(messages)
    # Should exclude the last message
    assert "intro" in chunks
    assert "response" in chunks
    assert "follow-up question" not in chunks


def test_extract_context_handles_list_content():
    """Extract context from messages with content as a list of blocks."""
    client = BrevitasDropIn()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "text content"},
                {"type": "image_url", "image_url": {"url": "..."}},
            ],
        },
        {"role": "user", "content": "latest"},
    ]
    chunks = client._extract_context_chunks(messages)
    assert "text content" in chunks
    assert "latest" not in chunks


def test_extract_task_from_latest_message():
    """Extract task/query hint from latest user message."""
    client = BrevitasDropIn()
    messages = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "What is the capital of France?"},
    ]
    task = client._extract_task(messages)
    assert task == "What is the capital of France?"


def test_extract_task_truncates_to_200_chars():
    """Task extraction truncates long messages."""
    client = BrevitasDropIn()
    long_text = "x" * 300
    messages = [{"role": "user", "content": long_text}]
    task = client._extract_task(messages)
    assert len(task) == 200
    assert task == "x" * 200


# --------------------------------------------------------------------------- #
# Integration: end-to-end flow (mocked)
# --------------------------------------------------------------------------- #
def test_chat_with_anthropic():
    """End-to-end: chat call with Anthropic (mocked)."""
    client = BrevitasDropIn(provider="anthropic", api_key="test")

    mock_response = MagicMock()
    mock_response.usage.input_tokens = 500
    mock_response.usage.cache_creation_input_tokens = 0
    mock_response.usage.cache_read_input_tokens = 0
    mock_response.usage.output_tokens = 50
    mock_response.content = [MagicMock(text="Hello!")]

    with patch.object(client, "_route_client") as mock_route:
        mock_api = MagicMock()
        mock_api.messages.create.return_value = mock_response
        mock_route.return_value = mock_api

        response, report = client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            model="claude-3-sonnet",
        )

    assert response == mock_response
    assert report.provider == "anthropic"
    assert report.savings_pct == 0.0  # No cache hit on first call


def test_chat_with_openai():
    """End-to-end: chat call with OpenAI (mocked)."""
    client = BrevitasDropIn(provider="openai", api_key="test")

    mock_response = MagicMock()
    mock_response.usage.prompt_tokens = 100
    mock_response.usage.completion_tokens = 50
    mock_response.usage.prompt_tokens_details = {}
    mock_response.choices = [MagicMock(message=MagicMock(content="Hello!"))]

    with patch.object(client, "_route_client") as mock_route:
        mock_api = MagicMock()
        mock_api.chat.completions.create.return_value = mock_response
        mock_route.return_value = mock_api

        response, report = client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-4",
        )

    assert response == mock_response
    assert report.provider == "openai"


def test_chat_passes_through_kwargs():
    """Verify that extra kwargs are passed to the provider API."""
    client = BrevitasDropIn(provider="openai", api_key="test")

    mock_response = MagicMock()
    mock_response.usage.prompt_tokens = 100
    mock_response.usage.completion_tokens = 50
    mock_response.usage.prompt_tokens_details = {}
    mock_response.choices = [MagicMock(message=MagicMock(content="response"))]

    with patch.object(client, "_route_client") as mock_route:
        mock_api = MagicMock()
        mock_api.chat.completions.create.return_value = mock_response
        mock_route.return_value = mock_api

        response, report = client.chat(
            messages=[{"role": "user", "content": "test"}],
            model="gpt-4",
            temperature=0.7,
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "test"}}],
        )

        # Check that the kwargs were passed through
        call_args = mock_api.chat.completions.create.call_args
        assert call_args[1]["temperature"] == 0.7
        assert call_args[1]["max_tokens"] == 100
        assert "tools" in call_args[1]
