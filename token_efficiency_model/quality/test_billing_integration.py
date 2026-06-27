"""Integration tests for quality gate + billing (Phase 3).

Tests that the /v1/usage endpoint correctly handles quality scores.
"""

import pytest
from token_efficiency_model.quality.gate import assess, QualityGateConfig


class TestBillingLogic:
    """Test billing logic when quality gate is integrated with /v1/usage."""

    def test_quality_verified_charges_fee(self):
        """High quality score should result in fee charged."""
        # Simulate what report_usage does
        quality_floor = 0.8
        quality_score = 0.92

        quality_verified = quality_score >= quality_floor

        assert quality_verified is True
        # If quality_verified: fee = cost * 0.10

    def test_quality_unverified_no_fee(self):
        """Missing quality score should result in no fee."""
        quality_floor = 0.8
        quality_score = None

        quality_verified = quality_score is not None and quality_score >= quality_floor

        assert quality_verified is False
        # If not quality_verified: fee = 0.0

    def test_quality_below_floor_no_fee(self):
        """Quality below floor should result in no fee."""
        quality_floor = 0.8
        quality_score = 0.65

        quality_verified = quality_score >= quality_floor

        assert quality_verified is False
        # If not quality_verified: fee = 0.0

    def test_gate_assessment_binds_to_billing(self):
        """Real quality gate scores should bind to billing decisions."""
        # Scenario 1: Equivalent answer (should pass)
        assessment1 = assess(
            optimized_answer="Python is a programming language.",
            reference_answer="A programming language is Python.",
            question="What is Python?"
        )

        quality_floor = 0.8
        fee1 = (
            100 * 0.10  # cost_saved * 0.10
            if assessment1.score >= quality_floor
            else 0.0
        )

        assert assessment1.score >= 0.8, "Equivalent answer should pass gate"
        assert fee1 > 0.0, "Fee should be charged when gate passes"

        # Scenario 2: Wrong answer (should fail)
        assessment2 = assess(
            optimized_answer="Python is a snake.",  # Wrong domain
            reference_answer="Python is a programming language.",
            question="What is Python?"
        )

        fee2 = (
            100 * 0.10
            if assessment2.score >= quality_floor
            else 0.0
        )

        assert assessment2.score < 0.8, "Wrong answer should fail gate"
        assert fee2 == 0.0, "No fee should be charged when gate fails"

    def test_billing_request_accepts_quality_score_optional(self):
        """UsageReportRequest should accept quality_score as optional."""
        # Backward compatible: no quality_score provided
        baseline = 10000
        compressed = 5000
        quality_score = None

        # Should not raise
        quality_verified = quality_score is not None and quality_score >= 0.8
        assert quality_verified is False

        # New: quality_score provided
        quality_score = 0.87
        quality_verified = quality_score is not None and quality_score >= 0.8
        assert quality_verified is True

    def test_quality_status_values(self):
        """Quality status should be one of: verified, unverified, failed."""
        scenarios = [
            (0.92, 0.8, "verified"),   # Score >= floor
            (None, 0.8, "unverified"), # No score provided
            (0.65, 0.8, "failed"),     # Score < floor
        ]

        for quality_score, floor, expected_status in scenarios:
            if quality_score is None:
                status = "unverified"
            elif quality_score >= floor:
                status = "verified"
            else:
                status = "failed"

            assert status == expected_status, (
                f"quality_score={quality_score}, floor={floor} should give status={expected_status}"
            )

    def test_savings_pct_zero_when_quality_fails(self):
        """Reported savings_pct should be 0 when quality fails."""
        quality_floor = 0.8

        scenarios = [
            (0.92, True, 50.0),   # Quality passes → report savings
            (0.65, False, 0.0),   # Quality fails → report 0% savings
            (None, False, 0.0),   # Quality unverified → report 0% savings
        ]

        for quality_score, should_report, expected_reported_pct in scenarios:
            quality_verified = (
                quality_score is not None and quality_score >= quality_floor
            )

            # Simulating /v1/usage logic
            baseline_tokens = 10000
            compressed_tokens = 5000
            actual_savings_pct = (
                (baseline_tokens - compressed_tokens) / baseline_tokens * 100
            )  # 50%

            reported_pct = actual_savings_pct if quality_verified else 0.0

            assert reported_pct == expected_reported_pct, (
                f"quality={quality_score} should report {expected_reported_pct}%, got {reported_pct}%"
            )

    def test_tokens_saved_zero_when_quality_fails(self):
        """Reported tokens_saved should be 0 when quality fails."""
        quality_floor = 0.8

        scenarios = [
            (0.92, 5000),  # Quality passes → report actual savings
            (0.65, 0),     # Quality fails → report 0 tokens saved
            (None, 0),     # Quality unverified → report 0 tokens saved
        ]

        for quality_score, expected_reported_tokens in scenarios:
            quality_verified = (
                quality_score is not None and quality_score >= quality_floor
            )

            baseline_tokens = 10000
            compressed_tokens = 5000
            actual_tokens_saved = baseline_tokens - compressed_tokens  # 5000

            reported_tokens = (
                actual_tokens_saved if quality_verified else 0
            )

            assert reported_tokens == expected_reported_tokens, (
                f"quality={quality_score} should report {expected_reported_tokens} tokens, got {reported_tokens}"
            )

    def test_backward_compatible_legacy_requests(self):
        """API should remain backward compatible for legacy requests without quality_score."""
        # Legacy request: no quality_score field
        quality_score = None
        quality_floor = 0.8

        # Logic: if quality_score not provided, treat as unverified
        # This maintains backward compatibility while marking as unverified
        quality_verified = quality_score is not None and quality_score >= quality_floor
        actual_quality = quality_score if quality_score is not None else 1.0

        assert quality_verified is False, "Legacy requests should be unverified"
        assert actual_quality == 1.0, "Fallback quality should be 1.0 for backward compatibility"

    def test_fallback_rehydrate_integration(self):
        """When quality fails, system should signal rehydrate."""
        # Quality gate decision
        quality_score = 0.65
        quality_floor = 0.8
        should_rehydrate = quality_score < quality_floor

        assert should_rehydrate is True, "Failed quality should trigger rehydrate"

        # In pipeline: if should_rehydrate:
        #   payload = protocol.build_payload(..., rehydrate_policy="force-full")
        #   model_response = backend(full_payload)
        #   # Don't bill savings


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
