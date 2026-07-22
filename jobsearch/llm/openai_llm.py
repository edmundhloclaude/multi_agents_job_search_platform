"""OpenAI-backed LLM (spec extension: reasoning layer powered by OpenAI).

Uses the OpenAI Chat Completions API, behind the ``LLM`` interface so the
reasoning agents never import the SDK directly.

Handles both classic chat models (gpt-4o / gpt-4.1: `temperature` + `max_tokens`)
and reasoning models (gpt-5.x / o-series), which instead require
`max_completion_tokens`, reject a custom `temperature`, and accept
`reasoning_effort` (e.g. "high" for maximum intelligence).
"""

from __future__ import annotations

import json
import os
from typing import Any

from .base import LLM

# Model families that use the reasoning-model calling convention.
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


class OpenAILLM(LLM):
    def __init__(self, model: str = "gpt-4o-mini", *, api_key: str | None = None,
                 max_tokens: int = 2000, reasoning_effort: str | None = None):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise ImportError("pip install openai to use OpenAILLM") from e
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        self._client = OpenAI(api_key=key)
        self.model = model
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.is_reasoning = model.startswith(_REASONING_PREFIXES)

    def _extra(self, temperature: float) -> dict:
        """Model-appropriate params. Reasoning models: max_completion_tokens +
        reasoning_effort, no temperature. Classic models: max_tokens + temperature."""
        if self.is_reasoning:
            # Reasoning tokens count against the budget, so give output room.
            kw: dict[str, Any] = {"max_completion_tokens": max(self.max_tokens, 6000)}
            if self.reasoning_effort:
                kw["reasoning_effort"] = self.reasoning_effort
            return kw
        return {"max_tokens": self.max_tokens, "temperature": temperature}

    def complete_text(self, system: str, user: str, *, temperature: float = 0.0) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **self._extra(temperature),
        )
        return (resp.choices[0].message.content or "").strip()

    def complete_json(self, system: str, user: str, *, temperature: float = 0.0) -> dict[str, Any]:
        resp = self._client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                # The word "JSON" must appear for json_object mode.
                {"role": "system", "content": system + "\nRespond ONLY with a valid JSON object."},
                {"role": "user", "content": user},
            ],
            **self._extra(temperature),
        )
        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            start, end = content.find("{"), content.rfind("}")
            if start >= 0 and end > start:
                return json.loads(content[start:end + 1])
            raise
