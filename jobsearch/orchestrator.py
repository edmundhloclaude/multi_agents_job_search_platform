"""Orchestrator: workflow control, tier enforcement, and the submit gate (spec §5).

Owns the job store and the BrowserController lifecycle. Enforces trust tiers
(spec §0): a READ_BROWSER unit is never handed a submit-capable controller, and
the GATED submit phase always routes through the human approval gate.

The submit gate (spec §0.2) is mandatory and cannot be disabled by config,
flag, or agent decision. ``Approval`` objects carry a private token that ONLY
``human_approval_gate`` can stamp, so no other code path can forge an approved
application. There is deliberately no auto-approve mode and no bypass env var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .browser.controller import BrowserController, ReadOnlyBrowser
from .models import ApplyStatus, FilledApplication, Tier
from .store.job_store import JobStore


# --------------------------------------------------------------------------- #
# Tier enforcement
# --------------------------------------------------------------------------- #
class TierViolation(Exception):
    """Raised when a unit would be run in a way that escalates its tier."""


def assert_controller_for_tier(tier: Tier, controller: Optional[BrowserController]) -> None:
    """Assert the controller handed to a unit matches its declared tier.

    * SAFE           -> must receive no browser controller at all.
    * READ_BROWSER   -> must receive a controller that cannot submit.
    * GATED          -> may receive a submit-capable controller (only ever
                        constructed behind the approval gate).
    """
    if tier is Tier.SAFE:
        if controller is not None:
            raise TierViolation("SAFE unit must not receive a browser controller.")
        return
    if tier is Tier.READ_BROWSER:
        if controller is None:
            raise TierViolation("READ_BROWSER unit requires a (read-only) controller.")
        if getattr(controller, "submit_enabled", False):
            raise TierViolation(
                "READ_BROWSER unit received a submit-capable controller — refused."
            )
        return
    if tier is Tier.GATED:
        if controller is None or not getattr(controller, "submit_enabled", False):
            raise TierViolation("GATED submit phase requires a submit-capable controller.")
        return
    raise TierViolation(f"Unknown tier: {tier!r}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# The submit gate (spec §0.2)
# --------------------------------------------------------------------------- #
# Private token: an Approval with approved=True is only valid if it carries this
# exact object, which lives module-private and is stamped ONLY by the gate. This
# is what makes an approved application impossible to forge from an agent or a
# config flag.
_GATE_TOKEN = object()

#: The exact phrase a human must type. Not configurable.
APPROVE_PHRASE = "SUBMIT"


@dataclass(frozen=True)
class Approval:
    dedup_key: str
    approved: bool
    confirmation_text: str = ""
    _token: object = None  # set only by human_approval_gate for approved==True

    def is_valid_for(self, filled: "FilledApplication") -> bool:
        """True only for a genuine, approved gate result matching this app."""
        return (
            self.approved
            and self._token is _GATE_TOKEN
            and self.dedup_key == filled.dedup_key
        )


def human_approval_gate(
    filled: FilledApplication,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> Approval:
    """Print the fully-filled application and block on typed human approval.

    This is the ONLY function that can mint an approved ``Approval``. It ignores
    every environment variable and flag by design — there is no bypass.
    Returns an Approval; ``approved`` is True only if the human typed the exact
    confirmation phrase.
    """
    output_fn(filled.render_for_review())
    output_fn(
        f"\nTo SUBMIT this application, type exactly: {APPROVE_PHRASE}\n"
        "Anything else (including empty) will SKIP it. No auto-approve exists."
    )
    resp = input_fn("Confirm > ").strip()
    if resp == APPROVE_PHRASE:
        return Approval(
            dedup_key=filled.dedup_key,
            approved=True,
            confirmation_text=resp,
            _token=_GATE_TOKEN,
        )
    return Approval(dedup_key=filled.dedup_key, approved=False, confirmation_text=resp)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    db_path: str
    output_dir: str
    strategy_path: str
    accomplishment_bank_path: str
    sources: list[dict] = field(default_factory=list)
    model: str = "claude-opus-4-8"
    raw: dict = field(default_factory=dict)


class Orchestrator:
    """Coordinates stages, enforces tiers, owns store + browser lifecycle."""

    def __init__(
        self,
        config: Config,
        *,
        controller_factory: Optional[Callable[[bool], BrowserController]] = None,
    ):
        self.config = config
        self.store = JobStore(config.db_path)
        # controller_factory(submit_enabled) -> BrowserController. Injectable so
        # tests use the mock and production uses CUA.
        self._controller_factory = controller_factory or self._default_factory
        Path(config.output_dir).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default_factory(submit_enabled: bool) -> BrowserController:
        from .browser.cua_controller import CUABrowserController
        return CUABrowserController(submit_enabled=submit_enabled)

    def close(self) -> None:
        self.store.close()

    # -- controller provisioning (tier-enforced) ----------------------- #
    def read_only_controller(self) -> BrowserController:
        """A controller for READ_BROWSER units — guaranteed non-submitting."""
        inner = self._controller_factory(False)
        ro = ReadOnlyBrowser(inner)
        assert_controller_for_tier(Tier.READ_BROWSER, ro)
        return ro

    def _submit_controller(self) -> BrowserController:
        """A submit-capable controller. PRIVATE — only used inside the gate flow."""
        ctrl = self._controller_factory(True)
        assert_controller_for_tier(Tier.GATED, ctrl)
        return ctrl

    def log(self, stage: str, tier: str = "", message: str = "") -> None:
        self.store.log_run(stage, tier, message)

    # ------------------------------------------------------------------ #
    # Loading helpers
    # ------------------------------------------------------------------ #
    def _load_bank(self):
        import yaml
        from .models import AccomplishmentBank
        data = yaml.safe_load(Path(self.config.accomplishment_bank_path).read_text("utf-8"))
        return AccomplishmentBank.from_dict(data or {})

    def _make_llm(self, agent: str | None = None):
        """Build the reasoning LLM for a given agent, or None (deterministic).

        Uses the top-level llm config plus optional per-agent overrides
        (llm.agents.<agent>). agent is one of: strategy | screener | crafter.
        """
        from .llm.factory import make_llm
        return make_llm((self.config.raw or {}).get("llm", {}), agent)

    def _load_criteria(self):
        from .agents.strategy import load_criteria_from_strategy
        from .models import StrategyCriteria
        if not Path(self.config.strategy_path).exists():
            # No strategy yet — fall back to empty criteria (stage still runs).
            return StrategyCriteria()
        crit = load_criteria_from_strategy(self.config.strategy_path)
        return crit or StrategyCriteria()

    # ------------------------------------------------------------------ #
    # Stages (each independently runnable — spec §5)
    # ------------------------------------------------------------------ #
    def run_strategy(self, profile: dict):
        from .agents.strategy import StrategyAdvisor, TIER
        assert_controller_for_tier(TIER, None)  # SAFE: no controller
        self.log("strategy", TIER.value, "started")
        # Ground criteria in the accomplishment bank when available (SAFE: no browser).
        try:
            bank = self._load_bank()
        except Exception:
            bank = None
        crit = StrategyAdvisor(llm=self._make_llm("strategy")).run(
            profile, self.config.strategy_path, bank=bank)
        self.log("strategy", TIER.value, f"criteria roles={crit.target_roles}")
        return crit

    def _build_api_fetchers(self) -> dict:
        """Map source name -> fetcher for `type: api` sources with a known provider."""
        from .sources.theirstack import theirstack_fetcher
        registry = {"theirstack": theirstack_fetcher}
        fetchers = {}
        for s in self.config.sources:
            if s.get("type") == "api" and s.get("provider") in registry:
                fetchers[s["name"]] = registry[s["provider"]]
        return fetchers

    def run_crawl(self, *, api_fetchers=None):
        from .agents.crawler import Crawler, TIER
        self.log("crawl", TIER.value, "started")
        controller = self.read_only_controller()  # tier-enforced read-only
        assert_controller_for_tier(TIER, controller)
        crawler = Crawler(self.store, controller,
                          api_fetchers=api_fetchers or self._build_api_fetchers())
        reports = crawler.run(self._load_criteria(), self.config.sources)
        controller.close()
        return reports

    def run_screen(self, *, rescreen: bool = False):
        from .agents.screener import Screener, TIER
        assert_controller_for_tier(TIER, None)
        self.log("screen", TIER.value, "started")
        n = Screener(self.store, llm=self._make_llm("screener")).run(
            self._load_criteria(), rescreen=rescreen)
        self.log("screen", TIER.value, f"screened={n}")
        return n

    def run_craft(self):
        from .agents.crafter import Crafter, TIER
        from .agents.strategy import load_positioning_from_strategy
        assert_controller_for_tier(TIER, None)
        self.log("craft", TIER.value, "started")
        # The Crafter consumes the Strategy Advisor's positioning lens from strategy.md.
        positioning = None
        if Path(self.config.strategy_path).exists():
            positioning = load_positioning_from_strategy(self.config.strategy_path)
        res = Crafter(self.store, self._load_bank(), self.config.output_dir,
                      llm=self._make_llm("crafter"), positioning=positioning).run()
        self.log("craft", TIER.value, f"crafted={res['crafted']} refused={res['refused']}")
        return res

    def _identity_from_bank(self) -> dict:
        """Candidate identity for filling applications comes from the accomplishment
        bank (the Crafter's domain) — NOT from config. Single source of truth for
        who you are; the Strategy Advisor never sees this."""
        try:
            bank = self._load_bank()
            return {"name": bank.name, "contact": dict(bank.contact)}
        except Exception:
            return {"name": "", "contact": {}}

    def run_apply_map(self, user: dict | None = None, *,
                      form_field_selector: str = "form_field"):
        """SAFE map phase over all drafted (screened_in + docs) jobs."""
        from .agents.applier import Applier, MAP_TIER
        from .models import ApplyStatus, ScreenStatus
        if user is None:
            user = self._identity_from_bank()
        applier = Applier(self.store, self.config.output_dir)
        self.log("apply-map", MAP_TIER.value, "started")
        controller = self.read_only_controller()  # map reads read-only
        assert_controller_for_tier(MAP_TIER, None)  # the mapping reasoning is SAFE
        mapped = []
        for p in self.store.get_by_status(
            screen_status=ScreenStatus.SCREENED_IN, apply_status=ApplyStatus.DRAFTED
        ):
            if not p.resume_path:
                continue
            app = applier.map_application(p, controller, user,
                                          form_field_selector=form_field_selector)
            mapped.append(app)
        controller.close()
        self.log("apply-map", MAP_TIER.value, f"mapped={len(mapped)}")
        return mapped

    def run_apply_submit(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> list[tuple[str, str]]:
        """GATED submit phase, ONE job at a time, each through the human gate."""
        from .agents.applier import Applier, SUBMIT_TIER
        from .models import ApplyStatus
        applier = Applier(self.store, self.config.output_dir)
        self.log("apply-submit", SUBMIT_TIER.value, "started")
        results: list[tuple[str, str]] = []
        awaiting = self.store.get_by_status(apply_status=ApplyStatus.AWAITING_APPROVAL)
        for p in awaiting:
            app = applier.load_application(p.dedup_key)
            if app is None:
                continue
            # Human gate — the only place an approval can be minted.
            approval = human_approval_gate(app, input_fn=input_fn, output_fn=output_fn)
            controller = self._submit_controller()  # submit-capable, built here only
            assert_controller_for_tier(SUBMIT_TIER, controller)
            status = applier.submit(app, approval, controller)
            controller.close()
            self.log("apply-submit", SUBMIT_TIER.value, f"{p.dedup_key}: {status}")
            results.append((p.dedup_key, status))
        return results

    def run_full(self, profile: dict, *, api_fetchers=None):
        """Full pipeline, halting AT the submit gate (spec §5)."""
        self.run_strategy(profile)
        self.run_crawl(api_fetchers=api_fetchers)
        self.run_screen()
        self.run_craft()
        self.run_apply_map()   # identity sourced from the bank, not config
        # Stops here: apply-submit is a separate, human-gated stage.
        return self.store.get_by_status(apply_status=ApplyStatus.AWAITING_APPROVAL)
