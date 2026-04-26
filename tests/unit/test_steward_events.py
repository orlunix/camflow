"""Unit tests for camflow.steward.events.

Production transport (``camc send``) is dependency-injected; every
test replaces it with a recording stub.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camflow.steward.events import (
    EVENT_PREFIX,
    EVENT_TYPES,
    emit,
    emit_flow_started,
    emit_flow_terminal,
    emit_node_done,
    emit_node_failed,
    emit_node_started,
)
from camflow.steward.spawn import spawn_steward


def _spawn(tmp_path):
    """Helper: register a Steward pointer so events have a target."""
    spawn_steward(
        tmp_path,
        workflow_path=None,
        camc_runner=lambda *_a, **_k: "deadbeef",
    )


def _read_mirror(tmp_path):
    p = tmp_path / ".camflow" / "steward-events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _read_trace(tmp_path):
    p = tmp_path / ".camflow" / "trace.log"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _trace_event_emitted(tmp_path):
    return [e for e in _read_trace(tmp_path) if e.get("kind") == "event_emitted"]


# ---- emit ---------------------------------------------------------------


class TestEmit:
    def test_unknown_type_raises(self, tmp_path):
        with pytest.raises(ValueError, match="unknown event type"):
            emit(tmp_path, "not_a_real_event")

    def test_all_event_types_are_listed(self):
        # Documentation invariant — design.md §7.3.
        expected = {
            "flow_started", "flow_terminal", "flow_idle",
            "node_started", "node_done", "node_failed",
            "node_retry", "verify_failed",
            "escalation_level_change", "heartbeat_stale_worker",
            "replan_done", "engine_resumed", "checkpoint_now",
        }
        assert expected == EVENT_TYPES

    def test_emit_with_no_steward_mirrors_and_traces_but_returns_false(
        self, tmp_path
    ):
        # No spawn_steward call → no pointer → no target.
        ok = emit(
            tmp_path,
            "node_done",
            flow_id="f1",
            step=3,
            node="build",
        )
        assert ok is False

        mirror = _read_mirror(tmp_path)
        assert len(mirror) == 1
        assert mirror[0]["type"] == "node_done"
        assert mirror[0]["node"] == "build"

        traced = _trace_event_emitted(tmp_path)
        assert len(traced) == 1
        assert traced[0]["sent"] is False
        assert traced[0]["to"] is None

    def test_emit_with_alive_steward_sends(self, tmp_path):
        _spawn(tmp_path)
        sent: list[tuple[str, str]] = []

        def fake_send(agent_id, message):
            sent.append((agent_id, message))
            return True

        ok = emit(
            tmp_path,
            "node_failed",
            flow_id="f1",
            step=3,
            node="test",
            summary="boom",
            camc_send=fake_send,
        )
        assert ok is True
        assert len(sent) == 1
        agent_id, message = sent[0]
        assert agent_id == "deadbeef"
        assert message.startswith(EVENT_PREFIX + " ")

        # Mirror has the structured payload.
        mirror = _read_mirror(tmp_path)
        assert mirror[0]["type"] == "node_failed"
        assert mirror[0]["node"] == "test"

        # Trace records sent=True with target id.
        traced = _trace_event_emitted(tmp_path)
        assert traced[0]["sent"] is True
        assert traced[0]["to"] == "deadbeef"
        assert traced[0]["payload_size"] > 0

    def test_send_failure_returns_false_and_traces(self, tmp_path):
        _spawn(tmp_path)
        ok = emit(
            tmp_path,
            "node_done",
            flow_id="f1",
            step=1,
            node="build",
            camc_send=lambda *_: False,
        )
        assert ok is False
        traced = _trace_event_emitted(tmp_path)
        assert traced[0]["sent"] is False
        assert traced[0]["to"] == "deadbeef"

    def test_send_exception_swallowed(self, tmp_path):
        _spawn(tmp_path)
        captured: list[str] = []

        def bad_send(*_):
            raise RuntimeError("network down")

        def log_fn(msg, exc):
            captured.append(msg)

        ok = emit(
            tmp_path,
            "node_done",
            flow_id="f1",
            step=1,
            node="build",
            camc_send=bad_send,
            log_failure=log_fn,
        )
        assert ok is False
        assert any("camc send failed" in m for m in captured)

    def test_extra_fields_propagate_to_payload(self, tmp_path):
        _spawn(tmp_path)
        sent: list[str] = []
        emit(
            tmp_path,
            "node_done",
            flow_id="f1",
            step=2,
            node="test",
            agent_id="cafe",
            duration_ms=1500,
            camc_send=lambda _id, msg: sent.append(msg) or True,
        )
        # Pull the JSON out of the [CAMFLOW EVENT] {…} message.
        msg = sent[0]
        json_part = msg[len(EVENT_PREFIX):].strip()
        payload = json.loads(json_part)
        assert payload["agent_id"] == "cafe"
        assert payload["duration_ms"] == 1500


# ---- convenience wrappers ----------------------------------------------


class TestConvenienceWrappers:
    def test_flow_started(self, tmp_path):
        _spawn(tmp_path)
        sent: list[str] = []
        ok = emit_flow_started(
            tmp_path,
            flow_id="f1",
            workflow_path="/abs/workflow.yaml",
            steward_id="deadbeef",
            camc_send=lambda _id, msg: sent.append(msg) or True,
        )
        assert ok is True
        payload = json.loads(sent[0][len(EVENT_PREFIX):].strip())
        assert payload["type"] == "flow_started"
        assert payload["workflow"] == "/abs/workflow.yaml"
        assert payload["steward"] == "deadbeef"

    def test_flow_terminal(self, tmp_path):
        _spawn(tmp_path)
        sent: list[str] = []
        ok = emit_flow_terminal(
            tmp_path,
            flow_id="f1",
            final={"status": "done", "pc": "deploy"},
            camc_send=lambda _id, msg: sent.append(msg) or True,
        )
        assert ok is True
        payload = json.loads(sent[0][len(EVENT_PREFIX):].strip())
        assert payload["type"] == "flow_terminal"
        assert payload["final"]["status"] == "done"

    def test_node_started(self, tmp_path):
        _spawn(tmp_path)
        sent: list[str] = []
        ok = emit_node_started(
            tmp_path,
            flow_id="f1",
            step=1,
            node="build",
            agent_id="a1b2",
            attempt=2,
            camc_send=lambda _id, msg: sent.append(msg) or True,
        )
        assert ok is True
        payload = json.loads(sent[0][len(EVENT_PREFIX):].strip())
        assert payload["type"] == "node_started"
        assert payload["attempt"] == 2

    def test_node_done(self, tmp_path):
        _spawn(tmp_path)
        sent: list[str] = []
        emit_node_done(
            tmp_path,
            flow_id="f1",
            step=1,
            node="build",
            summary="compiled in 12s",
            agent_id="a1b2",
            camc_send=lambda _id, msg: sent.append(msg) or True,
        )
        payload = json.loads(sent[0][len(EVENT_PREFIX):].strip())
        assert payload["status"] == "success"
        assert payload["summary"] == "compiled in 12s"

    def test_node_done_truncates_long_summary(self, tmp_path):
        _spawn(tmp_path)
        sent: list[str] = []
        long = "x" * 1000
        emit_node_done(
            tmp_path,
            flow_id="f1",
            step=1,
            node="build",
            summary=long,
            camc_send=lambda _id, msg: sent.append(msg) or True,
        )
        payload = json.loads(sent[0][len(EVENT_PREFIX):].strip())
        assert len(payload["summary"]) == 240

    def test_node_failed_carries_error(self, tmp_path):
        _spawn(tmp_path)
        sent: list[str] = []
        emit_node_failed(
            tmp_path,
            flow_id="f1",
            step=2,
            node="test",
            summary="assertion failed",
            error={"code": "NODE_FAIL", "reason": "boom"},
            camc_send=lambda _id, msg: sent.append(msg) or True,
        )
        payload = json.loads(sent[0][len(EVENT_PREFIX):].strip())
        assert payload["status"] == "fail"
        assert payload["error"]["code"] == "NODE_FAIL"


# ---- mirror durability --------------------------------------------------


def test_mirror_persists_even_when_send_fails(tmp_path):
    _spawn(tmp_path)
    emit(
        tmp_path,
        "node_done",
        flow_id="f1",
        step=1,
        node="build",
        camc_send=lambda *_: False,
    )
    mirror = _read_mirror(tmp_path)
    assert len(mirror) == 1
    assert mirror[0]["node"] == "build"
