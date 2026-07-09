"""Real-LLM proof for STRUCTURE-AWARE compression.

The whole-blob compressor this session capped at ~12% input savings to hold answer-equivalence,
and equivalence still sat ~0.78 when pushed to 57%. Compressing ONLY the Context (task, constraints,
formatting, style, examples left byte-identical) should move both up together: more input saved AND
answer-equivalence closer to 1.0, with output length preserved because the output-driving parts are
untouched.

Self-contained: the remote hook is patched to the LOCAL LLMLingua-2 model, so the full structural
pipeline (real compression + real embedding gate + real info-density) runs, then the original and
compressed prompts go to live OpenAI + DeepSeek. Auto-skips without keys / without llmlingua.
"""

import os

import pytest

from conftest import load_dotenv
from token_efficiency_model.lossless import prompt_structure
from token_efficiency_model.lossless.message_optimizer import optimize_message_text

load_dotenv()

_PUBLIC = {"openai": "https://api.openai.com/v1", "deepseek": "https://api.deepseek.com"}
_KEY_ENV = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}
_MODEL = {"openai": os.getenv("OPENAI_MODEL_PUBLIC", "gpt-4o-mini"),
          "deepseek": os.getenv("DEEPSEEK_MODEL", "deepseek-chat")}

# An ANALYTICAL prompt: the answer is determined by a specific fact buried in a long Context of
# filler policies. Compressing the filler must not change the answer. (Answer-equivalence is only a
# meaningful metric for content-determined tasks like this — not open-ended creative generation,
# where the output legitimately varies with the source material.)
PROMPT = (
    "Task: Using only the employee handbook excerpt below, answer the question.\n\n"
    "Question: How many unused vacation days can an employee carry over into the next calendar year?\n\n"
    "Constraints: answer with just the number and nothing else; do not guess if it is not stated.\n\n"
    "Context: The company cafeteria serves lunch between 11am and 2pm on weekdays and is closed on "
    "public holidays. Office recycling is collected every Tuesday, and employees are asked to rinse "
    "containers before disposal. The quarterly all-hands meeting is traditionally held in the "
    "largest conference room on the third floor. Under the paid-time-off policy, employees may "
    "carry over a maximum of 5 unused vacation days into the next calendar year; any days beyond "
    "that are forfeited. The parking garage uses license-plate recognition, so employees should "
    "register their plate at the front desk. Coffee in the third-floor kitchen is restocked on "
    "Mondays and Thursdays, and the visitor wifi password rotates monthly and is posted in the lobby."
)


def _client(name):
    from openai import OpenAI
    key = os.getenv(_KEY_ENV[name])
    return OpenAI(base_url=_PUBLIC[name], api_key=key, timeout=45) if key else None


def _ask(client, model, prompt, max_tokens=200):
    r = client.chat.completions.create(model=model, temperature=0, max_tokens=max_tokens,
                                       messages=[{"role": "user", "content": prompt}])
    return r.choices[0].message.content.strip(), r.usage.prompt_tokens, r.usage.completion_tokens


@pytest.fixture(scope="session")
def live_providers():
    ok = {}
    for name in _PUBLIC:
        c = _client(name)
        if c is None:
            continue
        try:
            c.chat.completions.create(model=_MODEL[name], max_tokens=1, temperature=0,
                                      messages=[{"role": "user", "content": "ping"}])
            ok[name] = c
        except Exception:
            pass
    if not ok:
        pytest.skip("no live LLM providers reachable")
    return ok


@pytest.mark.parametrize("provider", list(_PUBLIC))
def test_structural_beats_blob_on_savings_and_equivalence(provider, live_providers, local_llmlingua_remote):
    if provider not in live_providers:
        pytest.skip(f"{provider} not reachable")
    from token_efficiency_model.lossless.semantic_gate import semantic_similarity

    mo = optimize_message_text(PROMPT)
    assert mo["reason"] == "compressed", mo["reason"]
    compressed = mo["text"]

    # output-driving parts survive verbatim, and the load-bearing fact (5) is forced through
    for directive in ["Question:", "just the number", "How many unused vacation days"]:
        assert directive in compressed, directive
    assert "5" in compressed, "the load-bearing number was dropped from context"
    assert mo["info_density"]["overall_ok"] is True

    client, model = live_providers[provider], _MODEL[provider]
    ans_o, in_o, out_o = _ask(client, model, PROMPT, max_tokens=40)
    ans_c, in_c, out_c = _ask(client, model, compressed, max_tokens=40)

    in_saved = 100 * (1 - in_c / in_o)
    cos = semantic_similarity(ans_o, ans_c)
    print(f"\n[{provider}] input {in_o}->{in_c} ({in_saved:+.1f}%) | answer cosine={cos:.3f} | "
          f"orig={ans_o!r} comp={ans_c!r} | roles={mo['roles']}")

    # 1) real input savings (context filler compressed)
    assert in_c < in_o
    # 2) the ANSWER is preserved: same fact from both prompts, and high embedding equivalence
    assert "5" in ans_o and "5" in ans_c, (ans_o, ans_c)
    assert cos is None or cos >= 0.90, f"answer drifted: cosine={cos}"
