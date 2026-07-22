"""Crawler / Extractor (READ_BROWSER) — spec §4.2.

For each enabled source: search, paginate, and extract postings into the shared
schema, computing dedup_key and calling upsert_posting for every posting (so
already-seen postings are skipped automatically). It makes NO judgment about fit
(that is the Screener's job) and NEVER clicks apply/submit.

ToS posture (spec §0.6):
* per-source ``enabled`` flag and per-source rate limiting, conservative default
* the source of every posting is logged / stored
* official API / feed sources are preferred; browser scraping is the fallback
* on a login wall or CAPTCHA the unit pauses and hands off to the human

The unit only ever drives a read-only controller. It also defensively refuses a
submit-capable controller, so it cannot mutate a page even if mis-wired.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..browser.controller import BrowserController, HumanHandoffRequired
from ..models import Posting, Tier
from ..store.job_store import JobStore

TIER = Tier.READ_BROWSER

# Conservative default when a source omits its own limit (spec §0.6).
DEFAULT_RATE_LIMIT_PER_MIN = 6
DEFAULT_MAX_PAGES = 3


@dataclass
class CrawlReport:
    source: str
    new: int = 0
    seen: int = 0
    pages: int = 0
    handoff: bool = False
    error: str = ""


class _RateLimiter:
    """Simple per-source throttle. sleep_fn/clock injectable for tests."""

    def __init__(self, per_min: int, *, sleep_fn=time.sleep, clock=time.monotonic):
        per_min = max(1, int(per_min or DEFAULT_RATE_LIMIT_PER_MIN))
        self.min_interval = 60.0 / per_min
        self._sleep = sleep_fn
        self._clock = clock
        self._last: Optional[float] = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            elapsed = now - self._last
            if elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
        self._last = self._clock()


def _postings_from_query(controller: BrowserController, selector: str, source_name: str
                         ) -> list[Posting]:
    """Parse posting summaries the page exposes as JSON under ``selector``.

    This is the mock-able seam. A live CUA adapter would parse ``page_dom`` /
    screen reads here instead; the rest of the crawler is unchanged.
    """
    out: list[Posting] = []
    for blob in controller.query(selector):
        try:
            d = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        out.append(Posting(
            company=d.get("company", ""),
            title=d.get("title", ""),
            location=d.get("location", ""),
            source=source_name,
            source_url=d.get("url", ""),
            comp_text=d.get("comp_text", ""),
            requirements=d.get("requirements", []),
            application_method=d.get("application_method", ""),
            raw=d,
        ))
    return out


class Crawler:
    def __init__(
        self,
        store: JobStore,
        controller: BrowserController,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        api_fetchers: Optional[dict[str, Callable[[dict, str], list[dict]]]] = None,
        mcp_factory: Optional[Callable[[dict], Any]] = None,
    ):
        # Defensive tier check: the Crawler must never hold a submit-capable ctrl.
        if getattr(controller, "submit_enabled", False):
            raise PermissionError(
                "Crawler is READ_BROWSER; it must not receive a submit-capable controller."
            )
        self.store = store
        self.controller = controller
        self._sleep = sleep_fn
        self._clock = clock
        # Optional injected API/feed fetchers keyed by source name; preferred
        # over browser scraping when present.
        self.api_fetchers = api_fetchers or {}
        # Optional factory(mcp_cfg)->MCPJobSource, injectable for tests.
        self._mcp_factory = mcp_factory

    # ------------------------------------------------------------------ #
    def _queries(self, criteria) -> list[str]:
        qs = list(getattr(criteria, "target_roles", []) or [])
        return qs or [""]

    def _upsert_all(self, postings: list[Posting], report: CrawlReport) -> None:
        for p in postings:
            if not (p.company and p.title):
                continue
            is_new, _ = self.store.upsert_posting(p)
            if is_new:
                report.new += 1
            else:
                report.seen += 1  # already seen -> skip deep extraction

    def crawl_source(self, source: dict[str, Any], criteria) -> CrawlReport:
        name = source.get("name", "unknown")
        report = CrawlReport(source=name)
        if not source.get("enabled", False):
            report.error = "disabled"
            return report

        # MCP-backed source: the crawler acts as an MCP client (spec §0.6,
        # generalized to MCP). No browser, read-only tool only.
        if source.get("type") == "mcp":
            return self._crawl_mcp(source, criteria, report)

        # Prefer official API / feed when this source provides one (spec §0.6).
        fetcher = self.api_fetchers.get(name)
        if source.get("type") == "api" and fetcher is not None:
            try:
                for q in self._queries(criteria):
                    for d in fetcher(source, q):
                        d.setdefault("source", name)
                        self._upsert_all([_posting_from_dict(d, name)], report)
                    report.pages += 1
            except HumanHandoffRequired:
                report.handoff = True
            return report

        # Browser fallback.
        return self._crawl_browser(source, criteria, report)

    def _crawl_mcp(self, source, criteria, report: CrawlReport) -> CrawlReport:
        """Fetch postings from an MCP server via a read-only search tool."""
        from ..mcp_sources.job_source import MCPJobSource, MCPToolNotAllowed
        name = report.source
        mcp_cfg = source.get("mcp", {})
        # Allow the query to be injected into per-source static params too.
        source_obj = self._mcp_factory(mcp_cfg) if self._mcp_factory else MCPJobSource(mcp_cfg)
        limiter = _RateLimiter(
            source.get("rate_limit_per_min", DEFAULT_RATE_LIMIT_PER_MIN),
            sleep_fn=self._sleep, clock=self._clock,
        )
        try:
            for query in self._queries(criteria):
                limiter.wait()
                jobs = source_obj.fetch(query)
                report.pages += 1
                self._upsert_all([_posting_from_dict(d, name) for d in jobs], report)
        except MCPToolNotAllowed as e:
            report.error = f"mcp-tool-refused: {e}"
        except Exception as e:
            report.error = f"mcp-error: {e}"
        return report

    def _crawl_browser(self, source, criteria, report: CrawlReport) -> CrawlReport:
        name = report.source
        limiter = _RateLimiter(
            source.get("rate_limit_per_min", DEFAULT_RATE_LIMIT_PER_MIN),
            sleep_fn=self._sleep, clock=self._clock,
        )
        results_selector = source.get("results_selector", "job")
        next_selector = source.get("next_selector", "next")
        max_pages = int(source.get("max_pages", DEFAULT_MAX_PAGES))
        search_tmpl = source.get("search_url", "{query}")

        for query in self._queries(criteria):
            url = search_tmpl.format(query=query)
            for _page in range(max_pages):
                limiter.wait()
                view = self.controller.open(url)
                report.pages += 1
                # Login wall / CAPTCHA -> pause and hand off (spec §0.5).
                if getattr(view, "requires_login", False) or \
                   getattr(view, "requires_captcha", False) or \
                   self.controller.needs_human():
                    report.handoff = True
                    return report
                self._upsert_all(
                    _postings_from_query(self.controller, results_selector, name), report
                )
                nxt = self.controller.query(next_selector)
                if not nxt:
                    break
                url = nxt[0]
        return report

    def run(self, criteria, sources: list[dict]) -> list[CrawlReport]:
        reports: list[CrawlReport] = []
        for source in sources:
            name = source.get("name", "unknown")
            try:
                rep = self.crawl_source(source, criteria)
            except HumanHandoffRequired:
                rep = CrawlReport(source=name, handoff=True)
            except Exception as e:  # a bad source must not kill the whole crawl
                rep = CrawlReport(source=name, error=str(e))
            self.store.log_run(
                "crawl", TIER.value,
                f"{name}: new={rep.new} seen={rep.seen} pages={rep.pages} "
                f"handoff={rep.handoff} error={rep.error}",
            )
            reports.append(rep)
        return reports


def _posting_from_dict(d: dict, name: str) -> Posting:
    return Posting(
        company=d.get("company", ""),
        title=d.get("title", ""),
        location=d.get("location", ""),
        source=name,
        source_url=d.get("url", d.get("source_url", "")),
        comp_text=d.get("comp_text", ""),
        requirements=d.get("requirements", []),
        application_method=d.get("application_method", ""),
        # Prefer the original source record (fetchers carry it under "raw") so
        # downstream agents (e.g. the Screener) see the full posting, not the
        # already-mapped fields.
        raw=d.get("raw", d),
    )
