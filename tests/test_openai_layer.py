"""Offline tests for the OpenAI-backed layers, using fakes (no network).

Covers the pluggable reasoning path and — critically — that the fabrication
guard still holds when an LLM is in the loop (an LLM that hallucinates a metric
must be refused), plus the read-only enforcement of the OpenAI browser
controller.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.crafter import Crafter, FabricationError
from jobsearch.agents.screener import Screener
from jobsearch.browser.mock_controller import MockComputer
from jobsearch.browser.openai_cua_controller import OpenAIComputerUseController
from jobsearch.llm.base import EchoLLM, LLM
from jobsearch.models import (
    Accomplishment, AccomplishmentBank, Posting, ScreenStatus, StrategyCriteria,
)
from jobsearch.store.job_store import JobStore


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "j.db")
    yield s
    s.close()


@pytest.fixture
def bank():
    return AccomplishmentBank(
        name="Jane Doe", contact={"email": "j@e.com"},
        accomplishments=[
            Accomplishment("Acme Corp", "Senior Engineer", "2019", "2023",
                           "Built a distributed system in Python that cut latency by 40%.",
                           metrics=["40%"], skills=["python", "distributed systems"]),
        ],
        skills=["python", "distributed systems"], credentials=[],
    )


class _FieldLLM(LLM):
    """Returns different JSON per system prompt so reword/cover/score differ."""
    def __init__(self, bullet="", body="", score_obj=None):
        self.bullet, self.body, self.score_obj = bullet, body, (score_obj or {})
    def complete_text(self, system, user, *, temperature=0.0):
        return ""
    def complete_json(self, system, user, *, temperature=0.0):
        if "screener" in system.lower():
            return dict(self.score_obj)
        if "bullet" in system.lower():
            return {"bullet": self.bullet}
        if "cover" in system.lower() or "letter" in system.lower():
            return {"body": self.body}
        return {}


# --------------------------------------------------------------------------- #
# Screener with an LLM
# --------------------------------------------------------------------------- #
def test_screener_uses_llm_result(store):
    p = Posting(company="Acme", title="Staff Engineer", location="Remote", source="t")
    store.upsert_posting(p)
    llm = _FieldLLM(score_obj={"score": 77, "status": "screened_in", "rationale": "great"})
    Screener(store, llm=llm).run(StrategyCriteria())
    got = store.get(p.dedup_key)
    assert got.screen_score == 77
    assert got.screen_status == ScreenStatus.SCREENED_IN.value
    assert got.screen_rationale.startswith("[openai]")


def test_screener_clamps_bad_llm_score(store):
    p = Posting(company="Acme", title="Eng", location="Remote", source="t")
    store.upsert_posting(p)
    llm = _FieldLLM(score_obj={"score": 999, "status": "nonsense", "rationale": "x"})
    Screener(store, llm=llm).run(StrategyCriteria())
    got = store.get(p.dedup_key)
    assert 0 <= got.screen_score <= 100
    assert got.screen_status in (ScreenStatus.SCREENED_IN.value, ScreenStatus.SCREENED_OUT.value)


# --------------------------------------------------------------------------- #
# Crafter with an LLM — the guard must hold
# --------------------------------------------------------------------------- #
def test_crafter_llm_reword_preserving_metrics_passes(store, bank, tmp_path):
    p = Posting(company="Initech", title="Senior Engineer", location="Remote",
                source="t", requirements=["python"])
    store.upsert_posting(p)
    store.annotate_screen(p.dedup_key, status=ScreenStatus.SCREENED_IN, score=90, rationale="")
    # LLM rewords but keeps the real metric 40% and adds no new number.
    llm = _FieldLLM(bullet="Cut latency by 40% via a Python distributed system.",
                    body="I cut latency by 40% at Acme Corp.")
    res = Crafter(store, bank, tmp_path / "out", llm=llm).run()
    assert res == {"crafted": 1, "refused": 0}


def test_crafter_refuses_llm_hallucinated_metric(store, bank, tmp_path):
    """If the LLM invents a metric not in the bank, the guard refuses to emit."""
    p = Posting(company="Initech", title="Senior Engineer", location="Remote",
                source="t", requirements=["python"])
    store.upsert_posting(p)
    store.annotate_screen(p.dedup_key, status=ScreenStatus.SCREENED_IN, score=90, rationale="")
    # LLM hallucinates "999%" — not in the bank.
    llm = _FieldLLM(bullet="Boosted revenue by 999% at Acme Corp.",
                    body="I boosted revenue by 999%.")
    crafter = Crafter(store, bank, tmp_path / "out", llm=llm)
    with pytest.raises(FabricationError):
        crafter.craft_one(store.get(p.dedup_key))
    res = crafter.run()
    assert res["refused"] == 1 and res["crafted"] == 0
    assert store.get(p.dedup_key).resume_path == ""


# --------------------------------------------------------------------------- #
# OpenAI browser controller — read-only enforcement (offline, injected client)
# --------------------------------------------------------------------------- #
def test_openai_cua_readonly_blocks_mutation():
    ctrl = OpenAIComputerUseController(MockComputer(), submit_enabled=False,
                                       client=object())
    assert ctrl.submit_enabled is False
    with pytest.raises(PermissionError):
        ctrl.type_text("x", "y")
    with pytest.raises(PermissionError):
        ctrl.click("x")
    with pytest.raises(PermissionError):
        ctrl.submit()


def test_openai_cua_gated_allows_mutation_to_backend():
    comp = MockComputer()
    ctrl = OpenAIComputerUseController(comp, submit_enabled=True, client=object())
    ctrl.type_text("email", "j@e.com")
    ctrl.submit()
    kinds = [a["type"] for a in comp.actions]
    assert "type" in kinds and "submit" in kinds
