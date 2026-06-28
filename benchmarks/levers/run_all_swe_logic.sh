#!/bin/bash
# Sequential orchestrator — runs the SWE + LOGIC benchmarks on both models, one at a time,
# to avoid rate-limit contention. Each writes its own results_*.json.
set -e
cd "$(dirname "$0")/../.."
LOG=/private/tmp/claude-501/-Users-james-Documents-GitHub-Brevitas-Systems-brevitas-systems/4ce2c701-a3ed-4ec0-96a9-2cf6dfa9f7f1/scratchpad

echo "=== [1/4] BBH logic — deepseek ===" ; python3 benchmarks/levers/bench_bbh_logic.py 50 deepseek      > "$LOG/bbh_deepseek_n50.log" 2>&1 ; echo "done bbh deepseek"
echo "=== [2/4] BBH logic — openai ===" ;   python3 benchmarks/levers/bench_bbh_logic.py 50 openai        > "$LOG/bbh_openai_n50.log" 2>&1 ; echo "done bbh openai"
echo "=== [3/4] HumanEval SWE — deepseek ===" ; python3 benchmarks/levers/bench_humaneval_swe.py 50 deepseek > "$LOG/humaneval_deepseek_n50.log" 2>&1 ; echo "done humaneval deepseek"
echo "=== [4/4] HumanEval SWE — openai ===" ;   python3 benchmarks/levers/bench_humaneval_swe.py 50 openai   > "$LOG/humaneval_openai_n50.log" 2>&1 ; echo "done humaneval openai"
echo "=== ALL SWE+LOGIC RUNS COMPLETE ==="
