"""Build an LLM from config, with optional per-agent overrides.

Config shape (under `llm:`):

    llm:
      provider: openai
      model: gpt-5.6-sol
      reasoning_effort: high
      max_tokens: 6000
      agents:                      # optional per-agent overrides
        screener: { reasoning_effort: medium }
        crafter:  { model: gpt-4.1 }   # cheaper for per-bullet rewording

Each agent ("strategy" | "screener" | "crafter") inherits the top-level defaults
and applies its own overrides on top. provider is global.
"""

from __future__ import annotations

from typing import Optional

from .base import LLM

_FIELDS = ("model", "reasoning_effort", "max_tokens")


def make_llm(llm_config: dict, agent: Optional[str] = None) -> Optional[LLM]:
    cfg = llm_config or {}
    if cfg.get("provider") != "openai":
        return None
    settings = {
        "model": cfg.get("model", "gpt-4o-mini"),
        "reasoning_effort": cfg.get("reasoning_effort"),
        "max_tokens": int(cfg.get("max_tokens", 2000)),
    }
    if agent:
        override = (cfg.get("agents") or {}).get(agent) or {}
        for k in _FIELDS:
            if override.get(k) is not None:
                settings[k] = override[k]
    from .openai_llm import OpenAILLM
    return OpenAILLM(model=settings["model"],
                     reasoning_effort=settings["reasoning_effort"],
                     max_tokens=int(settings["max_tokens"]))
