"""Interactive Strategy Advisor session (chat + documents -> criteria).

Holds a conversation, ingested document text, and an evolving StrategyCriteria
draft. Each turn asks OpenAI for BOTH a chat reply and an updated criteria object,
grounded in the accomplishment bank + uploaded documents + the conversation.

Human-in-the-loop, so grounding is advisory here (the UI flags ungrounded
must-haves rather than silently dropping them — the user is authoritative). The
hard constraints from the config profile seed the initial draft but can be
changed through chat. Nothing is written until ``save`` is called.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..models import StrategyCriteria
from .strategy import StrategyAdvisor, _clean_list

_CHAT_SYS = (
    "You are a career strategy advisor helping a job seeker define machine-usable "
    "job-search targeting criteria. Converse naturally and briefly: ask a clarifying "
    "question when it helps, and fold in the uploaded documents and the verified "
    "accomplishment bank. Prefer grounding must_haves in skills the candidate has "
    "demonstrated or explicitly stated. Each turn, return JSON with exactly two keys: "
    "\"reply\" (your short chat message to the user) and \"criteria\" (the FULL updated "
    "criteria object). criteria keys: target_roles[], seniority[], comp_min(int|null), "
    "comp_currency(str), geographies[], remote_ok(bool), must_haves[], dealbreakers[], "
    "keywords_boost[]. Always return the complete criteria reflecting everything so far, "
    "not just changes."
)

_MAX_DOC_CHARS = 12000


class StrategySession:
    def __init__(self, llm, *, profile: Optional[dict] = None, bank=None,
                 bank_path: Optional[str] = None):
        self.llm = llm
        self.profile = profile or {}
        self.bank = bank
        self.bank_path = bank_path              # where committed bank entries are written
        self.messages: list[dict] = []          # [{role, content}]
        self.documents: list[dict] = []         # [{name, text}]
        self.draft_entries: list[dict] = []     # last drafted bank entries (unsaved)
        # Seed the draft from the deterministic profile criteria.
        self.criteria = StrategyAdvisor().build_criteria(self.profile)

    # ------------------------------------------------------------------ #
    def add_document(self, name: str, text: str) -> None:
        self.documents.append({"name": name, "text": (text or "")[:_MAX_DOC_CHARS]})

    def note_document(self, name: str) -> dict:
        """Auto-turn after an upload so the user gets immediate feedback."""
        return self.send(
            f"I uploaded a document named '{name}'. Please read it and update my "
            "targeting criteria, then briefly tell me what you learned from it."
        )

    def send(self, user_text: str) -> dict:
        self.messages.append({"role": "user", "content": user_text})
        data = self._turn()
        reply = str(data.get("reply", "")).strip() or "(no reply)"
        crit = data.get("criteria")
        if isinstance(crit, dict):
            self._apply(crit)
        self.messages.append({"role": "assistant", "content": reply})
        return self.state(last_reply=reply)

    # ------------------------------------------------------------------ #
    def _bank_summary(self) -> dict:
        if not self.bank:
            return {}
        return {
            "skills": sorted(self.bank.known_skills()),
            "titles": sorted(self.bank.known_titles()),
            "employers": sorted(self.bank.known_employers()),
            "accomplishments": [a.text for a in self.bank.accomplishments][:12],
        }

    def _turn(self) -> dict:
        context = {
            "conversation": self.messages[-20:],
            "documents": [{"name": d["name"], "excerpt": d["text"]} for d in self.documents],
            "accomplishment_bank": self._bank_summary(),
            "current_criteria": self.criteria.to_dict(),
            "profile_hard_constraints": {
                k: self.profile.get(k) for k in ("comp_min", "dealbreakers")
            },
        }
        try:
            return self.llm.complete_json(_CHAT_SYS, json.dumps(context, ensure_ascii=False))
        except Exception as e:
            return {"reply": f"(advisor error: {e})", "criteria": self.criteria.to_dict()}

    def _apply(self, crit: dict) -> None:
        cur = self.criteria.to_dict()
        list_fields = {"target_roles", "seniority", "geographies",
                       "must_haves", "dealbreakers", "keywords_boost"}
        for k, v in crit.items():
            if k not in cur or v is None:
                continue
            if k in list_fields:
                cur[k] = _clean_list(v)
            elif k == "comp_min":
                try:
                    cur[k] = int(v)
                except (TypeError, ValueError):
                    pass
            elif k == "remote_ok":
                cur[k] = bool(v)
            else:
                cur[k] = str(v)
        self.criteria = StrategyCriteria.from_dict(cur)

    # ------------------------------------------------------------------ #
    def ungrounded_must_haves(self) -> list[str]:
        """Must-haves not traceable to bank/aspirations (advisory flag for the UI)."""
        if not self.bank:
            return []
        adv = StrategyAdvisor()
        blob, vocab = adv._grounding(self.profile, self.bank)
        out = []
        for m in self.criteria.must_haves:
            ml = m.lower().strip()
            if not (ml in blob or all(w in vocab for w in re.findall(r"[a-z0-9+#.]+", ml))):
                out.append(m)
        return out

    def state(self, last_reply: str = "") -> dict:
        import yaml
        return {
            "messages": self.messages,
            "criteria": self.criteria.to_dict(),
            "yaml": yaml.safe_dump(self.criteria.to_dict(), sort_keys=False).rstrip(),
            "documents": [d["name"] for d in self.documents],
            "ungrounded_must_haves": self.ungrounded_must_haves(),
            "draft_entries": self.draft_entries,
            "last_reply": last_reply,
        }

    # ------------------------------------------------------------------ #
    # Shared intake -> bank: draft real accomplishments from uploaded docs.
    # ------------------------------------------------------------------ #
    def draft_bank_entries(self) -> list[dict]:
        """Draft bank entries from all uploaded documents (for user review)."""
        from .llm_reasoner import llm_draft_bank_entries
        text = "\n\n".join(f"# {d['name']}\n{d['text']}" for d in self.documents)
        self.draft_entries = llm_draft_bank_entries(self.llm, text)
        return self.draft_entries

    def commit_bank_entries(self, entries: list[dict]) -> int:
        """Append reviewed entries to the accomplishment bank file. Returns count.

        Entries were extracted from the user's own documents and explicitly
        accepted here — the bank stays a human-reviewed source of truth. Existing
        bank content (name/contact/other accomplishments) is preserved.
        """
        import yaml
        from ..models import AccomplishmentBank
        if not self.bank_path or not entries:
            return 0
        p = Path(self.bank_path)
        data = {}
        if p.exists():
            data = yaml.safe_load(p.read_text("utf-8")) or {}
        data.setdefault("accomplishments", [])
        data.setdefault("skills", [])
        added = 0
        for e in entries:
            data["accomplishments"].append({
                "employer": e.get("employer", ""), "title": e.get("title", ""),
                "start_date": e.get("start_date", ""), "end_date": e.get("end_date", ""),
                "text": e.get("text", ""), "metrics": e.get("metrics", []),
                "skills": e.get("skills", []),
            })
            for s in e.get("skills", []):
                if s and s not in data["skills"]:
                    data["skills"].append(s)
            added += 1
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        # Refresh in-memory bank so grounding/positioning reflect the new entries.
        self.bank = AccomplishmentBank.from_dict(data)
        self.draft_entries = []
        return added

    def save(self, strategy_path: str | Path) -> str:
        """Write strategy.md from the current draft (same format the Screener reads)."""
        adv = StrategyAdvisor(llm=None)  # deterministic render; no extra LLM call
        meta = {
            "source": "interactive session",
            "rationale": "Criteria built interactively from chat" +
                         (f" and {len(self.documents)} document(s)" if self.documents else "") + ".",
        }
        md = adv.render_markdown(self.profile, self.criteria, meta)
        Path(strategy_path).parent.mkdir(parents=True, exist_ok=True)
        Path(strategy_path).write_text(md, encoding="utf-8")
        return str(strategy_path)
