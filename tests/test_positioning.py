"""Tests: strategy positioning + gap report + Crafter consuming positioning."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.crafter import Crafter, _relevance
from jobsearch.agents.strategy import (
    StrategyAdvisor, gap_report, load_positioning_from_strategy,
    load_criteria_from_strategy,
)
from jobsearch.llm.base import LLM
from jobsearch.models import (
    Accomplishment, AccomplishmentBank, Posting, Positioning, StrategyCriteria,
)
from jobsearch.store.job_store import JobStore


class _LLM(LLM):
    model = "fake"
    def __init__(self, payload): self.payload = payload
    def complete_text(self, *a, **k): return ""
    def complete_json(self, *a, **k): return dict(self.payload)


@pytest.fixture
def bank():
    return AccomplishmentBank(
        name="Jane Doe", contact={"email": "j@e.com"},
        accomplishments=[
            Accomplishment("Acme", "Senior Engineer", "2019", "2023",
                           "Built distributed systems in Python that cut latency 40%.",
                           metrics=["40%"], skills=["python", "distributed systems"]),
            Accomplishment("Globex", "Engineer", "2016", "2019",
                           "Led a team of 6 engineers to ship a platform.",
                           metrics=["6"], skills=["leadership", "python"]),
        ],
        skills=["python", "distributed systems", "leadership"], credentials=[])


@pytest.fixture
def profile():
    return {"summary": "backend eng", "comp_min": 180000, "dealbreakers": ["unpaid"]}


# --------------------------------------------------------------------------- #
# Strategy emits grounded positioning
# --------------------------------------------------------------------------- #
def test_positioning_grounded_and_roundtrips(tmp_path, bank, profile):
    llm = _LLM({
        "target_roles": ["Staff Engineer"], "seniority": ["staff"],
        "must_haves": ["python"], "keywords_boost": ["distributed systems"],
        "rationale": "x",
        "positioning": {
            "narrative": "Distributed-systems engineer who ships reliable platforms.",
            "lead_with": ["distributed systems", "underwater basket weaving"],  # 2nd ungrounded
            "emphasize": ["python"], "de_emphasize": ["frontend"],
        },
    })
    crit, meta = StrategyAdvisor(llm=llm).build_criteria_grounded(profile, bank)
    pos = meta["positioning"]
    assert "distributed systems" in pos.lead_with
    assert "underwater basket weaving" not in pos.lead_with   # ungrounded -> dropped
    assert pos.narrative.startswith("Distributed-systems")

    path = tmp_path / "strategy.md"
    StrategyAdvisor(llm=llm).run(profile, path, bank=bank)
    loaded = load_positioning_from_strategy(path)
    assert "distributed systems" in loaded.lead_with
    # criteria still parse from the same (now wrapped) block
    assert "python" in load_criteria_from_strategy(path).must_haves


# --------------------------------------------------------------------------- #
# Gap report
# --------------------------------------------------------------------------- #
def test_gap_report_flags_missing_and_weak(bank):
    crit = StrategyCriteria(must_haves=["python", "rust"],
                            keywords_boost=["kubernetes", "leadership"])
    gaps = gap_report(crit, bank)
    assert "rust" in gaps["missing"]           # no evidence
    assert "kubernetes" in gaps["missing"]     # no evidence
    assert "python" not in gaps["missing"]     # well evidenced
    assert "leadership" in gaps["weak"]        # exactly one accomplishment


def test_gaps_render_in_strategy_md(tmp_path, bank, profile):
    llm = _LLM({"target_roles": ["Eng"], "must_haves": ["python", "rust"],
                "keywords_boost": [], "positioning": {}})
    path = tmp_path / "strategy.md"
    StrategyAdvisor(llm=llm).run(profile, path, bank=bank)
    text = path.read_text()
    assert "Skill gaps" in text and "rust" in text


# --------------------------------------------------------------------------- #
# Crafter consumes positioning (deterministic ranking)
# --------------------------------------------------------------------------- #
def test_positioning_biases_accomplishment_order(tmp_path, bank):
    posting = Posting(company="X", title="Engineer", location="Remote", source="t",
                      requirements=["python"])
    store = JobStore(tmp_path / "j.db")
    # Without positioning, the distributed-systems bullet (more python/keyword overlap)
    # would lead. With leadership positioning, the leadership bullet is foregrounded.
    pos = Positioning(lead_with=["leadership"], emphasize=[])
    crafter = Crafter(store, bank, tmp_path / "out", positioning=pos)
    selected = crafter._select(posting)
    assert "team of 6" in selected[0].text.lower() or "led" in selected[0].text.lower()
    # relevance boost is applied
    lead = bank.accomplishments[1]   # the leadership one
    assert _relevance(lead, posting, pos) > _relevance(lead, posting, None)
    store.close()
