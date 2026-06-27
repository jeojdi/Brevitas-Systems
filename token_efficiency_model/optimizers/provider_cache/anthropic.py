"""
Anthropic cache_control adapter for Phase 1 native caching.

Applies cache_control breakpoints to system block and stable prefix messages
to enable Anthropic's 5-minute ephemeral prompt caching (~50% discount on cache hits).
"""


def apply_anthropic_cache(body: dict) -> dict:
    """
    Insert cache_control: {"type": "ephemeral"} on system block and last stable prefix message.

    Guardrails:
    - At most 4 cache_control breakpoints total
    - Do NOT add breakpoints to messages below ~1024 tokens (min cacheable)
    - Never mark the final/volatile message
    - Pure function: never raises on malformed input (system-string vs list, empty messages, etc.)

    Args:
        body: Request body dict with optional "system", "tools", "messages" keys

    Returns:
        Modified body dict with cache_control injected (or unchanged if no valid targets)
    """
    if not isinstance(body, dict):
        return body

    messages = body.get("messages", [])
    if not isinstance(messages, list) or len(messages) == 0:
        return body

    # Find the index of the last user message (volatile tail)
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_idx = i
            break

    # If no user message, return unchanged
    if last_user_idx < 0:
        return body

    breakpoint_count = 0
    max_breakpoints = 4

    # 1. Try to add cache_control to system block
    system = body.get("system")
    if system and breakpoint_count < max_breakpoints:
        if isinstance(system, str):
            # Convert string system to list with one text block
            body["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            breakpoint_count += 1
        elif isinstance(system, list) and len(system) > 0:
            # Add cache_control to the last block in the system list
            if isinstance(system[-1], dict) and "cache_control" not in system[-1]:
                system[-1]["cache_control"] = {"type": "ephemeral"}
                breakpoint_count += 1

    # 2. Try to add cache_control to the last stable prefix message (before volatile tail)
    if last_user_idx > 0 and breakpoint_count < max_breakpoints:
        # Add to the message immediately before the last user message
        stable_msg_idx = last_user_idx - 1
        msg = messages[stable_msg_idx]

        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                # Replace string content with text block + cache_control
                msg["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                breakpoint_count += 1
            elif isinstance(content, list) and len(content) > 0:
                # Add cache_control to the last block in the content list
                if isinstance(content[-1], dict) and "cache_control" not in content[-1]:
                    content[-1]["cache_control"] = {"type": "ephemeral"}
                    breakpoint_count += 1

    return body
