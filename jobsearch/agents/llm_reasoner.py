"""OpenAI-backed reasoning for the SAFE agents (Screener / Crafter / Strategy).

All functions take an ``LLM`` and return plain data. They add NO new authority:
the Crafter's output still passes through ``find_fabrications`` (spec §0.3), so
anything the model invents is caught and the document is refused. The model only
*rewords real bullets* and *chooses emphasis* — never introduces facts.
"""

from __future__ import annotations

import json
from typing import Optional

from ..models import Posting, ScreenStatus, StrategyCriteria
from .screener import ScreenResult, SCREEN_IN_THRESHOLD


# --------------------------------------------------------------------------- #
# Screener
# --------------------------------------------------------------------------- #
_SCREEN_SYS = (
    "You are a rigorous job-fit screener. Given a job posting and a candidate's "
    "screening criteria, score fit 0-100. Any dealbreaker present forces score 0 "
    "and screened_out. Return JSON: {\"score\": int, \"status\": "
    "\"screened_in\"|\"screened_out\", \"rationale\": str}."
)


def llm_score(llm, posting: Posting, criteria: StrategyCriteria) -> ScreenResult:
    desc = (posting.raw or {}).get("description") if isinstance(posting.raw, dict) else ""
    user = json.dumps({
        "posting": {
            "title": posting.title, "company": posting.company,
            "location": posting.location, "comp_text": posting.comp_text,
            "requirements": posting.requirements,
            "description": (str(desc)[:4000] if desc else ""),
        },
        "criteria": criteria.to_dict(),
    }, ensure_ascii=False)
    data = llm.complete_json(_SCREEN_SYS, user)
    score = int(max(0, min(100, int(data.get("score", 0)))))
    status = data.get("status")
    if status not in (ScreenStatus.SCREENED_IN.value, ScreenStatus.SCREENED_OUT.value):
        status = (ScreenStatus.SCREENED_IN.value if score >= SCREEN_IN_THRESHOLD
                  else ScreenStatus.SCREENED_OUT.value)
    rationale = str(data.get("rationale", ""))[:1000]
    return ScreenResult(status=status, score=score, rationale=f"[openai] {rationale}")


# --------------------------------------------------------------------------- #
# Crafter — reword ONE real bullet; must preserve every number/fact.
# --------------------------------------------------------------------------- #
_BULLET_SYS = (
    "You reword a single resume bullet to fit a target job. STRICT RULES: keep "
    "every number, metric, employer, and technology EXACTLY as given; do not add "
    "any fact, number, employer, title, or metric not present in the input bullet. "
    "You may only rephrase and reorder. Return JSON: {\"bullet\": str}."
)


def llm_reword_bullet(llm, bullet_text: str, posting: Posting) -> str:
    user = json.dumps({
        "bullet": bullet_text,
        "target_title": posting.title,
        "target_requirements": posting.requirements,
    }, ensure_ascii=False)
    try:
        data = llm.complete_json(_BULLET_SYS, user)
        reworded = str(data.get("bullet", "")).strip()
        return reworded or bullet_text
    except Exception:
        return bullet_text  # fail safe to the verbatim real bullet


_COVER_SYS = (
    "You write a concise, professional cover-letter body (2-3 short paragraphs). "
    "STRICT RULES: use ONLY the accomplishments provided; do not invent metrics, "
    "employers, titles, dates, or skills. Do not add numbers not present in the "
    "provided accomplishments. Return JSON: {\"body\": str}."
)


def llm_cover_body(llm, name: str, posting: Posting, bullets: list[str]) -> Optional[str]:
    user = json.dumps({
        "candidate_name": name,
        "company": posting.company,
        "role": posting.title,
        "accomplishments": bullets,
    }, ensure_ascii=False)
    try:
        data = llm.complete_json(_COVER_SYS, user)
        body = str(data.get("body", "")).strip()
        return body or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Strategy — narrative prose only; hard criteria stay deterministic.
# --------------------------------------------------------------------------- #
_STRATEGY_SYS = (
    "You are a career strategist. Given a candidate profile, write a short "
    "market-context and positioning note (3-5 bullet points). Do not invent "
    "compensation figures or company names. Return JSON: {\"notes\": [str, ...]}."
)


_DERIVE_SYS = (
    "You are a career strategist. Using the candidate's VERIFIED accomplishment bank "
    "(real skills, job titles, employers) and their stated aspirations, propose "
    "job-search targeting criteria. RULES: ground `must_haves` and `keywords_boost` in "
    "skills the candidate has demonstrated in the bank OR explicitly listed in their "
    "aspirations — do NOT introduce skills they have neither demonstrated nor listed. "
    "`must_haves` = the few (2-5) non-negotiable capabilities a good-fit posting should "
    "require. `keywords_boost` = nice-to-have signals. `target_roles` = specific job "
    "titles to search for. `seniority` = levels (e.g. senior, staff). Include a brief "
    "`rationale` citing the candidate's real experience. Return JSON with keys: "
    "target_roles, seniority, must_haves, keywords_boost, rationale."
)


def llm_derive_criteria(llm, profile: dict, bank) -> dict:
    """Ask the LLM to propose targeting criteria grounded in the bank + aspirations."""
    payload = {
        "aspirations": profile.get("summary", ""),
        "profile_hints": {k: profile.get(k) for k in
                          ("target_roles", "seniority", "must_haves",
                           "keywords_boost", "comp_min", "geographies")},
        "bank": {
            "skills": sorted(bank.known_skills()),
            "titles": sorted(bank.known_titles()),
            "employers": sorted(bank.known_employers()),
            "accomplishments": [a.text for a in bank.accomplishments][:12],
            "credentials": list(bank.credentials),
        },
    }
    data = llm.complete_json(_DERIVE_SYS, json.dumps(payload, ensure_ascii=False))
    def _l(key):
        v = data.get(key, [])
        return [str(x) for x in v] if isinstance(v, list) else []
    return {
        "target_roles": _l("target_roles"),
        "seniority": _l("seniority"),
        "must_haves": _l("must_haves"),
        "keywords_boost": _l("keywords_boost"),
        "rationale": str(data.get("rationale", "")),
    }


def llm_strategy_notes(llm, profile: dict) -> list[str]:
    try:
        data = llm.complete_json(_STRATEGY_SYS, json.dumps(profile, ensure_ascii=False))
        notes = data.get("notes", [])
        return [str(n) for n in notes][:8] if isinstance(notes, list) else []
    except Exception:
        return []
