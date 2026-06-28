"""
Provider abstraction for marketing agency.
Supports both mock (deterministic, no API key) and real DeepSeek providers.
"""
import os
from typing import Any


class Provider:
    """Base provider interface."""

    def chat(self, model: str, messages: list[dict], **kwargs: Any) -> str:
        """Send a chat message and return the response text."""
        raise NotImplementedError


class MockProvider(Provider):
    """Deterministic mock provider for CI testing."""

    # Sample responses per agent type for a marketing campaign
    RESPONSES = {
        "intake": "Goals identified: Q3 campaign for widget-saas product. Target audience: technical founders. Channels: LinkedIn, Twitter, product hunt. Budget: $50k. Timeline: 30 days.",
        "researcher": "Market analysis complete. Widget-saas space growing 40% YoY. Top 3 competitors: Acme Inc, TechFlow, Widget Pro. Audience pain points: vendor lock-in, integration complexity, pricing opacity. Opportunity gap: transparent pricing + open API.",
        "strategist": "Channel strategy: LinkedIn thought leadership + Twitter engagement + PH launch. Messaging pillar: 'Widget freedom for engineers'. Key differentiator: transparent pricing model. Call-to-action: free trial + early-adopter discount. Budget allocation: 50% LinkedIn ads, 30% influencer partnerships, 20% content.",
        "copywriter": "LinkedIn: 'Every engineer deserves a transparent widget solution. No hidden fees. Full API docs. 30-day free trial.' Twitter: 'widget-saas without the lock-in 🔓 transparent pricing. open api. your data, your rules.' Product Hunt: 'Widget Freedom for Engineers - Transparent, Open, Simple.'",
        "seo_optimizer": "Keywords: 'open widget saas', 'transparent pricing widget', 'widget api freedom'. On-page: meta title='Open Widget SaaS | Transparent Pricing', meta desc='Widget platform with open API and transparent pricing.' Link strategy: PH, HN, /r/webdev posts.",
        "editor": "Copy approved with feedback: - LinkedIn: tighten value prop to 2 sentences. - Twitter: emoji usage good, add one more stat. - Product Hunt: add 'trusted by 500+ engineers' social proof. All changes integrated and brand-aligned.",
        "reporter": "Final campaign brief: Q3 Widget Freedom Campaign. 7-agent orchestrated strategy. LinkedIn+Twitter+PH launch. Transparent pricing messaging. 40% market growth opportunity. Full execution plan with daily social calendar, ad copy variants, and SEO roadmap attached.",
    }

    def chat(self, model: str, messages: list[dict], **kwargs: Any) -> str:
        """Return a deterministic mock response based on message content."""
        # Infer agent from message context or role
        for agent_name, response in self.RESPONSES.items():
            if agent_name in str(messages).lower():
                return response
        # Default response
        return f"Mock response from {model}"


class DeepSeekProvider(Provider):
    """Real DeepSeek provider via OpenAI-compatible API."""

    def __init__(self):
        """Initialize with DeepSeek API key from environment."""
        import os
        self.api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("Deepseek_api_key")
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY not found in environment")
        # Use OpenAI client with DeepSeek endpoint
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package required for DeepSeek provider. Install with: pip install openai")
        self.client = OpenAI(
            api_key=self.api_key,
            base_url="https://api.deepseek.com/v1",
        )

    def chat(self, model: str, messages: list[dict], **kwargs: Any) -> str:
        """Send a real request to DeepSeek and return the response text."""
        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            **kwargs
        )
        return response.choices[0].message.content


def get_provider(provider_name: str = None) -> Provider:
    """Get provider instance based on BREVITAS_AGENCY_PROVIDER env var or parameter."""
    if provider_name is None:
        provider_name = os.environ.get("BREVITAS_AGENCY_PROVIDER", "mock")

    if provider_name == "mock":
        return MockProvider()
    elif provider_name == "deepseek":
        return DeepSeekProvider()
    else:
        raise ValueError(f"Unknown provider: {provider_name}")
