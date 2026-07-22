"""Step 5 tests: Applier map phase + GATED submit behind the human gate.

The central safety property: submit is unreachable without a valid approval
return value from the gate. These tests exercise decline, forge, and approve.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.applier import Applier, SubmitBlocked
from jobsearch.browser.controller import ReadOnlyBrowser
from jobsearch.browser.mock_controller import MockBrowserController, MockPage
from jobsearch.models import ApplyStatus, Posting, ScreenStatus
from jobsearch.orchestrator import APPROVE_PHRASE, Approval, human_approval_gate
from jobsearch.store.job_store import JobStore


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


def _form_page(url):
    fields = [
        json.dumps({"name": "full_name", "label": "Full Name", "type": "text", "required": True}),
        json.dumps({"name": "email", "label": "Email", "type": "email", "required": True}),
        json.dumps({"name": "resume", "label": "Resume", "type": "file", "required": True}),
        json.dumps({"name": "cover", "label": "Cover Letter", "type": "file"}),
        json.dumps({"name": "password", "label": "Create a password", "type": "password"}),
        json.dumps({"name": "captcha", "label": "Enter CAPTCHA", "type": "text"}),
    ]
    return MockPage(url=url, elements={"form_field": fields})


@pytest.fixture
def mapped(store, tmp_path):
    """A screened_in job with docs, mapped to a FilledApplication."""
    p = Posting(company="Acme", title="Staff Engineer", location="Remote",
                source="t", source_url="http://acme/apply",
                application_method="external_ats")
    store.upsert_posting(p)
    store.annotate_screen(p.dedup_key, status=ScreenStatus.SCREENED_IN, score=90, rationale="")
    store.set_docs(p.dedup_key, resume_path="/tmp/r.md", cover_letter_path="/tmp/c.md")
    store.set_apply_status(p.dedup_key, ApplyStatus.DRAFTED)

    ro = ReadOnlyBrowser(MockBrowserController({"http://acme/apply": _form_page("http://acme/apply")}))
    applier = Applier(store, tmp_path / "out")
    user = {"name": "Jane Doe", "contact": {"email": "jane@example.com", "phone": "555-1234"}}
    app = applier.map_application(store.get(p.dedup_key), ro, user)
    return applier, app, store.get(p.dedup_key)


# --------------------------------------------------------------------------- #
# MAP phase
# --------------------------------------------------------------------------- #
def test_map_sets_awaiting_approval(mapped):
    _, app, posting = mapped
    assert posting.apply_status == ApplyStatus.AWAITING_APPROVAL.value


def test_map_fills_known_fields(mapped):
    _, app, _ = mapped
    by_name = {f.name: f for f in app.fields}
    assert by_name["full_name"].value == "Jane Doe"
    assert by_name["email"].value == "jane@example.com"
    assert by_name["resume"].value == "/tmp/r.md"


def test_map_flags_sensitive_never_autofilled(mapped):
    _, app, _ = mapped
    by_name = {f.name: f for f in app.fields}
    assert by_name["password"].sensitive is True
    assert by_name["password"].value == "<HUMAN MUST ENTER>"
    assert by_name["captcha"].sensitive is True
    assert any("password" in m.lower() for m in app.manual_required)
    assert any("captcha" in m.lower() for m in app.manual_required)


def test_map_requires_readonly_controller(store, tmp_path):
    p = Posting(company="Acme", title="Eng", location="Remote", source="t",
                source_url="http://x")
    store.upsert_posting(p)
    applier = Applier(store, tmp_path / "out")
    submit_ctrl = MockBrowserController(submit_enabled=True)
    with pytest.raises(PermissionError):
        applier.map_application(store.get(p.dedup_key), submit_ctrl, {"name": "J"})


def test_map_application_persists_and_reloads(mapped):
    applier, app, _ = mapped
    reloaded = applier.load_application(app.dedup_key)
    assert reloaded is not None
    assert reloaded.dedup_key == app.dedup_key
    assert len(reloaded.fields) == len(app.fields)


# --------------------------------------------------------------------------- #
# SUBMIT phase — the gate
# --------------------------------------------------------------------------- #
def _submit_ctrl():
    return MockBrowserController(submit_enabled=True)


def test_submit_without_approval_does_not_submit(mapped):
    applier, app, _ = mapped
    ctrl = _submit_ctrl()
    status = applier.submit(app, None, ctrl)
    assert status == ApplyStatus.SKIPPED.value
    assert ("submit", ()) not in ctrl.actions
    assert ctrl.actions == []


def test_submit_with_declined_approval_skips(mapped):
    applier, app, store_posting = mapped
    ctrl = _submit_ctrl()
    declined = human_approval_gate(app, input_fn=lambda _: "no", output_fn=lambda _: None)
    status = applier.submit(app, declined, ctrl)
    assert status == ApplyStatus.SKIPPED.value
    assert ctrl.actions == []


def test_submit_with_forged_approval_refuses(mapped):
    """An Approval constructed outside the gate must not enable submit."""
    applier, app, _ = mapped
    forged = Approval(dedup_key=app.dedup_key, approved=True, confirmation_text=APPROVE_PHRASE)
    ctrl = _submit_ctrl()
    status = applier.submit(app, forged, ctrl)
    assert status == ApplyStatus.SKIPPED.value
    assert ctrl.actions == []


def test_submit_with_valid_approval_submits(mapped):
    applier, app, _ = mapped
    approval = human_approval_gate(app, input_fn=lambda _: APPROVE_PHRASE, output_fn=lambda _: None)
    ctrl = _submit_ctrl()
    status = applier.submit(app, approval, ctrl)
    assert status == ApplyStatus.SUBMITTED.value
    assert ("submit", ()) in ctrl.actions


def test_submit_never_types_sensitive_fields(mapped):
    applier, app, _ = mapped
    approval = human_approval_gate(app, input_fn=lambda _: APPROVE_PHRASE, output_fn=lambda _: None)
    ctrl = _submit_ctrl()
    applier.submit(app, approval, ctrl)
    typed_selectors = [a[1][0] for a in ctrl.actions if a[0] == "type_text"]
    assert "password" not in typed_selectors
    assert "captcha" not in typed_selectors
    assert "full_name" in typed_selectors  # non-sensitive fields are typed


def test_submit_requires_submit_capable_controller(mapped):
    applier, app, _ = mapped
    approval = human_approval_gate(app, input_fn=lambda _: APPROVE_PHRASE, output_fn=lambda _: None)
    readonly = MockBrowserController(submit_enabled=False)
    with pytest.raises(SubmitBlocked):
        applier.submit(app, approval, readonly)


def test_approval_for_one_app_cannot_submit_another(mapped, store, tmp_path):
    """An approval minted for app A must not enable submitting app B (reuse attack)."""
    applier, app_a, _ = mapped
    approval_a = human_approval_gate(app_a, input_fn=lambda _: APPROVE_PHRASE, output_fn=lambda _: None)

    # A second, genuinely different mapped application.
    p = Posting(company="Globex", title="Engineer", location="Remote", source="t",
                source_url="http://globex/apply", application_method="external_ats")
    store.upsert_posting(p)
    store.annotate_screen(p.dedup_key, status=ScreenStatus.SCREENED_IN, score=80, rationale="")
    store.set_docs(p.dedup_key, resume_path="/tmp/r2.md", cover_letter_path="/tmp/c2.md")
    store.set_apply_status(p.dedup_key, ApplyStatus.DRAFTED)
    ro = ReadOnlyBrowser(MockBrowserController({"http://globex/apply": _form_page("http://globex/apply")}))
    app_b = applier.map_application(store.get(p.dedup_key), ro,
                                   {"name": "Jane Doe", "contact": {"email": "j@e.com"}})

    ctrl = _submit_ctrl()
    status = applier.submit(app_b, approval_a, ctrl)  # wrong approval for app_b
    assert status == ApplyStatus.SKIPPED.value
    assert ctrl.actions == []
