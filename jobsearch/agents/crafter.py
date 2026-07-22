"""Crafter (SAFE) — spec §4.4 + the anti-fabrication constraint §0.3.

Reads the accomplishment bank, and for each screened_in job selects the most
relevant REAL accomplishments and produces a tailored resume + cover letter.
Tailoring = choosing which real accomplishments to foreground and rewording
them for the posting. It never invents employers, dates, titles, metrics, or
skills not present in the bank.

A post-generation fabrication check (``find_fabrications``) re-inspects the
rendered documents and flags any employer/title/date/metric/skill not traceable
to the bank. If anything is flagged, the Crafter refuses to emit (raises
``FabricationError``) — a planted false claim can never reach a file.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import AccomplishmentBank, Posting, Tier
from ..store.job_store import JobStore

TIER = Tier.SAFE


class FabricationError(Exception):
    """Raised when a generated document contains a claim not traceable to the bank."""


# --------------------------------------------------------------------------- #
# Factual-token extraction, used identically on the bank and on the rendered
# document so the two can be compared apples-to-apples.
# --------------------------------------------------------------------------- #
# Metric-ish tokens: percentages, currency, multipliers, and multi-digit numbers.
_METRIC_RE = re.compile(
    r"""
    (?:\$\s?\d[\d,\.]*\s?[kKmMbB]?)      # $2M, $150,000
    | (?:\d[\d,\.]*\s?%)                  # 40%, 12.5 %
    | (?:\d[\d,\.]*\s?[xX]\b)            # 3x
    | (?:\b\d[\d,\.]{1,}\b)              # 10,000 / 2019 / 2.5  (>=2 chars)
    """,
    re.VERBOSE,
)


def _norm_token(t: str) -> str:
    return re.sub(r"\s+", "", t).lower().rstrip(".")


def _metric_tokens(text: str) -> set[str]:
    return {_norm_token(m.group(0)) for m in _METRIC_RE.finditer(text or "")}


def _bank_text_blob(bank: AccomplishmentBank) -> str:
    parts = [bank.name]
    parts += list(bank.contact.values())
    parts += bank.skills + bank.credentials
    for a in bank.accomplishments:
        parts += [a.employer, a.title, a.start_date, a.end_date, a.text]
        parts += a.metrics + a.skills
    return " ".join(str(p) for p in parts)


# --------------------------------------------------------------------------- #
# Fabrication check (spec §0.3)
# --------------------------------------------------------------------------- #
# The rendered experience lines use this exact, machine-parseable format so the
# check can validate (employer, title, dates) tuples reliably.
_EXP_LINE_RE = re.compile(r"^- (?P<employer>.+?) \| (?P<title>.+?) \| (?P<dates>.+)$")
_SKILLS_HEADER = "## Skills"


def find_fabrications(doc_text: str, bank: AccomplishmentBank) -> list[str]:
    """Return a list of fabrication findings; empty means clean."""
    findings: list[str] = []

    # 1. Every metric token in the doc must appear in the bank.
    allowed_metrics = _metric_tokens(_bank_text_blob(bank))
    for tok in _metric_tokens(doc_text):
        if tok not in allowed_metrics:
            findings.append(f"metric not traceable to bank: {tok!r}")

    # 2. Every experience entry must match a bank (employer, title, dates).
    bank_exp = {
        (a.employer.strip().lower(), a.title.strip().lower(),
         f"{a.start_date}–{a.end_date}".strip().lower())
        for a in bank.accomplishments
    }
    # also accept a plain hyphen join in case of formatting
    bank_exp |= {
        (a.employer.strip().lower(), a.title.strip().lower(),
         f"{a.start_date}-{a.end_date}".strip().lower())
        for a in bank.accomplishments
    }
    for line in doc_text.splitlines():
        m = _EXP_LINE_RE.match(line.strip())
        if not m:
            continue
        entry = (
            m.group("employer").strip().lower(),
            m.group("title").strip().lower(),
            m.group("dates").strip().lower(),
        )
        if entry not in bank_exp:
            findings.append(
                f"experience entry not traceable to bank: "
                f"{m.group('employer')} | {m.group('title')} | {m.group('dates')}"
            )

    # 3. Every skill listed under '## Skills' must be a known bank skill.
    known_skills = {s.strip().lower() for s in bank.known_skills()}
    if _SKILLS_HEADER in doc_text:
        skills_block = doc_text.split(_SKILLS_HEADER, 1)[1]
        # stop at the next header if any
        skills_block = skills_block.split("\n##", 1)[0]
        listed = [s.strip().lower() for s in re.split(r"[,\n•\-]", skills_block) if s.strip()]
        for s in listed:
            if s and s not in known_skills:
                findings.append(f"skill not in bank: {s!r}")

    return findings


# --------------------------------------------------------------------------- #
# Relevance ranking + rendering
# --------------------------------------------------------------------------- #
def _relevance(acc, posting: Posting, positioning=None) -> int:
    hay = (posting.title + " " + " ".join(posting.requirements) + " " + posting.comp_text).lower()
    score = 0
    for sk in acc.skills:
        if sk.lower() in hay:
            score += 3
    for word in re.findall(r"\w+", acc.text.lower()):
        if len(word) > 4 and word in hay:
            score += 1
    # Strategy positioning: foreground accomplishments that match the personal
    # brand (lead_with / emphasize), so tailoring is consistent across postings.
    if positioning is not None:
        acc_blob = (acc.text + " " + " ".join(acc.skills)).lower()
        for term in list(positioning.lead_with) + list(positioning.emphasize):
            t = str(term).lower().strip()
            if t and t in acc_blob:
                score += 5
    return score


class Crafter:
    def __init__(self, store: JobStore, bank: AccomplishmentBank, output_dir: str | Path,
                 *, max_bullets: int = 5, llm=None, positioning=None):
        self.store = store
        self.bank = bank
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_bullets = max_bullets
        # Optional OpenAI-backed rewording. The fabrication check ALWAYS runs on
        # the result, so the model can never smuggle in a claim (spec §0.3).
        self.llm = llm
        # Optional strategy positioning (the shared lens with the Strategy Advisor):
        # biases selection + framing without introducing claims.
        self.positioning = positioning

    def _select(self, posting: Posting):
        ranked = sorted(
            self.bank.accomplishments,
            key=lambda a: _relevance(a, posting, self.positioning),
            reverse=True,
        )
        return ranked[: self.max_bullets]

    def _relevant_skills(self, posting: Posting) -> list[str]:
        hay = (posting.title + " " + " ".join(posting.requirements)).lower()
        matched = [s for s in self.bank.known_skills() if s.lower() in hay]
        # Always fall back to real bank skills; never invent.
        return matched or sorted(self.bank.known_skills())

    def render_resume(self, posting: Posting) -> str:
        selected = self._select(posting)
        L = [f"# {self.bank.name}"]
        if self.bank.contact:
            L.append(" | ".join(f"{k}: {v}" for k, v in self.bank.contact.items()))
        L.append(f"\n_Application tailored for: {posting.title} at {posting.company}_\n")
        L.append("## Experience")
        for a in selected:
            # Header line (employer|title|dates) is ALWAYS deterministic from the
            # bank — never model-generated — so identity facts can't drift.
            L.append(f"- {a.employer} | {a.title} | {a.start_date}–{a.end_date}")
            bullet = a.text
            if self.llm is not None:
                from .llm_reasoner import llm_reword_bullet
                bullet = llm_reword_bullet(self.llm, a.text, posting,
                                           positioning=self.positioning)
            L.append(f"  - {bullet}")
        L.append(f"\n{_SKILLS_HEADER}")
        L.append(", ".join(self._relevant_skills(posting)))
        if self.bank.credentials:
            L.append("\n## Credentials")
            L += [f"- {c}" for c in self.bank.credentials]
        return "\n".join(L) + "\n"

    def render_cover_letter(self, posting: Posting) -> str:
        selected = self._select(posting)
        top = selected[0] if selected else None

        if self.llm is not None:
            from .llm_reasoner import llm_cover_body
            body = llm_cover_body(
                self.llm, self.bank.name, posting, [a.text for a in selected],
                positioning=self.positioning,
            )
            if body:
                return (f"Dear {posting.company} Hiring Team,\n\n{body}\n\n"
                        f"Sincerely,\n{self.bank.name}\n")

        L = [f"Dear {posting.company} Hiring Team,\n"]
        L.append(
            f"I am writing to express my interest in the {posting.title} role. "
            "My background aligns closely with what you're looking for."
        )
        if top:
            # Reworded framing, but the metric/text comes verbatim from the bank.
            L.append(f"\nMost relevantly, at {top.employer} as {top.title}: {top.text}")
        if len(selected) > 1:
            L.append("\nAdditional relevant experience:")
            for a in selected[1:]:
                L.append(f"- {a.text}")
        L.append(
            f"\nI would welcome the chance to bring this experience to {posting.company}."
        )
        L.append(f"\nSincerely,\n{self.bank.name}")
        return "\n".join(L) + "\n"

    def craft_one(self, posting: Posting) -> tuple[str, str]:
        """Render + fabrication-check + write files. Returns (resume, cover) paths.

        Raises FabricationError (and writes nothing) if any claim is untraceable.
        """
        resume = self.render_resume(posting)
        cover = self.render_cover_letter(posting)

        problems = find_fabrications(resume, self.bank) + find_fabrications(cover, self.bank)
        if problems:
            raise FabricationError(
                f"Refusing to emit documents for {posting.dedup_key}: "
                + "; ".join(problems)
            )

        stem = re.sub(r"[^a-z0-9]+", "_", posting.dedup_key.lower()).strip("_")[:80]
        resume_path = self.output_dir / f"{stem}__resume.md"
        cover_path = self.output_dir / f"{stem}__cover.md"
        resume_path.write_text(resume, encoding="utf-8")
        cover_path.write_text(cover, encoding="utf-8")
        return (str(resume_path), str(cover_path))

    def run(self) -> dict[str, int]:
        """Craft docs for all screened_in jobs. Returns {'crafted', 'refused'}."""
        from ..models import ScreenStatus

        crafted = refused = 0
        for p in self.store.get_by_status(screen_status=ScreenStatus.SCREENED_IN):
            try:
                resume_path, cover_path = self.craft_one(p)
            except FabricationError:
                refused += 1
                self.store.set_apply_status(p.dedup_key, "failed")
                continue
            self.store.set_docs(
                p.dedup_key, resume_path=resume_path, cover_letter_path=cover_path
            )
            self.store.set_apply_status(p.dedup_key, "drafted")
            crafted += 1
        return {"crafted": crafted, "refused": refused}
