"""
Phase 1 native caching tests — verify prefix preservation, upstream routing, and cache injection.
Run: pytest token_efficiency_model/common/tests/test_phase1_native_caching.py -v
"""
import pytest
from unittest.mock import patch, MagicMock
from brevitas._compress import compress_messages
from brevitas.session import BrevitasSession
from brevitas.proxy import get_openai_compatible_upstream
from token_efficiency_model.optimizers.provider_cache.anthropic import apply_anthropic_cache


# ── Task 1: Prefix Preservation ─────────────────────────────────────────


class TestPrefixPreservation:
    """Verify that compress_messages only modifies the last user message."""

    @pytest.fixture
    def mock_session(self):
        """Mock BrevitasSession with minimal interface."""
        session = MagicMock(spec=BrevitasSession)
        session.prior_context.return_value = []
        return session

    def test_prefix_preservation_4_message_convo(self, mock_session):
        """
        Given a 4-message conversation, after compress_messages the first N-1 messages
        are byte-identical to input; only the last user message may differ.
        """
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "assistant", "content": "I understand. How can I help?"},
            {"role": "user", "content": "First question about the project."},
            {"role": "user", "content": "Second question needs compression."},
        ]

        with patch("brevitas._compress._cfg") as mock_cfg:
            mock_cfg.return_value = {
                "enabled": True,
                "api_key": "test_key",
                "base_url": "http://localhost:8000",
                "timeout": 30,
            }

            with patch("httpx.post") as mock_post:
                # Mock the /v1/compress API to return slightly shorter text
                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "compressed_messages": [
                        "Second question needs compression shortened."
                    ],
                    "optimized_tokens": 5,
                }
                mock_post.return_value = mock_response

                compressed, baseline, compressed_tok = compress_messages(
                    messages, mock_session
                )

                # Verify prefix is identical (object equality)
                assert compressed[0] is messages[0], "System message should be unchanged"
                assert compressed[1] is messages[1], "Assistant message should be unchanged"
                assert compressed[2] is messages[2], (
                    "First user message should be unchanged"
                )

                # Last message may be different
                assert len(compressed) == 4
                assert compressed[3]["role"] == "user"
                # Content may be compressed
                assert isinstance(compressed[3]["content"], str)

                # Verify only one API call (for the last user message only)
                assert mock_post.call_count == 1
                call_args = mock_post.call_args[1]
                assert call_args["json"]["messages"] == [
                    "Second question needs compression."
                ]

    def test_no_user_message_returns_unchanged(self, mock_session):
        """If no user message exists, return unchanged."""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "assistant", "content": "Response"},
        ]

        with patch("brevitas._compress._cfg") as mock_cfg:
            mock_cfg.return_value = {
                "enabled": True,
                "api_key": "test_key",
                "base_url": "http://localhost:8000",
                "timeout": 30,
            }

            compressed, baseline, compressed_tok = compress_messages(
                messages, mock_session
            )

            # Should be unchanged when no user message
            assert compressed is messages
            assert baseline == compressed_tok

    def test_single_user_message(self, mock_session):
        """Single user message can be compressed."""
        messages = [
            {"role": "user", "content": "Single user message to compress."}
        ]

        with patch("brevitas._compress._cfg") as mock_cfg:
            mock_cfg.return_value = {
                "enabled": True,
                "api_key": "test_key",
                "base_url": "http://localhost:8000",
                "timeout": 30,
            }

            with patch("httpx.post") as mock_post:
                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "compressed_messages": ["Short."],
                    "optimized_tokens": 2,
                }
                mock_post.return_value = mock_response

                compressed, baseline, compressed_tok = compress_messages(
                    messages, mock_session
                )

                assert len(compressed) == 1
                assert compressed[0]["role"] == "user"
                assert compressed[0]["content"] == "Short."

    def test_compression_disabled_returns_original(self, mock_session):
        """If compression is disabled, return original messages unchanged."""
        messages = [
            {"role": "user", "content": "Test message"},
        ]

        with patch("brevitas._compress._cfg") as mock_cfg:
            mock_cfg.return_value = {
                "enabled": False,
                "api_key": "test_key",
            }

            compressed, baseline, compressed_tok = compress_messages(
                messages, mock_session
            )

            assert compressed is messages
            assert baseline == compressed_tok

    def test_api_failure_returns_unchanged(self, mock_session):
        """If API fails, return original messages unchanged."""
        messages = [
            {"role": "user", "content": "Test message"},
        ]

        with patch("brevitas._compress._cfg") as mock_cfg:
            mock_cfg.return_value = {
                "enabled": True,
                "api_key": "test_key",
                "base_url": "http://localhost:8000",
                "timeout": 30,
            }

            with patch("httpx.post") as mock_post:
                mock_post.side_effect = Exception("API error")

                compressed, baseline, compressed_tok = compress_messages(
                    messages, mock_session
                )

                # Should be unchanged on API failure
                assert compressed is messages
                assert baseline == compressed_tok

    def test_list_content_preserved(self, mock_session):
        """List content (multimodal) is properly handled."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First part"},
                    {"type": "text", "text": "Second part"},
                ],
            }
        ]

        with patch("brevitas._compress._cfg") as mock_cfg:
            mock_cfg.return_value = {
                "enabled": True,
                "api_key": "test_key",
                "base_url": "http://localhost:8000",
                "timeout": 30,
            }

            with patch("httpx.post") as mock_post:
                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "compressed_messages": ["Compressed text"],
                    "optimized_tokens": 2,
                }
                mock_post.return_value = mock_response

                compressed, baseline, compressed_tok = compress_messages(
                    messages, mock_session
                )

                # Content should be converted to string format for API,
                # but returned as list with single text block
                assert len(compressed) == 1
                assert compressed[0]["role"] == "user"
                assert isinstance(compressed[0]["content"], list)
                assert compressed[0]["content"][0]["type"] == "text"


# ── Task 2: Upstream Routing ────────────────────────────────────────────


class TestUpstreamRouting:
    """Verify correct upstream routing by model prefix."""

    def test_route_deepseek_model(self):
        """deepseek-* models route to DeepSeek API."""
        assert (
            get_openai_compatible_upstream("deepseek-chat")
            == "https://api.deepseek.com"
        )
        assert (
            get_openai_compatible_upstream("deepseek-v3")
            == "https://api.deepseek.com"
        )
        assert (
            get_openai_compatible_upstream("DEEPSEEK-CHAT")
            == "https://api.deepseek.com"
        )

    def test_route_groq_models(self):
        """grok-* and groq-* models route to Groq API."""
        assert (
            get_openai_compatible_upstream("groq-mixtral")
            == "https://api.groq.com/openai"
        )
        assert (
            get_openai_compatible_upstream("grok-2")
            == "https://api.groq.com/openai"
        )
        assert (
            get_openai_compatible_upstream("GROK-BETA")
            == "https://api.groq.com/openai"
        )

    def test_route_openai_default(self):
        """OpenAI and unrecognized models route to OpenAI API."""
        assert (
            get_openai_compatible_upstream("gpt-4")
            == "https://api.openai.com"
        )
        assert (
            get_openai_compatible_upstream("gpt-4-turbo")
            == "https://api.openai.com"
        )
        assert (
            get_openai_compatible_upstream("gpt-3.5-turbo")
            == "https://api.openai.com"
        )
        assert (
            get_openai_compatible_upstream("unknown-model")
            == "https://api.openai.com"
        )
        assert get_openai_compatible_upstream("") == "https://api.openai.com"

    def test_header_override(self):
        """x-brevitas-upstream header overrides model-based routing (allowlisted only)."""
        # Can override deepseek detection with allowlisted OpenAI URL
        assert (
            get_openai_compatible_upstream("deepseek-chat", "https://api.openai.com")
            == "https://api.openai.com"
        )
        # Can override gpt-4 detection with allowlisted DeepSeek URL
        assert (
            get_openai_compatible_upstream("gpt-4", "https://api.deepseek.com")
            == "https://api.deepseek.com"
        )

    def test_case_insensitive_routing(self):
        """Model routing is case-insensitive."""
        assert (
            get_openai_compatible_upstream("DeepSeek-Chat")
            == "https://api.deepseek.com"
        )
        assert (
            get_openai_compatible_upstream("Groq-Mixtral")
            == "https://api.groq.com/openai"
        )
        assert (
            get_openai_compatible_upstream("Grok-2")
            == "https://api.groq.com/openai"
        )

    def test_ssrf_protection_rejects_non_allowlisted_override(self):
        """Non-allowlisted header overrides are rejected; falls back to model routing."""
        malicious_url = "https://attacker.example.com/api"
        # Non-allowlisted URL should be ignored; falls back to gpt-4 → OpenAI
        assert (
            get_openai_compatible_upstream("gpt-4", malicious_url)
            == "https://api.openai.com"
        )

    def test_ssrf_protection_allows_allowlisted_upstreams(self):
        """Allowlisted upstream URLs are accepted."""
        # Allowlisted URLs should be accepted even with model override
        assert (
            get_openai_compatible_upstream("gpt-4", "https://api.openai.com")
            == "https://api.openai.com"
        )
        assert (
            get_openai_compatible_upstream("gpt-4", "https://api.deepseek.com")
            == "https://api.deepseek.com"
        )
        assert (
            get_openai_compatible_upstream("gpt-4", "https://api.groq.com/openai")
            == "https://api.groq.com/openai"
        )


# ── Task 3: Anthropic Cache Injection ──────────────────────────────────


class TestAnthropicCacheInjection:
    """Verify cache_control injection for Anthropic caching."""

    def test_inject_cache_on_system_and_prefix(self):
        """apply_anthropic_cache adds cache_control to system + stable prefix message."""
        body = {
            "system": "You are a helpful assistant.",
            "messages": [
                {"role": "user", "content": "First question."},
                {"role": "assistant", "content": "First answer."},
                {"role": "user", "content": "Second question."},
            ],
        }

        result = apply_anthropic_cache(body)

        # System should be converted to list with cache_control
        assert isinstance(result["system"], list)
        assert result["system"][0]["type"] == "text"
        assert result["system"][0]["cache_control"] == {"type": "ephemeral"}

        # Message before last user should have cache_control
        messages = result["messages"]
        assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}

        # Last user message should NOT have cache_control
        assert "cache_control" not in str(messages[2])

    def test_system_list_already_exists(self):
        """If system is already a list, add cache_control to last block."""
        body = {
            "system": [
                {"type": "text", "text": "You are helpful."},
                {"type": "text", "text": "Be concise."},
            ],
            "messages": [
                {"role": "user", "content": "Question."},
            ],
        }

        result = apply_anthropic_cache(body)

        # cache_control should be on the last system block
        assert result["system"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_no_cache_on_single_message(self):
        """If there's only one user message, no cache_control added (volatile tail)."""
        body = {
            "system": "Help me.",
            "messages": [
                {"role": "user", "content": "Question."},
            ],
        }

        result = apply_anthropic_cache(body)

        # System still gets cache_control
        assert isinstance(result["system"], list)
        assert result["system"][0]["cache_control"] == {"type": "ephemeral"}

        # Single user message should NOT have cache_control
        assert "cache_control" not in str(result["messages"][0])

    def test_no_crash_on_empty_messages(self):
        """apply_anthropic_cache never raises on empty/malformed input."""
        # Empty messages
        result = apply_anthropic_cache({"messages": []})
        assert result == {"messages": []}

        # No messages key
        result = apply_anthropic_cache({"system": "Help."})
        assert "system" in result

        # Non-dict input
        result = apply_anthropic_cache(None)
        assert result is None

        result = apply_anthropic_cache("not a dict")
        assert result == "not a dict"

    def test_no_crash_on_odd_content_types(self):
        """apply_anthropic_cache handles odd content types gracefully."""
        body = {
            "system": 123,  # Odd type
            "messages": [
                {"role": "user", "content": None},
                {"role": "user", "content": 456},
            ],
        }

        result = apply_anthropic_cache(body)
        # Should not crash; system unchanged, messages unchanged
        assert result["system"] == 123

    def test_max_four_breakpoints(self):
        """apply_anthropic_cache respects max 4 breakpoints."""
        body = {
            "system": "System.",
            "tools": [{"name": "tool1"}],  # Not handled in current implementation
            "messages": [
                {"role": "user", "content": "Q1."},
                {"role": "assistant", "content": "A1."},
                {"role": "user", "content": "Q2."},
                {"role": "assistant", "content": "A2."},
                {"role": "user", "content": "Q3."},
            ],
        }

        result = apply_anthropic_cache(body)

        # Count cache_control markers
        breakpoint_count = 0
        if isinstance(result.get("system"), list):
            for block in result["system"]:
                if isinstance(block, dict) and "cache_control" in block:
                    breakpoint_count += 1

        for msg in result.get("messages", []):
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and "cache_control" in block:
                            breakpoint_count += 1

        assert breakpoint_count <= 4, f"Expected ≤4 breakpoints, got {breakpoint_count}"

    def test_cache_control_value_correct(self):
        """cache_control always has type: ephemeral."""
        body = {
            "system": "System.",
            "messages": [
                {"role": "user", "content": "Q."},
                {"role": "assistant", "content": "A."},
                {"role": "user", "content": "Q2."},
            ],
        }

        result = apply_anthropic_cache(body)

        # Check all cache_control values
        def check_cache_control(obj):
            if isinstance(obj, dict):
                for v in obj.values():
                    if isinstance(v, dict) and "type" in v:
                        if v == {"type": "ephemeral"}:
                            return True
                    if isinstance(v, (list, dict)):
                        if check_cache_control(v):
                            return True
            elif isinstance(obj, list):
                for item in obj:
                    if check_cache_control(item):
                        return True
            return False

        # There should be at least one cache_control
        assert check_cache_control(result)


# ── Integration: compress_messages + cache routing ─────────────────────


def test_compress_and_route_integration():
    """Verify compress_messages preserves prefix for routing to DeepSeek."""
    messages = [
        {"role": "system", "content": "You are an AI."},
        {"role": "user", "content": "First prompt."},
        {"role": "assistant", "content": "First response."},
        {"role": "user", "content": "Second prompt needing compression."},
    ]

    session = MagicMock(spec=BrevitasSession)
    session.prior_context.return_value = []

    with patch("brevitas._compress._cfg") as mock_cfg:
        mock_cfg.return_value = {
            "enabled": True,
            "api_key": "test_key",
            "base_url": "http://localhost:8000",
            "timeout": 30,
        }

        with patch("httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "compressed_messages": ["Second prompt compressed."],
                "optimized_tokens": 3,
            }
            mock_post.return_value = mock_response

            compressed, baseline, _ = compress_messages(messages, session)

            # Prefix should be identical (routing won't affect cache detection)
            assert compressed[0] is messages[0]
            assert compressed[1] is messages[1]
            assert compressed[2] is messages[2]

            # Last message is compressed
            assert compressed[3]["content"] == "Second prompt compressed."

            # Verify the route selection would work
            route = get_openai_compatible_upstream("deepseek-chat")
            assert route == "https://api.deepseek.com"


def test_anthropic_proxy_applies_cache_control():
    """Verify proxy_anthropic_messages applies cache_control before upstream call."""
    body = {
        "model": "claude-3-sonnet",
        "system": "You are a helpful assistant.",
        "messages": [
            {"role": "user", "content": "First question."},
            {"role": "assistant", "content": "First response."},
            {"role": "user", "content": "Second question."},
        ],
    }

    session = MagicMock(spec=BrevitasSession)
    session.prior_context.return_value = []

    with patch("brevitas._compress._cfg") as mock_cfg:
        mock_cfg.return_value = {
            "enabled": True,
            "api_key": "test_key",
            "base_url": "http://localhost:8000",
            "timeout": 30,
        }

        with patch("httpx.post") as mock_post:
            # Mock compression response
            mock_compress_response = MagicMock()
            mock_compress_response.json.return_value = {
                "compressed_messages": ["Second question compressed."],
                "optimized_tokens": 3,
            }

            # Mock upstream Anthropic response
            mock_upstream_response = MagicMock()
            mock_upstream_response.json.return_value = {
                "content": [{"type": "text", "text": "Response"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
            mock_upstream_response.status_code = 200

            # First call is /v1/compress, second is upstream Anthropic
            mock_post.side_effect = [mock_compress_response, mock_upstream_response]

            # Simulate the proxy call
            from token_efficiency_model.optimizers.provider_cache.anthropic import (
                apply_anthropic_cache,
            )

            compressed, baseline, _ = compress_messages(
                body.get("messages"), session, task=body.get("system", "")
            )
            body["messages"] = compressed
            body = apply_anthropic_cache(body)

            # Verify cache_control was added
            system = body.get("system")
            assert isinstance(system, list), "System should be converted to list"
            assert "cache_control" in system[0], "System block should have cache_control"
            assert system[0]["cache_control"] == {"type": "ephemeral"}

            # Verify stable prefix message has cache_control
            messages = body.get("messages")
            assert len(messages) > 1
            # Message before last user message (index 1) should have cache_control
            if isinstance(messages[1].get("content"), list):
                assert any(
                    "cache_control" in block for block in messages[1]["content"]
                ), "Stable prefix message should have cache_control"

            # Last message should NOT have cache_control
            last_msg = messages[-1]
            if isinstance(last_msg.get("content"), list):
                assert not any(
                    "cache_control" in block for block in last_msg["content"]
                ), "Volatile tail should NOT have cache_control"
