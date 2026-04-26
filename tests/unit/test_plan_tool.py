"""Unit tests for the ``camflow plan-tool`` subcommands."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from camflow.cli_entry import plan_tool as plan_tool_module
from camflow.cli_entry.plan_tool import plan_tool_command


VALID_WORKFLOW_YAML = """\
build:
  do: cmd echo build
  next: done
done:
  do: cmd echo done
"""


# ---- validate -----------------------------------------------------------


class TestValidate:
    def test_missing_file_reports_error_and_exits_1(
        self, tmp_path, capsys,
    ):
        rc = plan_tool_command(["validate", str(tmp_path / "missing.yaml")])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert any("file not found" in e for e in out["errors"])

    def test_invalid_yaml_reports_error(self, tmp_path, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("foo: [unterminated\n")
        rc = plan_tool_command(["validate", str(bad)])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert any("invalid YAML" in e for e in out["errors"])

    def test_dsl_failure_reports_error(self, tmp_path, capsys):
        # Reference an undefined node — DSL validation rejects this.
        bad = tmp_path / "wf.yaml"
        bad.write_text("a:\n  do: cmd echo\n  next: nonexistent\n")
        rc = plan_tool_command(["validate", str(bad)])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert out["errors"]

    def test_valid_workflow_returns_ok(self, tmp_path, capsys):
        wf = tmp_path / "wf.yaml"
        wf.write_text(VALID_WORKFLOW_YAML)
        rc = plan_tool_command(["validate", str(wf)])
        out = json.loads(capsys.readouterr().out)
        # plan-quality may emit warnings (e.g. no verify), but no errors.
        assert out["ok"] is True
        assert out["errors"] == []
        # Quality warnings are non-fatal — exit 0.
        assert rc == 0


# ---- write --------------------------------------------------------------


class TestWrite:
    def test_writes_yaml_atomically(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / ".camflow" / "workflow.yaml"
        monkeypatch.setattr(
            plan_tool_module.sys, "stdin",
            io.StringIO(VALID_WORKFLOW_YAML),
        )
        rc = plan_tool_command(
            ["write", "--project-dir", str(tmp_path), str(target)]
        )
        assert rc == 0
        assert target.exists()
        # Round-trip
        text = target.read_text(encoding="utf-8")
        assert "build:" in text
        # No tmp leftover in the parent.
        leftovers = [
            p for p in target.parent.iterdir()
            if p.name.startswith(target.name + ".tmp.")
        ]
        assert leftovers == []

    def test_refuses_path_outside_camflow_dir(
        self, tmp_path, monkeypatch, capsys,
    ):
        outside = tmp_path / "evil.yaml"
        monkeypatch.setattr(
            plan_tool_module.sys, "stdin",
            io.StringIO(VALID_WORKFLOW_YAML),
        )
        rc = plan_tool_command(
            ["write", "--project-dir", str(tmp_path), str(outside)]
        )
        assert rc == 1
        assert not outside.exists()
        assert "must live under" in capsys.readouterr().err

    def test_refuses_invalid_yaml(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / ".camflow" / "wf.yaml"
        monkeypatch.setattr(
            plan_tool_module.sys, "stdin",
            io.StringIO("foo: [unterminated\n"),
        )
        rc = plan_tool_command(
            ["write", "--project-dir", str(tmp_path), str(target)]
        )
        assert rc == 1
        assert not target.exists()

    def test_refuses_dsl_invalid(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / ".camflow" / "wf.yaml"
        monkeypatch.setattr(
            plan_tool_module.sys, "stdin",
            io.StringIO("a:\n  do: cmd echo\n  next: ghost\n"),
        )
        rc = plan_tool_command(
            ["write", "--project-dir", str(tmp_path), str(target)]
        )
        assert rc == 1
        assert not target.exists()
        assert "DSL validation" in capsys.readouterr().err

    def test_refuses_empty_stdin(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / ".camflow" / "wf.yaml"
        monkeypatch.setattr(plan_tool_module.sys, "stdin", io.StringIO(""))
        rc = plan_tool_command(
            ["write", "--project-dir", str(tmp_path), str(target)]
        )
        assert rc == 1
        assert "stdin is empty" in capsys.readouterr().err


# ---- argparse hookup ----------------------------------------------------


def test_no_args_prints_help_returns_2(capsys):
    rc = plan_tool_command([])
    assert rc == 2
    assert "camflow plan-tool" in capsys.readouterr().out
