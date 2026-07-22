"""OpenAI-backed LLM (spec extension: reasoning layer powered by OpenAI).

Uses the OpenAI Chat Completions API. JSON responses use JSON mode. Kept behind
the ``LLM`` interface so the reasoning agents never import the SDK directly.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .base import LLM


class OpenAILLM(LLM):
    def __init__(self, model: str = "gpt-4o-mini", *, api_key: str | None = None,
                 max_tokens: int = 1200):
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

    def complete_text(self, system: str, user: str, *, temperature: float = 0.0) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    def complete_json(self, system: str, user: str, *, temperature: float = 0.0) -> dict[str, Any]:
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system + "\nRespond ONLY with a valid JSON object."},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # last-ditch: extract the first {...} block
            start, end = content.find("{"), content.rfind("}")
            if start >= 0 and end > start:
                return json.loads(content[start:end + 1])
            raise
