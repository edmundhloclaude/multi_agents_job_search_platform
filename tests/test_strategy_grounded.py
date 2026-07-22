"""Tests for bank-grounded LLM strategy criteria (offline, fake LLM)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.strategy import StrategyAdvisor, load_criteria_from_strategy
from jobsearch.llm.base import LLM
from jobsearch.models import Accomplishment, AccomplishmentBank


class _CriteriaLLM(LLM):
    def __init__(self, payload):
        self.payload = payload
    def complete_text(self, system, user, *, temperature=0.0):
        return ""
    def complete_json(self, system, user, *, temperature=0.0):
        return dict(self.payload)
    model = "fake-model"


@pytest.fixture
def bank():
    return AccomplishmentBank(
        name="Jane Doe", contact={},
        accomplishments=[
            Accomplishment("Acme", "Senior Engineer", "2019", "2023",
                           "Built a distributed system in Python that cut latency 40%.",
                           metrics=["40%"], skills=["python", "distributed systems", "kubernetes"]),
            Accomplishment("Globex", "Software Engineer", "2016", "2019",
                           "Led a team of 6 on a data platform.",
                           metrics=["6"], skills=["python", "postgres", "leadership"]),
        ],
        skills=["python", "distributed systems", "kubernetes", "postgres", "leadership"],
        credentials=["BS Computer Science"],
    )


@pytest.fixture
def profile():
    return {"summary": "Staff-track backend engineer.",
            "comp_min": 180000, "geographies": ["remote"], "remote_ok": True,
            "dealbreakers": ["unpaid"], "target_roles": ["staff engineer"]}


def test_grounded_uses_llm_lists(bank, profile):
    llm = _CriteriaLLM({
        "target_roles": ["Staff Backend Engineer", "Senior Platform Engineer"],
        "seniority": ["staff", "senior"],
        "must_haves": ["python", "distributed systems"],
        "keywords_boost": ["kubernetes", "postgres"],
        "rationale": "Grounded in Acme distributed-systems work.",
    })
    adv = StrategyAdvisor(llm=llm)
    crit, meta = adv.build_criteria_grounded(profile, bank)
    assert crit.target_roles[:2] == ["Staff Backend Engineer", "Senior Platform Engineer"]
    assert "python" in crit.must_haves and "distributed systems" in crit.must_haves
    assert meta["source"].startswith("llm:")
    # hard constraints preserved from profile, not the LLM
    assert crit.comp_min == 180000
    assert crit.dealbreakers == ["unpaid"]


def test_ungrounded_must_have_is_dropped(bank, profile):
    """A must-have the candidate never demonstrated/aspired to is excluded."""
    llm = _CriteriaLLM({
        "target_roles": ["Staff Engineer"], "seniority": ["staff"],
        "must_haves": ["python", "rust", "kubernetes"],  # 'rust' not in bank/profile
        "keywords_boost": [], "rationale": "x",
    })
    crit, meta = StrategyAdvisor(llm=llm).build_criteria_grounded(profile, bank)
    assert "python" in crit.must_haves
    assert "rust" not in crit.must_haves           # dropped as ungrounded
    assert "rust" in meta["dropped_must_haves"]


def test_falls_back_without_llm(bank, profile):
    adv = StrategyAdvisor(llm=None)
    crit, meta = adv.build_criteria_grounded(profile, bank)
    assert meta["source"] == "profile"
    assert crit.target_roles == ["staff engineer"]  # verbatim from profile


def test_run_writes_grounded_md_and_roundtrips(tmp_path, bank, profile):
    llm = _CriteriaLLM({
        "target_roles": ["Staff Backend Engineer"], "seniority": ["staff"],
        "must_haves": ["python", "distributed systems"],
        "keywords_boost": ["kubernetes"],
        "rationale": "Strong distributed-systems track record at Acme.",
    })
    path = tmp_path / "strategy.md"
    crit = StrategyAdvisor(llm=llm).run(profile, path, bank=bank)
    text = path.read_text()
    assert "How these criteria were derived" in text
    assert "distributed-systems" in text or "distributed systems" in text
    # machine-usable block still parses for the Screener
    parsed = load_criteria_from_strategy(path)
    assert "python" in parsed.must_haves
    assert parsed.comp_min == 180000


def test_run_degrades_on_llm_error(tmp_path, bank, profile):
    class Boom(LLM):
        def complete_text(self, *a, **k): raise RuntimeError("boom")
        def complete_json(self, *a, **k): raise RuntimeError("boom")
    path = tmp_path / "strategy.md"
    crit = StrategyAdvisor(llm=Boom()).run(profile, path, bank=bank)
    # fell back to deterministic profile criteria; file still written
    assert path.exists()
    assert crit.target_roles == ["staff engineer"]
