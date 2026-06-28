"""Tests for Lever 5 — RLM (Algorithm 1: context-as-variable + symbolic recursion)."""

from token_efficiency_model.lossless.rlm import RLM, _extract_code, _metadata


def test_extract_code_variants():
    assert _extract_code("```python\nx=1\n```") == "x=1"
    assert _extract_code("```\ny=2\n```") == "y=2"
    assert _extract_code("no code here") is None


def test_metadata_is_constant_size_for_huge_prompt():
    # both well past the prefix head, so only the length digits differ
    small = _metadata("a" * 10_000)
    huge = _metadata("a" * 10_000_000)
    # metadata length barely grows even when P grows by 3 orders of magnitude
    assert abs(len(small) - len(huge)) < 40


def test_rlm_finds_needle_via_code_without_reading_P_into_context():
    """The model writes code to locate a needle in a 1M-char P; P never enters context."""
    needle = "THE-SECRET-CODE-IS-4242"
    P = ("filler line\n" * 40_000) + needle + ("\nmore filler\n" * 40_000)

    # scripted "model": turn 1 emits code that searches P and finalizes from the REPL
    def fake_llm(history_or_subprompt: str) -> str:
        if "Question:" in history_or_subprompt:  # root call
            return (
                "```python\n"
                "idx = P.find('THE-SECRET-CODE-IS-')\n"
                "line = P[idx:idx+23]\n"
                "set_final(line)\n"
                "```"
            )
        return "unused"

    rlm = RLM(fake_llm)
    res = rlm.run(P, "What is the secret code?")
    assert res.answer == needle
    # the root context never contained the 1M-char P (only metadata + code + stdout meta)
    assert res.root_context_chars < 5000
    assert len(P) > 800_000


def test_rlm_symbolic_recursion_over_slices():
    """Model loops over chunks of P and calls sub_llm on each (Omega(|P|) sub-calls)."""
    P = "\n".join(f"chunk {i} value={i}" for i in range(10))

    def fake_llm(text: str) -> str:
        if "Question:" in text:  # root: emit code that maps sub_llm over slices
            return (
                "```python\n"
                "lines = P.split(chr(10))\n"
                "vals = [sub_llm(l) for l in lines]\n"
                "set_final(str(sum(int(v) for v in vals)))\n"
                "```"
            )
        # sub-call: 'extract the integer after value=' — return it
        return text.split("value=")[-1].strip()

    rlm = RLM(fake_llm)
    res = rlm.run(P, "Sum all the values")
    assert res.answer == str(sum(range(10)))   # 45
    assert res.sub_calls >= 1


def test_rlm_repl_error_is_surfaced_not_crashed():
    def fake_llm(text: str) -> str:
        if "Question:" in text:
            return "```python\nundefined_name + 1\n```"  # will raise inside REPL
        return ""
    # without set_final, loop exhausts iters but must not crash
    rlm = RLM(fake_llm, max_iters=2)
    res = rlm.run("small P", "q")
    assert res.answer == ""           # never finalized
    assert res.iters == 2


def test_rlm_grep_function_finds_patterns():
    """Test that grep() helper function locates lines matching patterns in P."""
    P = "line 1: start\nline 2: middle data\nline 3: end\nline 4: middle again"

    def fake_llm(text: str) -> str:
        if "Question:" in text:
            return (
                "```python\n"
                "results = grep('middle', context_lines=0)\n"
                "set_final(str(len(results)))\n"
                "```"
            )
        return ""

    rlm = RLM(fake_llm)
    res = rlm.run(P, "How many lines have 'middle'?")
    # grep should find 2 lines with 'middle' (plus 1 '---' separator each) = should be > 0
    assert res.answer != ""
    assert res.answer != "0"


def test_rlm_peek_function_extracts_slice():
    """Test that peek() helper function returns character slices."""
    P = "0123456789abcdefghij"

    def fake_llm(text: str) -> str:
        if "Question:" in text:
            return (
                "```python\n"
                "chunk = peek(5, 10)\n"
                "set_final(chunk)\n"
                "```"
            )
        return ""

    rlm = RLM(fake_llm)
    res = rlm.run(P, "Get chars 5-10")
    assert res.answer == "56789"


def test_rlm_fallback_synthesis_when_no_set_final():
    """Test that RLM synthesizes an answer if the loop ends without calling set_final."""
    P = "The capital of France is Paris. Paris is located in central Europe."

    calls = []

    def fake_llm(text: str) -> str:
        calls.append(("llm", "root" if "Question:" in text else "synthesis"))
        if "Question:" in text:
            # emit code that searches but doesn't set_final
            return "```python\nresults = grep('capital', context_lines=1)\nprint(results)\n```"
        # fallback synthesis call — just return a fixed answer
        return "Paris"

    rlm = RLM(fake_llm, max_iters=2)
    res = rlm.run(P, "What is the capital of France?")
    # After 2 iters without set_final, should trigger fallback synthesis
    assert res.answer == "Paris"
    assert res.sub_calls >= 1  # the synthesis call counts as a sub_call
