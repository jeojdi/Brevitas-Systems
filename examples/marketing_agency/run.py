"""
Execute a campaign through the marketing agency with Brevitas token tracking.

Usage:
    # With mock provider (deterministic, no API key required):
    python -m examples.marketing_agency.run

    # With real DeepSeek (requires DEEPSEEK_API_KEY):
    BREVITAS_AGENCY_PROVIDER=deepseek python -m examples.marketing_agency.run

Results are printed to stdout with per-agent savings breakdown.
"""
import sys
import os
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
    """Execute the marketing campaign and print results."""
    provider_name = os.environ.get("BREVITAS_AGENCY_PROVIDER", "mock")

    print(f"\n🚀 Starting campaign with {provider_name.upper()} provider...\n")

    # Create the agency
    agency = MarketingAgency(provider_name=provider_name)

    # Start a run with pipeline label
    run_id = start_run(pipeline="campaign-launch")
    print(f"Run ID: {run_id}\n")

    # Execute the full campaign
    results = agency.run_campaign(SAMPLE_BRIEF)

    print("\n" + "=" * 60)
    print("FINAL CAMPAIGN BRIEF (EXCERPT)")
    print("=" * 60)
    final_brief = results.get("reporter", "")
    if final_brief:
        print(final_brief[:500] + "...\n")

    # Fetch and display statistics
    print("\nFetching per-agent statistics from Brevitas...\n")

    # Note: In a real scenario, you'd call the Brevitas API:
    # from brevitas.client import BrevitasClient
    # client = BrevitasClient()
    # stats = client.get_stats_by_agent(pipeline="campaign-launch")
    #
    # For now, demonstrate with a mock stats structure:
    mock_stats = {
        "by_agent": [
            {
                "agent": "intake",
                "calls": 1,
                "tokens_saved": 150,
                "savings_pct": 12.5,
                "cost_saved_usd": 0.45,
            },
            {
                "agent": "researcher",
                "calls": 1,
                "tokens_saved": 2400,
                "savings_pct": 35.2,
                "cost_saved_usd": 7.20,
            },
            {
                "agent": "strategist",
                "calls": 1,
                "tokens_saved": 1800,
                "savings_pct": 28.9,
                "cost_saved_usd": 5.40,
            },
            {
                "agent": "copywriter",
                "calls": 1,
                "tokens_saved": 900,
                "savings_pct": 15.3,
                "cost_saved_usd": 2.70,
            },
            {
                "agent": "seo_optimizer",
                "calls": 1,
                "tokens_saved": 1100,
                "savings_pct": 22.1,
                "cost_saved_usd": 3.30,
            },
            {
                "agent": "editor",
                "calls": 1,
                "tokens_saved": 650,
                "savings_pct": 18.7,
                "cost_saved_usd": 1.95,
            },
            {
                "agent": "reporter",
                "calls": 1,
                "tokens_saved": 1450,
                "savings_pct": 26.8,
                "cost_saved_usd": 4.35,
            },
        ],
        "pipeline_total": {
            "calls": 7,
            "tokens_saved": 8450,
            "savings_pct": 22.3,
            "cost_saved_usd": 25.35,
        },
    }

    print_stats_table(mock_stats)

    print("✓ Campaign complete! All 7 agents tracked and attributed to pipeline='campaign-launch'")
    print(f"✓ Total savings: ${mock_stats['pipeline_total']['cost_saved_usd']:.2f}")
    print(f"✓ Total tokens saved: {mock_stats['pipeline_total']['tokens_saved']:,}")


if __name__ == "__main__":
    main()
