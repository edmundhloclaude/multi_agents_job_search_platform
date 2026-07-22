"""MCP-backed Crawler tests: real stdio client -> fake Indeed MCP server."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.crawler import Crawler
from jobsearch.browser.mock_controller import MockBrowserController
from jobsearch.mcp_sources.job_source import MCPJobSource, MCPToolNotAllowed
from jobsearch.models import StrategyCriteria
from jobsearch.store.job_store import JobStore

_SERVER = os.path.join(os.path.dirname(__file__), "fixtures", "fake_indeed_mcp_server.py")
_PY = sys.executable


def _mcp_source_cfg(tool="search_jobs"):
    return {
        "name": "indeed",
        "enabled": True,
        "type": "mcp",
        "rate_limit_per_min": 600000,
        "mcp": {
            "transport": "stdio",
            "command": _PY,
            "args": [_SERVER],
            "tool": tool,
            "query_arg": "query",
            "params": {"location": "Remote", "limit": 10},
            "result_path": "jobs",
            "field_map": {"company": "company", "title": "title", "location": "location",
                          "url": "url", "comp_text": "salary",
                          "application_method": "application_method"},
        },
    }


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


def test_mcp_source_fetches_from_real_server():
    src = MCPJobSource(_mcp_source_cfg()["mcp"])
    jobs = src.fetch("engineer")
    assert len(jobs) >= 1
    companies = {j["company"] for j in jobs}
    assert "Nimbus Systems" in companies


def test_crawler_ingests_mcp_source_and_dedups(store):
    crawler = Crawler(store, MockBrowserController(), sleep_fn=lambda s: None)
    crit = StrategyCriteria(target_roles=["engineer"])
    reports = crawler.run(crit, [_mcp_source_cfg()])
    assert reports[0].error == ""
    assert reports[0].new == 2
    assert {p.source for p in store.all()} == {"indeed"}
    assert any(p.application_method == "indeed_apply" for p in store.all())

    # re-crawl -> all seen, none new (dedup via upsert)
    reports2 = crawler.run(crit, [_mcp_source_cfg()])
    assert reports2[0].new == 0 and reports2[0].seen == 2
    assert len(store.all()) == 2


def test_crawler_never_touches_browser_for_mcp(store):
    ctrl = MockBrowserController()
    crawler = Crawler(store, ctrl, sleep_fn=lambda s: None)
    crawler.run(StrategyCriteria(target_roles=["engineer"]), [_mcp_source_cfg()])
    assert ctrl.actions == []  # no browser mutation
    assert ctrl.current_url() == ""  # browser never even navigated


def test_readonly_guard_refuses_mutating_tool(store):
    """Configuring the MCP source to call apply_to_job must be refused."""
    crawler = Crawler(store, MockBrowserController(), sleep_fn=lambda s: None)
    cfg = _mcp_source_cfg(tool="apply_to_job")
    reports = crawler.run(StrategyCriteria(target_roles=["engineer"]), [cfg])
    assert "mcp-tool-refused" in reports[0].error
    assert len(store.all()) == 0  # nothing ingested, nothing applied


def test_readonly_guard_direct():
    src = MCPJobSource(_mcp_source_cfg(tool="apply_to_job")["mcp"])
    with pytest.raises(MCPToolNotAllowed):
        src.fetch("engineer")
