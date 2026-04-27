"""Phase C smooth-mode driver — ``camflow "<NL>"``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camflow.cli_entry import smooth as smooth_module
from camflow.cli_entry.smooth import smooth_command
from camflow.steward.spawn import STEWARD_POINTER_FILE


# ---- helpers ----------------------------------------------------------


def _seed_pointer(tmp_path: Path, agent_id: str = "steward-7c2a") -> None:
    cf = tmp_path / ".camflow"
    cf.mkdir(parents=True, exist_ok=True)
    (cf / STEWARD_POINTER_FILE).write_text(
        json.dumps({"agent_id": agent_id, "name": agent_id})
    )


# ---- argparse hookup --------------------------------------------------


class TestParser:
    def test_request_required(self):
        parser = smooth_module.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_yes_flag_skips_countdown(self):
        parser = smooth_module.build_parser()
        args = parser.parse_args(["build a thing", "--yes"])
        assert args.yes is True
        assert args.countdown == 5  # default still set

    def test_no_daemon_overrides(self):
        parser = smooth_module.build_parser()
        args = parser.parse_args(["x", "--no-daemon"])
        assert args.daemon is False


# ---- step 2: alive Steward routes the request -----------------------


class TestStewardRouting:
    def test_alive_steward_camc_send_short_circuit(
        self, tmp_path, monkeypatch, capsys,
    ):
        _seed_pointer(tmp_path)
        monkeypatch.setattr(
            smooth_module, "is_steward_alive", lambda *a, **k: True,
        )

        sent: list[tuple[str, str]] = []
        monkeypatch.setattr(
            smooth_module, "_camc_send",
            lambda aid, msg: sent.append((aid, msg)) or True,
        )

        # If we reach plan_and_review, fail loudly.
        called_plan = []
        monkeypatch.setattr(
            smooth_module, "_plan_and_review",
            lambda *a, **k: called_plan.append(1) or {"a": {}},
        )

        rc = smooth_command([
            "现在状况?",
            "--project-dir", str(tmp_path),
            "--yes",
        ])
        assert rc == 0
        assert sent == [("steward-7c2a", "现在状况?")]
        assert called_plan == []
        err = capsys.readouterr().err
        assert "routing your request" in err

    def test_alive_steward_send_failure_falls_through(
        self, tmp_path, monkeypatch,
    ):
        _seed_pointer(tmp_path)
        monkeypatch.setattr(
            smooth_module, "is_steward_alive", lambda *a, **k: True,
        )
        monkeypatch.setattr(
            smooth_module, "_camc_send", lambda *a, **k: False,
        )
        # Plan path replaced with a known sentinel return.
        monkeypatch.setattr(
            smooth_module, "_plan_and_review",
            lambda *a, **k: None,  # simulate plan failure
        )
        rc = smooth_command([
            "x", "--project-dir", str(tmp_path), "--yes",
        ])
        # Plan returned None → smooth exits 1.
        assert rc == 1


# ---- step 5: countdown handles --------------------------------------


class TestCountdownHandles:
    def test_non_tty_returns_go_immediately(self, monkeypatch):
        # When stdin isn't a TTY (CI, pipes), countdown is a no-op.
        class _FakeStdin:
            def isatty(self):
                return False
        monkeypatch.setattr(smooth_module.sys, "stdin", _FakeStdin())
        result = smooth_module._countdown_with_handles(5)
        assert result == smooth_module.COUNTDOWN_GO


# ---- step 6: kickoff ------------------------------------------------


class TestKickoff:
    def test_kickoff_passes_no_steward_through(
        self, tmp_path, monkeypatch,
    ):
        cf = tmp_path / ".camflow"
        cf.mkdir(parents=True, exist_ok=True)
        (cf / "workflow.yaml").write_text("a:\n  do: cmd echo\n")

        captured: list[list[str]] = []
        monkeypatch.setattr(
            "camflow.cli_entry.main._run_workflow",
            lambda argv: captured.append(list(argv)) or 0,
        )

        # Build args the same way the parser would.
        from argparse import Namespace
        args = Namespace(
            request="x",
            project_dir=str(tmp_path),
            yes=True,
            countdown=5,
            timeout=180,
            daemon=True,
            no_steward=True,
        )
        rc = smooth_module._kickoff_engine(args, str(tmp_path))
        assert rc == 0
        assert "--no-steward" in captured[0]
        assert "--daemon" in captured[0]


# ---- end-to-end: plan + kickoff ---------------------------------------


class TestEndToEnd:
    def test_yes_path_skips_countdown_and_kicks_off(
        self, tmp_path, monkeypatch,
    ):
        # No Steward → falls through to plan.
        monkeypatch.setattr(
            smooth_module, "is_steward_alive", lambda *a, **k: False,
        )

        # Mock generate_workflow_via_agent so the test doesn't shell
        # out to camc. Returns a fake PlannerResult.
        from camflow.planner.agent_planner import PlannerResult

        fake_result = PlannerResult(
            workflow={"build": {"do": "cmd echo build"}},
            workflow_path=str(tmp_path / ".camflow" / "workflow.yaml"),
            rationale_path=None,
            warnings=[],
            agent_id="planner-fake",
            duration_s=0.5,
        )
        # Seed the workflow.yaml so kickoff sees it.
        cf = tmp_path / ".camflow"
        cf.mkdir(parents=True, exist_ok=True)
        (cf / "workflow.yaml").write_text("build:\n  do: cmd echo build\n")

        monkeypatch.setattr(
            smooth_module, "generate_workflow_via_agent",
            lambda *a, **k: fake_result,
        )
        # Stub kickoff so we don't actually run an engine.
        kickoff_calls: list = []
        monkeypatch.setattr(
            smooth_module, "_kickoff_engine",
            lambda args, pdir: kickoff_calls.append((args, pdir)) or 0,
        )

        rc = smooth_command([
            "build me a calculator",
            "--project-dir", str(tmp_path),
            "--yes",
        ])
        assert rc == 0
        assert len(kickoff_calls) == 1


class TestPlanFailure:
    def test_planner_error_returns_1_with_legacy_hint(
        self, tmp_path, monkeypatch, capsys,
    ):
        from camflow.planner.agent_planner import PlannerAgentError

        monkeypatch.setattr(
            smooth_module, "is_steward_alive", lambda *a, **k: False,
        )

        def fake_generate(*a, **k):
            raise PlannerAgentError("camc unreachable")

        monkeypatch.setattr(
            smooth_module, "generate_workflow_via_agent", fake_generate,
        )
        rc = smooth_command([
            "build x", "--project-dir", str(tmp_path), "--yes",
        ])
        err = capsys.readouterr().err
        assert rc == 1
        assert "Planner failed" in err
        assert "--legacy" in err


# ---- main dispatch heuristic -----------------------------------------


class TestDispatchHeuristic:
    def test_yaml_path_recognised(self, tmp_path):
        from camflow.cli_entry.main import _looks_like_workflow_path
        wf = tmp_path / "wf.yaml"
        wf.write_text("a:\n  do: cmd echo\n")
        assert _looks_like_workflow_path(str(wf)) is True

    def test_yaml_extension_without_existence(self):
        from camflow.cli_entry.main import _looks_like_workflow_path
        assert _looks_like_workflow_path("nonexistent.yaml") is True
        assert _looks_like_workflow_path("nonexistent.yml") is True

    def test_natural_language_not_recognised(self):
        from camflow.cli_entry.main import _looks_like_workflow_path
        assert _looks_like_workflow_path("build me a thing") is False
        assert _looks_like_workflow_path("现在状况?") is False
        assert _looks_like_workflow_path("") is False
