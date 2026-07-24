"""Recursive, bounded credential redaction for logs, metrics, and exceptions."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
REDACTED_KEY = "[REDACTED_KEY]"
TRUNCATED = "[TRUNCATED]"

_SECRET_FIELD = re.compile(
    r"(?i)(?:^|[_-])(?:authorization|proxy_authorization|cookie|set_cookie|"
    r"api[_-]?key|apikey|provider[_-]?api[_-]?key|password|passwd|secret|"
    r"client[_-]?secret|token|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"private[_-]?key|service[_-]?role|signature|credential)(?:$|[_-])"
    r"|^(?:x[_-](?:api|brevitas)[_-]key|x[_-]auth[_-]token)$"
)
_URL_FIELD = re.compile(r"(?i)(?:^|[_-])(?:url|uri|endpoint)(?:$|[_-])")
_HEADER_CONTAINER = re.compile(r"(?i)(?:^|[_-])headers?(?:$|[_-])")
_SAFE_HEADERS = frozenset({
    "accept", "content-type", "content-length", "user-agent", "x-request-id",
    "x-brevitas-request-id", "traceparent", "tracestate",
})
_SAFE_URL_QUERY = frozenset({"page", "limit", "offset", "status", "version"})
_TOKEN = re.compile(
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}|"
    r"\b(?:sk|rk|pk|bvt|phx|phs|whsec|xox[baprs]|gh[opusr]|sb_secret)[_-]"
    r"[A-Za-z0-9_-]{6,}|\bAIza[A-Za-z0-9_-]{20,}"
)
_JWT = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])")
_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|password|secret|token|credential)"
    r"\s*[:=]\s*[^\s,;]+"
)


def _field_name(value: object) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", str(value).strip().lower())[:128]


def _safe_mapping_key(value: object) -> tuple[str, str]:
    """Return a safe output key and its normalized form without echoing credentials."""

    try:
        raw = str(value)
    except Exception:
        return REDACTED_KEY, "redacted"
    if len(raw) > 512 or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw):
        return REDACTED_KEY, "redacted"
    normalized = _field_name(raw)
    cleaned = redact_text(raw, maximum=512)
    if not cleaned or REDACTED in cleaned or cleaned != raw:
        return REDACTED_KEY, normalized
    return cleaned[:128], normalized


def redact_url(value: object) -> str:
    """Remove userinfo, fragments, and non-allowlisted query values from a URL."""

    text = str(value or "")[:4096]
    try:
        parsed = urlsplit(text)
    except ValueError:
        return REDACTED
    if parsed.scheme not in {"http", "https", "postgres", "postgresql", "redis", "rediss"}:
        return REDACTED
    host = parsed.hostname or ""
    if not host:
        return REDACTED
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return REDACTED
    # Bracket IPv6 hosts after removing userinfo.
    safe_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = safe_host + port
    safe_query = []
    try:
        query_fields = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=64)
    except ValueError:
        query_fields = [("query", REDACTED)]
    for key, item in query_fields:
        safe_key, normalized = _safe_mapping_key(key)
        if _SECRET_FIELD.search(normalized):
            safe_key = REDACTED_KEY
        safe_query.append((
            safe_key[:128],
            redact_text(item, maximum=256) if normalized in _SAFE_URL_QUERY else REDACTED,
        ))
    safe_path = redact_text(parsed.path, maximum=2048)
    return urlunsplit((parsed.scheme, netloc, safe_path, urlencode(safe_query), ""))


def redact_text(value: object, *, maximum: int = 2048) -> str:
    text = str(value or "")[: max(0, min(int(maximum), 16_384))]
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        return redact_url(text)
    text = _TOKEN.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    text = _ASSIGNMENT.sub(REDACTED, text)
    return "".join(char for char in text if char >= " " and char not in "\x7f\r\n")


def _redact_exception(
    error: BaseException,
    *,
    safe_fields: frozenset[str] | None,
    depth: int,
    max_depth: int,
    max_items: int,
    seen: set[int],
) -> dict[str, object]:
    result: dict[str, object] = {
        "type": type(error).__name__[:128],
        "message": redact_text(str(error), maximum=512),
    }
    cause = error.__cause__ or error.__context__
    if cause is not None and cause is not error and depth < max_depth:
        result["cause"] = _redact_exception(
            cause,
            safe_fields=safe_fields,
            depth=depth + 1,
            max_depth=max_depth,
            max_items=max_items,
            seen=seen,
        )
    # Structured exception attributes are useful but must pass the same policy.
    attributes = getattr(error, "__dict__", None)
    if attributes:
        result["attributes"] = _redact(
            attributes,
            field="attributes",
            safe_fields=safe_fields,
            depth=depth + 1,
            max_depth=max_depth,
            max_items=max_items,
            seen=seen,
            in_headers=False,
        )
    return result


def _redact(
    value: Any,
    *,
    field: str,
    safe_fields: frozenset[str] | None,
    depth: int,
    max_depth: int,
    max_items: int,
    seen: set[int],
    in_headers: bool,
) -> Any:
    normalized_field = _field_name(field)
    if _SECRET_FIELD.search(normalized_field):
        return REDACTED
    if depth > max_depth:
        return TRUNCATED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return REDACTED
    if isinstance(value, BaseException):
        return _redact_exception(
            value,
            safe_fields=safe_fields,
            depth=depth,
            max_depth=max_depth,
            max_items=max_items,
            seen=seen,
        )
    if isinstance(value, str):
        if _URL_FIELD.search(normalized_field):
            return redact_url(value)
        return redact_text(value)

    object_id = id(value)
    if object_id in seen:
        return TRUNCATED
    seen.add(object_id)
    try:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            header_container = in_headers or bool(_HEADER_CONTAINER.search(normalized_field))
            for index, (raw_key, item) in enumerate(value.items()):
                if index >= max_items:
                    result[TRUNCATED] = TRUNCATED
                    break
                key, normalized_key = _safe_mapping_key(raw_key)
                if _SECRET_FIELD.search(normalized_key):
                    result[key] = REDACTED
                elif header_container and normalized_key not in _SAFE_HEADERS:
                    result[key] = REDACTED
                elif safe_fields is not None and normalized_key not in safe_fields:
                    result[key] = REDACTED
                else:
                    result[key] = _redact(
                        item,
                        field=normalized_key,
                        safe_fields=safe_fields,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_items=max_items,
                        seen=seen,
                        in_headers=header_container,
                    )
            return result
        if isinstance(value, Sequence):
            result = [
                _redact(
                    item,
                    field=normalized_field,
                    safe_fields=safe_fields,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_items=max_items,
                    seen=seen,
                    in_headers=in_headers,
                )
                for item in value[:max_items]
            ]
            if len(value) > max_items:
                result.append(TRUNCATED)
            return tuple(result) if isinstance(value, tuple) else result
        return redact_text(value)
    finally:
        seen.discard(object_id)


def redact(
    value: Any,
    *,
    safe_fields: Iterable[str] | None = None,
    max_depth: int = 8,
    max_items: int = 256,
) -> Any:
    """Recursively redact secrets while preserving bounded diagnostic structure.

    When ``safe_fields`` is provided, every mapping value whose normalized key is
    not allowlisted is redacted.  Secret fields always lose, even if allowlisted.
    """

    if not 1 <= int(max_depth) <= 16 or not 1 <= int(max_items) <= 1024:
        raise ValueError("redaction bounds are outside safe limits")
    allowed = None if safe_fields is None else frozenset(
        _field_name(field) for field in safe_fields
    )
    return _redact(
        value,
        field="root",
        safe_fields=allowed,
        depth=0,
        max_depth=int(max_depth),
        max_items=int(max_items),
        seen=set(),
        in_headers=False,
    )


def redact_exception(error: BaseException) -> dict[str, object]:
    return _redact_exception(
        error,
        safe_fields=None,
        depth=0,
        max_depth=8,
        max_items=256,
        seen=set(),
    )


__all__ = [
    "REDACTED", "REDACTED_KEY", "TRUNCATED", "redact", "redact_exception",
    "redact_text", "redact_url",
]
