"""
Wraps openai.OpenAI so that chat.completions.create() automatically
compresses context before forwarding to OpenAI.

Usage:
    import openai, brevitas
    brevitas.configure(api_key="bvt_...", base_url="http://localhost:8000")
    client = brevitas.wrap(openai.OpenAI(api_key="sk-..."))
    response = client.chat.completions.create(model="gpt-4o", messages=[...])
"""
from __future__ import annotations

from typing import Any

from .._compress import compress_messages, report_usage
from ..session import BrevitasSession
from ..labels import resolve_labels

_PROVIDER = "openai"


class _BrevitasCompletions:
    def __init__(self, completions_obj: Any, session: BrevitasSession) -> None:
        self._orig = completions_obj
        self._session = session

    def create(self, *, messages: list[dict], model: str = "", **kwargs: Any) -> Any:
        task = kwargs.pop("_brevitas_task", "")
        _brevitas_meta = kwargs.pop("_brevitas_meta", None)
        labels = resolve_labels(_brevitas_meta)

        compressed, baseline, compressed_tok = compress_messages(
            messages, self._session, task=task,
            pipeline=labels["pipeline"],
            agent=labels["agent"],
            run_id=labels["run_id"],
        )
        response = self._orig.create(messages=compressed, model=model, **kwargs)
        response_text = ""
        if hasattr(response, "choices") and response.choices:
            response_text = response.choices[0].message.content or ""
        self._session.record_response(response_text)
        self._session.advance()
        report_usage(_PROVIDER, model, baseline, compressed_tok, self._session,
                     pipeline=labels["pipeline"],
                     agent=labels["agent"],
                     run_id=labels["run_id"])
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


class _BrevitasChat:
    def __init__(self, chat_obj: Any, session: BrevitasSession) -> None:
        self._orig = chat_obj
        self.completions = _BrevitasCompletions(chat_obj.completions, session)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


class BrevitasOpenAIClient:
    def __init__(self, client: Any, session: BrevitasSession | None = None) -> None:
        self._client = client
        self._session = session or BrevitasSession()
        self.chat = _BrevitasChat(client.chat, self._session)

    @property
    def session(self) -> BrevitasSession:
        return self._session

    def new_session(self) -> None:
        self._session = BrevitasSession()
        self.chat = _BrevitasChat(self._client.chat, self._session)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
