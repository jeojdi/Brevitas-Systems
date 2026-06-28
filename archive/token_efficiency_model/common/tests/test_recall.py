import pytest
from token_efficiency_model.common.recall import critical_context_recall


class TestCriticalContextRecall:
    def test_basic_case(self):
        """Test basic case with partial matches."""
        must_keep = ["P99<200ms", "HIPAA"]
        surviving_text = "we need P99<200ms and SOC-2"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 0.5

    def test_empty_must_keep(self):
        """Test that empty must_keep returns 1.0 (vacuous truth)."""
        must_keep = []
        surviving_text = "some random text"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 1.0

    def test_case_insensitive_and_whitespace_normalized(self):
        """Test case-insensitive and whitespace normalization."""
        must_keep = ["Foo  Bar"]
        surviving_text = "we used foo bar today"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 1.0

    def test_all_survive(self):
        """Test when all items survive."""
        must_keep = ["alpha", "beta", "gamma"]
        surviving_text = "we need alpha and beta and gamma"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 1.0

    def test_zero_survive(self):
        """Test when no items survive."""
        must_keep = ["alpha", "beta", "gamma"]
        surviving_text = "we need delta and epsilon"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 0.0

    def test_whitespace_only_entries_ignored(self):
        """Test that whitespace-only entries are ignored (don't count toward denominator)."""
        must_keep = ["alpha", "   ", "\t", "beta"]
        surviving_text = "we have alpha and beta"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 1.0

    def test_empty_string_entries_ignored(self):
        """Test that empty string entries are ignored."""
        must_keep = ["alpha", "", "beta"]
        surviving_text = "we have alpha and beta"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 1.0

    def test_multiple_whitespace_collapsed(self):
        """Test that multiple whitespaces are collapsed to single space."""
        must_keep = ["foo    bar    baz"]
        surviving_text = "the foo bar baz test"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 1.0

    def test_mixed_case_and_whitespace(self):
        """Test mixed case variations and whitespace."""
        must_keep = ["SoC  2", "P99 200ms"]
        surviving_text = "we need Soc 2 compliance and p99 200ms latency"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 1.0

    def test_partial_matches_not_counted(self):
        """Test that partial substring matches within words don't count."""
        must_keep = ["test"]
        surviving_text = "testing is important"
        result = critical_context_recall(must_keep, surviving_text)
        # "test" is in "testing", so it should match
        assert result == 1.0

    def test_partial_word_boundary(self):
        """Test substring matching (not word boundary)."""
        must_keep = ["http"]
        surviving_text = "use https://example.com"
        result = critical_context_recall(must_keep, surviving_text)
        assert result == 1.0

    def test_return_type_is_float(self):
        """Test that return value is a float."""
        result = critical_context_recall(["test"], "test")
        assert isinstance(result, float)

    def test_return_value_in_range(self):
        """Test that return value is in [0.0, 1.0]."""
        must_keep = ["a", "b", "c", "d"]
        surviving_text = "we have a and b"
        result = critical_context_recall(must_keep, surviving_text)
        assert 0.0 <= result <= 1.0
