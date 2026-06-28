"""
Execute a campaign through the marketing agency with Brevitas token tracking.

Usage:
    # With mock provider (deterministic, no API key required):
    python -m examples.marketing_agency.run

    # With real DeepSeek (requires DEEPSEEK_API_KEY + Brevitas API key):
    BREVITAS_API_KEY=bvt_... DEEPSEEK_API_KEY=sk-... python -m examples.marketing_agency.run

Results are fetched from the Brevitas API and printed to stdout.
"""
import sys
import os
import time
import requests
from pathlib import Path

# Ensure brevitas is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from brevitas.labels import start_run
from .orchestrator import MarketingAgency


SAMPLE_BRIEF = """
Product: AuthFlow - Developer-friendly authentication platform
Goal: Launch Q3 marketing campaign to reach 100k+ developer signups
Target Audience: Full-stack engineers, indie hackers, startup CTOs
Budget: $75k
Timeline: 45 days (July 1 - August 15)
Success Metrics: 50k+ impressions, 5k+ signups, <$15 CAC

Key Features to Highlight:
- Dead simple REST API (OAuth, JWT, MFA out of the box)
- Works with any framework (Node, Python, Go, Rust)
- Transparent pricing ($0.01 per MAU after free tier)
- SOC2 Type II compliant, no vendor lock-in
- 99.99% uptime SLA

Unique Angles:
- "Auth shouldn't require a PhD" messaging
- Anti-vendor lock-in positioning
- Obsessive focus on developer experience
- Real humans, no bots customer support
"""


def print_stats_table(stats: dict) -> None:
    """Pretty-print per-agent statistics from the API."""
    if not stats or "by_agent" not in stats:
        print("No statistics available yet.")
        return

    print("\n" + "=" * 100)
    print("PER-AGENT SAVINGS BREAKDOWN")
    print("=" * 100)

    agents_data = stats.get("by_agent", [])
    if not agents_data:
        print("No agent data available.")
        return

    # Print header
    print(f"{'Agent':<20} {'Calls':<10} {'Tokens Saved':<15} {'Savings %':<12} {'Cost Saved':<12}")
    print("-" * 100)

    total_tokens_saved = 0
    total_calls = 0
    total_cost = 0

    for agent_stat in agents_data:
        agent_name = agent_stat.get("agent", "unknown")
        calls = agent_stat.get("calls", 0)
        tokens_saved = agent_stat.get("tokens_saved", 0)
        savings_pct = agent_stat.get("savings_pct", 0)
        cost_saved = agent_stat.get("cost_saved_usd", 0)

        print(
            f"{agent_name:<20} {calls:<10} {tokens_saved:<15} {savings_pct:.1f}%{'':<8} ${cost_saved:.2f}"
        )

        total_tokens_saved += tokens_saved
        total_calls += calls
        total_cost += cost_saved

    print("-" * 100)
    print(f"{'TOTAL':<20} {total_calls:<10} {total_tokens_saved:<15} {'':<12} ${total_cost:.2f}")
    print("=" * 100 + "\n")


def main():
    """Execute the marketing campaign and fetch real results from Brevitas API."""
    provider_name = os.environ.get("BREVITAS_AGENCY_PROVIDER", "mock")
    brevitas_api_key = os.environ.get("BREVITAS_API_KEY")
    brevitas_base_url = os.environ.get("BREVITAS_BASE_URL", "http://localhost:8000")

    print(f"\n🚀 Starting campaign with {provider_name.upper()} provider...\n")

    # Initialize Brevitas client if doing a real run
    brevitas_client = None
    if provider_name == "deepseek":
        if not brevitas_api_key:
            print("❌ ERROR: BREVITAS_API_KEY not set for DeepSeek run")
            print("Set BREVITAS_API_KEY=<key> and try again")
            sys.exit(1)

        try:
            import brevitas
            from openai import OpenAI

            # Configure Brevitas
            brevitas.configure(api_key=brevitas_api_key, base_url=brevitas_base_url)

            # Create OpenAI client pointed at DeepSeek, wrap with Brevitas
            deepseek_client = OpenAI(
                api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                base_url="https://api.deepseek.com/v1",
            )
            brevitas_client = brevitas.wrap(deepseek_client)
            print(f"✓ Brevitas configured (api_key={brevitas_api_key[:8]}...)")
            print(f"✓ DeepSeek client configured (base_url=https://api.deepseek.com/v1)\n")

        except Exception as e:
            print(f"❌ Failed to configure Brevitas: {e}")
            sys.exit(1)

    # Create the agency with Brevitas client
    agency = MarketingAgency(provider_name=provider_name, brevitas_client=brevitas_client)

    # Start a run with pipeline label
    run_id = start_run(pipeline="campaign-launch")
    print(f"Pipeline: campaign-launch")
    print(f"Run ID: {run_id}\n")

    # Execute the full campaign
    results = agency.run_campaign(SAMPLE_BRIEF)

    print("\n" + "=" * 60)
    print("FINAL CAMPAIGN BRIEF (EXCERPT)")
    print("=" * 60)
    final_brief = results.get("reporter", "")
    if final_brief:
        print(final_brief[:500] + "...\n")

    # Fetch and display statistics from API
    print("\nFetching per-agent statistics from Brevitas API...\n")

    if provider_name == "mock":
        print("(Mock provider — statistics not persisted to API)\n")
        return

    # For real DeepSeek run, query the API
    try:
        time.sleep(1)  # Allow server to process
        stats_url = f"{brevitas_base_url}/v1/stats/agents?pipeline=campaign-launch"
        headers = {"X-API-Key": brevitas_api_key}

        response = requests.get(stats_url, headers=headers, timeout=10)
        response.raise_for_status()

        stats = response.json()

        if not stats or "by_agent" not in stats or len(stats.get("by_agent", [])) == 0:
            print("⚠️  No agent statistics recorded in database")
            print("Possible causes:")
            print("  - Brevitas server not running (start with: uvicorn api.server:app)")
            print("  - API key not set in provider_config")
            print("  - Calls did not route through /v1/compress")
            sys.exit(1)

        print_stats_table(stats)

        print("✓ Campaign complete! All agents tracked and attributed to pipeline='campaign-launch'")
        if "pipeline_total" in stats:
            total = stats["pipeline_total"]
            print(f"✓ Total savings: ${total.get('cost_saved_usd', 0):.2f}")
            print(f"✓ Total tokens saved: {total.get('tokens_saved', 0):,}")

    except requests.exceptions.ConnectionError:
        print("❌ ERROR: Cannot connect to Brevitas API")
        print(f"Is the server running? (Expected at {brevitas_base_url})")
        print("Start with: uvicorn api.server:app --host 127.0.0.1 --port 8000")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"❌ API Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error fetching statistics: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
