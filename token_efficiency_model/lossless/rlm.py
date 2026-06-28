"""Lever 5 — Recursive Language Model (RLM).

Faithful implementation of Algorithm 1 from "Recursive Language Models"
(Zhang, Kraska, Khattab, MIT CSAIL, arXiv:2512.24601):

    state <- InitREPL(prompt = P)            # the long prompt P lives as a REPL VARIABLE
    state <- AddFunction(state, sub_RLM_M)   # the model can recursively call itself
    hist  <- [ Metadata(state) ]             # only constant-size metadata enters context
    while True:
        code          <- LLM_M(hist)         # the model writes code
        state, stdout <- REPL(state, code)   # execute; intermediates stay in REPL vars
        hist <- hist || code || Metadata(stdout)   # only a short prefix+len of stdout re-enters
        if state[Final] is set: return state[Final]

Key design choices from the paper, preserved here:
  1. The model gets a *symbolic handle* to P — it never copies the full prompt into context.
  2. Output is built up in a REPL variable (`Final`), so it can exceed the context window.
  3. *Symbolic recursion*: code inside the REPL invokes the model over programmatic slices
     of P (here exposed as the `sub_llm(prompt)` function), and only constant-size metadata
     of each stdout re-enters the root model's history.

This module is model-agnostic: pass any `llm(prompt:str)->str` callable. A restricted REPL
executes the model's emitted Python with only the prompt variable + sub_llm + safe builtins
in scope, so the root context stays tiny regardless of |P|.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class REPLState:
    """Persistent REPL environment. `P` is the long prompt as a variable; `Final` is the
    output slot; `env` holds intermediate variables the model builds up."""
    P: str
    env: Dict[str, object] = field(default_factory=dict)
    final: Optional[str] = None


def _metadata(text: str, head: int = 240) -> str:
    """Constant-size view of a string: its length + a short prefix (paper's Metadata())."""
    text = text if isinstance(text, str) else str(text)
    prefix = text[:head].replace("\n", " ")
    return f"<len={len(text)} chars; prefix={prefix!r}{'...' if len(text) > head else ''}>"


class RLM:
    """Recursive Language Model scaffold around a base LLM callable."""

    def __init__(self, llm: Callable[[str], str], max_iters: int = 8,
                 stdout_head: int = 240):
        self.llm = llm
        self.max_iters = max_iters
        self.stdout_head = stdout_head

    # -- the recursive sub-call exposed *inside* the REPL ------------------- #
    def _sub_llm(self, prompt: str) -> str:
        """sub_RLM_M: invoke the base model on a programmatically constructed slice of P.
        (Depth-1 recursion: a sub-call is a direct model call, matching the paper's
        recursion=1 setting used in most experiments.)"""
        return self.llm(prompt)

    def _repl(self, state: REPLState, code: str) -> str:
        """Execute model-emitted code with P, sub_llm, and the persistent env in scope.
        Returns captured stdout. Restricted namespace keeps this a tool, not arbitrary exec."""
        import io
        import contextlib
        import re as re_module

        safe_builtins = {
            "len": len, "range": range, "min": min, "max": max, "sum": sum,
            "sorted": sorted, "enumerate": enumerate, "list": list, "dict": dict,
            "str": str, "int": int, "float": float, "print": print, "abs": abs,
            "any": any, "all": all, "set": set, "zip": zip, "map": map, "filter": filter,
            "chr": chr, "ord": ord, "bool": bool, "round": round, "reversed": reversed,
            "isinstance": isinstance, "tuple": tuple, "repr": repr,
        }

        def set_final(value: str) -> None:
            state.final = value if isinstance(value, str) else str(value)

        def grep(pattern: str, context_lines: int = 0) -> List[str]:
            """Search P for lines matching a regex pattern. Return matching lines with context.

            Examples:
              grep("answer", context_lines=1)  # lines containing "answer", plus 1 line before/after
              grep("^[0-9]+\\.", context_lines=0)  # lines starting with digits and period
            """
            try:
                regex = re_module.compile(pattern, re_module.IGNORECASE | re_module.MULTILINE)
            except re_module.error:
                return []

            lines = state.P.split('\n')
            results = []
            for i, line in enumerate(lines):
                if regex.search(line):
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    results.extend(lines[start:end])
                    results.append("---")
            return results if results else []

        def peek(start: int, end: int) -> str:
            """Return a slice of P from character index start to end (exclusive).

            Examples:
              peek(0, 100)  # first 100 characters
              peek(1000, 1500)  # characters 1000–1499
            """
            return state.P[start:end]

        ns = dict(state.env)
        ns.update({
            "P": state.P,
            "prompt": state.P,
            "sub_llm": self._sub_llm,
            "set_final": set_final,
            "grep": grep,
            "peek": peek,
            "__builtins__": safe_builtins,
        })
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(code, ns)  # noqa: S102 - restricted namespace; this is the REPL tool
        except Exception as e:  # surface errors back to the model like a real REPL
            buf.write(f"\n<error: {type(e).__name__}: {e}>")
        # persist intermediate variables (excluding the injected handles)
        for k, v in ns.items():
            if k not in ("P", "prompt", "sub_llm", "set_final", "grep", "peek", "__builtins__"):
                state.env[k] = v
        return buf.getvalue()

    def run(self, prompt: str, question: str) -> "RLMResult":
        """Run the RLM loop over a long `prompt` (the environment) to answer `question`."""
        state = REPLState(P=prompt)
        # Root sees only constant-size metadata about P — never P itself.
        hist: List[str] = [
            "You are a Recursive Language Model. The long input is stored in the REPL "
            "variable P (a string). You may NOT assume you have seen P; inspect it with code.\n"
            f"P metadata: {_metadata(prompt)}\n\n"
            "REPL Functions:\n"
            "  - grep(pattern, context_lines=0) -> list[str]: Search P for lines matching regex. Returns list of matching lines.\n"
            "  - peek(start, end) -> str: Return characters P[start:end]. Use for targeted extraction.\n"
            "  - sub_llm(text) -> str: Ask the base model about text. MUST pass actual text + the question, e.g. sub_llm(f'{passage}\\n\\nQ: {question}').\n"
            "  - set_final(answer) -> None: Store final answer. REQUIRED at the end.\n"
            "  - print(...): Debug output (appears as metadata).\n\n"
            "IMPORTANT USAGE PATTERN:\n"
            "1. SEARCH first: Use grep() with patterns like '[0-9]+', '[A-Z][a-z]+', etc.\n"
            "2. EXTRACT next: For each matching passage, call sub_llm(passage_text + question).\n"
            "   Example: sub_llm(f'{passage_text}\\n\\nAnswer the question: {question}')\n"
            "3. ACCUMULATE: Collect answers in a list as you go.\n"
            "4. SYNTHESIZE: Call set_final() with the aggregated answer.\n\n"
            "STRATEGY FOR MULTI-DOCUMENT QA:\n"
            "1. First, search P for passages likely to contain the answer (grep for keywords from the question)\n"
            "2. Extract those passages and call sub_llm on them ONLY\n"
            "3. Synthesize the answer from the relevant passages\n"
            "4. Return via set_final()\n\n"
            "WORKED EXAMPLE (question: 'Who won the 2020 election?'):\n"
            "```python\n"
            "# Extract keywords from question\n"
            "keywords = ['2020', 'election', 'won', 'winner']\n"
            "# Search for passages mentioning these keywords\n"
            "relevant = []\n"
            "for kw in keywords:\n"
            "    matches = grep(kw, context_lines=2)\n"
            "    if matches:\n"
            "        passage_text = '\\n'.join(m for m in matches if m != '---')\n"
            "        relevant.append(passage_text)\n"
            "# Ask about the relevant passages\n"
            "if relevant:\n"
            "    combined = '\\n---\\n'.join(relevant[:3])  # limit to first 3 relevant sections\n"
            "    ans = sub_llm(f'Passages:\\n{combined}\\n\\nQuestion: {question}\\nAnswer in 1-3 words:')\n"
            "    answer = ans.strip().split('\\n')[0][:50]\n"
            "else:\n"
            "    answer = 'Not found'\n"
            "set_final(answer)\n"
            "```\n\n"
            f"Question: {question}\n\n"
            "CRITICAL: Each code block must end with set_final() — never skip this step.\n"
            "Reply with a single fenced ```python code block each turn. Use sub_llm() to extract "
            "answers from text slices, then call set_final() with the final answer."
        ]
        iters = 0
        sub_calls = 0
        accumulated_slices = []  # Track relevant slices for fallback synthesis

        for _ in range(self.max_iters):
            iters += 1
            reply = self.llm("\n\n".join(hist))
            code = _extract_code(reply)
            if code is None:
                # no code -> treat the reply itself as the final answer (graceful)
                state.final = reply.strip()
                break
            sub_calls += code.count("sub_llm(")
            stdout = self._repl(state, code)
            hist.append(f"```python\n{code}\n```")
            hist.append(f"stdout: {_metadata(stdout, self.stdout_head)}")
            # Track output for fallback synthesis
            if stdout.strip():
                accumulated_slices.append(stdout)
            if state.final is not None:
                break

        # Fallback: if the loop ended without set_final, synthesize from accumulated output
        if state.final is None and accumulated_slices:
            synthesis_prompt = (
                f"Based on the following partial search results about the question '{question}', "
                f"synthesize a final answer (be concise):\n\n"
                + "\n\n".join(accumulated_slices[:3])  # limit to first 3 slices to avoid context bloat
            )
            state.final = self.llm(synthesis_prompt).strip()
            sub_calls += 1

        return RLMResult(answer=state.final or "", iters=iters, sub_calls=sub_calls,
                         root_context_chars=sum(len(h) for h in hist))


def _extract_code(reply: str) -> Optional[str]:
    """Pull a python code block from the model reply (```python ... ``` or ``` ... ```)."""
    if "```" not in reply:
        return None
    parts = reply.split("```")
    for seg in parts[1:]:
        body = seg
        if body.lstrip().lower().startswith("python"):
            body = body.lstrip()[len("python"):]
        if body.strip():
            return body.strip("\n")
    return None


@dataclass
class RLMResult:
    answer: str
    iters: int
    sub_calls: int
    root_context_chars: int
