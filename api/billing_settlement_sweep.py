"""The settlement CALLER: the missing link between a closed billing week and a
draft settlement a human can read.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
=============================================
Two RPCs have existed since 202607280013 with no caller anywhere in the tree:

  * ``public.billing_periods_awaiting_settlement(integer)`` — closed Stripe weeks
    (up to four back) that have no live settlement.
  * ``public.settle_billing_period(uuid, timestamptz, text, boolean)`` — computes
    ONE settlement for the closed week containing an anchor and writes it as
    ``status = 'draft'``.

This module is the loop that joins them, and NOTHING else. In particular it does
not, and structurally cannot:

  * promote anything. ``promote_billing_period_settlement`` is
    ``revoke all ... from public, anon, authenticated, service_role``
    (202607280013:981-983). Draft -> pending is the deliberate manual act that
    makes a period billable ("runtime may COMPUTE a settlement, only a human may
    BILL one", 202607280013:9-23). The promoter's name does not appear in this
    file, and tests/test_billing_settlement_sweep.py fails the build if it ever
    does. This module ships with NO new GRANT.
  * revise anything. ``p_allow_revision`` is the module constant
    :data:`ALLOW_REVISION`, hardcoded ``False``. A period that already has a
    settlement comes back ``period_already_settled`` and is left alone;
    appending a revision stays an operator act (202607280013 step 10).
  * decide any money. ``settle_billing_period`` takes no fee, status or evidence
    argument. Every number is derived inside the writer from
    ``billing_period_settlement_evidence`` and capped by the halting-condition
    ceiling, and ``assert_billing_period_settlement_allowed`` — which runs the
    attestation gate, the relative ceiling, the cumulative weekly cap and the
    zero-spend conditions — is evaluated in the writer's own transaction before
    any write. There is no argument this caller could pass to skip any of it.

OFF BY DEFAULT
==============
:data:`SWEEP_ENABLED_ENV` is compared with ``== "true"`` — byte for byte the same
exact-match test as ``BREVITAS_BILLING_ENABLED``
(api/billing_recovery.py:billing_recovery_is_configured) and
``BREVITAS_BILLING_SETTLEMENT_ENABLED``. Unset, empty, ``"1"``, ``"True"`` and
``"TRUE"`` all mean OFF. Deploying this file therefore changes nothing about any
running worker until somebody sets one variable on purpose.

A HALT IS AN OUTCOME, NOT AN ERROR
==================================
``settle_billing_period`` answers a halting condition with
``{"ok": false, "outcome": "halted", "halting_condition": "<name>"}`` and a
RETURN — not a raise (see its step 11, which catches SQLSTATE 55000/22023 from
the guard and converts it). Halts are counted and logged as
``billing_settlement_sweep_halted`` and are NOT retried, NOT counted as errors,
and do NOT make the cycle unhealthy. Retrying a halt would just re-run the same
deterministic guard against the same evidence; the fix is human.

IDEMPOTENCE ACROSS REPLICAS
===========================
No new lock is invented here. The fence is the one the writer already takes:
``pg_advisory_xact_lock(hashtextextended(p_organization_id::text, 0))``
(202607280013:399, live body 202607280031:1311), the SAME advisory key the
per-row claim path uses. Two Railway replicas that enumerate the same candidate
serialise on it, and the loser then falls into one of two writer branches, both
of which write nothing:

  * evidence unchanged -> step 8 returns ``unchanged`` with the winner's
    settlement id;
  * evidence moved -> step 10 returns ``period_already_settled`` because
    :data:`ALLOW_REVISION` is False.

Exactly one draft row exists either way. scripts/ci/migration-settlement-writer-
assertions.sql §6 proves both branches against a real database (one row,
22,500,000 microUSD, ``computed_by`` still naming the first call), and §12 proves
the enumerator stops offering a period the moment a live settlement exists, so an
hourly sweep converges.

WHAT IT WILL DO IN PRODUCTION TODAY
===================================
Nothing, for a while, and provably so: the one paying organization's
``billing_started_at`` equals its ``current_period_start``, so every back-offset
window starts before enrollment and ``billing_periods_awaiting_settlement``
returns zero rows until that anchor's next period closes. Beyond that, evidence
sums only ``authoritative and pricing_status = 'priced'`` usage, of which
production currently has none. A no-op sweep is a free soak, not a defect.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from .billing_recovery import SupabaseRpcClient, _bounded_float, _bounded_int


logger = logging.getLogger("brevitas.billing_settlement_sweep")

#: The one variable that arms this loop. Exact-"true" only; see the module
#: docstring. Named here rather than inlined so the tests can assert the
#: comparison and the default together.
SWEEP_ENABLED_ENV = "BREVITAS_BILLING_SETTLEMENT_SWEEP_ENABLED"

#: Hardcoded, never parameterised, never read from the environment. An automated
#: caller that could pass True would be able to supersede a settled period on its
#: own authority; 202607280013 step 10 exists precisely to make that an operator
#: act. tests/test_billing_settlement_sweep.py asserts both this value and that
#: the RPC payload carries it.
ALLOW_REVISION = False

#: settle_billing_period's ``computed_by`` is written verbatim into what
#: 202607280007 calls a seven-year financial record, and the writer refuses a
#: blank one. Prefixing the per-replica lease owner makes every draft traceable
#: to the process that produced it.
COMPUTED_BY_PREFIX = "sweep"
_COMPUTED_BY_MAX = 200

#: The writer's own vocabulary, mirrored so a code the caller has never seen is
#: loud rather than silently bucketed. tests/test_billing_settlement_sweep.py
#: greps 202607280031/202607280013 and fails if the writer grows an outcome that
#: is missing here.
OUTCOME_SETTLED = "settled"
OUTCOME_REVISED = "revised"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_BLOCKED = "blocked"
OUTCOME_HALTED = "halted"
KNOWN_OUTCOMES = frozenset({
    OUTCOME_SETTLED, OUTCOME_REVISED, OUTCOME_UNCHANGED,
    OUTCOME_BLOCKED, OUTCOME_HALTED,
})


@dataclass
class SettlementSweepResult:
    """One cycle, in counts. ``errors`` is the only unhealthy field.

    ``blocked`` and ``halted`` are normal, expected, terminal-for-this-cycle
    outcomes: a period that is not closed, an organization that is not attested,
    a period already settled by the other replica. None of them is retried here
    and none of them fails the cycle.
    """

    enumerated: int = 0
    drafted: int = 0
    unchanged: int = 0
    blocked: int = 0
    halted: int = 0
    errors: int = 0

    @property
    def healthy(self) -> bool:
        return self.errors == 0


class SettlementSweepStore(Protocol):
    def awaiting_periods(self, limit: int) -> Sequence[Mapping[str, Any]]: ...

    def settle(
        self, organization_id: str, period_anchor: str, computed_by: str,
    ) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class SupabaseSettlementSweepStore(SupabaseRpcClient):
    """Service-role adapter over exactly the two RPCs this sweep is allowed.

    It subclasses the bare transport, NOT SupabaseBillingStore: inheriting the
    ledger store would silently hand the sweep a claim/complete/release surface
    over the per-row billing_ledger that it has no business holding.
    """

    RPCS: Mapping[str, str] = {
        "awaiting": "billing_periods_awaiting_settlement",
        "settle": "settle_billing_period",
    }

    def awaiting_periods(self, limit: int) -> Sequence[Mapping[str, Any]]:
        rows = self._rpc(self.RPCS["awaiting"], {"p_limit": int(limit)})
        if not isinstance(rows, list):
            raise RuntimeError("settlement enumeration returned an invalid response")
        return rows

    def settle(
        self, organization_id: str, period_anchor: str, computed_by: str,
    ) -> Mapping[str, Any]:
        body = self._rpc(self.RPCS["settle"], {
            "p_organization_id": organization_id,
            "p_period_anchor": period_anchor,
            "p_computed_by": computed_by[:_COMPUTED_BY_MAX],
            # Constant, never a parameter. See ALLOW_REVISION.
            "p_allow_revision": ALLOW_REVISION,
        })
        # A `returns jsonb` function comes back as the object itself; tolerate a
        # single-element array rather than guessing about a PostgREST shape.
        if isinstance(body, list):
            body = body[0] if len(body) == 1 else None
        if not isinstance(body, Mapping):
            raise RuntimeError("settlement writer returned an invalid response")
        return body


class BillingSettlementSweep:
    """Enumerate closed unsettled weeks and ask the writer to draft each one."""

    def __init__(
        self,
        store: SettlementSweepStore,
        *,
        owner: str,
        limit: int = 50,
        interval_seconds: float = 3600.0,
    ):
        if not owner:
            raise ValueError("the settlement sweep needs a non-blank owner")
        self.store = store
        self.owner = owner
        self.limit = max(1, min(int(limit), 500))
        self.interval_seconds = interval_seconds
        self.computed_by = f"{COMPUTED_BY_PREFIX}:{owner}"[:_COMPUTED_BY_MAX]

    # -- logging ---------------------------------------------------------
    @staticmethod
    def _log(level: int, event: str, **fields: Any) -> None:
        logger.log(level, json.dumps(
            {"event": event, **fields}, separators=(",", ":"), sort_keys=True,
            default=str,
        ))

    # -- one candidate ---------------------------------------------------
    def _settle_one(
        self, candidate: Mapping[str, Any], result: SettlementSweepResult,
    ) -> None:
        organization_id = str(candidate.get("organization_id") or "")
        period_start = str(candidate.get("period_start") or "")
        period_end = str(candidate.get("period_end") or "")
        if not organization_id or not period_start:
            result.errors += 1
            self._log(logging.ERROR, "billing_settlement_sweep_bad_candidate")
            return

        try:
            # period_start is itself an instant inside its own half-open window
            # (billing_period_for_occurrence floors epoch/604800, so an exact
            # multiple maps to that period), so no caller-side date arithmetic
            # is needed or wanted here.
            body = self.store.settle(organization_id, period_start, self.computed_by)
        except Exception as exc:
            result.errors += 1
            self._log(
                logging.ERROR, "billing_settlement_sweep_rpc_failed",
                organization_id=organization_id, period_start=period_start,
                error_type=type(exc).__name__,
            )
            return

        outcome = str(body.get("outcome") or "")
        code = str(body.get("code") or "")
        common = {
            "organization_id": organization_id,
            "period_start": period_start,
            "period_end": period_end,
            "outcome": outcome,
            "code": code,
        }

        if outcome == OUTCOME_SETTLED:
            result.drafted += 1
            self._log(
                logging.INFO, "billing_settlement_sweep_drafted", **common,
                settlement_id=body.get("settlement_id"),
                fee_microusd=body.get("fee_microusd"),
                settlement_status=body.get("settlement_status"),
            )
        elif outcome == OUTCOME_UNCHANGED:
            # The other replica won the advisory lock and the evidence has not
            # moved since. Nothing was written; nothing is wrong.
            result.unchanged += 1
            self._log(
                logging.INFO, "billing_settlement_sweep_unchanged", **common,
                settlement_id=body.get("settlement_id"),
            )
        elif outcome == OUTCOME_HALTED:
            # A NORMAL outcome. Named, logged, not retried, not an error.
            result.halted += 1
            self._log(
                logging.WARNING, "billing_settlement_sweep_halted", **common,
                halting_condition=body.get("halting_condition") or code,
                message=str(body.get("message") or "")[:500],
            )
        elif outcome == OUTCOME_BLOCKED:
            result.blocked += 1
            self._log(
                logging.INFO, "billing_settlement_sweep_blocked", **common,
                settlement_id=body.get("settlement_id"),
            )
        else:
            # OUTCOME_REVISED lands here on purpose: this caller never passes
            # p_allow_revision, so a revision means the writer's contract moved
            # under it and a human must look. So does any outcome the writer
            # grows later that this module has not been taught.
            result.errors += 1
            self._log(
                logging.ERROR, "billing_settlement_sweep_unexpected_outcome",
                **common, settlement_id=body.get("settlement_id"),
            )

    # -- one cycle -------------------------------------------------------
    def run_once(
        self, should_stop: Callable[[], bool] = lambda: False,
    ) -> SettlementSweepResult:
        result = SettlementSweepResult()
        try:
            candidates = self.store.awaiting_periods(self.limit)
        except Exception as exc:
            result.errors += 1
            self._log(
                logging.ERROR, "billing_settlement_sweep_enumeration_failed",
                error_type=type(exc).__name__,
            )
            return result

        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            if should_stop():
                break
            if not isinstance(candidate, Mapping):
                result.errors += 1
                self._log(logging.ERROR, "billing_settlement_sweep_bad_candidate")
                continue
            key = (
                str(candidate.get("organization_id") or ""),
                str(candidate.get("period_start") or ""),
            )
            # Belt and braces with the enumerator's own dedupe: one cycle must
            # never call the writer twice for one (org, period), because the
            # second call would be indistinguishable from the other replica's.
            if key in seen:
                continue
            seen.add(key)
            result.enumerated += 1
            self._settle_one(candidate, result)

        self._log(
            logging.INFO, "billing_settlement_sweep_cycle",
            enumerated=result.enumerated, drafted=result.drafted,
            unchanged=result.unchanged, blocked=result.blocked,
            halted=result.halted, errors=result.errors,
        )
        return result

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()


def billing_settlement_sweep_is_enabled() -> bool:
    """Exact-"true" only, defaulted OFF. See the module docstring."""
    return os.getenv(SWEEP_ENABLED_ENV, "") == "true"


def billing_settlement_sweep_is_configured() -> bool:
    """Armed AND able to reach Supabase with a service-role key."""
    return billing_settlement_sweep_is_enabled() and bool(
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    ) and bool(os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL"))


def build_settlement_sweep_from_env(*, owner: str) -> BillingSettlementSweep:
    """Build the sweep, or raise. Callers must gate on
    :func:`billing_settlement_sweep_is_configured` first; the raise exists so an
    armed-but-unconfigured deployment is loud rather than a silent no-op."""
    if not billing_settlement_sweep_is_enabled():
        raise RuntimeError(f"{SWEEP_ENABLED_ENV} is not set to \"true\"")
    supabase_url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_role_key:
        raise RuntimeError("settlement sweep environment is incomplete")
    timeout = _bounded_float("BREVITAS_BILLING_HTTP_TIMEOUT_SECONDS", 10, 1, 30)
    store = SupabaseSettlementSweepStore(
        supabase_url, service_role_key, timeout_seconds=timeout,
    )
    return BillingSettlementSweep(
        store,
        owner=owner,
        limit=_bounded_int("BREVITAS_BILLING_SETTLEMENT_SWEEP_LIMIT", 50, 1, 500),
        interval_seconds=_bounded_float(
            "BREVITAS_BILLING_SETTLEMENT_SWEEP_INTERVAL_SECONDS", 3600, 60, 86400,
        ),
    )


async def run_billing_settlement_sweep_loop(
    sweep: BillingSettlementSweep,
    stop: asyncio.Event,
    *,
    interval_seconds: float | None = None,
) -> None:
    """Run one bounded sweep per interval until ``stop``.

    There is no lease to release on shutdown: this loop holds nothing between
    cycles. The writer's advisory lock is transaction-scoped, so a replica that
    dies mid-settle releases it when its transaction ends, and the surviving
    replica's next cycle simply re-enumerates.
    """
    interval = sweep.interval_seconds if interval_seconds is None else interval_seconds
    try:
        while not stop.is_set():
            try:
                await asyncio.to_thread(sweep.run_once, stop.is_set)
            except Exception as exc:
                # run_once already absorbs per-candidate failures; this is the
                # backstop for anything that escapes it. A sweep outage must
                # never take the worker down — a draft is not money.
                logger.error(json.dumps({
                    "event": "billing_settlement_sweep_cycle_failed",
                    "error_type": type(exc).__name__,
                }, separators=(",", ":")))
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
    finally:
        try:
            sweep.close()
        except Exception as exc:
            logger.error(json.dumps({
                "event": "billing_settlement_sweep_close_failed",
                "error_type": type(exc).__name__,
            }, separators=(",", ":")))
