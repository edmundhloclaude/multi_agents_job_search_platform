"""Step 3 tests: Strategy, Screener idempotency, Crafter fabrication check."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.crafter import Crafter, FabricationError, find_fabrications
from jobsearch.agents.screener import Screener, score_posting
from jobsearch.agents.strategy import StrategyAdvisor, load_criteria_from_strategy
from jobsearch.models import (
    Accomplishment,
    AccomplishmentBank,
    Posting,
    ScreenStatus,
    StrategyCriteria,
)
from jobsearch.store.job_store import JobStore


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


@pytest.fixture
def criteria():
    return StrategyCriteria(
        target_roles=["staff engineer", "senior engineer"],
        seniority=["staff", "senior"],
        comp_min=180000,
        geographies=["san francisco"],
        remote_ok=True,
        must_haves=["python", "distributed systems"],
        dealbreakers=["unpaid", "commission only"],
        keywords_boost=["kubernetes"],
    )


@pytest.fixture
def bank():
    return AccomplishmentBank(
        name="Jane Doe",
        contact={"email": "jane@example.com"},
        accomplishments=[
            Accomplishment(
                employer="Acme Corp", title="Senior Engineer",
                start_date="2019", end_date="2023",
                text="Built a distributed system in Python that cut latency by 40%.",
                metrics=["40%"], skills=["python", "distributed systems", "kubernetes"],
            ),
            Accomplishment(
                employer="Globex", title="Engineer",
                start_date="2016", end_date="2019",
                text="Led a team of 6 to ship a data platform serving 10,000 users.",
                metrics=["6", "10,000"], skills=["python", "leadership"],
            ),
        ],
        skills=["python", "distributed systems", "kubernetes", "leadership"],
        credentials=["BS Computer Science"],
    )


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
def test_strategy_writes_md_and_roundtrips_criteria(tmp_path):
    adv = StrategyAdvisor()
    profile = {
        "target_roles": ["Staff Engineer"], "seniority": ["staff"],
        "comp_min": 200000, "geographies": ["Remote"], "must_haves": ["python"],
        "dealbreakers": ["unpaid"],
    }
    path = tmp_path / "strategy.md"
    crit = adv.run(profile, path)
    assert path.exists()
    assert crit.target_roles == ["Staff Engineer"]
    parsed = load_criteria_from_strategy(path)
    assert parsed.target_roles == ["Staff Engineer"]
    assert parsed.comp_min == 200000


# --------------------------------------------------------------------------- #
# Screener idempotency
# --------------------------------------------------------------------------- #
def _mk(store, title, reqs, comp="", loc="Remote", company="Acme"):
    store.upsert_posting(Posting(
        company=company, title=title, location=loc, source="t",
        requirements=reqs, comp_text=comp,
    ))


def test_score_posting_is_pure_and_deterministic(criteria):
    p = Posting(company="Acme", title="Staff Engineer", location="Remote",
                source="t", requirements=["python", "distributed systems"])
    r1 = score_posting(p, criteria)
    r2 = score_posting(p, criteria)
    assert (r1.status, r1.score, r1.rationale) == (r2.status, r2.score, r2.rationale)


def test_dealbreaker_forces_screened_out(criteria):
    p = Posting(company="X", title="Staff Engineer", location="Remote", source="t",
                comp_text="commission only", requirements=["python"])
    r = score_posting(p, criteria)
    assert r.status == ScreenStatus.SCREENED_OUT.value
    assert r.score == 0


def test_screener_idempotent_over_store(store, criteria):
    _mk(store, "Staff Engineer", ["python", "distributed systems", "kubernetes"])
    _mk(store, "Marketing Lead", ["seo"])
    sc = Screener(store)
    n1 = sc.run(criteria)
    snap1 = {p.dedup_key: (p.screen_status, p.screen_score) for p in store.all()}
    # re-run with rescreen -> identical annotations
    sc.run(criteria, rescreen=True)
    snap2 = {p.dedup_key: (p.screen_status, p.screen_score) for p in store.all()}
    assert snap1 == snap2
    # second default run does nothing (already screened)
    assert sc.run(criteria) == 0
    assert n1 == 2


def test_screener_screens_in_good_match(store, criteria):
    _mk(store, "Staff Engineer", ["python", "distributed systems", "kubernetes"], loc="San Francisco")
    Screener(store).run(criteria)
    ins = store.get_by_status(screen_status=ScreenStatus.SCREENED_IN)
    assert len(ins) == 1
    assert ins[0].screen_score >= 50


# --------------------------------------------------------------------------- #
# Crafter fabrication check
# --------------------------------------------------------------------------- #
def test_crafter_emits_clean_docs(store, bank, tmp_path):
    p = Posting(company="Initech", title="Senior Engineer", location="Remote",
                source="t", requirements=["python", "distributed systems"])
    store.upsert_posting(p)
    store.annotate_screen(p.dedup_key, status=ScreenStatus.SCREENED_IN, score=90, rationale="")
    c = Crafter(store, bank, tmp_path / "out")
    res = c.run()
    assert res == {"crafted": 1, "refused": 0}
    updated = store.get(p.dedup_key)
    assert updated.resume_path and os.path.exists(updated.resume_path)
    # emitted docs are clean by construction
    assert find_fabrications(open(updated.resume_path).read(), bank) == []


def test_fabrication_check_flags_planted_false_metric(bank):
    doc = (
        "# Jane Doe\n## Experience\n- Acme Corp | Senior Engineer | 2019–2023\n"
        "  - Increased revenue by 999% overnight.\n## Skills\npython\n"
    )
    findings = find_fabrications(doc, bank)
    assert any("999" in f for f in findings)


def test_fabrication_check_flags_fake_employer(bank):
    doc = (
        "# Jane Doe\n## Experience\n- Umbrella Corp | Chief Wizard | 2000–2010\n"
        "  - Did magical things.\n## Skills\npython\n"
    )
    findings = find_fabrications(doc, bank)
    assert any("Umbrella Corp" in f for f in findings)


def test_fabrication_check_flags_fake_skill(bank):
    doc = "# Jane Doe\n## Experience\n## Skills\npython, quantum telepathy\n"
    findings = find_fabrications(doc, bank)
    assert any("quantum telepathy" in f for f in findings)


def test_crafter_refuses_to_emit_when_fabrication_present(store, bank, tmp_path, monkeypatch):
    """Planted false claim in rendering -> FabricationError, nothing written."""
    p = Posting(company="Initech", title="Senior Engineer", location="Remote",
                source="t", requirements=["python"])
    store.upsert_posting(p)
    store.annotate_screen(p.dedup_key, status=ScreenStatus.SCREENED_IN, score=90, rationale="")
    c = Crafter(store, bank, tmp_path / "out")

    # Inject a fabricated metric into the rendered resume.
    orig = c.render_resume
    monkeypatch.setattr(
        c, "render_resume",
        lambda posting: orig(posting) + "\n- Boosted sales by 12345%.\n",
    )
    with pytest.raises(FabricationError):
        c.craft_one(p)
    # run() should record it as refused, not crafted, and write no docs
    res = c.run()
    assert res["refused"] == 1 and res["crafted"] == 0
    assert store.get(p.dedup_key).resume_path == ""


def test_clean_resume_passes_fabrication_check(store, bank, tmp_path):
    p = Posting(company="Initech", title="Senior Engineer", location="Remote",
                source="t", requirements=["python", "kubernetes"])
    c = Crafter(store, bank, tmp_path / "out")
    resume = c.render_resume(p)
    assert find_fabrications(resume, bank) == []
