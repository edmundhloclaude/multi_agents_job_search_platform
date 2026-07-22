"""Pluggable LLM interface for the SAFE reasoning agents.

The agents depend only on this abstraction, never on a specific provider, so an
OpenAI backend, a Claude backend, or a deterministic fake can be swapped in. All
methods are pure request/response — no browser, no side effects — keeping the
reasoning agents SAFE.
"""

from __future__ import annotations

import abc
from typing import Any


class LLM(abc.ABC):
    @abc.abstractmethod
    def complete_text(self, system: str, user: str, *, temperature: float = 0.0) -> str:
        """Return a plain-text completion."""

    @abc.abstractmethod
    def complete_json(
        self, system: str, user: str, *, temperature: float = 0.0
    ) -> dict[str, Any]:
        """Return a JSON object completion (the model is asked for strict JSON)."""


class EchoLLM(LLM):
    """Deterministic fake for tests — no network. Returns canned/derived output."""

    def __init__(self, text: str = "", json_obj: dict | None = None):
        self._text = text
        self._json = json_obj or {}

    def complete_text(self, system: str, user: str, *, temperature: float = 0.0) -> str:
        return self._text

    def complete_json(self, system: str, user: str, *, temperature: float = 0.0) -> dict:
        return dict(self._json)
