"""Per-agent LLM config resolution (offline; dummy key, no network)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.llm.factory import make_llm

_CFG = {
    "provider": "openai",
    "model": "gpt-5.6-sol",
    "reasoning_effort": "high",
    "max_tokens": 6000,
    "agents": {
        "screener": {"reasoning_effort": "medium"},
        "crafter": {"model": "gpt-4.1", "max_tokens": 1500},
    },
}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")   # client builds w/o network


def test_none_provider_returns_none():
    assert make_llm({"provider": "none"}) is None
    assert make_llm({}) is None


def test_default_and_strategy_inherit_defaults():
    for agent in (None, "strategy"):
        llm = make_llm(_CFG, agent)
        assert llm.model == "gpt-5.6-sol"
        assert llm.reasoning_effort == "high"
        assert llm.max_tokens == 6000
        assert llm.is_reasoning is True


def test_screener_overrides_only_effort():
    llm = make_llm(_CFG, "screener")
    assert llm.model == "gpt-5.6-sol"          # inherited
    assert llm.reasoning_effort == "medium"    # overridden


def test_crafter_uses_cheap_classic_model():
    llm = make_llm(_CFG, "crafter")
    assert llm.model == "gpt-4.1"
    assert llm.max_tokens == 1500
    assert llm.is_reasoning is False           # classic model -> no reasoning params


def test_unknown_agent_falls_back_to_defaults():
    llm = make_llm(_CFG, "does-not-exist")
    assert llm.model == "gpt-5.6-sol" and llm.reasoning_effort == "high"
