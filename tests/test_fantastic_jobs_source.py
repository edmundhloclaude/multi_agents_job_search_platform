"""Offline tests for the Fantastic.jobs source (mocked HTTP, no network/key)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.crawler import Crawler
from jobsearch.browser.mock_controller import MockBrowserController
from jobsearch.models import StrategyCriteria
from jobsearch.sources import fantastic_jobs as fj
from jobsearch.store.job_store import JobStore

# Mirrors the real Active Jobs DB schema (ai_* enrichments, location_type, etc.)
_SAMPLE = [
    {
        "title": "Staff Software Engineer",
        "organization": "Nimbus Systems",
        "url": "https://nimbus.example/careers/1",
        "locations_derived": ["San Francisco, United States"],
        "cities_derived": ["San Francisco"], "countries_derived": ["United States"],
        "location_type": "Remote",
        "ai_work_arrangement": "Remote",
        "ai_salary_min_value": 200000, "ai_salary_max_value": 240000,
        "ai_salary_currency": "USD", "ai_salary_unit_text": "YEAR",
        "ai_key_skills": ["Python", "Distributed Systems", "Go"],
        "employment_type": ["Full time"],
        "date_posted": "2026-07-20T00:00:00",
        "description_text": "Build distributed systems in Python and Go.",
    },
    {
        "title": "Backend Engineer",
        "organization": "Vertex Labs",
        "url": "https://vertex.example/jobs/2",
        "locations_derived": ["Remote"],
        "ai_key_skills": ["Python"],
        "description_text": "Own the platform.",
    },
]


def test_map_job_fields():
    m = fj._map_job(_SAMPLE[0])
    assert m["company"] == "Nimbus Systems"
    assert m["title"] == "Staff Software Engineer"
    assert "San Francisco" in m["location"] and "Remote" in m["location"]
    assert m["url"] == "https://nimbus.example/careers/1"
    assert "200,000-240,000" in m["comp_text"] and "USD" in m["comp_text"]
    # ai_key_skills -> requirements (screening signal)
    assert "python" in m["requirements"] and "distributed systems" in m["requirements"]
    # description exposed under raw["description"] for the Screener/dashboard
    assert m["raw"]["description"] == "Build distributed systems in Python and Go."


def test_search_jobs_bearer_headers_and_params():
    seen = {}
    def fake(url, headers, params, **kw):
        seen["url"], seen["headers"], seen["params"] = url, headers, params
        return _SAMPLE
    jobs = fj.search_jobs("staff engineer", token="k", params={"limit": 10, "time_frame": "7d"},
                          max_pages=1, http=fake)
    assert len(jobs) == 2
    assert seen["url"] == fj._DIRECT_URL
    assert seen["headers"]["Authorization"] == "Bearer k"
    assert seen["params"]["title"] == '"staff engineer"'
    assert seen["params"]["time_frame"] == "7d"
    assert seen["params"]["offset"] == 0


def test_rapidapi_mode_uses_rapidapi_headers():
    seen = {}
    def fake(url, headers, params, **kw):
        seen.update(url=url, headers=headers); return []
    fj.search_jobs("eng", token="rk", params={"auth": "rapidapi", "limit": 5}, http=fake)
    assert seen["url"] == fj._RAPIDAPI_URL
    assert seen["headers"]["X-RapidAPI-Key"] == "rk"
    assert seen["headers"]["X-RapidAPI-Host"] == fj._RAPIDAPI_HOST


def test_fetcher_requires_env_key(monkeypatch):
    monkeypatch.delenv("FANTASTIC_JOBS_API_KEY", raising=False)
    with pytest.raises(fj.FantasticJobsError):
        fj.fantastic_jobs_fetcher({"name": "fantastic_jobs"}, "engineer")


def test_crawler_ingests_fantastic(tmp_path, monkeypatch):
    monkeypatch.setattr(fj, "_http_get_json", lambda *a, **k: _SAMPLE)
    monkeypatch.setenv("FANTASTIC_JOBS_API_KEY", "test-key")
    store = JobStore(tmp_path / "j.db")
    crawler = Crawler(store, MockBrowserController(), sleep_fn=lambda s: None,
                      api_fetchers={"fantastic_jobs": fj.fantastic_jobs_fetcher})
    src = {"name": "fantastic_jobs", "enabled": True, "type": "api",
           "params": {"limit": 10}, "max_pages": 1}
    reports = crawler.run(StrategyCriteria(target_roles=["engineer"]), [src])
    assert reports[0].new == 2
    assert {p.company for p in store.all()} == {"Nimbus Systems", "Vertex Labs"}
    store.close()
