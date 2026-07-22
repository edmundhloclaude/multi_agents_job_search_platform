"""Step 1 tests: dedup_key normalization + collision handling."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.models import ApplyStatus, Posting, ScreenStatus, normalize_dedup_key
from jobsearch.store.job_store import JobStore


# --------------------------------------------------------------------------- #
# dedup_key normalization
# --------------------------------------------------------------------------- #
def test_normalize_lowercases_and_collapses_whitespace():
    assert normalize_dedup_key("  ACME   Corp ", "Staff  Engineer", "  San Francisco ") \
        == "acme corp|staff engineer|san francisco"


def test_normalize_strips_punctuation():
    a = normalize_dedup_key("Acme, Inc.", "Sr. Engineer (Backend)", "New York, NY")
    b = normalize_dedup_key("Acme Inc", "Sr Engineer Backend", "New York NY")
    assert a == b


def test_normalize_equivalent_variants_collide():
    variants = [
        ("Acme Inc.", "Staff Engineer", "Remote"),
        ("  acme   inc  ", "staff   engineer", "remote"),
        ("ACME, INC", "Staff Engineer!", "Remote."),
    ]
    keys = {normalize_dedup_key(*v) for v in variants}
    assert len(keys) == 1


def test_normalize_distinct_titles_do_not_collide():
    k1 = normalize_dedup_key("Acme", "Staff Engineer", "Remote")
    k2 = normalize_dedup_key("Acme", "Senior Engineer", "Remote")
    assert k1 != k2


def test_posting_autocomputes_dedup_key():
    p = Posting(company="Acme, Inc.", title="Sr. Engineer", location="NYC", source="test")
    assert p.dedup_key == normalize_dedup_key("Acme, Inc.", "Sr. Engineer", "NYC")


# --------------------------------------------------------------------------- #
# collision handling / upsert
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


def _posting(**kw):
    base = dict(company="Acme", title="Staff Engineer", location="Remote", source="src")
    base.update(kw)
    return Posting(**base)


def test_upsert_new_returns_is_new(store):
    is_new, key = store.upsert_posting(_posting())
    assert is_new is True
    assert store.get(key) is not None


def test_upsert_collision_returns_not_new(store):
    store.upsert_posting(_posting())
    is_new, _ = store.upsert_posting(_posting(company="  ACME  ", title="staff engineer!"))
    assert is_new is False


def test_upsert_collision_does_not_duplicate_rows(store):
    store.upsert_posting(_posting())
    store.upsert_posting(_posting(company="ACME,"))
    assert len(store.all()) == 1


def test_upsert_collision_bumps_last_seen_preserves_first_seen(store):
    _, key = store.upsert_posting(_posting())
    before = store.get(key)
    store.upsert_posting(_posting(source_url="http://new"))
    after = store.get(key)
    assert after.first_seen == before.first_seen
    assert after.last_seen >= before.last_seen
    assert after.source_url == "http://new"


def test_upsert_collision_preserves_pipeline_state(store):
    """Re-crawling must NOT clobber screening/apply state."""
    _, key = store.upsert_posting(_posting())
    store.annotate_screen(key, status=ScreenStatus.SCREENED_IN, score=90, rationale="good")
    store.set_apply_status(key, ApplyStatus.SUBMITTED)
    store.upsert_posting(_posting(comp_text="$$$ updated"))  # re-crawl
    after = store.get(key)
    assert after.screen_status == ScreenStatus.SCREENED_IN.value
    assert after.screen_score == 90
    assert after.apply_status == ApplyStatus.SUBMITTED.value
    assert after.comp_text == "$$$ updated"  # discovery field did refresh


def test_get_by_status_filters(store):
    store.upsert_posting(_posting(title="A"))
    store.upsert_posting(_posting(title="B"))
    keys = [p.dedup_key for p in store.all()]
    store.annotate_screen(keys[0], status=ScreenStatus.SCREENED_IN, score=80, rationale="")
    ins = store.get_by_status(screen_status=ScreenStatus.SCREENED_IN)
    assert len(ins) == 1


def test_annotate_screen_unknown_key_raises(store):
    with pytest.raises(KeyError):
        store.annotate_screen("nope", status=ScreenStatus.SCREENED_IN, score=1, rationale="")


def test_status_counts(store):
    store.upsert_posting(_posting(title="A"))
    store.upsert_posting(_posting(title="B"))
    counts = store.status_counts()
    assert counts["screen"].get("unscreened") == 2
