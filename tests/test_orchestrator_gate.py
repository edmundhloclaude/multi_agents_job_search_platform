"""Step 2 tests: tier enforcement + the submit gate.

The gate test proves the core safety invariant: an application cannot be
submitted without a genuine approval return value from the gate.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.browser.controller import ReadOnlyBrowser
from jobsearch.browser.mock_controller import MockBrowserController
from jobsearch.models import FilledApplication, FilledField, Tier
from jobsearch.orchestrator import (
    APPROVE_PHRASE,
    Approval,
    TierViolation,
    assert_controller_for_tier,
    human_approval_gate,
)


# --------------------------------------------------------------------------- #
# Tier enforcement
# --------------------------------------------------------------------------- #
def test_safe_unit_rejects_any_controller():
    with pytest.raises(TierViolation):
        assert_controller_for_tier(Tier.SAFE, MockBrowserController())


def test_safe_unit_ok_with_no_controller():
    assert_controller_for_tier(Tier.SAFE, None)  # no raise


def test_read_browser_rejects_submit_capable_controller():
    submit_ctrl = MockBrowserController(submit_enabled=True)
    with pytest.raises(TierViolation):
        assert_controller_for_tier(Tier.READ_BROWSER, submit_ctrl)


def test_read_browser_ok_with_readonly_wrapper():
    ro = ReadOnlyBrowser(MockBrowserController(submit_enabled=True))
    assert ro.submit_enabled is False
    assert_controller_for_tier(Tier.READ_BROWSER, ro)  # no raise


def test_readonly_wrapper_blocks_mutation():
    ro = ReadOnlyBrowser(MockBrowserController(submit_enabled=True))
    with pytest.raises(PermissionError):
        ro.submit()
    with pytest.raises(PermissionError):
        ro.click("x")
    with pytest.raises(PermissionError):
        ro.type_text("x", "y")


def test_gated_requires_submit_capable_controller():
    with pytest.raises(TierViolation):
        assert_controller_for_tier(Tier.GATED, MockBrowserController(submit_enabled=False))
    assert_controller_for_tier(Tier.GATED, MockBrowserController(submit_enabled=True))  # ok


# --------------------------------------------------------------------------- #
# The submit gate
# --------------------------------------------------------------------------- #
def _filled():
    return FilledApplication(
        dedup_key="acme|staff engineer|remote",
        company="Acme",
        title="Staff Engineer",
        source_url="http://acme/apply",
        application_method="external_ats",
        fields=[FilledField("name", "Jane Doe", source="bank")],
    )


def test_gate_approves_only_on_exact_phrase():
    fa = _filled()
    appr = human_approval_gate(fa, input_fn=lambda _: APPROVE_PHRASE, output_fn=lambda _: None)
    assert appr.approved is True
    assert appr.is_valid_for(fa) is True


def test_gate_declines_on_anything_else():
    fa = _filled()
    for typed in ["", "yes", "y", "submit please", "Submit", "SUBMITT"]:
        appr = human_approval_gate(fa, input_fn=lambda _, t=typed: t, output_fn=lambda _: None)
        assert appr.approved is False
        assert appr.is_valid_for(fa) is False


def test_forged_approval_is_not_valid():
    """An Approval fabricated outside the gate must NOT validate (no token)."""
    fa = _filled()
    forged = Approval(dedup_key=fa.dedup_key, approved=True, confirmation_text=APPROVE_PHRASE)
    assert forged.is_valid_for(fa) is False  # missing the private gate token


def test_approval_bound_to_specific_application():
    fa = _filled()
    appr = human_approval_gate(fa, input_fn=lambda _: APPROVE_PHRASE, output_fn=lambda _: None)
    other = _filled()
    other.dedup_key = "different|key|here"
    assert appr.is_valid_for(other) is False  # can't reuse approval for another app


def test_gate_prints_full_application_for_review():
    fa = _filled()
    printed = []
    human_approval_gate(fa, input_fn=lambda _: "", output_fn=printed.append)
    blob = "\n".join(printed)
    assert "APPLICATION FOR HUMAN REVIEW" in blob
    assert "Jane Doe" in blob
    assert "Acme" in blob
