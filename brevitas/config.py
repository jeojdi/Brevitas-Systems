import os
import warnings
from urllib.parse import urlsplit

# The hosted control plane. Must be a bare origin: report_usage joins
# f"{base_url}/v1/usage", so a "/v1"-suffixed base yields /v1/v1 and 404s silently.
DEFAULT_BASE_URL = "https://api.brevitassystems.com"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", ""}
_warned_base_urls: set[str] = set()


def check_base_url(base_url: str) -> None:
    """Warn (never raise) about a base URL that will leak or silently 404.

    The Brevitas key travels in a header on every metered call, so a plaintext
    or wrong-path base URL is a credential-disclosure bug — but configure() runs
    on import-adjacent paths and report_usage runs on the hot path, so this can
    only ever warn, and only once per distinct URL per process.
    """
    if base_url in _warned_base_urls:
        return
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    loopback = host in _LOOPBACK_HOSTS or host.endswith(".localhost")
    problem = ""
    if parts.path.rstrip("/").endswith("/v1"):
        problem = (
            f"BREVITAS_BASE_URL {base_url!r} ends in /v1; Brevitas appends /v1/... itself, "
            "so requests would go to /v1/v1. Use the bare origin"
        )
    elif parts.scheme != "https" and not loopback:
        problem = (
            f"BREVITAS_BASE_URL {base_url!r} is not https; your Brevitas API key would be "
            "sent in cleartext. Use https, or a loopback address for local development"
        )
    if problem:
        _warned_base_urls.add(base_url)
        warnings.warn(problem, stacklevel=3)


_cfg: dict = {
    "api_key":  os.getenv("BREVITAS_API_KEY", ""),
    "base_url": os.getenv("BREVITAS_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
    "enabled":  os.getenv("BREVITAS_ENABLED", "true").lower() != "false",
    "timeout":  30,
}
check_base_url(_cfg["base_url"])


def configure(
    api_key: str = "",
    base_url: str = "",
    enabled: bool = True,
    timeout: int = 30,
) -> None:
    if api_key:   _cfg["api_key"]  = api_key
    if base_url:
        _cfg["base_url"] = base_url.rstrip("/")
        check_base_url(_cfg["base_url"])
    _cfg["enabled"] = enabled
    _cfg["timeout"] = timeout


def get() -> dict:
    return dict(_cfg)
