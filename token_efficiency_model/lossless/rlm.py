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

        ns = dict(state.env)
        ns.update({
            "P": state.P,
            "prompt": state.P,
            "sub_llm": self._sub_llm,
            "set_final": set_final,
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
            if k not in ("P", "prompt", "sub_llm", "set_final", "__builtins__"):
                state.env[k] = v
        return buf.getvalue()

    def run(self, prompt: str, question: str) -> "RLMResult":
        """Run the RLM loop over a long `prompt` (the environment) to answer `question`."""
        state = REPLState(P=prompt)
        # Root sees only constant-size metadata about P — never P itself.
        hist: List[str] = [
            "You are a Recursive Language Model. The long input is stored in the REPL "
            "variable P (a string). You may NOT assume you have seen P; inspect it with code.\n"
            f"P metadata: {_metadata(prompt)}\n"
            "Available in the REPL: P (str), sub_llm(prompt:str)->str to ask the base model "
            "about a slice of P, set_final(answer:str) to finish, and print() to observe.\n"
            f"Question: {question}\n"
            "Reply with a single fenced ```python code block each turn. Build up the answer, "
            "then call set_final(...)."
        ]
        iters = 0
        sub_calls = 0
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
            if state.final is not None:
                break
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
