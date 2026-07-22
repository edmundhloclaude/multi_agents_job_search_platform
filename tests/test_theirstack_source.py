"""Offline tests for the TheirStack API source (mocked HTTP, no network/key)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.crawler import Crawler
from jobsearch.browser.mock_controller import MockBrowserController
from jobsearch.models import StrategyCriteria
from jobsearch.sources import theirstack as ts
from jobsearch.store.job_store import JobStore

_FAKE_RESPONSE = {
    "data": [
        {
            "job_title": "Staff Software Engineer",
            "company": "Nimbus Systems",
            "location": "San Francisco, CA",
            "url": "https://theirstack.test/job/1",
            "final_url": "https://nimbus.example/careers/1",
            "min_annual_salary_usd": 200000,
            "max_annual_salary_usd": 240000,
            "remote": True,
            "date_posted": "2026-07-15",
            "company_object": {"name": "Nimbus Systems", "domain": "nimbus.example"},
        },
        {
            "job_title": "Senior Backend Engineer",
            "company": "Vertex Labs",
            "location": "Remote",
            "url": "https://theirstack.test/job/2",
            "remote": True,
        },
    ],
    "metadata": {"total_results": 2},
}


def test_map_job_fields():
    m = ts._map_job(_FAKE_RESPONSE["data"][0])
    assert m["title"] == "Staff Software Engineer"
    assert m["company"] == "Nimbus Systems"
    assert "Remote" in m["location"]           # remote flag appended
    assert m["url"] == "https://nimbus.example/careers/1"  # prefers final_url
    assert m["comp_text"] == "$200,000 - $240,000 USD"


def test_search_jobs_paginates_and_maps():
    calls = []
    def fake_http(url, token, payload, **kw):
        calls.append(payload)
        return _FAKE_RESPONSE if payload["page"] == 0 else {"data": []}
    jobs = ts.search_jobs("engineer", token="k", params={"limit": 10},
                          max_pages=3, http=fake_http)
    assert len(jobs) == 2
    assert calls[0]["job_title_or"] == ["engineer"]
    assert calls[0]["posted_at_max_age_days"] == 30
    # stopped after page 0 because fewer than limit returned
    assert len(calls) == 1


def test_fetcher_requires_env_key(monkeypatch):
    monkeypatch.delenv("THEIRSTACK_API_KEY", raising=False)
    with pytest.raises(ts.TheirStackError):
        ts.theirstack_fetcher({"name": "theirstack"}, "engineer")


def test_crawler_ingests_theirstack(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_http_post_json", lambda *a, **k: _FAKE_RESPONSE)
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    store = JobStore(tmp_path / "j.db")
    crawler = Crawler(store, MockBrowserController(), sleep_fn=lambda s: None,
                      api_fetchers={"theirstack": ts.theirstack_fetcher})
    src = {"name": "theirstack", "enabled": True, "type": "api",
           "params": {"limit": 10}, "max_pages": 1}
    reports = crawler.run(StrategyCriteria(target_roles=["engineer"]), [src])
    assert reports[0].new == 2
    assert {p.company for p in store.all()} == {"Nimbus Systems", "Vertex Labs"}
    store.close()
