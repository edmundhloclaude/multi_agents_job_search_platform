"""Shared data schemas for the multi-agent job search platform.

Uses stdlib dataclasses (no pydantic dependency) to keep the footprint minimal.
These types are the contract between the store, the agents, and the CLI.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Trust tiers (see spec §0). Enforced by the orchestrator, not by convention.
# --------------------------------------------------------------------------- #
class Tier(str, enum.Enum):
    SAFE = "SAFE"                # pure reasoning; no browser
    READ_BROWSER = "READ_BROWSER"  # read-only browser, never submits
    GATED = "GATED"             # irreversible browser action, only via the gate


# --------------------------------------------------------------------------- #
# Status enums (stored as text in SQLite).
# --------------------------------------------------------------------------- #
class ScreenStatus(str, enum.Enum):
    UNSCREENED = "unscreened"
    SCREENED_IN = "screened_in"
    SCREENED_OUT = "screened_out"


class ApplyStatus(str, enum.Enum):
    NONE = "none"
    DRAFTED = "drafted"
    AWAITING_APPROVAL = "awaiting_approval"
    SUBMITTED = "submitted"
    SKIPPED = "skipped"
    FAILED = "failed"


class ResponseStatus(str, enum.Enum):
    NONE = "none"
    REJECTED = "rejected"
    INTERVIEW = "interview"
    OFFER = "offer"


# --------------------------------------------------------------------------- #
# dedup_key normalization (spec §3). Single source of truth — tested directly.
# --------------------------------------------------------------------------- #
_PUNCT_RE = re.compile(r"[^\w\s|]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_dedup_key(company: str, title: str, location: str) -> str:
    """Normalized ``company|title|location``.

    Lowercased, punctuation-stripped, whitespace-collapsed. This is the
    primary key of the jobs table and is how re-scanning is prevented.
    """
    parts = []
    for raw in (company, title, location):
        s = (raw or "").lower()
        s = _PUNCT_RE.sub(" ", s)      # strip punctuation (keep the | joiner out of fields)
        s = _WS_RE.sub(" ", s).strip()  # collapse whitespace
        parts.append(s)
    return "|".join(parts)


# --------------------------------------------------------------------------- #
# Posting: the extracted job, as it flows through the pipeline.
# --------------------------------------------------------------------------- #
@dataclass
class Posting:
    company: str
    title: str
    location: str
    source: str
    source_url: str = ""
    comp_text: str = ""
    requirements: list[str] = field(default_factory=list)
    application_method: str = ""          # linkedin_easy_apply | external_ats | email
    raw: dict[str, Any] = field(default_factory=dict)

    # Lifecycle fields (populated by the store / agents).
    dedup_key: str = ""
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    screen_status: str = ScreenStatus.UNSCREENED.value
    screen_score: Optional[int] = None
    screen_rationale: str = ""
    apply_status: str = ApplyStatus.NONE.value
    resume_path: str = ""
    cover_letter_path: str = ""
    response_status: str = ResponseStatus.NONE.value

    def __post_init__(self) -> None:
        if not self.dedup_key:
            self.dedup_key = normalize_dedup_key(self.company, self.title, self.location)

    # -- (de)serialization helpers -------------------------------------- #
    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["requirements"] = json.dumps(self.requirements)
        d["raw"] = json.dumps(self.raw)
        return d

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Posting":
        row = dict(row)
        row["requirements"] = json.loads(row.get("requirements") or "[]")
        row["raw"] = json.loads(row.get("raw") or "{}")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in row.items() if k in known})


# --------------------------------------------------------------------------- #
# Strategy criteria (spec §4.1). Machine-usable output the Screener consumes.
# --------------------------------------------------------------------------- #
@dataclass
class Positioning:
    """The shared lens between Strategy Advisor and Crafter (spec extension).

    Not screening criteria — this tells the Crafter HOW to present the candidate:
    which real accomplishments to foreground and the narrative to frame them with.
    It never introduces claims (the fabrication guard still runs); it only shifts
    emphasis, so every tailored document stays consistent with the strategy.
    """
    narrative: str = ""                                    # one-line personal brand
    lead_with: list[str] = field(default_factory=list)     # themes/skills to foreground
    emphasize: list[str] = field(default_factory=list)     # keywords to prioritize
    de_emphasize: list[str] = field(default_factory=list)  # things to downplay

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_empty(self) -> bool:
        return not (self.narrative or self.lead_with or self.emphasize or self.de_emphasize)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Positioning":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class StrategyCriteria:
    target_roles: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    comp_min: Optional[int] = None
    comp_currency: str = "USD"
    geographies: list[str] = field(default_factory=list)
    remote_ok: bool = True
    must_haves: list[str] = field(default_factory=list)     # keywords that should appear
    dealbreakers: list[str] = field(default_factory=list)   # keywords that disqualify
    keywords_boost: list[str] = field(default_factory=list)  # nice-to-have signals

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StrategyCriteria":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


# --------------------------------------------------------------------------- #
# Accomplishment bank (spec §4.3/§4.4). The ONLY source of truth for claims.
# --------------------------------------------------------------------------- #
@dataclass
class Accomplishment:
    employer: str
    title: str
    start_date: str
    end_date: str
    text: str
    metrics: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


@dataclass
class AccomplishmentBank:
    name: str
    contact: dict[str, str] = field(default_factory=dict)
    accomplishments: list[Accomplishment] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AccomplishmentBank":
        accs = [Accomplishment(**a) for a in d.get("accomplishments", [])]
        return cls(
            name=d.get("name", ""),
            contact=d.get("contact", {}),
            accomplishments=accs,
            skills=d.get("skills", []),
            credentials=d.get("credentials", []),
        )

    def known_employers(self) -> set[str]:
        return {a.employer for a in self.accomplishments}

    def known_titles(self) -> set[str]:
        return {a.title for a in self.accomplishments}

    def known_metrics(self) -> set[str]:
        out: set[str] = set()
        for a in self.accomplishments:
            out.update(a.metrics)
        return out

    def known_skills(self) -> set[str]:
        out = set(self.skills)
        for a in self.accomplishments:
            out.update(a.skills)
        return out


# --------------------------------------------------------------------------- #
# FilledApplication (spec §4.5). Produced by the map phase, consumed by submit.
# --------------------------------------------------------------------------- #
@dataclass
class FilledField:
    name: str
    value: str
    source: str = ""      # where the value came from (bank/user/doc), for the review print
    sensitive: bool = False  # credentials/payment/captcha are NEVER auto-filled
    selector: str = ""    # CSS selector for the real form input (browser fill target)


@dataclass
class FilledApplication:
    dedup_key: str
    company: str
    title: str
    source_url: str
    application_method: str
    fields: list[FilledField] = field(default_factory=list)
    resume_path: str = ""
    cover_letter_path: str = ""
    # Fields the human must handle manually (login/CAPTCHA/payment); never auto-filled.
    manual_required: list[str] = field(default_factory=list)

    def render_for_review(self) -> str:
        """Human-readable dump printed at the submit gate."""
        lines = [
            "=" * 70,
            "APPLICATION FOR HUMAN REVIEW",
            "=" * 70,
            f"Company : {self.company}",
            f"Title   : {self.title}",
            f"URL     : {self.source_url}",
            f"Method  : {self.application_method}",
            f"Resume  : {self.resume_path}",
            f"Cover   : {self.cover_letter_path}",
            "-" * 70,
            "FIELDS TO BE SUBMITTED:",
        ]
        for fld in self.fields:
            tag = " [SENSITIVE — WILL NOT BE AUTO-FILLED]" if fld.sensitive else ""
            src = f"  (from {fld.source})" if fld.source else ""
            lines.append(f"  • {fld.name}: {fld.value}{tag}{src}")
        if self.manual_required:
            lines.append("-" * 70)
            lines.append("REQUIRES MANUAL HUMAN ACTION (not automated):")
            for m in self.manual_required:
                lines.append(f"  ! {m}")
        lines.append("=" * 70)
        return "\n".join(lines)
