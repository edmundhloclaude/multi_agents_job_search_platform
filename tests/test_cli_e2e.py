"""Step 6: end-to-end CLI test over the shipped mock config (offline)."""

import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch import cli
from jobsearch.models import ApplyStatus


@pytest.fixture
def cfg_path(tmp_path):
    """A config pointing at tmp paths but reusing the shipped fixtures/bank."""
    pkg = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "jobsearch"))
    raw = yaml.safe_load(open(os.path.join(pkg, "config.example.yaml")))
    raw["paths"]["db_path"] = str(tmp_path / "jobs.db")
    raw["paths"]["output_dir"] = str(tmp_path / "out")
    raw["paths"]["strategy_path"] = str(tmp_path / "strategy.md")
    raw["paths"]["accomplishment_bank"] = os.path.join(pkg, "accomplishment_bank.example.yaml")
    raw["browser"]["fixtures"] = os.path.join(pkg, "mock_fixtures.json")
    # Hermetic: force the offline mock browser source only, regardless of what
    # config.yaml ships with enabled (never hit a live API from the test suite).
    raw["browser"]["driver"] = "mock"
    raw["llm"] = {"provider": "none"}
    for s in raw["sources"]:
        s["enabled"] = (s.get("name") == "example_ats_feed")
        s["rate_limit_per_min"] = 600000  # don't wait on the real-time limiter
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return str(p)


def test_full_pipeline_then_gate(cfg_path, capsys):
    # run halts before the gate
    cli.main(["-c", cfg_path, "run"])
    out = capsys.readouterr().out
    assert "awaiting approval" in out

    # 2 screened_in and awaiting approval, 1 screened_out
    orch, _ = cli._orchestrator(type("A", (), {"config": cfg_path})())
    awaiting = orch.store.get_by_status(apply_status=ApplyStatus.AWAITING_APPROVAL)
    assert len(awaiting) == 2

    # Identity in filled applications comes from the accomplishment bank
    # (name/contact), NOT from any config `user` section.
    from jobsearch.agents.applier import Applier
    applier = Applier(orch.store, orch.config.output_dir)
    app = applier.load_application(awaiting[0].dedup_key)
    by = {f.name: f.value for f in app.fields}
    assert by.get("full_name") == "Jane Doe"           # from the example bank
    assert by.get("email") == "jane.doe@example.com"   # from the bank's contact
    orch.close()


def test_apply_submit_gate_approve_and_decline(cfg_path, capsys, monkeypatch):
    cli.main(["-c", cfg_path, "run"])
    capsys.readouterr()

    # First job approved (types SUBMIT), second declined (types "no").
    answers = iter(["SUBMIT", "no"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    cli.main(["-c", cfg_path, "apply-submit"])
    out = capsys.readouterr().out
    assert "SUBMITTED" in out
    assert "SKIPPED" in out

    orch, _ = cli._orchestrator(type("A", (), {"config": cfg_path})())
    counts = orch.store.status_counts()["apply"]
    assert counts.get("submitted") == 1
    assert counts.get("skipped") == 1
    orch.close()


def test_reset_requires_confirmation(cfg_path, capsys, monkeypatch):
    cli.main(["-c", cfg_path, "run"])           # populate
    capsys.readouterr()
    orch, _ = cli._orchestrator(type("A", (), {"config": cfg_path})())
    assert len(orch.store.all()) > 0
    orch.close()

    monkeypatch.setattr("builtins.input", lambda *a: "no")   # decline
    cli.main(["-c", cfg_path, "reset"])
    out = capsys.readouterr().out
    assert "Aborted" in out
    orch, _ = cli._orchestrator(type("A", (), {"config": cfg_path})())
    assert len(orch.store.all()) > 0            # nothing deleted
    orch.close()


def test_reset_yes_clears_store(cfg_path, capsys):
    cli.main(["-c", cfg_path, "run"])
    capsys.readouterr()
    cli.main(["-c", cfg_path, "reset", "--yes"])
    out = capsys.readouterr().out
    assert "Cleared" in out
    orch, _ = cli._orchestrator(type("A", (), {"config": cfg_path})())
    assert orch.store.all() == []
    orch.close()


def test_reset_all_clears_docs_and_strategy(cfg_path, capsys):
    cli.main(["-c", cfg_path, "run"])           # crafts docs + writes strategy.md
    capsys.readouterr()
    orch, _ = cli._orchestrator(type("A", (), {"config": cfg_path})())
    out_dir, strat = orch.config.output_dir, orch.config.strategy_path
    orch.close()
    assert os.path.exists(strat)
    cli.main(["-c", cfg_path, "reset", "--yes", "--all"])
    assert not os.path.exists(strat)
    assert not any(os.scandir(out_dir))         # output dir emptied


def test_status_runs(cfg_path, capsys):
    cli.main(["-c", cfg_path, "crawl"])
    cli.main(["-c", cfg_path, "status", "-v"])
    out = capsys.readouterr().out
    assert "Job store:" in out
    assert "screen_status" in out
