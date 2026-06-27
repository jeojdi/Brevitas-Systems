import os

_cfg: dict = {
    "api_key":  os.getenv("BREVITAS_API_KEY", ""),
    "base_url": os.getenv("BREVITAS_BASE_URL", "http://localhost:8000"),
    "enabled":  os.getenv("BREVITAS_ENABLED", "true").lower() != "false",
    "timeout":  30,
}


def configure(
    api_key: str = "",
    base_url: str = "",
    enabled: bool = True,
    timeout: int = 30,
) -> None:
    if api_key:   _cfg["api_key"]  = api_key
    if base_url:  _cfg["base_url"] = base_url.rstrip("/")
    _cfg["enabled"] = enabled
    _cfg["timeout"] = timeout


def get() -> dict:
    return dict(_cfg)
