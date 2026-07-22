"""Screener (SAFE) — spec §4.3.

Scores each unscreened job 0–100 for fit against the strategy criteria and
writes screen_status / screen_score / screen_rationale via annotate_screen.
Pure reasoning, no browser. Idempotent: ``score_posting`` is a deterministic
pure function, so re-running never changes a score.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Posting, ScreenStatus, StrategyCriteria, Tier
from ..store.job_store import JobStore

TIER = Tier.SAFE

# A posting at or above this score with no dealbreaker is screened_in.
SCREEN_IN_THRESHOLD = 50


@dataclass
class ScreenResult:
    status: str
    score: int
    rationale: str


def _haystack(p: Posting) -> str:
    parts = [p.title, p.company, p.location, p.comp_text, " ".join(p.requirements)]
    # Many sources (e.g. TheirStack) provide a free-text description in raw
    # rather than parsed requirements — include it so screening has signal.
    desc = (p.raw or {}).get("description") if isinstance(p.raw, dict) else ""
    if desc:
        parts.append(str(desc))
    return " ".join(parts).lower()


def _contains_any(hay: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n and n.lower() in hay]


def score_posting(posting: Posting, criteria: StrategyCriteria) -> ScreenResult:
    """Pure, deterministic fit score. Same inputs → same output (idempotent)."""
    hay = _haystack(posting)
    reasons: list[str] = []

    # Dealbreakers short-circuit to screened_out.
    hit_dealbreakers = _contains_any(hay, criteria.dealbreakers)
    if hit_dealbreakers:
        return ScreenResult(
            status=ScreenStatus.SCREENED_OUT.value,
            score=0,
            rationale=f"Dealbreaker(s) present: {', '.join(hit_dealbreakers)}.",
        )

    score = 0

    # Role match (up to 35).
    role_hits = _contains_any(posting.title.lower(), criteria.target_roles)
    if role_hits:
        score += 35
        reasons.append(f"role matches {role_hits}")
    elif criteria.target_roles:
        # partial: any target-role word appears in title
        title_words = set(posting.title.lower().split())
        role_words = {w for r in criteria.target_roles for w in r.lower().split()}
        if title_words & role_words:
            score += 15
            reasons.append("partial role-keyword match")

    # Seniority (up to 15).
    sen_hits = _contains_any(hay, criteria.seniority)
    if sen_hits:
        score += 15
        reasons.append(f"seniority matches {sen_hits}")

    # Must-haves (up to 30, proportional).
    if criteria.must_haves:
        mh = _contains_any(hay, criteria.must_haves)
        frac = len(mh) / len(criteria.must_haves)
        score += int(round(30 * frac))
        if mh:
            reasons.append(f"must-haves {mh}")
        missing = [m for m in criteria.must_haves if m not in mh]
        if missing:
            reasons.append(f"missing must-haves {missing}")

    # Geography / remote (up to 10).
    geo_hits = _contains_any(hay, criteria.geographies)
    is_remote = "remote" in hay
    if geo_hits:
        score += 10
        reasons.append(f"geo matches {geo_hits}")
    elif criteria.remote_ok and is_remote:
        score += 10
        reasons.append("remote acceptable")
    elif not criteria.geographies:
        score += 5
        reasons.append("no geo constraint")

    # Nice-to-have boosts (up to 10).
    boosts = _contains_any(hay, criteria.keywords_boost)
    if boosts:
        score += min(10, 5 * len(boosts))
        reasons.append(f"boosts {boosts}")

    score = max(0, min(100, score))
    status = (
        ScreenStatus.SCREENED_IN.value
        if score >= SCREEN_IN_THRESHOLD
        else ScreenStatus.SCREENED_OUT.value
    )
    rationale = f"Score {score}: " + ("; ".join(reasons) if reasons else "no strong signals")
    return ScreenResult(status=status, score=score, rationale=rationale)


class Screener:
    def __init__(self, store: JobStore, *, llm=None):
        self.store = store
        # Optional OpenAI-backed reasoning. Default None -> deterministic scoring.
        self.llm = llm

    def _score(self, posting: Posting, criteria: StrategyCriteria) -> ScreenResult:
        if self.llm is not None:
            from .llm_reasoner import llm_score
            return llm_score(self.llm, posting, criteria)
        return score_posting(posting, criteria)

    def run(self, criteria: StrategyCriteria, *, rescreen: bool = False) -> int:
        """Score jobs; return the number annotated.

        Default: only unscreened jobs. ``rescreen=True`` re-scores everything —
        still idempotent because ``score_posting`` is pure.
        """
        if rescreen:
            targets = self.store.all()
        else:
            targets = self.store.get_by_status(screen_status=ScreenStatus.UNSCREENED)
        n = 0
        for p in targets:
            res = self._score(p, criteria)
            self.store.annotate_screen(
                p.dedup_key, status=res.status, score=res.score, rationale=res.rationale
            )
            n += 1
        return n
