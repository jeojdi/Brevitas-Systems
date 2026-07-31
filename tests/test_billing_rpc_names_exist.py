"""Every RPC name a billing store sends must exist in the migration chain.

WHY THIS FILE EXISTS. SupabaseSettlementStore.RPCS mapped "health" to
period_settlement_recovery_health, and 202607280029 shipped the six claim/send
RPCs and billing_period_settlement_history WITHOUT it. Nothing caught it:

  * The store tests drive FakeRpcSession, which answers ANY function name, so
    they asserted the names the store SENDS and passed while the function
    existed in no migration and on no database.
  * The SQL assertion suites check the functions the migrations CREATE. Neither
    side ever compared the two lists, so a name present in exactly one of them
    was invisible.

It surfaced only by querying production for the names the store actually calls,
which is a terrible way to find it -- SupabaseBillingStore._rpc calls
response.raise_for_status(), so PostgREST's PGRST202 raises, and
BillingRecoveryProcessor.check_health deliberately lets a settlement failure
fail the whole cycle. It stayed latent only because
billing_settlement_recovery_is_enabled() defaults OFF; setting
BREVITAS_BILLING_SETTLEMENT_ENABLED=true is what would have armed it.

This test is the missing join: it reads the RPCS maps off the store classes
themselves, so a name added to a store in the future is covered the moment it is
added, with no list here to keep in step.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from api.billing_recovery import SupabaseBillingStore, SupabaseSettlementStore
from api.billing_settlement_sweep import SupabaseSettlementSweepStore

REPO = Path(__file__).resolve().parents[1]
FRESH_MANIFEST = REPO / "scripts" / "ci" / "migration-fresh-manifest.txt"


def _migration_sources() -> str:
    """Only migrations the fresh manifest actually ships.

    Reading the directory instead would let a quarantined or stray .sql file
    satisfy the check for a function that never reaches a database.
    """
    sources = []
    for line in FRESH_MANIFEST.read_text().splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        path = REPO / entry
        assert path.is_file(), f"fresh manifest names a missing file: {entry}"
        sources.append(path.read_text())
    return "\n".join(sources)


MIGRATION_SOURCES = _migration_sources()


def _is_created(function_name: str) -> bool:
    # `create [or replace] function public.<name>(`, tolerating the newline this
    # chain usually puts before the parameter list.
    pattern = re.compile(
        r"create\s+(?:or\s+replace\s+)?function\s+public\."
        + re.escape(function_name)
        + r"\s*\(",
        re.IGNORECASE,
    )
    return bool(pattern.search(MIGRATION_SOURCES))


def _rpc_names(store_class) -> list[tuple[str, str]]:
    return sorted((role, name) for role, name in store_class.RPCS.items())


@pytest.mark.parametrize(
    "store_class",
    [SupabaseBillingStore, SupabaseSettlementStore, SupabaseSettlementSweepStore],
    ids=["billing_ledger", "period_settlement", "settlement_sweep"],
)
def test_every_store_rpc_name_is_created_by_a_shipped_migration(store_class):
    missing = [
        f"{store_class.__name__}.RPCS[{role!r}] -> public.{name}()"
        for role, name in _rpc_names(store_class)
        if not _is_created(name)
    ]
    assert not missing, (
        "these RPC names are sent by a billing store but are created by no "
        "migration in scripts/ci/migration-fresh-manifest.txt, so the call "
        "raises PGRST202 against a real database:\n  "
        + "\n  ".join(missing)
    )


def test_the_two_stores_do_not_share_rpc_names():
    """A shared name means one ledger is driving the other's rows.

    The settlement store subclasses the billing store, so an RPCS key it forgets
    to override silently inherits the per-row ledger's function -- which is
    exactly the shadowing defect that reached the tree once already, when a
    duplicate release_unsent definition made settlement releases call
    release_billing_ledger_unsent with a settlement id.
    """
    shared = sorted(
        set(SupabaseBillingStore.RPCS.values())
        & set(SupabaseSettlementStore.RPCS.values())
    )
    assert not shared, (
        "SupabaseSettlementStore inherits these RPC names from "
        f"SupabaseBillingStore instead of overriding them: {shared}"
    )
