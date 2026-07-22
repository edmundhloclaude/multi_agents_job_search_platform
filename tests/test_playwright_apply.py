"""End-to-end apply flow against a REAL headless Chromium (Playwright).

Drives a local HTML application form (no external site) through the Applier's
map phase and the human-gated submit phase, proving:
  * real form fields are read from the DOM,
  * fields are filled and the form submitted in a real browser,
  * the sensitive password field is NEVER filled,
  * read-only enforcement blocks mutation,
  * submit only happens with a valid gate approval.

Skips automatically if Chromium can't launch (e.g., no browser installed).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

pytest.importorskip("playwright")

from jobsearch.agents.applier import Applier
from jobsearch.browser.playwright_computer import PlaywrightComputer
from jobsearch.browser.playwright_controller import PlaywrightBrowserController
from jobsearch.models import ApplyStatus, Posting, ScreenStatus
from jobsearch.orchestrator import APPROVE_PHRASE, human_approval_gate
from jobsearch.store.job_store import JobStore

_FORM = Path(__file__).parent / "fixtures" / "apply_form.html"
_FORM_URL = _FORM.resolve().as_uri()


@pytest.fixture(scope="module")
def computer():
    try:
        comp = PlaywrightComputer(headless=True)
    except Exception as e:  # browser/libs unavailable
        pytest.skip(f"Chromium unavailable: {e}")
    yield comp
    comp.close()


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


def _seed(store, tmp_path):
    resume = tmp_path / "resume.md"
    resume.write_text("# Jane Doe\nreal resume\n")
    cover = tmp_path / "cover.md"
    cover.write_text("Dear team\n")
    p = Posting(company="Acme", title="Senior Engineer", location="Remote",
                source="t", source_url=_FORM_URL, application_method="external_ats")
    store.upsert_posting(p)
    store.annotate_screen(p.dedup_key, status=ScreenStatus.SCREENED_IN, score=90, rationale="")
    store.set_docs(p.dedup_key, resume_path=str(resume), cover_letter_path=str(cover))
    store.set_apply_status(p.dedup_key, ApplyStatus.DRAFTED)
    return store.get(p.dedup_key)


def test_map_reads_real_dom_fields(computer, store, tmp_path):
    posting = _seed(store, tmp_path)
    ro = PlaywrightBrowserController(submit_enabled=False, computer=computer)
    applier = Applier(store, tmp_path / "out")
    user = {"name": "Jane Doe", "contact": {"email": "jane@example.com", "phone": "555-0100"}}
    app = applier.map_application(posting, ro, user)

    names = {f.name for f in app.fields}
    assert {"full_name", "email", "phone", "resume", "cover", "password"} <= names
    by = {f.name: f for f in app.fields}
    # real CSS selectors were extracted from the DOM
    assert by["email"].selector == '[name="email"]'
    assert by["email"].value == "jane@example.com"
    # password flagged sensitive, not auto-filled
    assert by["password"].sensitive is True
    assert store.get(posting.dedup_key).apply_status == ApplyStatus.AWAITING_APPROVAL.value


def test_readonly_controller_blocks_fill(computer):
    ro = PlaywrightBrowserController(submit_enabled=False, computer=computer)
    with pytest.raises(PermissionError):
        ro.type_text('[name="email"]', "x")
    with pytest.raises(PermissionError):
        ro.submit()


def test_gated_submit_fills_real_form_and_skips_password(computer, store, tmp_path):
    posting = _seed(store, tmp_path)
    applier = Applier(store, tmp_path / "out")
    user = {"name": "Jane Doe", "contact": {"email": "jane@example.com", "phone": "555-0100"}}

    ro = PlaywrightBrowserController(submit_enabled=False, computer=computer)
    app = applier.map_application(posting, ro, user)  # opens form + maps

    approval = human_approval_gate(app, input_fn=lambda _: APPROVE_PHRASE,
                                   output_fn=lambda _: None)
    gated = PlaywrightBrowserController(submit_enabled=True, computer=computer)
    status = applier.submit(app, approval, gated)
    assert status == ApplyStatus.SUBMITTED.value

    # The page's onsubmit handler recorded what was actually submitted.
    result = computer._page.inner_text("#status")
    assert "SUBMITTED" in result
    assert "name=Jane Doe" in result
    assert "email=jane@example.com" in result
    assert "resume=resume.md" in result   # file was really uploaded
    assert "pw=EMPTY" in result            # password NEVER filled


def test_declined_gate_does_not_submit(computer, store, tmp_path):
    posting = _seed(store, tmp_path)
    applier = Applier(store, tmp_path / "out")
    ro = PlaywrightBrowserController(submit_enabled=False, computer=computer)
    app = applier.map_application(posting, ro,
                                  {"name": "Jane Doe", "contact": {"email": "j@e.com"}})
    declined = human_approval_gate(app, input_fn=lambda _: "no", output_fn=lambda _: None)
    gated = PlaywrightBrowserController(submit_enabled=True, computer=computer)
    status = applier.submit(app, declined, gated)
    assert status == ApplyStatus.SKIPPED.value
