# Billing reconciliation — production (wyfz), 2026-07-30

Read-only. **No production data was modified by this report.**

Run to answer one question before shipping the provider-label fix: does
host-derived provider labelling change what any historical row would have been
billed at?

## Answer: no historical rows are affected. Do not backfill.

```
rows with a provider label outside (anthropic, openai, '')   0
distinct providers present                                   '', anthropic, openai
```

The label change only bites when `x-brevitas-provider` disagrees with the
resolved destination host, which requires a reseller/override label such as
`groq`, `deepseek` or `perplexity`. Production has never recorded one. The fix is
therefore **forward-only with zero repricing**, and no customer's past invoice
changes. This is the outcome the audit recommended; it is now measured rather
than assumed.

## The larger finding: nothing in the ledger is authoritative

```
total usage_log rows        42,410
authoritative = true             0     <-- zero, not "few"
authoritative = false       42,410
verified_savings_usd       $163.33     all on non-authoritative rows
brevitas_fee_usd            $40.83     all on non-authoritative rows
priced rows                 19,969
```

Every row in the production ledger is `authoritative = false`. The hosted proxy
has never written an authoritative receipt to this database.

This matters because the code's own invariant is that `verified_savings_usd` is
non-zero only when `authoritative = true` (`api/server.py:_record_usage_report`).
Production holds $163.33 of verified savings and $40.83 of fees on rows that
cannot satisfy it. The most likely origin is the manual repricing of 1,897 fee
rows on 2026-07-29, which wrote fees directly rather than through the metering
path.

Two consequences to decide on:

1. **These figures are not evidence of billable savings** under the current
   contract. The new `GET /v1/admin/billing/settlement` will correctly refuse to
   state an amount for periods with no authoritative rows rather than report a
   confident wrong number — which is the intended behaviour, but it means the
   settlement screen will show "not settleable" until authoritative receipts start
   landing.
2. **It is consistent with the metering-suppression defects this audit found.**
   A caller-pinned `X-Brevitas-Request-Id`, an over-long tracking label, or a
   raise anywhere in the receipt bridge each caused the authoritative write to be
   dropped silently while returning HTTP 200. Those are fixed in this branch, but
   the fixes only take effect once the branch is deployed.

## Recommended next step

After deploying this branch, watch for the first `authoritative = true` row:

```sql
select count(*), min(request_id)
from public.usage_log
where authoritative is true;
```

If that stays at zero under real hosted-proxy traffic, the metering path is still
broken somewhere the audit did not reach, and the new produced-volume instrument
(`brevitas/observability.py`) is the intended detector — its alert is gated off
until volume appears precisely so it can be switched on at that moment.

Do **not** retro-fix the existing 42,410 rows without an explicit decision: they
have already been hand-adjusted once, and a second silent adjustment would make
the ledger impossible to reason about later.
