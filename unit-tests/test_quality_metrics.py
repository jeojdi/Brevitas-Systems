"""Information-Density metric: retention per class + the critical-class accept gate."""

from token_efficiency_model.lossless.quality_metrics import information_density

ORIG = ("Deliver the summary in JSON. Never exceed 250 words. Cite sources. "
        "Acme Corp owes Globex 90 days written notice under the agreement.")


def test_identical_text_scores_full_retention():
    d = information_density(ORIG, ORIG)
    assert d["overall_ok"] is True
    for k in ("numbers", "constraints", "formatting", "task"):
        assert d[k] == 1.0


def test_dropping_a_number_fails_the_gate():
    d = information_density(ORIG, ORIG.replace("90 days", "some days").replace("250", "many"))
    assert d["numbers"] < 1.0
    assert d["overall_ok"] is False


def test_dropping_a_constraint_fails_the_gate():
    d = information_density(ORIG, "Deliver the summary. Acme owes Globex written notice.")
    assert d["constraints"] < 1.0
    assert d["overall_ok"] is False


def test_paraphrasing_context_but_keeping_criticals_passes():
    # entities may drift, but numbers + constraints + task survive -> acceptable
    compressed = "Summary JSON. Never exceed 250 words. Cite sources. owes 90 days notice."
    d = information_density(ORIG, compressed)
    assert d["overall_ok"] is True


def test_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("BREVITAS_INFO_DENSITY_MIN", "0.5")
    d = information_density(ORIG, ORIG.replace("Cite sources", ""))  # drop one of several directives
    assert d["min_retain"] == 0.5
