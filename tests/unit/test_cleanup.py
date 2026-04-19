"""Unit tests for the agent-cleanup hardening (4 fixes).

Coverage:
  Fix 1: CAMC_BIN is resolved at import (uses shutil.which).
  Fix 3: cleanup_all_camflow_agents() removes only camflow-* names.
  Fix 4: kill_existing_camflow_agents(except_id=...) preserves the orphan.
  Engine: _cleanup_on_exit removes current_agent_id and sweeps.

We mock subprocess so the tests don't touch the real camc registry.
"""

import json
from unittest.mock import MagicMock

import pytest

from camflow.backend.cam import agent_runner
from camflow.backend.cam.agent_runner import (
    CAMC_BIN,
    _list_camflow_agent_ids,
    cleanup_all_camflow_agents,
    kill_existing_camflow_agents,
)


# ---- Fix 1: PATH resolution -------------------------------------------


def test_camc_bin_is_resolved_at_import():
    """CAMC_BIN must be either an absolute path (via shutil.which) or
    the literal 'camc' fallback. Never empty / None."""
    assert isinstance(CAMC_BIN, str)
    assert CAMC_BIN  # truthy


# ---- Fix 3: cleanup_all_camflow_agents -------------------------------


def _fake_list_response(*camflow_ids, other_ids=None):
    """Return a fake `camc --json list` payload as bytes-like text."""
    other_ids = other_ids or []
    payload = []
    for aid in camflow_ids:
        payload.append({"id": aid, "task": {"name": f"camflow-{aid}"}})
    for aid in other_ids:
        payload.append({"id": aid, "task": {"name": f"unrelated-{aid}"}})
    return json.dumps(payload)


class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def test_list_camflow_agent_ids_filters_by_prefix(monkeypatch):
    payload = _fake_list_response("aaa11111", "bbb22222",
                                   other_ids=["zzz99999"])

    def fake_run(args, capture_output=True, text=True, timeout=10):
        if args[1:3] == ["--json", "list"]:
            return _Proc(stdout=payload)
        return _Proc()

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    ids = _list_camflow_agent_ids()
    assert set(ids) == {"aaa11111", "bbb22222"}
    assert "zzz99999" not in ids


def test_list_camflow_agent_ids_handles_camc_failure(monkeypatch):
    def fake_run(args, capture_output=True, text=True, timeout=10):
        return _Proc(stdout="", returncode=1)

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    assert _list_camflow_agent_ids() == []


def test_list_camflow_agent_ids_handles_exception(monkeypatch):
    def fake_run(*_a, **_kw):
        raise OSError("camc not found")

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    assert _list_camflow_agent_ids() == []


def test_cleanup_all_calls_rm_for_each_camflow_agent(monkeypatch):
    rm_calls = []

    def fake_run(args, capture_output=True, text=True, timeout=10):
        if len(args) >= 3 and args[1:3] == ["--json", "list"]:
            return _Proc(stdout=_fake_list_response("aaa11111", "bbb22222"))
        if "rm" in args and "--kill" in args:
            rm_calls.append(args)
            return _Proc()
        return _Proc()

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    cleanup_all_camflow_agents()
    rm_ids = sorted(call[2] for call in rm_calls)
    assert rm_ids == ["aaa11111", "bbb22222"]


def test_cleanup_all_swallows_exceptions(monkeypatch):
    def fake_run(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    # Must not raise
    cleanup_all_camflow_agents()


# ---- Fix 4: kill_existing_camflow_agents ------------------------------


def test_kill_existing_skips_except_id(monkeypatch):
    rm_calls = []

    def fake_run(args, capture_output=True, text=True, timeout=10):
        if len(args) >= 3 and args[1:3] == ["--json", "list"]:
            return _Proc(stdout=_fake_list_response("orphan99", "ccc11111", "ddd22222"))
        if "rm" in args and "--kill" in args:
            rm_calls.append(args[2])
            return _Proc()
        return _Proc()

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    kill_existing_camflow_agents(except_id="orphan99")
    assert "orphan99" not in rm_calls
    assert sorted(rm_calls) == ["ccc11111", "ddd22222"]


def test_kill_existing_with_no_except_kills_all(monkeypatch):
    rm_calls = []

    def fake_run(args, capture_output=True, text=True, timeout=10):
        if len(args) >= 3 and args[1:3] == ["--json", "list"]:
            return _Proc(stdout=_fake_list_response("aaa11111"))
        if "rm" in args and "--kill" in args:
            rm_calls.append(args[2])
            return _Proc()
        return _Proc()

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    kill_existing_camflow_agents()
    assert rm_calls == ["aaa11111"]


# ---- Engine integration: _cleanup_on_exit -----------------------------


def test_engine_cleanup_on_exit_removes_current_agent(monkeypatch, tmp_path):
    from camflow.backend.cam.engine import Engine, EngineConfig

    cleanup_calls = []

    def fake_cleanup_agent(aid):
        cleanup_calls.append(("cleanup_agent", aid))

    def fake_cleanup_all():
        cleanup_calls.append(("cleanup_all", None))

    monkeypatch.setattr(agent_runner, "_cleanup_agent", fake_cleanup_agent)
    monkeypatch.setattr(agent_runner, "cleanup_all_camflow_agents", fake_cleanup_all)

    # Build an engine with state seeded to look mid-execution
    eng = Engine.__new__(Engine)
    eng.state = {"current_agent_id": "abc12345"}
    eng.state_path = str(tmp_path / "state.json")
    eng._cleanup_on_exit()

    # Both pathways fired: explicit cleanup of current, then sweep
    actions = [c[0] for c in cleanup_calls]
    assert "cleanup_agent" in actions
    assert "cleanup_all" in actions
    # And current_agent_id was cleared in state
    assert eng.state["current_agent_id"] is None


def test_engine_cleanup_on_exit_handles_no_current_agent(monkeypatch, tmp_path):
    from camflow.backend.cam.engine import Engine

    cleanup_calls = []

    def fake_cleanup_agent(aid):
        cleanup_calls.append(("cleanup_agent", aid))

    def fake_cleanup_all():
        cleanup_calls.append(("cleanup_all", None))

    monkeypatch.setattr(agent_runner, "_cleanup_agent", fake_cleanup_agent)
    monkeypatch.setattr(agent_runner, "cleanup_all_camflow_agents", fake_cleanup_all)

    eng = Engine.__new__(Engine)
    eng.state = {"current_agent_id": None}
    eng.state_path = str(tmp_path / "state.json")
    eng._cleanup_on_exit()

    # No specific cleanup, but the sweep still ran
    actions = [c[0] for c in cleanup_calls]
    assert "cleanup_agent" not in actions
    assert "cleanup_all" in actions


def test_engine_cleanup_on_exit_swallows_exceptions(monkeypatch, tmp_path):
    from camflow.backend.cam.engine import Engine

    def explode(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_runner, "_cleanup_agent", explode)
    monkeypatch.setattr(agent_runner, "cleanup_all_camflow_agents", explode)

    eng = Engine.__new__(Engine)
    eng.state = {"current_agent_id": "deadbeef"}
    eng.state_path = str(tmp_path / "state.json")
    # Must not raise even if every cleanup helper fails
    eng._cleanup_on_exit()
