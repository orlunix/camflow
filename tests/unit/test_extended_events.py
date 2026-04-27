"""Phase B extended event set — node_retry, escalation_level_change,
verify_failed, heartbeat_stale_worker."""

from __future__ import annotations

from camflow.steward import events as events_module
from camflow.steward.events import (
    RETRY_COALESCE_WINDOW,
    emit_escalation_level_change,
    emit_heartbeat_stale_worker,
    emit_node_retry,
    emit_verify_failed,
    reset_retry_coalesce_for_tests,
)


def _capture_emits(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_emit(project_dir, event_type, **kwargs):
        captured.append((event_type, kwargs))
        return False

    monkeypatch.setattr(events_module, "emit", fake_emit)
    return captured


# ---- node_retry rate limiting -----------------------------------------


class TestNodeRetryCoalesce:
    def setup_method(self):
        reset_retry_coalesce_for_tests()

    def test_first_retry_emits(self, tmp_path, monkeypatch):
        captured = _capture_emits(monkeypatch)
        emit_node_retry(
            tmp_path, flow_id="f", node="build", attempt=2,
            error_code="TRANSIENT",
        )
        assert len(captured) == 1
        assert captured[0][0] == "node_retry"
        assert captured[0][1]["since_count"] == 1

    def test_burst_within_window_coalesces(self, tmp_path, monkeypatch):
        captured = _capture_emits(monkeypatch)
        for n in (2, 3, 4, 5):
            emit_node_retry(
                tmp_path, flow_id="f", node="build", attempt=n,
            )
        # Only the first emit goes through; subsequent ones are
        # coalesced into the bucket counter.
        assert len(captured) == 1

    def test_after_window_emits_again(self, tmp_path, monkeypatch):
        import time
        captured = _capture_emits(monkeypatch)
        emit_node_retry(
            tmp_path, flow_id="f", node="build", attempt=2,
        )

        # Force the coalesce bucket to be older than the window.
        events_module._RETRY_LAST_EMIT[("f", "build")] = (
            time.monotonic() - RETRY_COALESCE_WINDOW - 1.0,
            5,
        )

        emit_node_retry(
            tmp_path, flow_id="f", node="build", attempt=8,
        )
        assert len(captured) == 2
        # The second emit carries the burst count from the prior bucket.
        assert captured[1][1]["since_count"] == 6

    def test_different_nodes_have_independent_buckets(
        self, tmp_path, monkeypatch,
    ):
        captured = _capture_emits(monkeypatch)
        emit_node_retry(tmp_path, flow_id="f", node="A", attempt=2)
        emit_node_retry(tmp_path, flow_id="f", node="B", attempt=2)
        # Both go through.
        assert len(captured) == 2


# ---- escalation_level_change ----------------------------------------


def test_escalation_level_change_payload(tmp_path, monkeypatch):
    captured = _capture_emits(monkeypatch)
    emit_escalation_level_change(
        tmp_path, flow_id="f", node="test", from_level=0, to_level=2,
    )
    assert captured[0][0] == "escalation_level_change"
    assert captured[0][1]["from_level"] == 0
    assert captured[0][1]["to_level"] == 2


# ---- verify_failed --------------------------------------------------


def test_verify_failed_truncates_stderr(tmp_path, monkeypatch):
    captured = _capture_emits(monkeypatch)
    long_stderr = "x" * 10000
    emit_verify_failed(
        tmp_path,
        flow_id="f",
        node="test",
        verify_cmd="pytest -q",
        exit_code=1,
        stderr_tail=long_stderr,
    )
    assert captured[0][0] == "verify_failed"
    assert captured[0][1]["exit_code"] == 1
    # stderr is truncated to last 500 chars.
    assert len(captured[0][1]["stderr_tail"]) == 500


# ---- heartbeat_stale_worker -----------------------------------------


def test_heartbeat_stale_worker(tmp_path, monkeypatch):
    captured = _capture_emits(monkeypatch)
    emit_heartbeat_stale_worker(
        tmp_path,
        flow_id="f",
        node="build",
        agent_id="abc1",
        since_s=125.0,
    )
    assert captured[0][0] == "heartbeat_stale_worker"
    assert captured[0][1]["since_s"] == 125
    assert captured[0][1]["agent_id"] == "abc1"


# ---- engine wiring ---------------------------------------------------


class TestEngineWiring:
    def test_engine_emit_helpers_short_circuit_on_no_steward(
        self, tmp_path, monkeypatch,
    ):
        from camflow.backend.cam.engine import Engine, EngineConfig
        captured = _capture_emits(monkeypatch)

        wf = tmp_path / "wf.yaml"
        wf.write_text("a:\n  do: cmd echo\n")
        cfg = EngineConfig(no_steward=True)
        eng = Engine(str(wf), str(tmp_path), cfg)
        eng.state = {"flow_id": "f", "pc": "a"}

        eng._emit_steward_node_retry("a", 2, "X")
        eng._emit_steward_escalation_change("a", 0, 1)
        eng._emit_steward_verify_failed("a", "true", 1, "")
        eng._emit_steward_heartbeat_stale("a", "abc1", 90)
        # All four helpers must short-circuit: nothing emitted.
        assert captured == []

    def test_engine_node_retry_emits_via_helper(
        self, tmp_path, monkeypatch,
    ):
        from camflow.backend.cam.engine import Engine, EngineConfig
        reset_retry_coalesce_for_tests()
        captured = _capture_emits(monkeypatch)

        wf = tmp_path / "wf.yaml"
        wf.write_text("a:\n  do: cmd echo\n")
        cfg = EngineConfig(no_steward=False)
        eng = Engine(str(wf), str(tmp_path), cfg)
        eng.state = {"flow_id": "f", "pc": "a"}

        eng._emit_steward_node_retry("a", 2, "TRANSIENT")
        assert any(c[0] == "node_retry" for c in captured)
