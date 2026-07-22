"""Command-line interface (spec §6).

    jobsearch strategy      # (re)generate strategy.md
    jobsearch crawl         # extract postings into store
    jobsearch screen        # score unscreened jobs
    jobsearch craft         # generate docs for screened_in jobs
    jobsearch apply-map     # prepare filled applications (no submit)
    jobsearch apply-submit  # review + approve + submit, one job at a time
    jobsearch run           # full pipeline, halting at the submit gate
    jobsearch status        # summary table of the store by status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from .orchestrator import Config, Orchestrator

# Project root (…/multi_agents_job_search_platform), parent of the jobsearch pkg.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# .env loading (dependency-free). Keeps API keys with the project, out of
# config.yaml. Existing environment variables always win (never overridden).
# --------------------------------------------------------------------------- #
def load_dotenv(*candidates: Path) -> None:
    seen = set()
    search = list(candidates) or [_PROJECT_ROOT / ".env", Path.cwd() / ".env"]
    for path in search:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:   # do not override real env vars
                os.environ[key] = val


# --------------------------------------------------------------------------- #
# Config loading + controller factory
# --------------------------------------------------------------------------- #
def _resolve(base: Path, p: str) -> str:
    q = Path(p)
    return str(q if q.is_absolute() else (base / q).resolve())


def load_config(config_path: str) -> tuple[Config, dict]:
    cfg_file = Path(config_path).resolve()
    base = cfg_file.parent
    raw = yaml.safe_load(cfg_file.read_text("utf-8")) or {}
    paths = raw.get("paths", {})
    cfg = Config(
        db_path=_resolve(base, paths.get("db_path", "jobs.db")),
        output_dir=_resolve(base, paths.get("output_dir", "output")),
        strategy_path=_resolve(base, paths.get("strategy_path", "strategy.md")),
        accomplishment_bank_path=_resolve(base, paths.get("accomplishment_bank",
                                                          "accomplishment_bank.yaml")),
        sources=raw.get("sources", []),
        model=raw.get("model", "claude-opus-4-8"),
        raw=raw,
    )
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    return cfg, raw


def make_controller_factory(raw: dict, base: Path):
    """Return a controller_factory(submit_enabled)->BrowserController per config."""
    b = raw.get("browser", {})
    driver = b.get("driver", "cua")
    if driver == "mock":
        from .browser.mock_controller import MockBrowserController, MockPage
        fixtures_path = _resolve(base, b.get("fixtures", "mock_fixtures.json"))
        data = json.loads(Path(fixtures_path).read_text("utf-8"))
        page_specs = data.get("pages", [])

        def factory(submit_enabled: bool):
            pages = {}
            for spec in page_specs:
                pages[spec["url"]] = MockPage(
                    url=spec["url"],
                    text=spec.get("text", ""),
                    dom=spec.get("dom", ""),
                    links=spec.get("links", []),
                    elements=spec.get("elements", {}),
                    requires_login=spec.get("requires_login", False),
                    requires_captcha=spec.get("requires_captcha", False),
                )
            return MockBrowserController(pages, submit_enabled=submit_enabled)
        return factory

    if driver == "playwright":
        # DOM-native real Chromium. A submit-capable instance is only ever built
        # by the orchestrator inside the human-gated submit flow.
        from .browser.playwright_controller import PlaywrightBrowserController
        headless = bool(b.get("headless", True))

        def factory(submit_enabled: bool):
            return PlaywrightBrowserController(submit_enabled=submit_enabled,
                                               headless=headless)
        return factory

    return None  # -> Orchestrator default (CUA)


def _orchestrator(args) -> tuple[Orchestrator, dict]:
    cfg, raw = load_config(args.config)
    base = Path(args.config).resolve().parent
    factory = make_controller_factory(raw, base)
    orch = Orchestrator(cfg, controller_factory=factory)
    return orch, raw


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_strategy(args):
    orch, raw = _orchestrator(args)
    crit = orch.run_strategy(raw.get("profile", {}))
    print(f"Wrote strategy → {orch.config.strategy_path}")
    print(f"Target roles: {', '.join(crit.target_roles) or '(none)'}")
    orch.close()


def cmd_crawl(args):
    orch, raw = _orchestrator(args)
    reports = orch.run_crawl()
    for r in reports:
        note = "HANDOFF-TO-HUMAN" if r.handoff else (r.error or "ok")
        print(f"[{r.source}] new={r.new} seen={r.seen} pages={r.pages} — {note}")
    orch.close()


def cmd_screen(args):
    orch, raw = _orchestrator(args)
    n = orch.run_screen(rescreen=args.rescreen)
    print(f"Screened {n} job(s).")
    orch.close()


def cmd_craft(args):
    orch, raw = _orchestrator(args)
    res = orch.run_craft()
    print(f"Crafted {res['crafted']} document set(s); refused {res['refused']} (fabrication guard).")
    orch.close()


def cmd_apply_map(args):
    orch, raw = _orchestrator(args)
    mapped = orch.run_apply_map()   # identity comes from the accomplishment bank
    print(f"Mapped {len(mapped)} application(s) → apply_status=awaiting_approval.")
    for app in mapped:
        print(f"  • {app.company} — {app.title} ({len(app.fields)} fields, "
              f"{len(app.manual_required)} need human action)")
    orch.close()


def cmd_apply_submit(args):
    orch, raw = _orchestrator(args)
    print("=== SUBMIT GATE === Each application requires typed human approval.\n")
    results = orch.run_apply_submit(input_fn=input, output_fn=print)
    if not results:
        print("No applications awaiting approval.")
    for key, status in results:
        print(f"  {status.upper():10s} {key}")
    orch.close()


def cmd_run(args):
    orch, raw = _orchestrator(args)
    print("Running full pipeline (halts BEFORE the submit gate)...")
    awaiting = orch.run_full(raw.get("profile", {}))
    print(f"\nPipeline complete. {len(awaiting)} application(s) awaiting approval.")
    print("Review and submit with:  jobsearch apply-submit")
    orch.close()


def cmd_serve(args):
    cfg, _ = load_config(args.config)
    from .web import serve
    serve(cfg.db_path, host=args.host, port=args.port, config=cfg)  # mounts /strategy


def cmd_strategy_web(args):
    cfg, _ = load_config(args.config)
    from .strategy_web import serve
    serve(cfg, host=args.host, port=args.port)


def cmd_gaps(args):
    orch, _ = _orchestrator(args)
    from .agents.strategy import gap_report
    gaps = gap_report(orch._load_criteria(), orch._load_bank())
    print("\nSkill gaps — what your strategy targets vs. your accomplishment bank:\n")
    if gaps["missing"]:
        print("  MISSING (no evidence in bank):")
        for g in gaps["missing"]:
            print(f"    - {g}")
    if gaps["weak"]:
        print("  WEAK (thin evidence — one story or listed-only):")
        for g in gaps["weak"]:
            print(f"    - {g}")
    if not gaps["missing"] and not gaps["weak"]:
        print("  none — your bank backs every targeted skill.")
    print()
    orch.close()


def cmd_status(args):
    orch, raw = _orchestrator(args)
    counts = orch.store.status_counts()
    total = len(orch.store.all())
    print(f"\nJob store: {total} posting(s)\n")

    def block(title, d):
        print(f"  {title}:")
        if not d:
            print("    (none)")
        for k, v in sorted(d.items()):
            print(f"    {k:18s} {v}")
        print()

    block("screen_status", counts["screen"])
    block("apply_status", counts["apply"])
    block("response_status", counts["response"])

    if args.verbose:
        print("  postings:")
        for p in orch.store.all():
            print(f"    [{p.screen_status}/{p.apply_status}] {p.company} — {p.title} "
                  f"(score={p.screen_score}) <{p.source}>")
        print()
        print("  recent runs:")
        for r in orch.store.recent_runs(10):
            print(f"    {r['ts']} {r['stage']:12s} {r['message']}")
    orch.close()


# --------------------------------------------------------------------------- #
def _default_config_path() -> str:
    """Prefer a personal config.yaml; fall back to the shipped example so a
    fresh clone runs with zero setup."""
    personal = Path(__file__).with_name("config.yaml")
    example = Path(__file__).with_name("config.example.yaml")
    return str(personal if personal.exists() else example)


def build_parser() -> argparse.ArgumentParser:
    default_cfg = _default_config_path()
    p = argparse.ArgumentParser(prog="jobsearch", description="Multi-agent job search platform.")
    p.add_argument("-c", "--config", default=default_cfg, help="path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("strategy", help="(re)generate strategy.md").set_defaults(func=cmd_strategy)
    sub.add_parser("crawl", help="extract postings into store").set_defaults(func=cmd_crawl)
    sp = sub.add_parser("screen", help="score unscreened jobs")
    sp.add_argument("--rescreen", action="store_true", help="re-score all jobs (idempotent)")
    sp.set_defaults(func=cmd_screen)
    sub.add_parser("craft", help="generate docs for screened_in jobs").set_defaults(func=cmd_craft)
    sub.add_parser("apply-map", help="prepare filled applications (no submit)").set_defaults(func=cmd_apply_map)
    sub.add_parser("apply-submit", help="review + approve + submit, one at a time").set_defaults(func=cmd_apply_submit)
    sub.add_parser("run", help="full pipeline, halting at the submit gate").set_defaults(func=cmd_run)
    sv = sub.add_parser("serve", help="read-only web dashboard of agent activity")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(func=cmd_serve)
    sub.add_parser("gaps", help="skills your strategy targets that the bank can't back").set_defaults(func=cmd_gaps)
    sw = sub.add_parser("strategy-web", help="interactive strategy advisor (chat + docs) web UI")
    sw.add_argument("--host", default="127.0.0.1")
    sw.add_argument("--port", type=int, default=8766)
    sw.set_defaults(func=cmd_strategy_web)
    st = sub.add_parser("status", help="summary table of the store by status")
    st.add_argument("-v", "--verbose", action="store_true", help="list postings + recent runs")
    st.set_defaults(func=cmd_status)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Load project-local .env (and one next to the chosen config) before running,
    # so API keys live with the project rather than in config.yaml.
    cfg_dir = Path(args.config).resolve().parent
    load_dotenv(_PROJECT_ROOT / ".env", cfg_dir / ".env", Path.cwd() / ".env")
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
