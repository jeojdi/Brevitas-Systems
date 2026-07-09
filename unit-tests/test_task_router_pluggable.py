"""The injected compress_fn seam: it must be used, protect code fences + forced tokens,
skip tiny segments, and report a reason."""

from token_efficiency_model.lossless.task_router import (
    TaskCompressionRouter,
    _MIN_SEGMENT_TOKENS,
)


def test_injected_compress_fn_is_used_and_saves(recording_compressor):
    prompt = (
        "Please write a long and detailed description of the onboarding flow so that a new "
        "engineer can understand every step without asking anyone for additional help today."
    )
    r = TaskCompressionRouter(compress_fn=recording_compressor).route(prompt)
    assert recording_compressor.calls, "compress_fn was never invoked"
    assert r.reason == "compressed"
    assert r.optimization.lossy is True
    assert r.optimization.tokens_after < r.optimization.tokens_before


def test_code_fence_preserved_byte_for_byte(recording_compressor):
    prose_a = ("Here is a reasonably long piece of prose before the code block that should be "
               "compressed because it comfortably exceeds the minimum segment size threshold.")
    prose_b = ("And here is another long trailing paragraph after the code that is also more than "
               "long enough to be worth compressing on its own without touching the code above.")
    fence = "```python\ndef f(x):\n    return  x  +  1\n```"
    prompt = f"{prose_a}\n{fence}\n{prose_b}"

    r = TaskCompressionRouter(compress_fn=recording_compressor).route(prompt)
    assert "def f(x):\n    return  x  +  1" in r.optimization.optimized  # untouched
    assert r.reason == "compressed"


def test_short_segment_is_skipped(recording_compressor):
    r = TaskCompressionRouter(compress_fn=recording_compressor).route("short prompt here")
    assert r.reason == "too_short"
    assert r.optimization.method == "lossless"
    assert not recording_compressor.calls


def test_forced_numbers_are_never_dropped(recording_compressor):
    # precise tasks force load-bearing NUMBERS (not prose identifiers, which crash real
    # LLMLingua-2); the number must survive compression and be present in the force list.
    prompt = ("Refactor the api handler so that the cache holds exactly 4096 entries and evicts "
              "the oldest once that limit of 4096 entries is exceeded on every incoming request.")
    r = TaskCompressionRouter(compress_fn=recording_compressor).route(prompt)
    assert r.task == "code"
    assert "4096" in r.optimization.optimized
    assert any("4096" in force for _, _, force in recording_compressor.calls)


def test_min_segment_threshold_lowered_from_40():
    assert _MIN_SEGMENT_TOKENS == 15


def test_none_return_from_compress_fn_falls_back_to_segment():
    # a compressor that always declines -> original text, method lossless, reason too_short
    r = TaskCompressionRouter(compress_fn=lambda s, rate, force: None).route(
        "This is a sufficiently long prose segment that would otherwise be compressed here."
    )
    assert r.optimization.method == "lossless"
    assert r.reason == "too_short"
    assert r.optimization.optimized  # non-empty, unchanged content
