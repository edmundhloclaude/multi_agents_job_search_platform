"""Step 4 tests: Crawler (READ_BROWSER) over the mock controller."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.crawler import Crawler
from jobsearch.browser.mock_controller import MockBrowserController, MockPage
from jobsearch.models import StrategyCriteria
from jobsearch.store.job_store import JobStore


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


@pytest.fixture
def criteria():
    return StrategyCriteria(target_roles=["engineer"])


def _job(company, title, loc="Remote", url="http://x", method="external_ats"):
    return json.dumps({
        "company": company, "title": title, "location": loc, "url": url,
        "application_method": method, "requirements": ["python"],
    })


def test_crawler_refuses_submit_capable_controller(store):
    with pytest.raises(PermissionError):
        Crawler(store, MockBrowserController(submit_enabled=True))


def test_crawl_extracts_and_dedups(store, criteria):
    page = MockPage(
        url="search/engineer",
        elements={"job": [_job("Acme", "Staff Engineer"), _job("Globex", "Engineer")]},
    )
    ctrl = MockBrowserController({"search/engineer": page})
    crawler = Crawler(store, ctrl, sleep_fn=lambda s: None)
    src = {"name": "mocksrc", "enabled": True, "search_url": "search/{query}",
           "results_selector": "job", "max_pages": 1}
    reports = crawler.run(criteria, [src])
    assert reports[0].new == 2
    assert len(store.all()) == 2
    # every posting has its source logged
    assert all(p.source == "mocksrc" for p in store.all())

    # re-crawl: all seen, none new
    reports2 = crawler.run(criteria, [src])
    assert reports2[0].new == 0
    assert reports2[0].seen == 2
    assert len(store.all()) == 2


def test_crawl_never_mutates_page(store, criteria):
    page = MockPage(url="search/engineer", elements={"job": [_job("Acme", "Engineer")]})
    ctrl = MockBrowserController({"search/engineer": page})
    crawler = Crawler(store, ctrl, sleep_fn=lambda s: None)
    src = {"name": "s", "enabled": True, "search_url": "search/{query}", "max_pages": 1}
    crawler.run(criteria, [src])
    assert ctrl.actions == []  # no click/type/submit ever recorded


def test_disabled_source_skipped(store, criteria):
    ctrl = MockBrowserController({})
    crawler = Crawler(store, ctrl, sleep_fn=lambda s: None)
    src = {"name": "off", "enabled": False}
    reports = crawler.run(criteria, [src])
    assert reports[0].error == "disabled"
    assert len(store.all()) == 0


def test_login_wall_triggers_handoff(store, criteria):
    page = MockPage(url="search/engineer", requires_login=True,
                    elements={"job": [_job("Acme", "Engineer")]})
    ctrl = MockBrowserController({"search/engineer": page})
    crawler = Crawler(store, ctrl, sleep_fn=lambda s: None)
    src = {"name": "walled", "enabled": True, "search_url": "search/{query}", "max_pages": 1}
    reports = crawler.run(criteria, [src])
    assert reports[0].handoff is True
    # nothing extracted past the wall
    assert len(store.all()) == 0


def test_captcha_triggers_handoff(store, criteria):
    page = MockPage(url="search/engineer", requires_captcha=True)
    ctrl = MockBrowserController({"search/engineer": page})
    crawler = Crawler(store, ctrl, sleep_fn=lambda s: None)
    src = {"name": "cap", "enabled": True, "search_url": "search/{query}", "max_pages": 1}
    reports = crawler.run(criteria, [src])
    assert reports[0].handoff is True


def test_pagination_follows_next(store, criteria):
    p1 = MockPage(url="search/engineer",
                  elements={"job": [_job("Acme", "Engineer")], "next": ["page2"]})
    p2 = MockPage(url="page2",
                  elements={"job": [_job("Globex", "Engineer")]})  # no next -> stop
    ctrl = MockBrowserController({"search/engineer": p1, "page2": p2})
    crawler = Crawler(store, ctrl, sleep_fn=lambda s: None)
    src = {"name": "s", "enabled": True, "search_url": "search/{query}",
           "results_selector": "job", "next_selector": "next", "max_pages": 5}
    reports = crawler.run(criteria, [src])
    assert reports[0].new == 2
    assert reports[0].pages == 2


def test_api_source_preferred_over_browser(store, criteria):
    def fake_api(source, query):
        return [{"company": "ApiCo", "title": "Engineer", "location": "Remote",
                 "url": "http://api", "requirements": ["python"]}]
    ctrl = MockBrowserController({})
    crawler = Crawler(store, ctrl, sleep_fn=lambda s: None,
                      api_fetchers={"apisrc": fake_api})
    src = {"name": "apisrc", "enabled": True, "type": "api"}
    reports = crawler.run(criteria, [src])
    assert reports[0].new == 1
    assert store.all()[0].company == "ApiCo"


def test_rate_limiter_waits_between_requests(store, criteria):
    slept = []
    t = {"now": 0.0}
    def clock():
        return t["now"]
    def sleep_fn(s):
        slept.append(s)
        t["now"] += s
    p1 = MockPage(url="search/engineer",
                  elements={"job": [_job("Acme", "Engineer")], "next": ["page2"]})
    p2 = MockPage(url="page2", elements={"job": [_job("Globex", "Engineer")]})
    ctrl = MockBrowserController({"search/engineer": p1, "page2": p2})
    crawler = Crawler(store, ctrl, sleep_fn=sleep_fn, clock=clock)
    src = {"name": "s", "enabled": True, "search_url": "search/{query}",
           "next_selector": "next", "rate_limit_per_min": 60, "max_pages": 5}
    crawler.run(criteria, [src])
    # 60/min -> 1s min interval; second page should have waited ~1s
    assert any(abs(s - 1.0) < 1e-6 for s in slept)
