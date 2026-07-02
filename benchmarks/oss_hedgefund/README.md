# Real ai-hedge-fund A/B (18 agents, tri-provider)

Runs the actual [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) LangGraph
end-to-end on free-tier tickers, every agent routed through the Brevitas proxy, providers
split across the fleet (Claude / OpenAI / DeepSeek). Baseline vs Brevitas decided by the
proxy's `BREVITAS_PASSTHROUGH` env; both metered identically via `BREVITAS_METER_FILE`.

## Run
1. Clone ai-hedge-fund next to this and `pip install` its langchain deps.
2. Start the proxy twice (baseline: `BREVITAS_PASSTHROUGH=1`, brevitas: unset), each with
   a distinct `BREVITAS_METER_FILE`.
3. `python run_hedgefund_ab.py --arm baseline --tickers AAPL,MSFT,NVDA` then `--arm brevitas`.
4. `python price_ab.py meter_baseline.jsonl meter_brevitas.jsonl`.

Two minimal documented patches to the upstream repo (no offline mode exists): `get_model`
honors ANTHROPIC_BASE_URL/DEEPSEEK_BASE_URL (as it already does for OpenAI); per-agent
provider assignment via `get_agent_model_config`. Neither changes agent logic.
