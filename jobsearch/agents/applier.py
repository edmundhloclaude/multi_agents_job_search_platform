"""Applier — SAFE mapping step + GATED submit step (spec §4.5, §0).

Two explicit phases:

* ``map`` (SAFE): read the application form read-only, map its fields to the
  user's data + crafted docs, produce a ``FilledApplication``, and set
  apply_status = awaiting_approval. It never submits and never fills
  credentials / payment / CAPTCHA — those are flagged for manual human action.
* ``submit`` (GATED): execute keystrokes and click submit. This method requires
  a valid ``Approval`` minted by ``human_approval_gate``; there is no code path
  that submits without one. On decline it skips. It never types sensitive fields.

The map phase reads the form through a read-only controller (submit_enabled
False), so the mapping step cannot mutate the page — consistent with its SAFE
posture.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..browser.controller import BrowserController
from ..models import (
    ApplyStatus,
    FilledApplication,
    FilledField,
    Posting,
    Tier,
)
from ..store.job_store import JobStore

MAP_TIER = Tier.SAFE
SUBMIT_TIER = Tier.GATED


class SubmitBlocked(Exception):
    """Raised if submit is attempted without a valid human approval."""


# Field-name patterns that must NEVER be auto-filled (spec §0.5).
_SENSITIVE_PATTERNS = [
    "password", "passwd", "ssn", "social security", "credit card", "card number",
    "cvv", "cvc", "payment", "bank account", "routing", "captcha", "security code",
    "date of birth", "dob",
]


def _is_sensitive(name: str, label: str = "") -> bool:
    blob = f"{name} {label}".lower()
    return any(pat in blob for pat in _SENSITIVE_PATTERNS)


class Applier:
    def __init__(self, store: JobStore, output_dir: str | Path):
        self.store = store
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # MAP phase (SAFE)
    # ------------------------------------------------------------------ #
    def _read_form_fields(self, controller: BrowserController, posting: Posting,
                          selector: str = "form_field") -> list[dict]:
        """Read the form structure read-only. Returns field descriptors."""
        if getattr(controller, "submit_enabled", False):
            # Defensive: the map phase must run with a read-only controller.
            raise PermissionError("map phase requires a read-only controller.")
        controller.open(posting.source_url)
        fields = []
        for blob in controller.query(selector):
            try:
                fields.append(json.loads(blob))
            except (json.JSONDecodeError, TypeError):
                continue
        return fields

    def _map_value(self, field: dict, posting: Posting, user: dict) -> Optional[str]:
        """Map a single form field to a real value, or None if unknown."""
        key = f"{field.get('name','')} {field.get('label','')}".lower()
        contact = user.get("contact", {})

        def has(*words):
            return any(w in key for w in words)

        if has("full name", "your name", "first and last"):
            return user.get("name", "")
        if has("first name"):
            return user.get("name", "").split(" ")[0]
        if has("last name", "surname"):
            parts = user.get("name", "").split(" ")
            return parts[-1] if len(parts) > 1 else ""
        if has("email"):
            return contact.get("email", "")
        if has("phone", "mobile", "telephone"):
            return contact.get("phone", "")
        if has("linkedin"):
            return contact.get("linkedin", "")
        if has("website", "portfolio", "github"):
            return contact.get("website", contact.get("github", ""))
        if has("resume", "cv", "curriculum"):
            return posting.resume_path
        if has("cover letter", "cover"):
            return posting.cover_letter_path
        if has("location", "city", "where are you"):
            return posting.location
        return None

    def map_application(
        self,
        posting: Posting,
        controller: BrowserController,
        user: dict[str, Any],
        *,
        form_field_selector: str = "form_field",
    ) -> FilledApplication:
        """Produce a FilledApplication and set apply_status = awaiting_approval."""
        fields_desc = self._read_form_fields(controller, posting, form_field_selector)
        filled: list[FilledField] = []
        manual: list[str] = []

        for fd in fields_desc:
            name = fd.get("name", "")
            label = fd.get("label", name)
            selector = fd.get("selector", "")
            if _is_sensitive(name, label):
                # Never auto-fill; hand to the human (spec §0.5).
                manual.append(f"{label or name} (sensitive — must be entered by human)")
                filled.append(FilledField(name=name or label, value="<HUMAN MUST ENTER>",
                                          source="manual", sensitive=True, selector=selector))
                continue
            value = self._map_value(fd, posting, user)
            if value is None:
                if fd.get("required"):
                    manual.append(f"{label or name} (required, no mapping found)")
                continue
            filled.append(FilledField(name=name or label, value=value,
                                      source="bank/docs", selector=selector))

        app = FilledApplication(
            dedup_key=posting.dedup_key,
            company=posting.company,
            title=posting.title,
            source_url=posting.source_url,
            application_method=posting.application_method,
            fields=filled,
            resume_path=posting.resume_path,
            cover_letter_path=posting.cover_letter_path,
            manual_required=manual,
        )
        self._persist(app)
        self.store.set_apply_status(posting.dedup_key, ApplyStatus.AWAITING_APPROVAL)
        return app

    # -- persistence of the mapped application (map/submit run separately) -- #
    def _app_path(self, dedup_key: str) -> Path:
        stem = re.sub(r"[^a-z0-9]+", "_", dedup_key.lower()).strip("_")[:80]
        return self.output_dir / f"{stem}__application.json"

    def _persist(self, app: FilledApplication) -> None:
        d = {
            "dedup_key": app.dedup_key, "company": app.company, "title": app.title,
            "source_url": app.source_url, "application_method": app.application_method,
            "resume_path": app.resume_path, "cover_letter_path": app.cover_letter_path,
            "manual_required": app.manual_required,
            "fields": [vars(f) for f in app.fields],
        }
        self._app_path(app.dedup_key).write_text(json.dumps(d, indent=2), encoding="utf-8")

    def load_application(self, dedup_key: str) -> Optional[FilledApplication]:
        path = self._app_path(dedup_key)
        if not path.exists():
            return None
        d = json.loads(path.read_text(encoding="utf-8"))
        d["fields"] = [FilledField(**f) for f in d.get("fields", [])]
        return FilledApplication(**d)

    # ------------------------------------------------------------------ #
    # SUBMIT phase (GATED)
    # ------------------------------------------------------------------ #
    def submit(
        self,
        app: FilledApplication,
        approval,                    # jobsearch.orchestrator.Approval
        controller: BrowserController,
    ) -> str:
        """Execute the submission — ONLY with a valid, matching human approval.

        Returns the new apply_status ('submitted' or 'skipped'). Raises
        ``SubmitBlocked`` if the controller cannot submit. If the approval is
        missing/declined/forged, nothing is submitted and status becomes skipped.
        """
        if not getattr(controller, "submit_enabled", False):
            raise SubmitBlocked("GATED submit requires a submit-capable controller.")

        # The one and only gate. is_valid_for checks: approved + genuine gate
        # token + dedup_key match. A forged or declined approval fails here.
        if approval is None or not approval.is_valid_for(app):
            self.store.set_apply_status(app.dedup_key, ApplyStatus.SKIPPED)
            return ApplyStatus.SKIPPED.value

        try:
            for field in app.fields:
                if field.sensitive:
                    continue  # never type credentials / payment / CAPTCHA
                # Prefer a real CSS selector (Playwright); fall back to name (mock).
                controller.type_text(field.selector or field.name, field.value)
            controller.submit()
        except Exception as e:
            self.store.set_apply_status(app.dedup_key, ApplyStatus.FAILED)
            raise SubmitBlocked(f"submission failed: {e}") from e

        self.store.set_apply_status(app.dedup_key, ApplyStatus.SUBMITTED)
        return ApplyStatus.SUBMITTED.value
