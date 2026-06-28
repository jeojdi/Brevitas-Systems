"""
Marketing agency orchestrator: 7-agent DAG for campaign planning.

Sequential execution: intake → researcher → strategist → {copywriter, seo_optimizer} → editor → reporter
All calls routed through Brevitas SDK with pipeline/agent labels.
"""
import sys
from pathlib import Path

# Ensure brevitas is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from brevitas.labels import start_run, agent
from .provider import get_provider


class MarketingAgency:
    """Orchestrates a 7-agent campaign planning workflow."""

    def __init__(self, provider_name: str = None):
        """Initialize with a provider (mock or deepseek)."""
        self.provider = get_provider(provider_name)
        self.context = {}  # Shared context between agents

    def _call_agent(self, agent_name: str, model: str, system_prompt: str, user_input: str) -> str:
        """Call an agent through Brevitas with proper label tracking."""
        # Use the Brevitas SDK with agent context manager
        with agent(agent_name):
            # Build the messages
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ]

            # Call through the provider
            # For DeepSeek, use the real API; for mock, use deterministic responses
            response_text = self.provider.chat(model, messages, temperature=0.7)
            return response_text

    def intake(self, brief: str) -> str:
        """Agent 1: Parse client brief into structured goals."""
        system_prompt = """You are the intake agent for a marketing agency.
Your job is to parse a client brief and extract structured goals, target audience, budget, timeline, and success metrics.
Return a clear summary of what the client wants to achieve."""

        response = self._call_agent(
            agent_name="intake",
            model="deepseek-chat",
            system_prompt=system_prompt,
            user_input=f"Client brief:\n{brief}",
        )
        self.context["brief_summary"] = response
        return response

    def researcher(self) -> str:
        """Agent 2: Conduct market, competitor, and audience research."""
        system_prompt = """You are the research agent for a marketing agency.
Based on the client's goals, conduct market research, competitive analysis, and audience insights.
Identify market trends, top 3 competitors, and key pain points in the target audience."""

        user_input = f"Client goals:\n{self.context['brief_summary']}"

        response = self._call_agent(
            agent_name="researcher",
            model="deepseek-reasoner",
            system_prompt=system_prompt,
            user_input=user_input,
        )
        self.context["research"] = response
        return response

    def strategist(self) -> str:
        """Agent 3: Develop channel and messaging strategy."""
        system_prompt = """You are the strategy agent for a marketing agency.
Based on market research, develop a multi-channel marketing strategy including:
- Best channels for the target audience
- Core messaging pillars
- Brand positioning
- Call-to-action strategy
- Budget allocation across channels"""

        user_input = f"""Client brief:\n{self.context['brief_summary']}

Market research:\n{self.context['research']}"""

        response = self._call_agent(
            agent_name="strategist",
            model="deepseek-reasoner",
            system_prompt=system_prompt,
            user_input=user_input,
        )
        self.context["strategy"] = response
        return response

    def copywriter(self) -> str:
        """Agent 4: Create ad, email, and social copy variants."""
        system_prompt = """You are the copywriter for a marketing agency.
Based on the strategy, create compelling copy for multiple channels:
- LinkedIn professional posts
- Twitter/X engaging tweets
- Email subject lines and body
- Product Hunt launch description
Focus on the core messaging and call-to-action."""

        user_input = f"""Strategy:\n{self.context['strategy']}

Research:\n{self.context['research']}"""

        response = self._call_agent(
            agent_name="copywriter",
            model="deepseek-chat",
            system_prompt=system_prompt,
            user_input=user_input,
        )
        self.context["copy"] = response
        return response

    def seo_optimizer(self) -> str:
        """Agent 5: Generate SEO keywords and on-page optimization."""
        system_prompt = """You are the SEO specialist for a marketing agency.
Based on the strategy and research, generate:
- Target keywords (short-tail and long-tail)
- Meta title and description suggestions
- On-page optimization recommendations
- Link-building strategy
- Content pillars for organic growth"""

        user_input = f"""Strategy:\n{self.context['strategy']}

Research:\n{self.context['research']}"""

        response = self._call_agent(
            agent_name="seo_optimizer",
            model="deepseek-chat",
            system_prompt=system_prompt,
            user_input=user_input,
        )
        self.context["seo"] = response
        return response

    def editor(self) -> str:
        """Agent 6: Quality assurance and brand alignment review."""
        system_prompt = """You are the editor and QA reviewer for a marketing agency.
Review all copy for:
- Brand voice consistency
- Grammar and tone
- Call-to-action clarity
- Factual accuracy based on research
- Message alignment with strategy
Provide feedback and corrections."""

        user_input = f"""Copy to review:\n{self.context['copy']}

Strategy:\n{self.context['strategy']}

Research:\n{self.context['research']}"""

        response = self._call_agent(
            agent_name="editor",
            model="deepseek-chat",
            system_prompt=system_prompt,
            user_input=user_input,
        )
        self.context["editor_feedback"] = response
        return response

    def reporter(self) -> str:
        """Agent 7: Assemble final campaign brief and execution plan."""
        system_prompt = """You are the final reporter for a marketing agency.
Based on all prior work, assemble a comprehensive campaign brief including:
- Executive summary
- Campaign overview (goals, target audience, timeline)
- Strategic approach
- Channel strategy with budget allocation
- Copy variants for each channel
- SEO roadmap
- Success metrics and KPIs
- Next steps and execution calendar"""

        user_input = f"""Brief summary:\n{self.context['brief_summary']}

Research:\n{self.context['research']}

Strategy:\n{self.context['strategy']}

Copy:\n{self.context['copy']}

SEO:\n{self.context['seo']}

Editor feedback:\n{self.context['editor_feedback']}"""

        response = self._call_agent(
            agent_name="reporter",
            model="deepseek-chat",
            system_prompt=system_prompt,
            user_input=user_input,
        )
        self.context["final_brief"] = response
        return response

    def run_campaign(self, brief: str) -> dict:
        """Execute the full 7-agent campaign planning workflow."""
        print("\n" + "=" * 60)
        print("MARKETING AGENCY: Campaign Planning Pipeline")
        print("=" * 60)

        # Phase 1: Intake
        print("\n[1/7] INTAKE AGENT: Parsing brief...")
        intake_result = self.intake(brief)
        print(f"✓ Brief processed\n{intake_result[:200]}...")

        # Phase 2: Researcher
        print("\n[2/7] RESEARCHER AGENT: Analyzing market...")
        research_result = self.researcher()
        print(f"✓ Market research complete\n{research_result[:200]}...")

        # Phase 3: Strategist
        print("\n[3/7] STRATEGIST AGENT: Developing strategy...")
        strategy_result = self.strategist()
        print(f"✓ Strategy developed\n{strategy_result[:200]}...")

        # Phase 4 & 5 (parallel): Copywriter and SEO
        print("\n[4/7] COPYWRITER AGENT: Creating copy...")
        copy_result = self.copywriter()
        print(f"✓ Copy created\n{copy_result[:200]}...")

        print("\n[5/7] SEO_OPTIMIZER AGENT: Optimizing for search...")
        seo_result = self.seo_optimizer()
        print(f"✓ SEO strategy complete\n{seo_result[:200]}...")

        # Phase 6: Editor
        print("\n[6/7] EDITOR AGENT: QA review...")
        editor_result = self.editor()
        print(f"✓ QA complete\n{editor_result[:200]}...")

        # Phase 7: Reporter
        print("\n[7/7] REPORTER AGENT: Final brief...")
        final_result = self.reporter()
        print(f"✓ Campaign brief assembled\n{final_result[:200]}...")

        print("\n" + "=" * 60)
        print("Campaign planning complete!")
        print("=" * 60)

        return {
            "intake": intake_result,
            "researcher": research_result,
            "strategist": strategy_result,
            "copywriter": copy_result,
            "seo_optimizer": seo_result,
            "editor": editor_result,
            "reporter": final_result,
        }
