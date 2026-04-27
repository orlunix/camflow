"""Phase B mutating ``camflow ctl`` verbs and engine drainer."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from camflow.backend.cam.control_drain import (
    KILL_COOLDOWN_SECONDS,
    drain_control_queue,
    reset_kill_cooldown_for_tests,
)
from camflow.cli_entry import ctl as ctl_module
from camflow.cli_entry.ctl import dispatch


@pytest.fixture(autouse=True)
def _reset_verbs():
    """Make sure both read + mutate verbs are present for the test.
    Importing the modules triggers their _register_all() at module
    body if it hasn't run yet; for subsequent imports it's a no-op
    so we explicitly call it. ``register_verb`` is idempotent for
    the same spec object."""
    saved = dict(ctl_module.VERBS)
    from camflow.cli_entry import ctl_read as _read_mod
    from camflow.cli_entry import ctl_mutate as _mutate_mod
    # Idempotent — same spec objects already in VERBS just no-op.
    _read_mod._register_all()
    _mutate_mod._register_all()
    yield
    ctl_module.VERBS.clear()
    ctl_module.VERBS.update(saved)


@pytest.fixture(autouse=True)
def _reset_cooldown():
    reset_kill_cooldown_for_tests()
    yield
    reset_kill_cooldown_for_tests()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


# ---- ctl side: each verb queues correctly -----------------------------


class TestCtlPause:
    def test_queues_to_control_jsonl(self, tmp_path):
        rc = dispatch(
            "pause",
            ["--reason", "I want to break"],
            project_dir=str(tmp_path),
        )
        assert rc == 0
        entries = _read_jsonl(tmp_path / ".camflow" / "control.jsonl")
        assert len(entries) == 1
        assert entries[0]["verb"] == "pause"
        assert entries[0]["args"]["reason"] == "I want to break"


class TestCtlResume:
    def test_queues_to_control_jsonl(self, tmp_path):
        rc = dispatch(
            "resume", [], project_dir=str(tmp_path),
        )
        assert rc == 0
        entries = _read_jsonl(tmp_path / ".camflow" / "control.jsonl")
        assert entries[0]["verb"] == "resume"


class TestCtlKillWorker:
    def test_queues_with_required_reason(self, tmp_path):
        rc = dispatch(
            "kill-worker",
            ["--reason", "stuck on compaction"],
            project_dir=str(tmp_path),
        )
        assert rc == 0
        entries = _read_jsonl(tmp_path / ".camflow" / "control.jsonl")
        assert entries[0]["verb"] == "kill-worker"
        assert entries[0]["args"]["reason"] == "stuck on compaction"

    def test_missing_reason_fails(self, tmp_path, capsys):
        rc = dispatch(
            "kill-worker", [],
            project_dir=str(tmp_path),
        )
        assert rc != 0


class TestCtlSpawnConfirm:
    def test_queues_to_pending_not_approved(self, tmp_path):
        rc = dispatch(
            "spawn",
            ["--node", "fix", "--brief", "resume the audit fix"],
            project_dir=str(tmp_path),
        )
        assert rc == 0
        # Approved queue is empty.
        approved = _read_jsonl(tmp_path / ".camflow" / "control.jsonl")
        assert approved == []
        # Pending queue has the entry.
        pending = _read_jsonl(
            tmp_path / ".camflow" / "control-pending.jsonl",
        )
        assert len(pending) == 1
        assert pending[0]["verb"] == "spawn"
        assert pending[0]["args"]["node"] == "fix"


class TestCtlSkipConfirm:
    def test_queues_to_pending(self, tmp_path):
        rc = dispatch(
            "skip", ["--reason", "manual override"],
            project_dir=str(tmp_path),
        )
        assert rc == 0
        approved = _read_jsonl(tmp_path / ".camflow" / "control.jsonl")
        assert approved == []
        pending = _read_jsonl(
            tmp_path / ".camflow" / "control-pending.jsonl",
        )
        assert pending[0]["verb"] == "skip"


# ---- engine drainer: each verb's effect on state ---------------------


class TestDrainPause:
    def test_running_to_waiting(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "control.jsonl").write_text(
            json.dumps({
                "ts": "2026-04-26T11:00:00Z",
                "verb": "pause",
                "args": {"reason": "x"},
                "issued_by": "user",
                "flow_id": "flow_a",
            }) + "\n"
        )
        state = {"status": "running", "flow_id": "flow_a"}
        n = drain_control_queue(tmp_path, state)
        assert n == 1
        assert state["status"] == "waiting"
        assert "paused_at" in state

    def test_already_waiting_is_skipped(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "control.jsonl").write_text(
            json.dumps({"verb": "pause", "args": {}, "issued_by": "u"}) + "\n"
        )
        state = {"status": "waiting"}
        drain_control_queue(tmp_path, state)
        assert state["status"] == "waiting"


class TestDrainResume:
    def test_waiting_to_running(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "control.jsonl").write_text(
            json.dumps({"verb": "resume", "args": {}, "issued_by": "u"}) + "\n"
        )
        state = {"status": "waiting", "paused_at": time.time()}
        drain_control_queue(tmp_path, state)
        assert state["status"] == "running"
        assert "paused_at" not in state


class TestDrainKillWorker:
    def test_kills_current_agent(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "control.jsonl").write_text(
            json.dumps({"verb": "kill-worker", "args": {}, "issued_by": "u"}) + "\n"
        )
        killed = []
        state = {
            "status": "running",
            "flow_id": "flow_a",
            "pc": "build",
            "current_agent_id": "agent-aaa",
        }
        drain_control_queue(
            tmp_path, state,
            cleanup_agent=lambda aid: killed.append(aid),
        )
        assert killed == ["agent-aaa"]
        assert state["current_agent_id"] is None

    def test_no_current_agent_is_skipped(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "control.jsonl").write_text(
            json.dumps({"verb": "kill-worker", "args": {}, "issued_by": "u"}) + "\n"
        )
        killed = []
        state = {"status": "running", "current_agent_id": None}
        drain_control_queue(
            tmp_path, state,
            cleanup_agent=lambda aid: killed.append(aid),
        )
        assert killed == []

    def test_30s_cooldown_per_tuple(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        cmd = json.dumps(
            {"verb": "kill-worker", "args": {}, "issued_by": "u",
             "flow_id": "f1"}
        ) + "\n"
        killed = []
        # First kill goes through.
        (tmp_path / ".camflow" / "control.jsonl").write_text(cmd)
        state = {
            "status": "running",
            "flow_id": "f1",
            "pc": "build",
            "current_agent_id": "agent-bbb",
        }
        drain_control_queue(
            tmp_path, state,
            cleanup_agent=lambda aid: killed.append(aid),
        )
        assert killed == ["agent-bbb"]
        # Second kill of same tuple within 30s is rate-limited.
        (tmp_path / ".camflow" / "control.jsonl").write_text(cmd)
        state["current_agent_id"] = "agent-bbb"
        drain_control_queue(
            tmp_path, state,
            cleanup_agent=lambda aid: killed.append(aid),
        )
        # Killed list unchanged — cooldown blocked the second.
        assert killed == ["agent-bbb"]


class TestDrainSpawn:
    def test_overrides_pc(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "control.jsonl").write_text(
            json.dumps({
                "verb": "spawn",
                "args": {"node": "debug", "brief": "fix the off-by-one"},
                "issued_by": "user",
            }) + "\n"
        )
        state = {"status": "running", "pc": "build"}
        drain_control_queue(tmp_path, state)
        assert state["pc"] == "debug"
        assert state["spawned_brief"] == "fix the off-by-one"


class TestDrainSkip:
    def test_sets_skip_current_flag(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "control.jsonl").write_text(
            json.dumps({
                "verb": "skip",
                "args": {"reason": "manual override"},
                "issued_by": "user",
            }) + "\n"
        )
        state = {"status": "running", "pc": "test"}
        drain_control_queue(tmp_path, state)
        assert state["skip_current"]["node"] == "test"
        assert state["skip_current"]["reason"] == "manual override"


# ---- queue housekeeping ----------------------------------------------


def test_drain_truncates_queue(tmp_path):
    (tmp_path / ".camflow").mkdir()
    (tmp_path / ".camflow" / "control.jsonl").write_text(
        json.dumps({"verb": "resume", "args": {}, "issued_by": "u"}) + "\n"
    )
    drain_control_queue(tmp_path, {"status": "waiting"})
    text = (tmp_path / ".camflow" / "control.jsonl").read_text()
    assert text == ""


def test_drain_no_queue_file_returns_zero(tmp_path):
    n = drain_control_queue(tmp_path, {"status": "running"})
    assert n == 0


def test_drain_emits_control_resolution_trace(tmp_path):
    (tmp_path / ".camflow").mkdir()
    (tmp_path / ".camflow" / "control.jsonl").write_text(
        json.dumps({
            "verb": "pause",
            "args": {},
            "issued_by": "steward-7c2a",
            "flow_id": "flow_q",
        }) + "\n"
    )
    drain_control_queue(tmp_path, {"status": "running", "flow_id": "flow_q"})
    trace = _read_jsonl(tmp_path / ".camflow" / "trace.log")
    res = [e for e in trace if e.get("kind") == "control_resolution"]
    assert len(res) == 1
    assert res[0]["verb"] == "pause"
    assert res[0]["resolution"] == "executed"
    assert res[0]["issued_by"] == "steward-7c2a"
