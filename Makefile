# Brevitas Systems — developer entrypoints.
#
# The organizing rule of this file is a hard split between two kinds of target:
#
#   OFFLINE  (test, bench, and everything they depend on) — pure local compute.
#            No provider HTTP, no API key required, no dollars. Safe in CI, safe
#            to run in a loop, safe as a default goal.
#
#   LIVE     (probe-anthropic, probe-deepseek) — real calls to a real provider
#            against a real key, which SPENDS REAL MONEY. These are never a
#            prerequisite of anything else and are never reachable from `make`
#            with no arguments. See the "money targets" section at the bottom.
#
# If you add a target that can spend money, it goes in the money section, it
# gets a cost in its `##` help text, and it must not appear in any aggregate.

# Resolve the repo root from this Makefile's own location so the venv python is
# found no matter where make is invoked from. PY is overridable but defaults to
# the repo venv on purpose: bare `python` on this machine is a different
# interpreter without the project deps installed.
REPO_ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
PY        ?= $(REPO_ROOT)/.venv/bin/python

# Scratch for generated benchmark artifacts. /build is gitignored (.gitignore:23),
# so `make bench` leaves the working tree clean. The lever benchmarks below write
# their own results next to themselves in benchmarks/levers/ — those paths are
# hardcoded in the scripts and are tracked files, but the generators are seeded
# and deterministic, so re-running them is a byte-identical no-op in git.
BENCH_OUT := $(REPO_ROOT)/build/bench

# The pytest invocation CI runs, verbatim (.github/workflows/security.yml:70).
# Keeping these in sync matters: a suite that only exists in CI is a suite that
# breaks in CI. Override PYTEST_ARGS to narrow a run without editing this file.
PYTEST_PATHS ?= tests unit-tests token_efficiency_model/lossless/tests
PYTEST_ARGS  ?=

# Offline replay-simulator scenarios. Chosen because the four together run in
# about a second and cover the distinct arrival shapes (periodic, bursty,
# churning, multi-customer org).
SIM_SCENARIOS ?= cron,bursty,churned,company

# The policy list is pinned here, NOT left to the simulator's default. The default
# is never-warm,v1heuristic — a contrast that flatters warming, because never-warm
# is not the incumbent. The engine already writes at the 1h tier, so ttl-1h-only is
# what any warming policy has to beat, and measured 2026-08-11 it beats all of them
# on every scenario with zero pings. A bench table that omits it reports a win where
# there is a loss; see docs/INDEX_FIX_ROUND.md on gates that cannot detect a losing
# policy. Keep ttl-1h-only in this list.
SIM_POLICIES ?= never-warm,ttl-1h-only,v1heuristic,v1heuristic+ttl-1h,learned-index-fixed,learned-index-fixed+ttl-1h

.DEFAULT_GOAL := help

.PHONY: help test test-collect bench bench-levers bench-sim bench-selftest \
        bench-report clean-bench probe-anthropic probe-deepseek

##@ Everyday

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} \
	  /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next } \
	  /^[a-zA-Z0-9_.-]+:.*?## / { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 } \
	  END { printf "\n" }' $(MAKEFILE_LIST)
	@echo 'Offline by default. Only the "Money" targets ever contact a provider.'
	@echo 'Vars: PY=$(PY)'
	@echo '      PYTEST_ARGS=... SIM_SCENARIOS=$(SIM_SCENARIOS)'
	@echo '      SIM_POLICIES=$(SIM_POLICIES)'
	@echo ''

test: ## Run the pytest suite exactly as CI runs it
	@echo '==> pytest $(PYTEST_PATHS)'
	$(PY) -m pytest -q $(PYTEST_PATHS) $(PYTEST_ARGS)

test-collect: ## Collect-only smoke: proves every test module imports (seconds, no run)
	$(PY) -m pytest -q --collect-only -p no:cacheprovider $(PYTEST_PATHS) $(PYTEST_ARGS) | tail -5

##@ Benchmarks (offline — no provider calls, no spend)

bench: ## Run the offline benchmarks + replay simulator, then print the summary table
	@$(MAKE) --no-print-directory bench-levers
	@$(MAKE) --no-print-directory bench-sim
	@$(MAKE) --no-print-directory bench-report

bench-levers: ## Lever 1-3 offline benchmarks (caching / dedup / delta)
	@echo '==> lever benchmarks (simulated provider accounting, no keys)'
	@$(PY) $(REPO_ROOT)/benchmarks/levers/bench_lever1_caching.py >/dev/null
	@$(PY) $(REPO_ROOT)/benchmarks/levers/bench_lever2_dedup.py   >/dev/null
	@$(PY) $(REPO_ROOT)/benchmarks/levers/bench_lever3_delta.py   >/dev/null
	@echo '    lever1/2/3 done'

bench-sim: ## Warming replay simulator over the synthetic scenarios
	@echo '==> warm replay simulator ($(SIM_SCENARIOS))'
	@mkdir -p $(BENCH_OUT)
	@$(PY) $(REPO_ROOT)/scripts/warm_replay_sim.py \
	    --synthetic $(SIM_SCENARIOS) --policy $(SIM_POLICIES) --json > $(BENCH_OUT)/sim.json
	@echo '    wrote $(BENCH_OUT)/sim.json'

bench-selftest: ## Simulator invariants (asserts ~60 properties across 8 scenarios)
	$(PY) $(REPO_ROOT)/scripts/warm_replay_sim.py --selftest

bench-report: ## Reprint the summary table from the last bench run (no recompute)
	@REPO_ROOT='$(REPO_ROOT)' BENCH_OUT='$(BENCH_OUT)' $(PY) -c "$$BENCH_TABLE_PY"

clean-bench: ## Remove generated benchmark artifacts under build/bench
	@rm -rf $(BENCH_OUT)
	@echo 'removed $(BENCH_OUT)'

##@ Money — LIVE provider calls, real spend. Never run by any other target.

# Both probes refuse to do anything unless their BREVITAS_*_PROBE ack env var is
# set, and both carry an in-script hard spend cap that aborts the instant a
# projected call would cross it (anthropic $5.00, deepseek $2.00 — the caps are
# literals in the scripts so a repricing cannot perturb the arithmetic).
#
# These targets satisfy that ack for you, so they add a SECOND gate rather than
# removing the first: CONFIRM=yes must be passed explicitly. It is a variable and
# not a read prompt on purpose — failing closed beats hanging on a tty that a CI
# runner does not have. Net effect is unchanged: one deliberate human act to spend.

probe-anthropic: ## LIVE. SPENDS MONEY (hard cap 5.00 USD). Needs CONFIRM=yes
	@if [ "$(CONFIRM)" != "yes" ]; then \
	  echo 'REFUSING: make probe-anthropic makes LIVE Anthropic calls and SPENDS REAL MONEY.'; \
	  echo '          Hard cap is 5.00 USD, enforced in scripts/anthropic_cache_probe.py.'; \
	  echo '          Re-run as: make probe-anthropic CONFIRM=yes'; \
	  exit 1; \
	fi
	@echo '==> LIVE Anthropic cache probe — SPENDING REAL MONEY, cap 5.00 USD'
	BREVITAS_ANTHROPIC_PROBE=1 $(PY) $(REPO_ROOT)/scripts/anthropic_cache_probe.py

probe-deepseek: ## LIVE. SPENDS MONEY (hard cap 2.00 USD). Needs CONFIRM=yes
	@if [ "$(CONFIRM)" != "yes" ]; then \
	  echo 'REFUSING: make probe-deepseek makes LIVE DeepSeek calls and SPENDS REAL MONEY.'; \
	  echo '          Hard cap is 2.00 USD, enforced in scripts/deepseek_cache_probe.py.'; \
	  echo '          Re-run as: make probe-deepseek CONFIRM=yes'; \
	  exit 1; \
	fi
	@echo '==> LIVE DeepSeek cache probe — SPENDING REAL MONEY, cap 2.00 USD'
	BREVITAS_DEEPSEEK_PROBE=1 $(PY) $(REPO_ROOT)/scripts/deepseek_cache_probe.py

# ---------------------------------------------------------------------------
# Summary table renderer.
#
# Lives inline (exported to the environment, run with python -c) rather than as
# a script under scripts/, so that `make bench` has no import surface of its own
# and cannot drift from the Makefile that calls it.
#
# Reduction percentages here are recomputed from the raw before/after magnitudes
# rather than read out of each benchmark's own reported pct field. The levers do
# not agree on a convention -- some report savings_pct (a reduction), others
# delta_pct/incremental_pct (a remainder) -- and silently mixing the two would
# print a number that flatters whichever lever used the other sense.
#
# No '$' or '#' characters below: this text is expanded by make and then by the
# shell inside double quotes, and both would mangle them.
# ---------------------------------------------------------------------------
define BENCH_TABLE_PY
import json, os

ROOT = os.environ.get('REPO_ROOT', '.')
OUT = os.environ.get('BENCH_OUT', 'build/bench')

def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None

def rule(ch='-', n=86):
    print(ch * n)

BEFORE_AFTER = [
    ('uncached_cost', 'actual_cost', 'tok-cost'),
    ('baseline_bytes', 'dedup_bytes', 'bytes'),
    ('artifact_bytes', 'incremental_bytes', 'bytes'),
    ('full_bytes', 'delta_bytes', 'bytes'),
]

LEVERS = [
    (1, 'caching', 'results_lever1.json'),
    (2, 'dedup', 'results_lever2.json'),
    (3, 'delta', 'results_lever3.json'),
]

print('')
rule('=')
print('BENCH SUMMARY -- offline only. No provider calls, no spend.')
rule('=')

print('')
print('LEVER BENCHMARKS  (before -> after, reduction recomputed from magnitudes)')
rule()
print('{:<18} {:<28} {:>16} {:>16} {:>10}'.format(
    'lever', 'scenario', 'before', 'after', 'reduction'))
rule()

gates = []
footnotes = []
any_lever = False
for num, label, fname in LEVERS:
    doc = load(os.path.join(ROOT, 'benchmarks', 'levers', fname))
    if doc is None:
        print('{:<18} {:<28} {:>16} {:>16} {:>10}'.format(
            'lever%d %s' % (num, label), '(no results -- run make bench)', '-', '-', '-'))
        continue
    any_lever = True
    for res in doc.get('results', []):
        scen = str(res.get('scenario', '?'))
        before = after = None
        unit = ''
        for bkey, akey, u in BEFORE_AFTER:
            if bkey in res and akey in res:
                before, after, unit = res[bkey], res[akey], u
                break
        if before is None or not before:
            reported = True
            pct = None
            for k in ('savings_pct', 'delta_pct', 'incremental_pct'):
                if k in res:
                    pct = float(res[k])
                    if k != 'savings_pct':
                        pct = 100.0 - pct
                    break
            cell = 'n/a' if pct is None else ('%.2f%% r' % pct)
            print('{:<18} {:<28} {:>16} {:>16} {:>10}'.format(
                'lever%d %s' % (num, label), scen[:28], '-', '-', cell))
            footnotes.append(scen)
            continue
        red = (1.0 - (float(after) / float(before))) * 100.0
        print('{:<18} {:<28} {:>16} {:>16} {:>9.2f}%'.format(
            'lever%d %s' % (num, label), scen[:28],
            '%.0f %s' % (before, unit), '%.0f %s' % (after, unit), red))
    checks = doc.get('checks', {})
    failed = [k for k, v in checks.items() if not v]
    gates.append(('lever%d %s' % (num, label), bool(doc.get('passed')), failed))

rule()
if footnotes:
    print('  r = reduction as REPORTED by the benchmark, not recomputed here: '
          + ', '.join(footnotes))
    print('      (these scenarios price cached vs uncached tokens differently, so')
    print('       the raw magnitudes alone do not determine the cost reduction)')
for name, passed, failed in gates:
    verdict = 'PASS' if passed else 'FAIL'
    extra = '' if not failed else '  failed: ' + ', '.join(failed)
    print('  gate {:<20} {}{}'.format(name, verdict, extra))

sim = load(os.path.join(OUT, 'sim.json'))
print('')
print('WARM REPLAY SIMULATOR  (net USD; never-warm is the do-nothing baseline)')
rule()
if sim is None:
    print('  no simulator output at %s -- run make bench-sim' % os.path.join(OUT, 'sim.json'))
else:
    print('{:<12} {:<28} {:>11} {:>11} {:>10} {:>7} {:>9}'.format(
        'scenario', 'policy', 'net_usd', 'savings', 'ping_cost', 'pings', 'pct_orcl'))
    rule()
    for scen in sim.get('scenarios', []):
        name = str(scen.get('scenario', '?'))
        oracle = float(scen.get('oracle', {}).get('net_usd', 0.0) or 0.0)
        for pol in scen.get('policies', []):
            net = float(pol.get('net_usd', 0.0) or 0.0)
            frac = ('%.1f%%' % (net / oracle * 100.0)) if oracle > 0 else '-'
            print('{:<12} {:<28} {:>11.4f} {:>11.4f} {:>10.4f} {:>7} {:>9}'.format(
                name[:12], str(pol.get('policy', '?'))[:28], net,
                float(pol.get('savings_usd', 0.0) or 0.0),
                float(pol.get('ping_cost_usd', 0.0) or 0.0),
                pol.get('pings', 0), frac))
        print('{:<12} {:<28} {:>11.4f} {:>11} {:>10} {:>7} {:>9}'.format(
            name[:12], 'oracle (ceiling)', oracle, '-', '-',
            scen.get('oracle', {}).get('pings', 0), '100.0%'))
        rule()
    losers = []
    for scen in sim.get('scenarios', []):
        for pol in scen.get('policies', []):
            if float(pol.get('net_usd', 0.0) or 0.0) < 0.0:
                losers.append('%s/%s' % (scen.get('scenario'), pol.get('policy')))
    if losers:
        print('  NET-NEGATIVE policies (these lose money on this trace):')
        for item in losers:
            print('    ' + item)
    else:
        print('  no policy is net-negative on these traces')

print('')
bad = [n for n, p, _ in gates if not p]
if not any_lever:
    print('RESULT: no lever results found -- run make bench')
elif bad:
    print('RESULT: LEVER GATE FAILURES: ' + ', '.join(bad))
else:
    print('RESULT: all lever gates PASS. Simulator numbers above are reported, not gated.')
print('Reminder: nothing above touched a provider. Live probes are make probe-* (they spend).')
print('')
endef
export BENCH_TABLE_PY
