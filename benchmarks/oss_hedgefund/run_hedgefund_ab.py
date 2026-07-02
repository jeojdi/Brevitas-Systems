"""Real ai-hedge-fund A/B through Brevitas — 18+ agents split across Claude/OpenAI/DeepSeek.

Runs the ACTUAL virattt/ai-hedge-fund LangGraph end-to-end on a free-tier ticker, with
every agent's LLM traffic routed through the Brevitas proxy. Two arms decided purely by
the proxy env (BREVITAS_PASSTHROUGH): baseline (untouched) vs Brevitas (caching + router
+ shared-prefix). Both metered identically by the proxy (BREVITAS_METER_FILE) so the
dollar comparison is apples-to-apples.

Providers are assigned round-robin across the analysts (heterogeneous fleet). Cheap
models only (haiku / gpt-4o-mini / deepseek-chat); most agents on DeepSeek to respect
the <$3-each cap on the paid providers.

Two minimal, documented patches to the upstream repo (allowed — no offline mode exists):
  1. get_model(): honor ANTHROPIC_BASE_URL / DEEPSEEK_BASE_URL like it already does for
     OpenAI, so all three providers can be pointed at the proxy.
  2. get_agent_model_config(): assign each agent its provider from AGENT_PROVIDER.
Neither changes agent logic — only where the bytes go.
"""
import os, sys, json, argparse
from pathlib import Path

HF = Path("/tmp/oss-ab/ai-hedge-fund")
sys.path.insert(0, str(HF))

PORT = int(os.environ.get("BREV_PORT", "4242"))
OPENAI_STYLE_BASE = f"http://127.0.0.1:{PORT}/openai/v1"
ANTHROPIC_BASE = f"http://127.0.0.1:{PORT}"

# cheap models per provider
MODELS = {"OpenAI": "gpt-4o-mini", "Anthropic": "claude-haiku-4-5-20251001",
          "DeepSeek": "deepseek-chat"}

# 21 agents; keep Claude/OpenAI to a few (budget), rest on DeepSeek
_CLAUDE = {"warren_buffett", "cathie_wood", "portfolio_manager"}
_OPENAI = {"charlie_munger", "michael_burry", "risk_management_agent"}


def provider_for(agent_name: str) -> str:
    base = (agent_name or "").replace("_agent", "")
    if base in _CLAUDE:
        return "Anthropic"
    if base in _OPENAI:
        return "OpenAI"
    return "DeepSeek"


def _patch():
    import src.llm.models as M
    import src.utils.llm as L

    _orig_get_model = M.get_model

    def get_model(model_name, model_provider, api_keys=None):
        prov = model_provider.value if hasattr(model_provider, "value") else str(model_provider)
        if prov == M.ModelProvider.ANTHROPIC or prov == "Anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(model=model_name,
                                 api_key=os.environ["ANTHROPIC_API_KEY"],
                                 base_url=os.environ.get("ANTHROPIC_BASE_URL"))
        if prov == M.ModelProvider.DEEPSEEK or prov == "DeepSeek":
            from langchain_openai import ChatOpenAI  # deepseek is OpenAI-compatible
            return ChatOpenAI(model=model_name, api_key=os.environ["DEEPSEEK_API_KEY"],
                              base_url=os.environ.get("DEEPSEEK_BASE_URL"))
        return _orig_get_model(model_name, model_provider, api_keys)

    def get_agent_model_config(state, agent_name):
        prov = provider_for(agent_name)
        return MODELS[prov], prov

    M.get_model = get_model
    L.get_model = get_model
    L.get_agent_model_config = get_agent_model_config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="AAPL,MSFT,NVDA")
    ap.add_argument("--arm", required=True, choices=["baseline", "brevitas"])
    args = ap.parse_args()
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    # keys + routing: OpenAI + DeepSeek agents use the OpenAI-style proxy path;
    # Anthropic agents use the proxy's /v1/messages path.
    os.environ["OPENAI_API_BASE"] = OPENAI_STYLE_BASE          # for any real-OpenAI path
    os.environ["DEEPSEEK_BASE_URL"] = OPENAI_STYLE_BASE
    os.environ["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE

    _patch()
    from src.main import run_hedge_fund

    analysts = ["aswath_damodaran", "ben_graham", "bill_ackman", "cathie_wood",
                "charlie_munger", "michael_burry", "mohnish_pabrai", "nassim_taleb",
                "peter_lynch", "phil_fisher", "rakesh_jhunjhunwala",
                "stanley_druckenmiller", "warren_buffett", "technical_analyst",
                "fundamentals_analyst", "growth_analyst", "sentiment_analyst",
                "valuation_analyst"]  # 18 analysts + risk + portfolio added by graph

    all_decisions = {}
    for tk in tickers:
        portfolio = {"cash": 100000.0, "margin_requirement": 0.0, "margin_used": 0.0,
                     "positions": {tk: {"long": 0, "short": 0, "long_cost_basis": 0.0,
                                   "short_cost_basis": 0.0, "short_margin_used": 0.0}},
                     "realized_gains": {tk: {"long": 0.0, "short": 0.0}}}
        res = run_hedge_fund(tickers=[tk], start_date="2024-10-01",
                             end_date="2024-12-31", portfolio=portfolio,
                             show_reasoning=False, selected_analysts=analysts,
                             model_name=MODELS["DeepSeek"], model_provider="DeepSeek")
        all_decisions[tk] = (res or {}).get("decisions", {})
        print(f"[{args.arm}] {tk}: {json.dumps(all_decisions[tk], default=str)[:160]}")
    Path(f"/tmp/oss-ab/hf_{args.arm}_decision.json").write_text(
        json.dumps(all_decisions, indent=2, default=str))


if __name__ == "__main__":
    main()
