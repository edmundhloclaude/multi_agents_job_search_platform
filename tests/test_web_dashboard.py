"""Tests for the read-only web dashboard."""

import json
import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.models import ApplyStatus, Posting, ScreenStatus
from jobsearch.store.job_store import JobStore
from jobsearch.web import build_state, create_server


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.db"
    s = JobStore(path)
    p = Posting(company="Acme", title="Staff Engineer", location="Remote", source="theirstack")
    s.upsert_posting(p)
    s.annotate_screen(p.dedup_key, status=ScreenStatus.SCREENED_IN, score=88, rationale="good")
    s.set_apply_status(p.dedup_key, ApplyStatus.AWAITING_APPROVAL)
    s.log_run("crawl", "READ_BROWSER", "theirstack: new=1")
    s.close()
    return str(path)


def test_build_state_shape(db):
    s = JobStore(db)
    st = build_state(s)
    s.close()
    assert st["totals"]["postings"] == 1
    assert st["counts"]["screen"].get("screened_in") == 1
    assert st["jobs"][0]["company"] == "Acme"
    assert st["jobs"][0]["screen_score"] == 88
    assert any(r["stage"] == "crawl" for r in st["runs"])


def test_server_serves_html_and_json(db):
    srv = create_server(db, host="127.0.0.1", port=0)  # ephemeral port
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "Agent Activity" in html
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state["totals"]["postings"] == 1
        assert state["jobs"][0]["title"] == "Staff Engineer"
    finally:
        srv.shutdown()
        srv.server_close()


def test_dashboard_links_to_strategy(db):
    srv = create_server(db, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert 'href="/strategy"' in html          # link to the advisor
    finally:
        srv.shutdown(); srv.server_close()


def test_strategy_mounted_under_dashboard(db):
    """The advisor is reachable at /strategy on the same server as the dashboard."""
    from jobsearch.agents.strategy_session import StrategySession
    from jobsearch.llm.base import LLM

    class _LLM(LLM):
        def complete_text(self, *a, **k): return ""
        def complete_json(self, *a, **k): return {"reply": "hi", "criteria": {"target_roles": ["SWE"]}}

    srv = create_server(db, host="127.0.0.1", port=0)
    srv._session = StrategySession(_LLM(), profile={}, bank=None)  # inject (no OpenAI)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(base + "/strategy").read().decode()
        assert "Strategy Advisor" in html
        # a strategy API call routed through the /strategy mount
        req = urllib.request.Request(base + "/strategy/api/message", method="POST",
                                     data=json.dumps({"text": "hi"}).encode(),
                                     headers={"Content-Type": "application/json"})
        st = json.loads(urllib.request.urlopen(req).read())
        assert st["criteria"]["target_roles"] == ["SWE"]
    finally:
        srv.shutdown(); srv.server_close()


def test_strategy_unavailable_without_config(db):
    """No config -> /strategy shows an 'unavailable' page, dashboard still works."""
    srv = create_server(db, host="127.0.0.1", port=0)   # config=None
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/strategy").read().decode()
        assert "unavailable" in html.lower()
    finally:
        srv.shutdown(); srv.server_close()


def test_dashboard_is_read_only(db):
    srv = create_server(db, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", method="POST",
                                     data=b"{}")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 405  # POST rejected — no way to trigger/submit
    finally:
        srv.shutdown()
        srv.server_close()
