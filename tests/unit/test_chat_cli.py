"""Unit tests for ``camflow chat`` CLI."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from camflow.cli_entry import chat as chat_module
from camflow.cli_entry.chat import chat_command
from camflow.steward.spawn import STEWARD_POINTER_FILE


def _seed_pointer(project_dir: Path, agent_id: str = "steward-7c2a"):
    p = Path(project_dir) / ".camflow" / STEWARD_POINTER_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"agent_id": agent_id, "name": agent_id}))


# ---- one-shot send ------------------------------------------------------


class TestChatSend:
    def test_no_steward_returns_1(self, tmp_path, capsys):
        rc = chat_command(["--project-dir", str(tmp_path), "hi"])
        assert rc == 1
        assert "no Steward" in capsys.readouterr().err

    def test_dead_steward_returns_1(self, tmp_path, capsys, monkeypatch):
        _seed_pointer(tmp_path)
        monkeypatch.setattr(
            chat_module, "is_steward_alive", lambda *a, **k: False,
        )
        rc = chat_command(["--project-dir", str(tmp_path), "hi"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "is dead" in err
        assert "camflow steward restart" in err

    def test_alive_send_invokes_camc_send(
        self, tmp_path, monkeypatch, capsys,
    ):
        _seed_pointer(tmp_path)
        monkeypatch.setattr(
            chat_module, "is_steward_alive", lambda *a, **k: True,
        )
        sent = []

        def fake_send(agent_id, message):
            sent.append((agent_id, message))
            return True

        monkeypatch.setattr(chat_module, "_camc_send", fake_send)

        rc = chat_command(
            ["--project-dir", str(tmp_path), "现在状况?"]
        )
        assert rc == 0
        assert sent == [("steward-7c2a", "现在状况?")]
        out = capsys.readouterr().out
        assert "sent to steward-7c2a" in out

    def test_send_failure_returns_1(self, tmp_path, monkeypatch, capsys):
        _seed_pointer(tmp_path)
        monkeypatch.setattr(
            chat_module, "is_steward_alive", lambda *a, **k: True,
        )
        monkeypatch.setattr(
            chat_module, "_camc_send", lambda *a, **k: False,
        )
        rc = chat_command(["--project-dir", str(tmp_path), "hello"])
        assert rc == 1
        assert "camc send" in capsys.readouterr().err

    def test_message_from_stdin(
        self, tmp_path, monkeypatch, capsys,
    ):
        _seed_pointer(tmp_path)
        monkeypatch.setattr(
            chat_module, "is_steward_alive", lambda *a, **k: True,
        )
        sent = []
        monkeypatch.setattr(
            chat_module, "_camc_send",
            lambda aid, msg: sent.append((aid, msg)) or True,
        )

        monkeypatch.setattr(
            chat_module.sys, "stdin",
            io.StringIO("piped message\n"),
        )
        rc = chat_command(["--project-dir", str(tmp_path)])
        assert rc == 0
        assert sent == [("steward-7c2a", "piped message")]

    def test_empty_stdin_returns_1(
        self, tmp_path, monkeypatch, capsys,
    ):
        _seed_pointer(tmp_path)
        monkeypatch.setattr(
            chat_module, "is_steward_alive", lambda *a, **k: True,
        )
        monkeypatch.setattr(
            chat_module.sys, "stdin", io.StringIO(""),
        )
        rc = chat_command(["--project-dir", str(tmp_path)])
        assert rc == 1
        assert "empty message" in capsys.readouterr().err


# ---- history ------------------------------------------------------------


class TestChatHistory:
    def test_no_steward_returns_1(self, tmp_path, capsys):
        rc = chat_command(["--project-dir", str(tmp_path), "--history"])
        assert rc == 1

    def test_no_events_prints_marker(self, tmp_path, capsys):
        _seed_pointer(tmp_path)
        rc = chat_command(["--project-dir", str(tmp_path), "--history"])
        assert rc == 0
        assert "no events recorded" in capsys.readouterr().out

    def test_history_prints_recent_events(self, tmp_path, capsys):
        _seed_pointer(tmp_path)
        events_path = (
            Path(tmp_path) / ".camflow" / "steward-events.jsonl"
        )
        events_path.write_text(
            "\n".join(
                json.dumps(e) for e in (
                    {
                        "type": "node_done", "ts": "2026-04-26T10:00:00Z",
                        "flow_id": "flow_001", "node": "build",
                        "summary": "compiled",
                    },
                    {
                        "type": "node_failed", "ts": "2026-04-26T10:01:00Z",
                        "flow_id": "flow_001", "node": "test",
                        "status": "fail",
                    },
                )
            )
            + "\n"
        )
        rc = chat_command(
            [
                "--project-dir", str(tmp_path),
                "--history",
                "--tail", "5",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "node_done" in out
        assert "node_failed" in out
        assert "flow_001" in out

    def test_history_respects_tail(self, tmp_path, capsys):
        _seed_pointer(tmp_path)
        events_path = (
            Path(tmp_path) / ".camflow" / "steward-events.jsonl"
        )
        events_path.write_text(
            "\n".join(
                json.dumps({"type": f"node_{i}", "ts": f"t{i}"})
                for i in range(10)
            )
            + "\n"
        )
        rc = chat_command(
            [
                "--project-dir", str(tmp_path),
                "--history",
                "--tail", "3",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        # Most recent three events are 7, 8, 9.
        assert "node_9" in out
        assert "node_8" in out
        assert "node_7" in out
        assert "node_0" not in out
