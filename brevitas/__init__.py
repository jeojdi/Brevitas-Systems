"""
Brevitas — drop compression between your agents.

Quick start (SDK wrapper):
    import anthropic, brevitas

    brevitas.configure(
        api_key="bvt_...",
        base_url="http://localhost:8000",   # or https://api.brevitassystems.com
    )
    client = brevitas.wrap(anthropic.Anthropic(api_key="sk-ant-..."))

    # All calls are now automatically compressed
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": "..."}],
    )

Quick start (zero-code proxy):
    $ brevitas start --api-key bvt_... --port 4242
    $ export ANTHROPIC_BASE_URL=http://localhost:4242
    # Your existing code works unchanged.
"""
from .config import configure, get as get_config
from .session import BrevitasSession


def wrap(client, session: BrevitasSession | None = None):
    """
    Wrap an Anthropic or OpenAI client.

    Returns a drop-in replacement that compresses messages before each call
    and tracks multi-hop context within the same pipeline run.

    Args:
        client:  An anthropic.Anthropic or openai.OpenAI instance.
        session: Optional existing BrevitasSession (creates a new one if omitted).
    """
    # Anthropic detection: has .messages attribute with a .create method
    if hasattr(client, "messages") and hasattr(getattr(client, "messages", None), "create"):
        from .wrappers.anthropic import BrevitasAnthropicClient
        return BrevitasAnthropicClient(client, session=session)

    # OpenAI detection: has .chat.completions
    if hasattr(client, "chat") and hasattr(getattr(client, "chat", None), "completions"):
        from .wrappers.openai import BrevitasOpenAIClient
        return BrevitasOpenAIClient(client, session=session)

    raise TypeError(
        f"brevitas.wrap() does not recognise client type {type(client).__name__!r}. "
        "Pass an anthropic.Anthropic or openai.OpenAI instance."
    )


__all__ = ["configure", "get_config", "wrap", "BrevitasSession"]
__version__ = "0.9.5"
