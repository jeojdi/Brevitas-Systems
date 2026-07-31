# Quarantine

Files here are **written but deliberately not shipped**. They live outside
`supabase/migrations/` and `scripts/ci/` on purpose: `verify-migrations.mjs`
requires the migrations directory to match `expectedFreshMigrationOrder`
*exactly*, so a migration that is present-but-unregistered turns the local gate
red for every developer while CI stays green only by accident of the file being
untracked. Quarantine-by-untracking does not survive a working tree that
contains the file.

## `202607280028_anchored_zero_spend_fee_basis.sql` + its assertion suite

**What it was for.** A Brevitas cache replay costs $0 upstream by construction,
so `202607280008`'s `zero_spend_concentration` halts any period whose savings
are mostly cache hits — which, for the current product, is every period. This
migration was meant to make those savings billable by requiring an "anchor": a
real, receipted, paid request behind the replay.

**Why it is quarantined.** Its adversarial review returned do-not-ship with one
blocker and three highs, all reproduced by execution:

1. The forward link it bills on does not exist — `savings_anchor_request_id`
   appears in no migration, so the predicate degrades to "this org once made any
   paid call for this provider+model at any earlier time".
2. A single `$0.0000000001` ancestor anchored $500 of replays → a $125 fee. No
   materiality floor, no lookback, org-wide scope.
3. A paid row inserted *after* settlement but backdated into the period
   retroactively re-anchors already-settled replays: fee went 2,500,000 →
   3,500,000 µUSD with the watermark and row count unchanged, defeating the
   reproducibility `202607280013`'s watermark exists to guarantee.
4. Narrowing the guard's numerator while leaving the gross denominator let
   fabricated rows dilute their own alarm: a shape that halts at share 1.00000
   settles at 0.00200.

**What replaces it.** The redesign should treat the paid ancestor as the *price
source* — bill 25% of `tokens_saved × the customer's observed price` — rather
than as an existence gate. That is strictly stronger: it cannot overbill by
construction, because the price is one the customer demonstrably paid. It needs
two owner decisions first: a materiality floor and a lookback window.

**Until then:** a customer whose savings are purely cache replays settles at
**$0**. Say so before quoting anyone a number.

Do not re-register these files without the redesign and a fresh review.
