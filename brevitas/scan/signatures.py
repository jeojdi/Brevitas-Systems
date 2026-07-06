"""
Polyglot signature registry for detecting LLM / AI API calls in source code.

The scanner is deliberately *not* built on per-language AST parsing. Instead it
matches on provider-identifying signals that appear in the source of **any**
language:

  1. HTTP endpoint hosts   — `api.openai.com`, `api.anthropic.com`, … These catch
                             calls made from Go, Rust, PHP, Ruby, Java, shell/curl,
                             etc., not just the official Python/JS SDKs.
  2. SDK import / package  — `import openai`, `@anthropic-ai/sdk`, `litellm`, …
  3. Call-method signatures — `.chat.completions.create`, `.messages.create`,
                             `generateContent(`, `invoke_model(`, …
  4. Model-id literals     — `gpt-4o`, `claude-…`, `gemini-…` (supporting evidence,
                             also used to estimate cost).

Each match carries a `kind` and a `confidence`. A file/line is *flagged as a call
site* when it has at least one high-confidence signal (endpoint or call method).
Imports and model IDs are corroborating signals that raise confidence and help
routing/estimation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


HIGH = "high"
MEDIUM = "medium"
LOW = "low"

# Signal kinds
ENDPOINT = "endpoint"   # a provider HTTP host — language-agnostic, strong
CALL = "call"           # an SDK call method — strong
IMPORT = "import"       # an SDK import / package reference — medium
MODEL = "model"         # a model-id literal — supporting


@dataclass(frozen=True)
class Pattern:
    """A single compiled signature pattern."""
    regex: re.Pattern
    kind: str
    confidence: str


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the scanner + installer need to know about one provider."""
    id: str
    name: str
    patterns: tuple[Pattern, ...]
    # OpenAI-compatible providers can be routed by pointing the OpenAI SDK's
    # base URL at the Brevitas proxy. `env_base_url` is the env var the official
    # SDK honors (if any); `honors_env` means it can be routed with zero code
    # changes. `openai_compatible` means the /openai proxy path applies.
    env_base_url: str | None = None
    honors_env: bool = False
    openai_compatible: bool = False
    # Approx blended $/1M tokens (input+output), for a *rough* spend estimate.
    # These are order-of-magnitude anchors — override with --price if it matters.
    approx_price_per_mtok: float = 5.0


def _p(pattern: str, kind: str, confidence: str, *, flags: int = re.IGNORECASE) -> Pattern:
    return Pattern(re.compile(pattern, flags), kind, confidence)


# ── Provider registry ─────────────────────────────────────────────────────────
# Ordered roughly by specificity; the scanner tries every provider on every line.

PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="openai",
        name="OpenAI",
        env_base_url="OPENAI_BASE_URL",
        honors_env=True,
        openai_compatible=True,
        approx_price_per_mtok=5.0,
        patterns=(
            _p(r"api\.openai\.com", ENDPOINT, HIGH),
            _p(r"\.chat\.completions\.create\b", CALL, HIGH),
            _p(r"\.responses\.create\b", CALL, HIGH),
            _p(r"\.completions\.create\b", CALL, MEDIUM),
            _p(r"\.embeddings\.create\b", CALL, MEDIUM),
            _p(r"(?:^|[^.\w])import\s+openai\b|from\s+openai\s+import|require\(['\"]openai['\"]\)|@ai-sdk/openai", IMPORT, MEDIUM),
            _p(r"\b(?:gpt-4o|gpt-4\.1|gpt-4|gpt-3\.5|o1|o3|o4|text-embedding-3)\b", MODEL, LOW),
        ),
    ),
    ProviderSpec(
        id="azure_openai",
        name="Azure OpenAI",
        env_base_url="AZURE_OPENAI_ENDPOINT",
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=5.0,
        patterns=(
            _p(r"[a-z0-9.-]*\.openai\.azure\.com", ENDPOINT, HIGH),
            _p(r"AzureOpenAI\b", CALL, HIGH),
            _p(r"azure[_-]?openai", IMPORT, MEDIUM),
        ),
    ),
    ProviderSpec(
        id="anthropic",
        name="Anthropic",
        env_base_url="ANTHROPIC_BASE_URL",
        honors_env=True,
        openai_compatible=False,
        approx_price_per_mtok=9.0,
        patterns=(
            _p(r"api\.anthropic\.com", ENDPOINT, HIGH),
            _p(r"\.messages\.create\b", CALL, HIGH),
            _p(r"\.messages\.stream\b", CALL, HIGH),
            _p(r"from\s+anthropic\s+import|(?:^|[^.\w])import\s+anthropic\b|@anthropic-ai/sdk|require\(['\"]@anthropic-ai/sdk['\"]\)", IMPORT, MEDIUM),
            _p(r"\bclaude-[a-z0-9.\-]+\b", MODEL, LOW),
        ),
    ),
    ProviderSpec(
        id="google_gemini",
        name="Google Gemini / Vertex",
        env_base_url=None,
        honors_env=False,
        openai_compatible=False,
        approx_price_per_mtok=3.0,
        patterns=(
            _p(r"generativelanguage\.googleapis\.com", ENDPOINT, HIGH),
            _p(r"[a-z0-9-]*aiplatform\.googleapis\.com", ENDPOINT, HIGH),
            _p(r"\.generate_content\b|\.generateContent\b", CALL, HIGH),
            _p(r"google\.generativeai|google\.genai|from\s+google\s+import\s+genai|@google/generative-ai|google-genai|vertexai", IMPORT, MEDIUM),
            _p(r"\bgemini-[a-z0-9.\-]+\b", MODEL, LOW),
        ),
    ),
    ProviderSpec(
        id="deepseek",
        name="DeepSeek",
        env_base_url=None,
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=0.5,
        patterns=(
            _p(r"api\.deepseek\.com", ENDPOINT, HIGH),
            _p(r"\bdeepseek-(?:chat|reasoner|[a-z0-9.\-]+)\b", MODEL, LOW),
        ),
    ),
    ProviderSpec(
        id="groq",
        name="Groq",
        env_base_url=None,
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=0.3,
        patterns=(
            _p(r"api\.groq\.com", ENDPOINT, HIGH),
            _p(r"from\s+groq\s+import|(?:^|[^.\w])import\s+groq\b|require\(['\"]groq-sdk['\"]\)", IMPORT, MEDIUM),
        ),
    ),
    ProviderSpec(
        id="xai",
        name="xAI Grok",
        env_base_url=None,
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=5.0,
        patterns=(
            _p(r"api\.x\.ai", ENDPOINT, HIGH),
            _p(r"\bgrok-[a-z0-9.\-]+\b", MODEL, LOW),
        ),
    ),
    ProviderSpec(
        id="mistral",
        name="Mistral",
        env_base_url=None,
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=2.0,
        patterns=(
            _p(r"api\.mistral\.ai", ENDPOINT, HIGH),
            _p(r"from\s+mistralai|(?:^|[^.\w])import\s+mistralai\b|@mistralai/mistralai", IMPORT, MEDIUM),
            _p(r"\b(?:mistral-[a-z0-9.\-]+|mixtral-[a-z0-9x.\-]+|codestral-[a-z0-9.\-]+)\b", MODEL, LOW),
        ),
    ),
    ProviderSpec(
        id="cohere",
        name="Cohere",
        env_base_url=None,
        honors_env=False,
        openai_compatible=False,
        approx_price_per_mtok=2.0,
        patterns=(
            _p(r"api\.cohere\.(?:ai|com)", ENDPOINT, HIGH),
            _p(r"from\s+cohere\s+import|(?:^|[^.\w])import\s+cohere\b|require\(['\"]cohere-ai['\"]\)", IMPORT, MEDIUM),
            _p(r"\bcommand-[a-z0-9.\-]+\b", MODEL, LOW),
        ),
    ),
    ProviderSpec(
        id="litellm",
        name="LiteLLM (router)",
        env_base_url=None,
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=5.0,
        patterns=(
            _p(r"(?:^|[^.\w])import\s+litellm\b|from\s+litellm\s+import", IMPORT, HIGH),
            _p(r"litellm\.(?:completion|acompletion|embedding)\b", CALL, HIGH),
        ),
    ),
    ProviderSpec(
        id="langchain",
        name="LangChain",
        env_base_url=None,
        honors_env=False,
        openai_compatible=False,
        approx_price_per_mtok=5.0,
        patterns=(
            _p(r"from\s+langchain|(?:^|[^.\w])import\s+langchain\b|@langchain/", IMPORT, MEDIUM),
            _p(r"\bChat(?:OpenAI|Anthropic|Google[A-Za-z]*|Mistral[A-Za-z]*|Groq|Cohere)\b", CALL, MEDIUM),
        ),
    ),
    ProviderSpec(
        id="bedrock",
        name="AWS Bedrock",
        env_base_url=None,
        honors_env=False,
        openai_compatible=False,
        approx_price_per_mtok=8.0,
        patterns=(
            _p(r"bedrock[a-z-]*\.[a-z0-9-]+\.amazonaws\.com", ENDPOINT, HIGH),
            _p(r"\binvoke_model\b|\bInvokeModel\b|\.converse\(", CALL, MEDIUM),
            _p(r"['\"]bedrock-runtime['\"]|['\"]bedrock['\"]", IMPORT, MEDIUM),
        ),
    ),
    ProviderSpec(
        id="together",
        name="Together AI",
        env_base_url=None,
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=0.9,
        patterns=(
            _p(r"api\.together\.(?:xyz|ai)", ENDPOINT, HIGH),
            _p(r"from\s+together\s+import|(?:^|[^.\w])import\s+together\b", IMPORT, MEDIUM),
        ),
    ),
    ProviderSpec(
        id="fireworks",
        name="Fireworks AI",
        env_base_url=None,
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=0.9,
        patterns=(
            _p(r"api\.fireworks\.ai", ENDPOINT, HIGH),
            _p(r"from\s+fireworks|(?:^|[^.\w])import\s+fireworks\b", IMPORT, MEDIUM),
        ),
    ),
    ProviderSpec(
        id="openrouter",
        name="OpenRouter",
        env_base_url=None,
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=5.0,
        patterns=(
            _p(r"openrouter\.ai/api", ENDPOINT, HIGH),
        ),
    ),
    ProviderSpec(
        id="perplexity",
        name="Perplexity",
        env_base_url=None,
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=1.0,
        patterns=(
            _p(r"api\.perplexity\.ai", ENDPOINT, HIGH),
        ),
    ),
    ProviderSpec(
        id="replicate",
        name="Replicate",
        env_base_url=None,
        honors_env=False,
        openai_compatible=False,
        approx_price_per_mtok=3.0,
        patterns=(
            _p(r"api\.replicate\.com", ENDPOINT, HIGH),
            _p(r"from\s+replicate|(?:^|[^.\w])import\s+replicate\b|require\(['\"]replicate['\"]\)", IMPORT, MEDIUM),
        ),
    ),
    ProviderSpec(
        id="huggingface",
        name="Hugging Face Inference",
        env_base_url=None,
        honors_env=False,
        openai_compatible=False,
        approx_price_per_mtok=1.0,
        patterns=(
            _p(r"api-inference\.huggingface\.co|router\.huggingface\.co", ENDPOINT, HIGH),
            _p(r"InferenceClient\b|from\s+huggingface_hub", IMPORT, MEDIUM),
        ),
    ),
    ProviderSpec(
        id="ollama",
        name="Ollama (local)",
        env_base_url="OLLAMA_HOST",
        honors_env=False,
        openai_compatible=True,
        approx_price_per_mtok=0.0,
        patterns=(
            _p(r"localhost:11434|127\.0\.0\.1:11434|/api/(?:generate|chat)\b", ENDPOINT, MEDIUM),
            _p(r"from\s+ollama|(?:^|[^.\w])import\s+ollama\b|require\(['\"]ollama['\"]\)", IMPORT, MEDIUM),
        ),
    ),
)


PROVIDERS_BY_ID: dict[str, ProviderSpec] = {p.id: p for p in PROVIDERS}


def iter_patterns():
    """Yield (provider, pattern) for every pattern in the registry."""
    for provider in PROVIDERS:
        for pattern in provider.patterns:
            yield provider, pattern
