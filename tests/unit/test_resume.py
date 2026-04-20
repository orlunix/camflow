"""Unit tests for the `camflow resume` subcommand.

The resume subcommand is mostly pure: it edits state.json then hands
off to the existing Engine.run() loop. We exercise:

  * `_prepare_state` — the state-mutation logic in isolation
  * The CLI wrapper (`resume_command`) end-to-end with a stubbed Engine
    so we don't actually spawn camc agents

Coverage targets:
  - flip 'failed' / 'aborted' / 'engine_error' → 'running'
  - --from updates pc and clears blocked / last_failure
  - --retry forces flip from arbitrary status (incl. 'running' is no-op)
  - 'done' refuses without --from; 'done' + --from re-runs
  - 'waiting' refuses without --retry
  - --from to a non-existent node raises ValueError
  - completed[] / lessons[] / trace are preserved
  - retry_counts and node_execution_count for the resumed pc reset to 0
  - missing state.json → CLI returns 1
  - --dry-run writes the state but doesn't spawn the Engine
"""

from __future__ import annotations

import argparse
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from camflow.cli_entry.resume import (
    RESUMABLE_FAILED_STATUSES,
    _prepare_state,
    build_parser,
    resume_command,
)


# ---- helpers -----------------------------------------------------------


def _wf(*node_ids):
    """Minimal workflow dict: each node has a `do` so DSL validation
    won't blow up if some downstream call peeks at it.
    """
    return {nid: {"do": "shell true"} for nid in node_ids}


def _state(**overrides):
    base = {
        "pc": "build",
        "status": "failed",
        "iteration": 4,
        "completed": [{"node": "setup-tree", "action": "ok", "iteration": 1}],
        "lessons": ["one-time gotcha"],
        "failed_approaches": [],
        "retry_counts": {"build": 3, "setup-tree": 0},
        "node_execution_count": {"build": 5},
        "blocked": {"node": "build", "reason": "compile error"},
        "last_failure": {"summary": "compile error"},
        "error": {"code": "NODE_FAIL"},
    }
    base.update(overrides)
    return base


# ---- _prepare_state ----------------------------------------------------


class TestPrepareState:
    def test_failed_flips_to_running(self):
        state, actions = _prepare_state(
            _state(status="failed"), _wf("setup-tree", "build"),
            from_node=None, retry=False,
        )
        assert state["status"] == "running"
        assert any("'failed' → 'running'" in a for a in actions)
        # error metadata cleared so engine doesn't see a terminal flag
        assert "error" not in state

    @pytest.mark.parametrize("status", sorted(RESUMABLE_FAILED_STATUSES))
    def test_resumable_statuses_all_flip(self, status):
        state, _ = _prepare_state(
            _state(status=status), _wf("setup-tree", "build"),
            from_node=None, retry=False,
        )
        assert state["status"] == "running"

    def test_done_without_from_raises(self):
        with pytest.raises(ValueError, match="status is 'done'"):
            _prepare_state(
                _state(status="done"), _wf("setup-tree", "build"),
                from_node=None, retry=False,
            )

    def test_done_with_from_reruns(self):
        state, actions = _prepare_state(
            _state(status="done", pc="build"),
            _wf("setup-tree", "build", "validate"),
            from_node="validate", retry=False,
        )
        assert state["status"] == "running"
        assert state["pc"] == "validate"
        assert any("'done' → 'running'" in a for a in actions)

    def test_waiting_without_retry_raises(self):
        with pytest.raises(ValueError, match="status is 'waiting'"):
            _prepare_state(
                _state(status="waiting"), _wf("build"),
                from_node=None, retry=False,
            )

    def test_waiting_with_retry_flips(self):
        state, _ = _prepare_state(
            _state(status="waiting"), _wf("build"),
            from_node=None, retry=True,
        )
        assert state["status"] == "running"

    def test_running_status_left_alone(self):
        """Auto-resume case — engine takes over without our help."""
        state, actions = _prepare_state(
            _state(status="running"), _wf("build"),
            from_node=None, retry=False,
        )
        assert state["status"] == "running"
        # No flip action recorded.
        assert not any("→ 'running'" in a for a in actions)

    def test_retry_on_running_is_noop(self):
        state, actions = _prepare_state(
            _state(status="running"), _wf("build"),
            from_node=None, retry=True,
        )
        assert state["status"] == "running"
        assert any("no-op" in a for a in actions)

    def test_from_jumps_pc_and_clears_blocked(self):
        state, actions = _prepare_state(
            _state(status="failed", pc="build"),
            _wf("setup-tree", "build", "validate"),
            from_node="validate", retry=False,
        )
        assert state["pc"] == "validate"
        assert state["blocked"] is None
        assert "last_failure" not in state
        assert any("pc 'build' → 'validate'" in a for a in actions)
        assert any("cleared state.blocked" in a for a in actions)

    def test_from_unknown_node_raises(self):
        with pytest.raises(ValueError, match="not a node in the workflow"):
            _prepare_state(
                _state(status="failed"), _wf("build"),
                from_node="ghost-node", retry=False,
            )

    def test_completed_lessons_trace_preserved(self):
        before = _state(status="failed")
        before_completed = list(before["completed"])
        before_lessons = list(before["lessons"])
        state, _ = _prepare_state(
            before, _wf("setup-tree", "build"),
            from_node=None, retry=False,
        )
        # Same list contents — not reset by resume.
        assert state["completed"] == before_completed
        assert state["lessons"] == before_lessons

    def test_retry_counts_reset_only_for_resumed_pc(self):
        before = _state(
            status="failed", pc="build",
            retry_counts={"build": 3, "setup-tree": 1, "deploy": 2},
            node_execution_count={"build": 7, "deploy": 4},
        )
        state, _ = _prepare_state(
            before, _wf("setup-tree", "build", "deploy"),
            from_node=None, retry=False,
        )
        # build gets a fresh budget; the other entries are untouched.
        assert state["retry_counts"]["build"] == 0
        assert state["retry_counts"]["setup-tree"] == 1
        assert state["retry_counts"]["deploy"] == 2
        assert state["node_execution_count"]["build"] == 0
        assert state["node_execution_count"]["deploy"] == 4


# ---- resume_command (CLI wrapper) -------------------------------------


def _wf_yaml(tmp_path, body="setup-tree:\n  do: shell true\nbuild:\n  do: shell false\n"):
    p = tmp_path / "workflow.yaml"
    p.write_text(body)
    return str(p)


def _write_state(tmp_path, state_dict):
    state_dir = tmp_path / ".camflow"
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "state.json"
    p.write_text(json.dumps(state_dict))
    return str(p)


def _args(workflow, **overrides):
    """Build an argparse Namespace shaped like the resume parser produces."""
    base = dict(
        workflow=workflow,
        project_dir=None,
        from_node=None,
        retry=False,
        dry_run=False,
        poll_interval=5,
        node_timeout=600,
        workflow_timeout=3600,
        max_retries=3,
        max_node_executions=10,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestResumeCommand:
    def test_missing_state_returns_1(self, tmp_path):
        wf = _wf_yaml(tmp_path)
        rc = resume_command(_args(wf))
        assert rc == 1

    def test_missing_workflow_file_returns_1(self, tmp_path):
        rc = resume_command(_args(str(tmp_path / "no-such.yaml")))
        assert rc == 1

    def test_dry_run_writes_state_but_does_not_spawn_engine(self, tmp_path):
        wf = _wf_yaml(tmp_path)
        sp = _write_state(tmp_path, _state(status="failed", pc="build"))
        with patch("camflow.cli_entry.resume.Engine") as Engine_cls:
            rc = resume_command(_args(wf, dry_run=True))
        assert rc == 0
        Engine_cls.assert_not_called()
        # state was rewritten with status flipped
        with open(sp) as f:
            saved = json.load(f)
        assert saved["status"] == "running"

    def test_runs_engine_and_returns_done(self, tmp_path):
        wf = _wf_yaml(tmp_path)
        _write_state(tmp_path, _state(status="failed", pc="build"))
        with patch("camflow.cli_entry.resume.Engine") as Engine_cls:
            inst = Engine_cls.return_value
            inst.run.return_value = {"status": "done", "pc": "build"}
            rc = resume_command(_args(wf))
        Engine_cls.assert_called_once()
        inst.run.assert_called_once()
        assert rc == 0

    def test_engine_failure_returns_1(self, tmp_path):
        wf = _wf_yaml(tmp_path)
        _write_state(tmp_path, _state(status="failed", pc="build"))
        with patch("camflow.cli_entry.resume.Engine") as Engine_cls:
            Engine_cls.return_value.run.return_value = {"status": "failed", "pc": "build"}
            rc = resume_command(_args(wf))
        assert rc == 1

    def test_done_without_from_returns_1(self, tmp_path):
        wf = _wf_yaml(tmp_path)
        _write_state(tmp_path, _state(status="done", pc="build"))
        with patch("camflow.cli_entry.resume.Engine") as Engine_cls:
            rc = resume_command(_args(wf))
        assert rc == 1
        Engine_cls.assert_not_called()


# ---- argparse wiring --------------------------------------------------


class TestParser:
    def test_positional_workflow_required(self):
        with pytest.raises(SystemExit):
            build_parser(None).parse_args([])

    def test_from_and_retry_flags(self):
        p = build_parser(None)
        args = p.parse_args(["wf.yaml", "--from", "validate", "--retry"])
        assert args.workflow == "wf.yaml"
        assert args.from_node == "validate"
        assert args.retry is True

    def test_dry_run_flag(self):
        args = build_parser(None).parse_args(["wf.yaml", "--dry-run"])
        assert args.dry_run is True
