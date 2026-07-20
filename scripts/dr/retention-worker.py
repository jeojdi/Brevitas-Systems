#!/usr/bin/env python3
"""Dedicated, content-free Railway authority for database retention."""
from __future__ import annotations

import json
import logging
import os
import random
import re
import signal
import socket
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger("brevitas.retention_worker")
_OWNER = re.compile(r"[^A-Za-z0-9._:-]+")
_COUNT_KEYS = (
    "usage_candidates", "audit_candidates", "support_candidates",
    "requests_candidates", "holds_candidates", "prior_run_evidence_candidates",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    supabase_url: str
    service_role_key: str
    batch_limit: int = 5000
    max_cycles: int = 100
    retries: int = 5
    request_timeout_seconds: int = 15
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    backlog_retry_seconds: int = 60
    health_port: int = 8080
    schedule_hour_utc: int = 3
    schedule_minute_utc: int = 15

    @classmethod
    def from_env(cls) -> "Settings":
        url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
        if not url.startswith("https://") or not key:
            raise RuntimeError("Supabase retention credentials are required")
        return cls(
            supabase_url=url, service_role_key=key,
            batch_limit=_bounded_int("RETENTION_BATCH_LIMIT", 5000, 1, 10000),
            max_cycles=_bounded_int("RETENTION_MAX_CYCLES", 100, 1, 1000),
            retries=_bounded_int("RETENTION_RETRIES", 5, 1, 10),
            request_timeout_seconds=_bounded_int("RETENTION_REQUEST_TIMEOUT_SECONDS", 15, 1, 60),
            retry_base_seconds=float(_bounded_int("RETENTION_RETRY_BASE_SECONDS", 1, 1, 30)),
            retry_max_seconds=float(_bounded_int("RETENTION_RETRY_MAX_SECONDS", 30, 1, 300)),
            backlog_retry_seconds=_bounded_int("RETENTION_BACKLOG_RETRY_SECONDS", 60, 5, 3600),
            health_port=_bounded_int("PORT", 8080, 1, 65535),
        )


class RPC(Protocol):
    def call(self, name: str, payload: dict[str, Any]) -> Any: ...


class RestRPC:
    def __init__(self, settings: Settings):
        self._settings = settings

    def call(self, name: str, payload: dict[str, Any]) -> Any:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        request = Request(
            f"{self._settings.supabase_url}/rest/v1/rpc/{name}", data=encoded,
            headers={
                "apikey": self._settings.service_role_key,
                "authorization": f"Bearer {self._settings.service_role_key}",
                "content-type": "application/json",
            }, method="POST",
        )
        try:
            with urlopen(request, timeout=self._settings.request_timeout_seconds) as response:
                raw = response.read(1024 * 1024 + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"retention RPC unavailable:{type(exc).__name__}") from exc
        if len(raw) > 1024 * 1024:
            raise RuntimeError("retention RPC response exceeded its bound")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("retention RPC returned invalid JSON") from exc


@dataclass(slots=True)
class Health:
    running: bool = False
    initialized: bool = False
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    next_scheduled_at: str | None = None
    consecutive_errors: int = 0
    remaining_candidates: int = 0
    backlog_remaining: bool = False
    backlog_over_24h: bool = False
    missed_run_24h: bool = True
    schema_contract_ok: bool = False
    legal_holds_evaluated: bool = False
    financial_ledger_preserved: bool = False
    evidence_contains_customer_content: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **values: Any) -> None:
        with self._lock:
            for name, value in values.items():
                if name != "_lock" and hasattr(self, name):
                    setattr(self, name, value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running, "initialized": self.initialized,
                "last_attempt_at": self.last_attempt_at,
                "last_success_at": self.last_success_at,
                "next_scheduled_at": self.next_scheduled_at,
                "consecutive_errors": self.consecutive_errors,
                "remaining_candidates": self.remaining_candidates,
                "backlog_remaining": self.backlog_remaining,
                "backlog_over_24h": self.backlog_over_24h,
                "missed_run_24h": self.missed_run_24h,
                "schema_contract_ok": self.schema_contract_ok,
                "legal_holds_evaluated": self.legal_holds_evaluated,
                "financial_ledger_preserved": self.financial_ledger_preserved,
                "evidence_contains_customer_content": self.evidence_contains_customer_content,
            }

    def ready(self) -> bool:
        value = self.snapshot()
        return bool(
            value["running"] and value["initialized"]
            and not value["missed_run_24h"] and not value["backlog_over_24h"]
            and value["schema_contract_ok"] and value["legal_holds_evaluated"]
            and value["financial_ledger_preserved"]
            and not value["evidence_contains_customer_content"]
        )


def _owner_id() -> str:
    host = _OWNER.sub("-", socket.gethostname()).strip("-")[:64] or "unknown"
    return f"retention:{host}:{os.getpid()}"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _next_schedule(now: datetime, settings: Settings) -> datetime:
    candidate = now.astimezone(timezone.utc).replace(
        hour=settings.schedule_hour_utc, minute=settings.schedule_minute_utc,
        second=0, microsecond=0,
    )
    return candidate if candidate > now else candidate + timedelta(days=1)


class RetentionWorker:
    def __init__(self, settings: Settings, rpc: RPC, *, health: Health | None = None,
                 now: Callable[[], datetime] = _utcnow,
                 jitter: Callable[[float, float], float] = random.SystemRandom().uniform):
        self.settings = settings
        self.rpc = rpc
        self.health = health or Health()
        self.now = now
        self.jitter = jitter
        self.owner = _owner_id()
        self.stop = threading.Event()

    @staticmethod
    def _one(value: Any) -> Any:
        if isinstance(value, list):
            return value[0] if len(value) == 1 else value
        return value

    @staticmethod
    def _health_contract(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) \
                or value.get("schema") != "brevitas.compliance-retention-health.v1" \
                or value.get("evidence_contains_customer_content") is not False:
            raise RuntimeError("retention health contract mismatch")
        return value

    def refresh_health(self) -> dict[str, Any]:
        value = self._health_contract(self._one(
            self.rpc.call("compliance_retention_worker_health", {})))
        initialized = value.get("initialized") is True
        self.health.update(
            initialized=initialized,
            last_success_at=value.get("last_success_at") if initialized else None,
            remaining_candidates=int(value.get("remaining_candidates") or 0),
            backlog_remaining=value.get("backlog_remaining") is True,
            backlog_over_24h=value.get("backlog_over_24h") is True,
            missed_run_24h=value.get("missed_run_24h") is True,
            schema_contract_ok=value.get("schema_contract_ok") is True if initialized else False,
            legal_holds_evaluated=value.get("legal_holds_evaluated") is True if initialized else False,
            financial_ledger_preserved=value.get("financial_ledger_preserved") is True if initialized else False,
            evidence_contains_customer_content=False,
        )
        return value

    def _cycle_payload(self) -> dict[str, Any]:
        identifiers = [str(uuid.uuid4()) for _ in range(4)]
        return {
            "p_cycle_id": identifiers[0], "p_dry_run_id": identifiers[1],
            "p_apply_run_id": identifiers[2], "p_post_run_id": identifiers[3],
            "p_worker_owner": self.owner, "p_actor_id": "system:retention-worker",
            "p_batch_limit": self.settings.batch_limit,
        }

    def run_cycle(self) -> dict[str, Any]:
        self.health.update(last_attempt_at=_iso(self.now()))
        value = self._one(self.rpc.call(
            "compliance_retention_worker_cycle", self._cycle_payload()))
        if not isinstance(value, dict) \
                or value.get("schema") != "brevitas.compliance-retention-cycle.v1" \
                or value.get("evidence_contains_customer_content") is not False:
            raise RuntimeError("retention cycle contract mismatch")
        if value.get("status") == "lease_unavailable":
            return value
        if value.get("status") != "completed" \
                or value.get("schema_contract_ok") is not True \
                or value.get("legal_holds_evaluated") is not True \
                or value.get("financial_ledger_preserved") is not True:
            raise RuntimeError("retention cycle invariant failed")
        for section in ("dry_run", "post_apply_dry_run"):
            result = value.get(section)
            if not isinstance(result, dict) or any(
                not isinstance(result.get(key), int)
                or isinstance(result.get(key), bool)
                or not 0 <= result[key] <= self.settings.batch_limit
                for key in _COUNT_KEYS
            ):
                raise RuntimeError("retention cycle counts exceeded their bound")
        remaining = int(value.get("remaining_candidates") or 0)
        if not 0 <= remaining <= self.settings.batch_limit * len(_COUNT_KEYS):
            raise RuntimeError("retention remaining count exceeded its bound")
        self.health.update(
            initialized=True, last_success_at=_iso(self.now()), consecutive_errors=0,
            remaining_candidates=remaining,
            backlog_remaining=value.get("backlog_remaining") is True,
            schema_contract_ok=True, legal_holds_evaluated=True,
            financial_ledger_preserved=True, evidence_contains_customer_content=False,
            missed_run_24h=False,
        )
        logger.info(json.dumps({
            "event": "retention_cycle_completed", "result": "success",
            "remaining_candidates": remaining,
            "backlog_remaining": value.get("backlog_remaining") is True,
        }, separators=(",", ":"), sort_keys=True))
        return value

    def run_until_drained(self) -> bool:
        for attempt in range(self.settings.max_cycles):
            if self.stop.is_set():
                return False
            for retry in range(self.settings.retries):
                try:
                    result = self.run_cycle()
                    if result.get("status") == "lease_unavailable":
                        delay = self.jitter(0.25, min(2.0, self.settings.retry_max_seconds))
                        if self.stop.wait(delay):
                            return False
                        continue
                    break
                except Exception as exc:
                    errors = self.health.snapshot()["consecutive_errors"] + 1
                    self.health.update(consecutive_errors=errors)
                    logger.warning(json.dumps({
                        "event": "retention_cycle_failed", "result": "error",
                        "error_type": type(exc).__name__, "consecutive_errors": errors,
                    }, separators=(",", ":"), sort_keys=True))
                    if retry + 1 == self.settings.retries:
                        return False
                    maximum = min(
                        self.settings.retry_max_seconds,
                        self.settings.retry_base_seconds * (2 ** retry),
                    )
                    if self.stop.wait(self.jitter(0.0, maximum)):
                        return False
            else:
                return False
            if not result.get("backlog_remaining"):
                return True
            if attempt + 1 < self.settings.max_cycles \
                    and self.stop.wait(self.jitter(0.05, 0.5)):
                return False
        return False

    def run_forever(self) -> None:
        self.health.update(running=True)
        while not self.stop.is_set():
            try:
                persisted = self.refresh_health()
            except Exception as exc:
                errors = self.health.snapshot()["consecutive_errors"] + 1
                self.health.update(consecutive_errors=errors)
                logger.warning(json.dumps({
                    "event": "retention_health_failed", "result": "error",
                    "error_type": type(exc).__name__, "consecutive_errors": errors,
                }, separators=(",", ":"), sort_keys=True))
                persisted = {"initialized": False, "missed_run_24h": True}
            now = self.now()
            catchup = persisted.get("initialized") is not True \
                or persisted.get("missed_run_24h") is True \
                or persisted.get("backlog_remaining") is True
            if catchup:
                drained = self.run_until_drained()
                if not drained and not self.stop.is_set():
                    retry_at = self.now() + timedelta(seconds=self.settings.backlog_retry_seconds)
                    self.health.update(next_scheduled_at=_iso(retry_at))
                    self.stop.wait(self.settings.backlog_retry_seconds)
                    continue
            next_run = _next_schedule(self.now(), self.settings)
            self.health.update(next_scheduled_at=_iso(next_run))
            self.stop.wait(max(0.0, (next_run - self.now()).total_seconds()))
        self.health.update(running=False)


class _Handler(BaseHTTPRequestHandler):
    health: Health

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/live", "/ready", "/metrics"}:
            self.send_error(404)
            return
        snapshot = self.health.snapshot()
        if self.path == "/metrics":
            metrics = {
                "brevitas_retention_worker_running": snapshot["running"],
                "brevitas_retention_worker_ready": self.health.ready(),
                "brevitas_retention_consecutive_errors": snapshot["consecutive_errors"],
                "brevitas_retention_remaining_candidates": snapshot["remaining_candidates"],
                "brevitas_retention_backlog_over_24h": snapshot["backlog_over_24h"],
                "brevitas_retention_missed_run_24h": snapshot["missed_run_24h"],
                "brevitas_retention_schema_contract_ok": snapshot["schema_contract_ok"],
                "brevitas_retention_legal_holds_evaluated": snapshot["legal_holds_evaluated"],
                "brevitas_retention_financial_ledger_preserved": snapshot["financial_ledger_preserved"],
            }
            body = "".join(f"{key} {int(value)}\n" for key, value in metrics.items()).encode()
            content_type = "text/plain; version=0.0.4"
            status = 200
        else:
            body = json.dumps({
                "schema": "brevitas.retention-worker-health.v1",
                **snapshot, "ready": self.health.ready(),
            }, separators=(",", ":"), sort_keys=True).encode()
            content_type = "application/json"
            status = 200 if self.path == "/live" or self.health.ready() else 503
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> int:
    settings = Settings.from_env()
    health = Health()
    worker = RetentionWorker(settings, RestRPC(settings), health=health)
    handler = type("RetentionHealthHandler", (_Handler,), {"health": health})
    server = ThreadingHTTPServer(("0.0.0.0", settings.health_port), handler)
    thread = threading.Thread(target=server.serve_forever, name="retention-health", daemon=True)
    thread.start()

    def shutdown(_signum: int, _frame: Any) -> None:
        worker.stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        worker.run_forever()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
