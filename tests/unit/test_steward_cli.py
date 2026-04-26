"""Unit tests for ``camflow steward`` CLI subcommands.

Covers ``steward status``, ``steward kill``, ``steward restart``, and
the helper consumed by ``camflow status``.

The global conftest blocks real ``camc`` shell-outs, so this file
exercises the orchestration logic and asserts on the side effects:
registry status flips, pointer file deletion, ``camc rm`` arg shape,
and exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camflow.cli_entry import steward as steward_module
from camflow.cli_entry.steward import (
    steward_command,
    steward_status_for_status_command,
)
from camflow.registry import (
    REGISTRY_VERSION,
    get_current_steward,
    register_agent,
    set_current_steward,
)
from camflow.steward.spawn import STEWARD_POINTER_FILE


# ---- helpers ------------------------------------------------------------


def _seed_steward(project_dir: Path, agent_id: str = "steward-7c2a"):
    register_agent(
        project_dir,
        {
            "id": agent_id,
            "role": "steward",
            "status": "alive",
            "spawned_at": "2026-04-26T10:00:00Z",
            "spawned_by": "test",
            "flows_witnessed": ["flow_001", "flow_002"],
        },
    )
    set_current_steward(project_dir, agent_id)
    pointer = Path(project_dir) / ".camflow" / STEWARD_POINTER_FILE
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "name": agent_id,
                "spawned_at": "2026-04-26T10:00:00Z",
                "spawned_by": "test",
                "prompt_file": str(
                    Path(project_dir) / ".camflow" / "steward-prompt.txt"
                ),
                "summary_path": str(
                    Path(project_dir) / ".camflow" / "steward-summary.md"
                ),
                "archive_path": str(
                    Path(project_dir) / ".camflow" / "steward-archive.md"
                ),
            }
        )
    )


# ---- steward status -----------------------------------------------------


class TestStewardStatus:
    def test_no_steward_returns_1(self, tmp_path, capsys):
        rc = steward_command(["status", "--project-dir", str(tmp_path)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no .camflow/steward.json" in err

    def test_alive_steward_prints_alive_and_returns_0(
        self, tmp_path, capsys, monkeypatch,
    ):
        _seed_steward(tmp_path)
        # Conftest blocks real camc; force is_steward_alive to True
        # for this test (we want to check the ALIVE branch).
        monkeypatch.setattr(
            steward_module, "is_steward_alive", lambda *a, **k: True,
        )
        rc = steward_command(["status", "--project-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ALIVE" in out
        assert "steward-7c2a" in out
        # Project line printed.
        assert str(tmp_path) in out
        # flows_witnessed surfaces.
        assert "Flows witnessed: 2" in out

    def test_dead_steward_returns_2(self, tmp_path, capsys, monkeypatch):
        _seed_steward(tmp_path)
        monkeypatch.setattr(
            steward_module, "is_steward_alive", lambda *a, **k: False,
        )
        rc = steward_command(["status", "--project-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 2
        assert "DEAD" in out


# ---- steward kill -------------------------------------------------------


class TestStewardKill:
    def test_no_steward_returns_1(self, tmp_path, capsys):
        rc = steward_command(["kill", "--project-dir", str(tmp_path)])
        assert rc == 1
        assert "no Steward registered" in capsys.readouterr().err

    def test_alive_steward_camc_rm_invoked_with_kill_flag(
        self, tmp_path, monkeypatch, capsys,
    ):
        _seed_steward(tmp_path)
        monkeypatch.setattr(
            steward_module, "is_steward_alive", lambda *a, **k: True,
        )
        captured_args = []

        def fake_rm(agent_id, kill=True):
            captured_args.append((agent_id, kill))
            return True

        monkeypatch.setattr(steward_module, "_camc_rm", fake_rm)

        rc = steward_command(["kill", "--project-dir", str(tmp_path)])
        assert rc == 0
        assert captured_args == [("steward-7c2a", True)]
        # Pointer file deleted; registry status flipped.
        assert not (tmp_path / ".camflow" / STEWARD_POINTER_FILE).exists()
        sw = get_current_steward(tmp_path)
        assert sw is None  # current_steward_id cleared

    def test_dead_steward_clears_pointer_without_camc_call(
        self, tmp_path, monkeypatch, capsys,
    ):
        _seed_steward(tmp_path)
        monkeypatch.setattr(
            steward_module, "is_steward_alive", lambda *a, **k: False,
        )

        called = []
        monkeypatch.setattr(
            steward_module, "_camc_rm",
            lambda *a, **k: called.append(a) or True,
        )

        rc = steward_command(["kill", "--project-dir", str(tmp_path)])
        assert rc == 0
        # camc rm not invoked for an already-dead Steward.
        assert called == []
        assert not (tmp_path / ".camflow" / STEWARD_POINTER_FILE).exists()

    def test_camc_rm_failure_returns_1(
        self, tmp_path, monkeypatch, capsys,
    ):
        _seed_steward(tmp_path)
        monkeypatch.setattr(
            steward_module, "is_steward_alive", lambda *a, **k: True,
        )
        monkeypatch.setattr(
            steward_module, "_camc_rm", lambda *a, **k: False,
        )
        rc = steward_command(["kill", "--project-dir", str(tmp_path)])
        assert rc == 1
        assert "camc rm" in capsys.readouterr().err
        # Pointer NOT deleted on failure (we don't lose the trail).
        assert (tmp_path / ".camflow" / STEWARD_POINTER_FILE).exists()


# ---- steward restart ----------------------------------------------------


class TestStewardRestart:
    def test_restart_kills_then_spawns_with_workflow_arg(
        self, tmp_path, monkeypatch, capsys,
    ):
        _seed_steward(tmp_path)
        monkeypatch.setattr(
            steward_module, "is_steward_alive", lambda *a, **k: True,
        )
        monkeypatch.setattr(
            steward_module, "_camc_rm", lambda *a, **k: True,
        )

        spawned: dict = {}

        def fake_spawn(project_dir, *, workflow_path=None, spawned_by="x"):
            spawned["project_dir"] = project_dir
            spawned["workflow"] = workflow_path
            spawned["spawned_by"] = spawned_by
            return "steward-fresh1"

        monkeypatch.setattr(steward_module, "spawn_steward", fake_spawn)

        wf = tmp_path / "workflow.yaml"
        wf.write_text("foo: {do: cmd echo}\n")

        rc = steward_command(
            [
                "restart",
                "--project-dir", str(tmp_path),
                "--workflow", str(wf),
            ]
        )
        assert rc == 0
        assert spawned["workflow"] == str(wf)
        assert "steward-fresh1" in capsys.readouterr().out

    def test_restart_with_no_existing_steward_just_spawns(
        self, tmp_path, monkeypatch, capsys,
    ):
        spawned = []
        monkeypatch.setattr(
            steward_module, "spawn_steward",
            lambda project_dir, **k: (spawned.append(project_dir)
                                      or "steward-new1"),
        )
        rc = steward_command(["restart", "--project-dir", str(tmp_path)])
        assert rc == 0
        assert spawned == [str(tmp_path)]


# ---- helper consumed by ``camflow status`` ------------------------------


class TestStatusHelper:
    def test_absent_steward(self, tmp_path):
        result = steward_status_for_status_command(str(tmp_path))
        assert result == {"present": False}

    def test_present_alive(self, tmp_path, monkeypatch):
        _seed_steward(tmp_path)
        monkeypatch.setattr(
            "camflow.cli_entry.steward.is_steward_alive",
            lambda *a, **k: True,
        )
        result = steward_status_for_status_command(str(tmp_path))
        assert result["present"] is True
        assert result["alive"] is True
        assert result["agent_id"] == "steward-7c2a"
        assert "ago" in result["age"]

    def test_present_dead(self, tmp_path, monkeypatch):
        _seed_steward(tmp_path)
        monkeypatch.setattr(
            "camflow.cli_entry.steward.is_steward_alive",
            lambda *a, **k: False,
        )
        result = steward_status_for_status_command(str(tmp_path))
        assert result["present"] is True
        assert result["alive"] is False


# ---- argparse hookup ----------------------------------------------------


def test_no_args_prints_help_returns_2(capsys):
    rc = steward_command([])
    assert rc == 2
    # argparse usage line goes to stdout via parser.print_help()
    assert "camflow steward" in capsys.readouterr().out
