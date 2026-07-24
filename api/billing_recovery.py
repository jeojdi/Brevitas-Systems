"""Durable Stripe billing recovery for the Railway worker process.

Postgres owns claim, lease, cap, and terminal-state decisions. This module owns
only the bounded send/reconcile loop. Stripe identifiers and idempotency keys
are stable for a ledger id, and an ambiguous send is never blindly retried.

Integration from ``api.worker`` (owned by the worker runtime) is intentionally
small::

    processor = build_billing_recovery_processor_from_env()
    task = asyncio.create_task(run_billing_recovery_loop(processor, stop))

The same shutdown event used by the durable-job worker should be passed here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote

import requests


logger = logging.getLogger("brevitas.billing_recovery")
_STRIPE_BASE_URL = "https://api.stripe.com"
_SUPABASE_RPC_PREFIX = "/rest/v1/rpc/"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class BillingEntry:
    id: int
    user_id: str
    occurred_at: datetime
    fee_microusd: int
    stripe_customer_id: str
    attempts: int
    reclaimed: bool
    outbound_started_at: datetime | None
    period_start: datetime
    period_end: datetime
    expected_period_microusd: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "BillingEntry":
        required_dates = {
            name: _parse_datetime(row.get(name))
            for name in ("occurred_at", "period_start", "period_end")
        }
        if any(value is None for value in required_dates.values()):
            raise ValueError("billing claim omitted a required timestamp")
        entry = cls(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            occurred_at=required_dates["occurred_at"],  # type: ignore[arg-type]
            fee_microusd=int(row["fee_microusd"]),
            stripe_customer_id=str(row["stripe_customer_id"]),
            attempts=int(row["attempts"]),
            reclaimed=bool(row["reclaimed"]),
            outbound_started_at=_parse_datetime(row.get("outbound_started_at")),
            period_start=required_dates["period_start"],  # type: ignore[arg-type]
            period_end=required_dates["period_end"],  # type: ignore[arg-type]
            expected_period_microusd=int(row["expected_period_microusd"]),
        )
        if entry.id <= 0 or entry.fee_microusd < 0 or not entry.stripe_customer_id:
            raise ValueError("billing claim contains invalid identifiers or amount")
        return entry

    @property
    def stripe_identifier(self) -> str:
        return f"brevitas-fee-{self.id}"

    @property
    def idempotency_key(self) -> str:
        return f"brevitas-meter-{self.id}"


@dataclass(frozen=True)
class BillingHealth:
    pending_count: int = 0
    review_count: int = 0
    dead_count: int = 0
    stale_sending_count: int = 0
    oldest_pending_seconds: int = 0

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "BillingHealth":
        row = row or {}
        return cls(**{
            name: int(row.get(name) or 0)
            for name in cls.__dataclass_fields__
        })


@dataclass
class BillingRunResult:
    claimed: int = 0
    reported: int = 0
    reconciled: int = 0
    review: int = 0
    dead: int = 0
    lease_lost: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class BillingLoopHealth:
    """Bounded, content-free readiness snapshot for the Railway worker."""

    running: bool
    initial_validation_succeeded: bool
    catalog_valid: bool
    last_success_monotonic: float | None
    last_error_monotonic: float | None
    consecutive_errors: int


class Reconciliation(str, Enum):
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"


class StripeRejected(RuntimeError):
    """Stripe definitively rejected a meter event; retrying cannot repair it."""


class StripeAmbiguous(RuntimeError):
    """Stripe may have accepted a request whose response was not observed."""


class CatalogContractError(RuntimeError):
    """Global Price/Meter configuration is invalid; no customer row is at fault."""


class BillingStore(Protocol):
    def claim_one(
        self, owner: str, *, lease_seconds: int, cap_microusd: int,
    ) -> BillingEntry | None: ...

    def begin_send(self, entry_id: int, owner: str) -> bool: ...

    def renew(self, entry_id: int, owner: str, lease_seconds: int) -> bool: ...

    def complete(self, entry_id: int, owner: str, status: str, error: str = "") -> bool: ...

    def release_owner(self, owner: str) -> int: ...

    def check_health(self) -> BillingHealth: ...


class StripeUsageGateway(Protocol):
    def validate_contract(self, heartbeat: Callable[[], bool] | None = None) -> str: ...

    def send(self, entry: BillingEntry) -> None: ...

    def reconcile(
        self,
        entry: BillingEntry,
        heartbeat: Callable[[], bool] | None = None,
    ) -> Reconciliation: ...

    def close(self) -> None: ...


class BillingTelemetry(Protocol):
    def metric(self, name: str, value: float, attributes: Mapping[str, str] | None = None) -> None: ...

    def alert(self, name: str, severity: str, fields: Mapping[str, int]) -> None: ...


class LoggingBillingTelemetry:
    """Content-free integration point for OpenTelemetry/monitoring adapters."""

    def metric(self, name: str, value: float, attributes: Mapping[str, str] | None = None) -> None:
        logger.info(json.dumps({
            "event": "billing_metric",
            "metric": name,
            "value": value,
            "attributes": dict(attributes or {}),
        }, separators=(",", ":"), sort_keys=True))

    def alert(self, name: str, severity: str, fields: Mapping[str, int]) -> None:
        logger.warning(json.dumps({
            "event": "billing_alert",
            "alert": name,
            "severity": severity,
            "fields": dict(fields),
        }, separators=(",", ":"), sort_keys=True))


class SupabaseBillingStore:
    """Service-role adapter over narrowly scoped Postgres RPC functions."""

    def __init__(
        self,
        url: str,
        service_role_key: str,
        *,
        timeout_seconds: float = 10.0,
        session: requests.Session | None = None,
    ):
        if not url.startswith("https://") and not url.startswith("http://localhost"):
            raise ValueError("Supabase billing URL must use HTTPS")
        if not service_role_key:
            raise ValueError("Supabase service role key is required")
        self.url = url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update({
            "apikey": service_role_key,
            "authorization": f"Bearer {service_role_key}",
            "content-type": "application/json",
        })

    def _rpc(self, function: str, payload: Mapping[str, Any]) -> Any:
        response = self.session.post(
            f"{self.url}{_SUPABASE_RPC_PREFIX}{quote(function, safe='')}",
            json=dict(payload),
            timeout=(min(3.0, self.timeout_seconds), self.timeout_seconds),
        )
        response.raise_for_status()
        return response.json()

    def claim_one(
        self, owner: str, *, lease_seconds: int, cap_microusd: int,
    ) -> BillingEntry | None:
        rows = self._rpc("claim_billing_ledger_entries", {
            "p_owner": owner,
            "p_lease_seconds": lease_seconds,
            "p_limit": 1,
            "p_cap_microusd": cap_microusd,
        })
        if not isinstance(rows, list):
            raise RuntimeError("billing claim returned an invalid response")
        if len(rows) > 1:
            raise RuntimeError("billing claim returned more than one row")
        return BillingEntry.from_row(rows[0]) if rows else None

    def begin_send(self, entry_id: int, owner: str) -> bool:
        return bool(self._rpc("mark_billing_outbound_started", {
            "p_entry_id": entry_id,
            "p_owner": owner,
        }))

    def renew(self, entry_id: int, owner: str, lease_seconds: int) -> bool:
        return bool(self._rpc("renew_billing_ledger_lease", {
            "p_entry_id": entry_id,
            "p_owner": owner,
            "p_lease_seconds": lease_seconds,
        }))

    def complete(self, entry_id: int, owner: str, status: str, error: str = "") -> bool:
        return bool(self._rpc("complete_billing_ledger_entry", {
            "p_entry_id": entry_id,
            "p_owner": owner,
            "p_status": status,
            "p_error": error[:500],
        }))

    def release_owner(self, owner: str) -> int:
        return int(self._rpc("release_billing_ledger_leases", {"p_owner": owner}) or 0)

    def check_health(self) -> BillingHealth:
        rows = self._rpc("billing_recovery_health", {})
        row = rows[0] if isinstance(rows, list) and rows else rows
        return BillingHealth.from_row(row if isinstance(row, Mapping) else None)

    def close(self) -> None:
        self.session.close()


class StripeRestBillingGateway:
    """Bounded Stripe REST adapter with conservative aggregate reconciliation.

    Stripe does not expose individual v1 meter events after ingestion. The
    only safe positive reconciliation is when its customer/period aggregate
    exactly equals the Postgres total expected at claim time. Every other
    result remains unknown and is sent to review rather than guessed.
    """

    def __init__(
        self,
        secret_key: str,
        price_id: str,
        event_name: str,
        *,
        timeout_seconds: float = 10.0,
        exclusive_meter_writer: bool = False,
        max_reconciliation_pages: int = 20,
        catalog_cache_seconds: float = 300.0,
        session: requests.Session | None = None,
        now: Callable[[], datetime] = _utcnow,
    ):
        if not secret_key or not price_id or not event_name:
            raise ValueError("Stripe billing configuration is incomplete")
        self.price_id = price_id
        self.event_name = event_name
        self.timeout_seconds = timeout_seconds
        self.exclusive_meter_writer = exclusive_meter_writer
        self.max_reconciliation_pages = max(1, min(100, max_reconciliation_pages))
        self.catalog_cache_seconds = max(1.0, min(900.0, catalog_cache_seconds))
        self.session = session or requests.Session()
        self.session.auth = (secret_key, "")
        self.session.headers.update({"user-agent": "Brevitas-Billing-Recovery/1.0"})
        self._meter_id: str | None = None
        self._catalog_error: str | None = None
        self._catalog_validated_at = 0.0
        self._catalog_lock = threading.Lock()
        self._now = now

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(
                method,
                f"{_STRIPE_BASE_URL}{path}",
                timeout=(min(3.0, self.timeout_seconds), self.timeout_seconds),
                **kwargs,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise StripeAmbiguous("Stripe request outcome is unknown") from exc
        if response.status_code >= 500 or response.status_code in (408, 409, 429):
            raise StripeAmbiguous(f"Stripe returned retryable status {response.status_code}")
        if response.status_code >= 400:
            try:
                error_type = str(response.json().get("error", {}).get("type", ""))
            except (ValueError, AttributeError):
                error_type = ""
            if error_type == "idempotency_error":
                raise StripeAmbiguous("Stripe idempotency result requires reconciliation")
            raise StripeRejected(f"Stripe rejected meter event with status {response.status_code}")
        return response

    def send(self, entry: BillingEntry) -> None:
        # Defense in depth for callers other than BillingRecoveryProcessor.
        # The processor already validates before its durable outbound marker,
        # so this resolves from the short cache during the normal path.
        self.validate_contract()
        self._request(
            "POST",
            "/v1/billing/meter_events",
            data={
                "event_name": self.event_name,
                "identifier": entry.stripe_identifier,
                "timestamp": str(int(entry.occurred_at.timestamp())),
                "payload[stripe_customer_id]": entry.stripe_customer_id,
                "payload[value]": str(entry.fee_microusd),
            },
            headers={"Idempotency-Key": entry.idempotency_key},
        )

    @staticmethod
    def _heartbeat(heartbeat: Callable[[], bool] | None) -> None:
        if heartbeat is not None and not heartbeat():
            raise StripeAmbiguous("billing lease was lost during reconciliation")

    def validate_contract(self, heartbeat: Callable[[], bool] | None = None) -> str:
        """Validate and briefly cache the configured Price→Meter contract."""
        with self._catalog_lock:
            cache_fresh = (
                time.monotonic() - self._catalog_validated_at < self.catalog_cache_seconds
            )
            if cache_fresh and self._catalog_error:
                self._heartbeat(heartbeat)
                raise CatalogContractError(self._catalog_error)
            if cache_fresh and self._meter_id:
                self._heartbeat(heartbeat)
                return self._meter_id
            self._heartbeat(heartbeat)
            try:
                response = self._request("GET", f"/v1/prices/{quote(self.price_id, safe='')}")
            except StripeRejected as exc:
                self._catalog_error = "Stripe Price is unavailable"
                self._catalog_validated_at = time.monotonic()
                raise CatalogContractError(self._catalog_error) from exc
            try:
                price = response.json()
                recurring = price["recurring"]
                meter_id = recurring["meter"]
            except (ValueError, KeyError, TypeError) as exc:
                self._catalog_error = "Stripe Price is not attached to a billing meter"
                self._catalog_validated_at = time.monotonic()
                raise CatalogContractError(self._catalog_error) from exc
            if (
                not isinstance(meter_id, str) or not meter_id
                or price.get("active") is not True
                or price.get("type") != "recurring"
                or price.get("currency") != "usd"
                or price.get("billing_scheme") != "per_unit"
                or str(price.get("unit_amount_decimal")) != "0.0001"
                or recurring.get("interval") != "week"
                or recurring.get("usage_type") != "metered"
            ):
                self._catalog_error = "Stripe Price violates the billing meter contract"
                self._catalog_validated_at = time.monotonic()
                raise CatalogContractError(self._catalog_error)
            self._heartbeat(heartbeat)
            try:
                meter_response = self._request(
                    "GET", f"/v1/billing/meters/{quote(meter_id, safe='')}",
                )
            except StripeRejected as exc:
                self._catalog_error = "Stripe meter is unavailable"
                self._catalog_validated_at = time.monotonic()
                raise CatalogContractError(self._catalog_error) from exc
            try:
                meter = meter_response.json()
                valid_meter = (
                    meter.get("status") == "active"
                    and meter.get("event_name") == self.event_name
                    and meter["default_aggregation"]["formula"] == "sum"
                    and meter["customer_mapping"]["type"] == "by_id"
                    and meter["customer_mapping"]["event_payload_key"] == "stripe_customer_id"
                    and meter["value_settings"]["event_payload_key"] == "value"
                )
            except (ValueError, KeyError, TypeError) as exc:
                self._catalog_error = "Stripe meter contract is invalid"
                self._catalog_validated_at = time.monotonic()
                raise CatalogContractError(self._catalog_error) from exc
            if not valid_meter:
                self._catalog_error = "Stripe meter contract is invalid"
                self._catalog_validated_at = time.monotonic()
                raise CatalogContractError(self._catalog_error)
            self._meter_id = meter_id
            self._catalog_error = None
            self._catalog_validated_at = time.monotonic()
            return meter_id

    def reconcile(
        self,
        entry: BillingEntry,
        heartbeat: Callable[[], bool] | None = None,
    ) -> Reconciliation:
        meter_id = self.validate_contract(heartbeat)
        start = int(entry.period_start.timestamp())
        start -= start % 60
        end = min(int(entry.period_end.timestamp()), int(self._now().timestamp()))
        end -= end % 60
        if end <= start:
            return Reconciliation.UNKNOWN
        params: dict[str, Any] = {
                "customer": entry.stripe_customer_id,
                "start_time": start,
                "end_time": end,
                "limit": 100,
        }
        remote_total = Decimal(0)
        seen_cursors: set[str] = set()
        for page_number in range(1, self.max_reconciliation_pages + 1):
            self._heartbeat(heartbeat)
            response = self._request(
                "GET",
                f"/v1/billing/meters/{quote(meter_id, safe='')}/event_summaries",
                params=params,
            )
            try:
                payload = response.json()
                rows = payload["data"]
                has_more = payload["has_more"]
                if not isinstance(rows, list) or not isinstance(has_more, bool):
                    raise TypeError("invalid Stripe list response")
                for row in rows:
                    if row.get("meter") != meter_id:
                        raise ValueError("Stripe summary belongs to another meter")
                    remote_total += Decimal(str(row["aggregated_value"]))
            except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
                raise StripeAmbiguous("Stripe reconciliation response is invalid") from exc
            if not has_more:
                break
            if not rows:
                raise StripeAmbiguous("Stripe reconciliation pagination did not advance")
            cursor = rows[-1].get("id")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise StripeAmbiguous("Stripe reconciliation pagination cursor is invalid")
            seen_cursors.add(cursor)
            params["starting_after"] = cursor
        else:
            # A positive reconciliation requires consuming every summary. At
            # the configured resource bound we remain unknown, never accepted.
            raise StripeAmbiguous("Stripe reconciliation exceeded the page limit")
        return (
            Reconciliation.ACCEPTED
            if self.exclusive_meter_writer
            and remote_total == Decimal(entry.expected_period_microusd)
            else Reconciliation.UNKNOWN
        )

    def close(self) -> None:
        self.session.close()


@dataclass(frozen=True)
class BillingRecoverySettings:
    lease_seconds: int = 120
    poll_seconds: float = 5.0
    cap_microusd: int = 100_000_000
    lag_alert_seconds: int = 300
    review_alert_count: int = 1
    dead_alert_count: int = 1

    @classmethod
    def from_environment(cls) -> "BillingRecoverySettings":
        cap_usd = _bounded_float("BREVITAS_BILLING_WEEKLY_CAP_USD", 0, 0.01, 100_000)
        return cls(
            lease_seconds=_bounded_int("BREVITAS_BILLING_LEASE_SECONDS", 120, 15, 900),
            poll_seconds=_bounded_float("BREVITAS_BILLING_POLL_SECONDS", 5, 1, 60),
            cap_microusd=int(Decimal(str(cap_usd)) * Decimal(1_000_000)),
            lag_alert_seconds=_bounded_int("BREVITAS_BILLING_LAG_ALERT_SECONDS", 300, 60, 86_400),
            review_alert_count=_bounded_int("BREVITAS_BILLING_REVIEW_ALERT_COUNT", 1, 1, 1_000_000),
            dead_alert_count=_bounded_int("BREVITAS_BILLING_DEAD_ALERT_COUNT", 1, 1, 1_000_000),
        )


@dataclass
class BillingRecoveryProcessor:
    store: BillingStore
    stripe: StripeUsageGateway
    settings: BillingRecoverySettings
    telemetry: BillingTelemetry = field(default_factory=LoggingBillingTelemetry)
    now: Callable[[], datetime] = _utcnow
    catalog_contract_valid: bool = field(default=True, init=False)

    def _catalog_failure(self) -> None:
        self.catalog_contract_valid = False
        self.telemetry.metric("billing.catalog_contract_valid", 0)
        self.telemetry.alert("billing_catalog_contract_invalid", "page", {
            "catalog_contract_valid": 0,
        })

    def _complete(
        self, result: BillingRunResult, entry: BillingEntry, owner: str,
        status: str, error: str = "",
    ) -> bool:
        if not self.store.complete(entry.id, owner, status, error):
            result.lease_lost += 1
            result.errors += 1
            self.telemetry.metric("billing.lease_lost", 1)
            return False
        setattr(result, status, getattr(result, status) + 1)
        self.telemetry.metric("billing.entries", 1, {"status": status})
        return True

    def _reconcile(
        self,
        result: BillingRunResult,
        entry: BillingEntry,
        owner: str,
        heartbeat: Callable[[], bool],
    ) -> bool:
        try:
            reconciled = self.stripe.reconcile(entry, heartbeat)
        except CatalogContractError:
            self._catalog_failure()
            result.errors += 1
            reconciled = Reconciliation.UNKNOWN
        except (StripeAmbiguous, StripeRejected, requests.RequestException):
            result.errors += 1
            reconciled = Reconciliation.UNKNOWN
        if reconciled is not Reconciliation.ACCEPTED:
            return False
        if self._complete(result, entry, owner, "reported", "reconciled with Stripe aggregate"):
            result.reconciled += 1
        return True

    def _process_entry(
        self,
        result: BillingRunResult,
        entry: BillingEntry,
        owner: str,
        should_stop: Callable[[], bool],
    ) -> None:
        def heartbeat() -> bool:
            return not should_stop() and self.store.renew(
                entry.id, owner, self.settings.lease_seconds,
            )

        # A claim-only crash is safe to send: no outbound request started. Only
        # reclaimed rows with an outbound marker have an ambiguous Stripe state.
        if (
            entry.reclaimed
            and entry.outbound_started_at is not None
            and self._reconcile(result, entry, owner, heartbeat)
        ):
            return
        if entry.reclaimed and entry.outbound_started_at is not None:
            age = (self.now() - entry.outbound_started_at).total_seconds()
            if age >= 23 * 3600:
                self._complete(
                    result, entry, owner, "review",
                    "ambiguous Stripe send exceeded safe replay window",
                )
                return

        if should_stop():
            # release_owner is safe here because this row has no outbound marker.
            if entry.outbound_started_at is None:
                self.store.release_owner(owner)
            return
        try:
            # Catalog validation is lease-covered and precedes both the durable
            # outbound marker and Stripe's meter-event POST. The short cache is
            # keyed by this configured gateway's immutable Price/event pair.
            self.stripe.validate_contract(heartbeat)
            self.catalog_contract_valid = True
            self.telemetry.metric("billing.catalog_contract_valid", 1)
        except CatalogContractError as exc:
            self._catalog_failure()
            result.errors += 1
            if entry.outbound_started_at is None:
                # Global recoverable configuration error: the customer row is
                # untouched and can retry after operators repair the catalog.
                self.store.release_owner(owner)
            else:
                # A prior outbound request remains ambiguous. Fence it in
                # review; never dead-letter it for today's global config.
                self._complete(result, entry, owner, "review", str(exc))
            return
        except StripeRejected as exc:
            self._complete(result, entry, owner, "dead", str(exc))
            return
        except (StripeAmbiguous, requests.RequestException):
            result.errors += 1
            self.store.release_owner(owner)
            self.telemetry.metric("billing.catalog_validation_error", 1)
            return
        if not heartbeat():
            result.lease_lost += 1
            self.telemetry.metric("billing.lease_lost", 1)
            return
        if not self.store.begin_send(entry.id, owner):
            result.lease_lost += 1
            self.telemetry.metric("billing.lease_lost", 1)
            return
        try:
            # Reclaimed sends use the same event identifier and idempotency key.
            # Stripe enforces identifier uniqueness for at least 24 hours.
            self.stripe.send(entry)
        except CatalogContractError as exc:
            self._catalog_failure()
            result.errors += 1
            self._complete(result, entry, owner, "review", str(exc))
        except StripeRejected as exc:
            result.errors += 1
            self._complete(result, entry, owner, "dead", str(exc))
        except (StripeAmbiguous, requests.Timeout, requests.ConnectionError, TimeoutError) as exc:
            result.errors += 1
            if not self._reconcile(result, entry, owner, heartbeat):
                self._complete(result, entry, owner, "review", str(exc) or "ambiguous Stripe outcome")
        except Exception as exc:
            # Unknown failures after begin_send are ambiguous by definition.
            result.errors += 1
            if not self._reconcile(result, entry, owner, heartbeat):
                self._complete(
                    result, entry, owner, "review",
                    f"unexpected ambiguous error: {type(exc).__name__}",
                )
        else:
            self._complete(result, entry, owner, "reported")

    def process_once(
        self,
        owner: str,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> BillingRunResult:
        started = time.monotonic()
        entry = None if should_stop() else self.store.claim_one(
            owner,
            lease_seconds=self.settings.lease_seconds,
            cap_microusd=self.settings.cap_microusd,
        )
        result = BillingRunResult(claimed=int(entry is not None))
        if entry is not None:
            self._process_entry(result, entry, owner, should_stop)
        self.telemetry.metric("billing.batch.claimed", result.claimed)
        self.telemetry.metric("billing.batch.duration_ms", (time.monotonic() - started) * 1000)
        return result

    def check_health(self) -> BillingHealth:
        health = self.store.check_health()
        for name in BillingHealth.__dataclass_fields__:
            self.telemetry.metric(f"billing.{name}", float(getattr(health, name)))
        self.telemetry.metric(
            "billing.catalog_contract_valid", float(self.catalog_contract_valid),
        )
        if not self.catalog_contract_valid:
            self.telemetry.alert("billing_catalog_contract_invalid", "page", {
                "catalog_contract_valid": 0,
            })
        if health.oldest_pending_seconds >= self.settings.lag_alert_seconds:
            self.telemetry.alert("billing_processing_lag", "page", {
                "oldest_pending_seconds": health.oldest_pending_seconds,
                "pending_count": health.pending_count,
            })
        if health.review_count >= self.settings.review_alert_count:
            self.telemetry.alert("billing_entries_require_review", "ticket", {
                "review_count": health.review_count,
            })
        if health.dead_count >= self.settings.dead_alert_count:
            self.telemetry.alert("billing_entries_dead", "page", {
                "dead_count": health.dead_count,
            })
        if health.stale_sending_count:
            self.telemetry.alert("billing_stale_leases", "page", {
                "stale_sending_count": health.stale_sending_count,
            })
        return health


def billing_recovery_is_configured() -> bool:
    enabled = os.getenv("BREVITAS_BILLING_ENABLED", "") == "true"
    return enabled and all(os.getenv(name) for name in (
        "STRIPE_SECRET_KEY",
        "STRIPE_PRICE_ID",
        "STRIPE_METER_EVENT_NAME",
        "BREVITAS_BILLING_WEEKLY_CAP_USD",
        "SUPABASE_SERVICE_ROLE_KEY",
    )) and bool(os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL"))


def build_billing_recovery_processor_from_env(
    *, telemetry: BillingTelemetry | None = None,
) -> BillingRecoveryProcessor:
    if not billing_recovery_is_configured():
        raise RuntimeError("billing recovery environment is incomplete")
    settings = BillingRecoverySettings.from_environment()
    timeout = _bounded_float("BREVITAS_BILLING_HTTP_TIMEOUT_SECONDS", 10, 1, 30)
    if settings.lease_seconds < timeout * 3 + 5:
        raise RuntimeError(
            "BREVITAS_BILLING_LEASE_SECONDS must cover send and reconciliation timeouts"
        )
    store = SupabaseBillingStore(
        os.getenv("SUPABASE_URL") or os.environ["NEXT_PUBLIC_SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        timeout_seconds=timeout,
    )
    stripe = StripeRestBillingGateway(
        os.environ["STRIPE_SECRET_KEY"],
        os.environ["STRIPE_PRICE_ID"],
        os.environ["STRIPE_METER_EVENT_NAME"],
        timeout_seconds=timeout,
        exclusive_meter_writer=(
            os.getenv("BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER", "").lower() == "true"
        ),
        max_reconciliation_pages=_bounded_int(
            "BREVITAS_BILLING_RECONCILIATION_MAX_PAGES", 20, 1, 100,
        ),
    )
    return BillingRecoveryProcessor(
        store=store,
        stripe=stripe,
        settings=settings,
        telemetry=telemetry or LoggingBillingTelemetry(),
    )


def billing_worker_owner(
    prefix: str | None = None,
    *,
    hostname: str | None = None,
    entropy: str | None = None,
) -> str:
    """Return a unique, non-secret lease owner for this process/replica.

    A configured worker id is only a human-readable prefix. Host and fresh
    entropy are always appended so two Railway replicas can never share a
    fencing identity.
    """
    configured = prefix or os.getenv("BREVITAS_BILLING_WORKER_ID") or "billing"
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]", "_", configured)[:32] or "billing"
    safe_host = re.sub(
        r"[^A-Za-z0-9._-]", "_", hostname or socket.gethostname(),
    )[:32] or "host"
    unique = re.sub(r"[^A-Za-z0-9]", "", entropy or uuid.uuid4().hex)[:24]
    if len(unique) < 12:
        raise ValueError("billing worker entropy must contain at least 12 safe characters")
    return f"{safe_prefix}_{safe_host}_{os.getpid()}_{unique}"


async def _run_thread_call_safely(
    call: Callable[[], Any],
    *,
    on_cancel: Callable[[], None] | None = None,
) -> Any:
    """Finish an in-flight thread before allowing cancellation to unwind.

    ``asyncio.to_thread`` cannot stop a running HTTP call. Shielding and then
    joining it prevents the loop from closing its requests sessions while that
    thread is still using them. Repeated cancellation remains deferred until
    the bounded call returns.
    """
    task = asyncio.create_task(asyncio.to_thread(call))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            value = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            if on_cancel is not None:
                on_cancel()
    if cancellation is not None:
        raise cancellation
    return value


async def run_billing_recovery_loop(
    processor: BillingRecoveryProcessor,
    stop: asyncio.Event,
    *,
    owner: str | None = None,
    health_reporter: Callable[[BillingLoopHealth], None] | None = None,
) -> None:
    """Run bounded billing cycles and publish content-free readiness state."""
    owner = billing_worker_owner(owner)
    snapshot = BillingLoopHealth(
        running=True,
        initial_validation_succeeded=False,
        catalog_valid=False,
        last_success_monotonic=None,
        last_error_monotonic=None,
        consecutive_errors=0,
    )

    def report() -> None:
        if health_reporter is None:
            return
        try:
            health_reporter(snapshot)
        except Exception as exc:
            # The readiness adapter is observational and cannot stop billing.
            logger.error(
                "billing health reporter failed error_type=%s", type(exc).__name__,
            )

    def record_error(*, catalog_valid: bool | None = None) -> None:
        nonlocal snapshot
        snapshot = replace(
            snapshot,
            catalog_valid=(snapshot.catalog_valid if catalog_valid is None else catalog_valid),
            last_error_monotonic=time.monotonic(),
            consecutive_errors=min(snapshot.consecutive_errors + 1, 1_000_000),
        )
        report()

    def record_success(*, initial: bool = False) -> None:
        nonlocal snapshot
        snapshot = replace(
            snapshot,
            initial_validation_succeeded=(
                snapshot.initial_validation_succeeded or initial
            ),
            catalog_valid=processor.catalog_contract_valid,
            last_success_monotonic=time.monotonic(),
            consecutive_errors=0,
        )
        report()

    report()
    try:
        while not stop.is_set():
            if not snapshot.initial_validation_succeeded:
                try:
                    await _run_thread_call_safely(
                        processor.stripe.validate_contract,
                        on_cancel=stop.set,
                    )
                    processor.catalog_contract_valid = True
                except CatalogContractError:
                    processor.catalog_contract_valid = False
                    processor._catalog_failure()
                    record_error(catalog_valid=False)
                except Exception as exc:
                    processor.catalog_contract_valid = False
                    logger.error(
                        "billing initial catalog validation failed error_type=%s",
                        type(exc).__name__,
                    )
                    record_error(catalog_valid=False)
                else:
                    try:
                        await _run_thread_call_safely(
                            processor.store.check_health,
                            on_cancel=stop.set,
                        )
                    except Exception as exc:
                        logger.error(
                            "billing initial store validation failed error_type=%s",
                            type(exc).__name__,
                        )
                        record_error(catalog_valid=True)
                    else:
                        record_success(initial=True)
                if not snapshot.initial_validation_succeeded:
                    try:
                        await asyncio.wait_for(
                            stop.wait(), timeout=processor.settings.poll_seconds,
                        )
                    except TimeoutError:
                        pass
                    continue
                if stop.is_set():
                    continue

            result = BillingRunResult()
            cycle_failed = False
            try:
                result = await _run_thread_call_safely(
                    lambda: processor.process_once(owner, stop.is_set),
                    on_cancel=stop.set,
                )
            except Exception as exc:
                logger.error(json.dumps({
                    "event": "billing_loop_error",
                    "error_type": type(exc).__name__,
                }, separators=(",", ":")))
                cycle_failed = True

            # Idle cycles must revalidate both dependencies too. The Stripe
            # gateway cache avoids network I/O until its bounded TTL expires.
            try:
                await _run_thread_call_safely(
                    processor.stripe.validate_contract,
                    on_cancel=stop.set,
                )
                processor.catalog_contract_valid = True
                processor.telemetry.metric("billing.catalog_contract_valid", 1)
            except CatalogContractError:
                processor.catalog_contract_valid = False
                processor._catalog_failure()
                cycle_failed = True
            except Exception as exc:
                processor.catalog_contract_valid = False
                processor.telemetry.metric("billing.catalog_contract_valid", 0)
                logger.error(
                    "billing catalog health validation failed error_type=%s",
                    type(exc).__name__,
                )
                cycle_failed = True

            try:
                await _run_thread_call_safely(
                    processor.check_health,
                    on_cancel=stop.set,
                )
            except Exception as exc:
                logger.error(
                    "billing store health validation failed error_type=%s",
                    type(exc).__name__,
                )
                cycle_failed = True

            if result.errors:
                cycle_failed = True
            if cycle_failed:
                record_error(catalog_valid=processor.catalog_contract_valid)
            else:
                record_success()
            delay = (
                0.05
                if result.claimed and not cycle_failed
                else processor.settings.poll_seconds
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
    finally:
        # Every processed send is terminal before process_once returns. This RPC
        # releases only claimed rows whose outbound request never began.
        try:
            await _run_thread_call_safely(
                lambda: processor.store.release_owner(owner),
                on_cancel=stop.set,
            )
        except Exception as exc:
            logger.error("billing lease release failed error_type=%s", type(exc).__name__)
            record_error()
        try:
            processor.stripe.close()
        except Exception as exc:
            logger.error("billing Stripe close failed error_type=%s", type(exc).__name__)
            record_error()
        finally:
            close_store = getattr(processor.store, "close", None)
            if callable(close_store):
                try:
                    close_store()
                except Exception as exc:
                    logger.error("billing store close failed error_type=%s", type(exc).__name__)
                    record_error()
            snapshot = replace(snapshot, running=False)
            report()
