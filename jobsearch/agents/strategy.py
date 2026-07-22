"""Strategy Advisor (SAFE, low frequency) — spec §4.1.

Turns the user's status + aspirations into a ``strategy.md`` document AND a
machine-usable ``StrategyCriteria`` block the Screener consumes. Runs on demand,
not in the per-job loop.

This implementation is deterministic and rule-based over a user profile dict so
the pipeline is reproducible and testable offline. The ``_market_context`` hook
is where a live LLM / web-search enrichment would attach (spec allows it) — it
returns notes only, never new hard criteria, keeping this unit pure reasoning.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from ..models import StrategyCriteria, Tier

TIER = Tier.SAFE


def _clean_list(v: Any) -> list[str]:
    """Coerce to a stripped, de-duplicated (case-insensitive) list of strings."""
    if not isinstance(v, list):
        return []
    out, seen = [], set()
    for x in v:
        s = str(x).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


class StrategyAdvisor:
    def __init__(self, *, market_context_fn=None, llm=None):
        # Injectable enrichment; default is a no-op so the unit stays offline/pure.
        # If an LLM is supplied, use it for the market-context notes (narrative
        # only — the hard screening criteria stay deterministic from the profile).
        self.llm = llm
        if market_context_fn is None and llm is not None:
            from .llm_reasoner import llm_strategy_notes
            market_context_fn = lambda profile: llm_strategy_notes(llm, profile)
        self._market_context_fn = market_context_fn or (lambda profile: [])

    # ------------------------------------------------------------------ #
    # Bank-grounded criteria (LLM proposes; guardrails validate)
    # ------------------------------------------------------------------ #
    def _grounding(self, profile: dict, bank) -> tuple[str, set]:
        """Blob + token vocabulary of everything the candidate has actually
        demonstrated (bank) or explicitly stated (profile) — the allowed
        vocabulary for grounding must-haves."""
        parts: list[str] = []
        parts += list(bank.known_skills()) + list(bank.known_titles())
        parts += list(bank.known_employers()) + list(bank.credentials)
        parts += [a.text for a in bank.accomplishments]
        for v in (profile or {}).values():
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, list):
                parts += [str(x) for x in v]
        blob = " ".join(parts).lower()
        vocab = set(re.findall(r"[a-z0-9+#.]+", blob))
        return blob, vocab

    def build_criteria_grounded(self, profile: dict, bank) -> tuple[StrategyCriteria, dict]:
        """Return (criteria, meta). Falls back to deterministic if no LLM/bank."""
        base = self.build_criteria(profile)   # deterministic hard constraints
        if self.llm is None or bank is None:
            return base, {"source": "profile"}

        from .llm_reasoner import llm_derive_criteria
        data = llm_derive_criteria(self.llm, profile, bank)

        blob, vocab = self._grounding(profile, bank)

        def grounded(c: str) -> bool:
            cl = c.lower().strip()
            return cl in blob or all(w in vocab for w in re.findall(r"[a-z0-9+#.]+", cl))

        llm_mh = _clean_list(data.get("must_haves"))
        kept_mh = [m for m in llm_mh if grounded(m)]
        dropped_mh = [m for m in llm_mh if not grounded(m)]

        roles = _clean_list(data.get("target_roles")) or base.target_roles
        seniority = _clean_list(data.get("seniority")) or base.seniority
        boosts = _clean_list(data.get("keywords_boost")) or base.keywords_boost

        criteria = StrategyCriteria(
            target_roles=roles[:8],
            seniority=seniority[:5],
            comp_min=base.comp_min,               # hard constraints: user owns these
            comp_currency=base.comp_currency,
            geographies=base.geographies,
            remote_ok=base.remote_ok,
            must_haves=(kept_mh or base.must_haves)[:6],
            dealbreakers=base.dealbreakers,
            keywords_boost=boosts[:10],
        )
        meta = {
            "source": f"llm:{getattr(self.llm, 'model', 'openai')}",
            "rationale": data.get("rationale", ""),
            "dropped_must_haves": dropped_mh,   # ungrounded → excluded (transparency)
        }
        return criteria, meta

    def build_criteria(self, profile: dict[str, Any]) -> StrategyCriteria:
        """Structure a raw user profile into machine-usable screening criteria."""
        return StrategyCriteria(
            target_roles=[r.strip() for r in profile.get("target_roles", []) if r.strip()],
            seniority=[s.strip() for s in profile.get("seniority", []) if s.strip()],
            comp_min=profile.get("comp_min"),
            comp_currency=profile.get("comp_currency", "USD"),
            geographies=[g.strip() for g in profile.get("geographies", []) if g.strip()],
            remote_ok=bool(profile.get("remote_ok", True)),
            must_haves=[m.strip() for m in profile.get("must_haves", []) if m.strip()],
            dealbreakers=[d.strip() for d in profile.get("dealbreakers", []) if d.strip()],
            keywords_boost=[k.strip() for k in profile.get("keywords_boost", []) if k.strip()],
        )

    def _market_context(self, profile: dict[str, Any]) -> list[str]:
        try:
            return list(self._market_context_fn(profile)) or []
        except Exception:
            return []

    def render_markdown(self, profile: dict[str, Any], criteria: StrategyCriteria,
                        meta: Optional[dict] = None) -> str:
        notes = self._market_context(profile)
        meta = meta or {}
        L: list[str] = []
        L.append("# Job Search Strategy\n")
        if profile.get("summary"):
            L.append(f"_{profile['summary']}_\n")
        L.append("## Target role types")
        L += [f"- {r}" for r in criteria.target_roles] or ["- (none specified)"]
        L.append("\n## Seniority")
        L += [f"- {s}" for s in criteria.seniority] or ["- (any)"]
        L.append("\n## Compensation band")
        if criteria.comp_min:
            L.append(f"- Minimum: {criteria.comp_min:,} {criteria.comp_currency}")
        else:
            L.append("- (no hard floor specified)")
        L.append("\n## Geography")
        L += [f"- {g}" for g in criteria.geographies] or ["- (flexible)"]
        L.append(f"- Remote acceptable: {'yes' if criteria.remote_ok else 'no'}")
        L.append("\n## Must-haves")
        L += [f"- {m}" for m in criteria.must_haves] or ["- (none)"]
        L.append("\n## Dealbreakers")
        L += [f"- {d}" for d in criteria.dealbreakers] or ["- (none)"]
        L.append("\n## Nice-to-have signals")
        L += [f"- {k}" for k in criteria.keywords_boost] or ["- (none)"]
        if notes:
            L.append("\n## Market context")
            L += [f"- {n}" for n in notes]
        if meta.get("rationale") or meta.get("dropped_must_haves"):
            L.append("\n## How these criteria were derived")
            L.append(f"- Source: {meta.get('source', 'profile')} — criteria grounded "
                     "in the verified accomplishment bank and stated aspirations.")
            if meta.get("rationale"):
                L.append(f"- {meta['rationale']}")
            if meta.get("dropped_must_haves"):
                L.append(f"- Dropped as ungrounded (not in bank or aspirations): "
                         f"{', '.join(meta['dropped_must_haves'])}")
        L.append("\n## Machine-usable screening criteria")
        L.append("```yaml")
        L.append(yaml.safe_dump(criteria.to_dict(), sort_keys=False).rstrip())
        L.append("```")
        return "\n".join(L) + "\n"

    def run(self, profile: dict[str, Any], strategy_path: str | Path,
            bank=None) -> StrategyCriteria:
        """Generate strategy.md and return the criteria (also embedded in the md).

        When an LLM and an accomplishment bank are available, criteria are derived
        and grounded (see ``build_criteria_grounded``); otherwise deterministic.
        """
        try:
            criteria, meta = self.build_criteria_grounded(profile, bank)
        except Exception:
            # Any LLM/parse failure degrades to the deterministic profile criteria.
            criteria, meta = self.build_criteria(profile), {"source": "profile (fallback)"}
        md = self.render_markdown(profile, criteria, meta)
        Path(strategy_path).parent.mkdir(parents=True, exist_ok=True)
        Path(strategy_path).write_text(md, encoding="utf-8")
        return criteria


def load_criteria_from_strategy(strategy_path: str | Path) -> Optional[StrategyCriteria]:
    """Parse the machine-usable YAML block back out of strategy.md."""
    text = Path(strategy_path).read_text(encoding="utf-8")
    marker = "```yaml"
    if marker not in text:
        return None
    block = text.split(marker, 1)[1].split("```", 1)[0]
    data = yaml.safe_load(block)
    return StrategyCriteria.from_dict(data or {})
