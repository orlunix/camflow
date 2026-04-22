"""Unit tests for `camflow stop`.

These cover the three paths the user actually hits:
  * no engine known → exit 2, no signal sent
  * pid dead (stale heartbeat) → cleanup stale files, exit 0
  * live process, SIGTERM delivered and handled → exit 0

We exercise a real subprocess for the "live" case because that is
the only way to prove we actually signal something — mocking
``os.kill`` would defeat the point.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from camflow.cli_entry.stop import stop_command
from camflow.engine.monitor import (
    _utcnow_iso,
    heartbeat_path,
    lock_path,
    write_heartbeat,
)


def _args(project_dir, *, force=False, timeout=5):
    return argparse.Namespace(
        project_dir=str(project_dir), force=force, timeout=timeout,
    )


class TestStopDiscovery:
    def test_no_heartbeat_no_pidfile_returns_2(self, tmp_path, capsys):
        rc = stop_command(_args(tmp_path))
        assert rc == 2
        err = capsys.readouterr().err
        assert "No engine pid found" in err

    def test_dead_pid_is_cleaned_up(self, tmp_path, capsys):
        # Heartbeat points to a guaranteed-dead pid.
        write_heartbeat(
            heartbeat_path(tmp_path),
            {
                "pid": 4194301,
                "timestamp": _utcnow_iso(),
                "pc": "build",
            },
        )
        # Also a lock file to verify cleanup.
        Path(lock_path(tmp_path)).write_text("4194301")
        rc = stop_command(_args(tmp_path))
        assert rc == 0
        assert not os.path.exists(heartbeat_path(tmp_path))
        assert not os.path.exists(lock_path(tmp_path))
        out = capsys.readouterr().out
        assert "not running" in out

    def test_falls_back_to_pidfile_when_no_heartbeat(self, tmp_path, capsys):
        # pidfile only — no heartbeat (e.g. engine already cleaned up on exit).
        pidfile = tmp_path / ".camflow" / "engine.pid"
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text("4194301")
        rc = stop_command(_args(tmp_path))
        assert rc == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
class TestStopLiveProcess:
    """Exercise a real detached child that `camflow stop` signals.

    The child is double-forked (init becomes its parent) so the test
    process doesn't have to reap it — otherwise ``os.kill(pid, 0)``
    keeps returning success against the zombie even after it exits.
    """

    def _spawn_detached(self, tmp_path, body: str, timeout: int = 30) -> int:
        """Double-fork a child that writes its pid to heartbeat and runs
        ``body`` (plus a safety timeout). Returns the grandchild pid."""
        pid_path = tmp_path / ".camflow" / "sleeper.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        # The wrapper writes the grandchild's pid to a file so we can
        # read it back after the launcher exits.
        script = textwrap.dedent(f"""
            import os, sys, signal, time
            pid = os.fork()
            if pid > 0:
                os._exit(0)
            os.setsid()
            pid = os.fork()
            if pid > 0:
                os._exit(0)
            # Grandchild
            with open({str(pid_path)!r}, "w") as f:
                f.write(str(os.getpid()))
            {body}
            time.sleep({timeout})
            os._exit(0)
        """)
        rc = subprocess.run([sys.executable, "-c", script])
        assert rc.returncode == 0
        # Wait for the pidfile write.
        deadline = time.time() + 2
        while not pid_path.exists() and time.time() < deadline:
            time.sleep(0.02)
        pid = int(pid_path.read_text().strip())
        write_heartbeat(
            heartbeat_path(tmp_path),
            {"pid": pid, "timestamp": _utcnow_iso(), "pc": "build"},
        )
        # Let the grandchild settle into its sleep loop.
        time.sleep(0.1)
        return pid

    def _kill_if_alive(self, pid: int) -> None:
        """Safety net: reap the grandchild in case the test left it."""
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def test_sigterm_delivered_and_process_exits(self, tmp_path):
        pid = self._spawn_detached(tmp_path, body="")
        try:
            rc = stop_command(_args(tmp_path, timeout=5))
            assert rc == 0
            # Grandchild should be gone — init reaped it.
            time.sleep(0.1)
            from camflow.engine.monitor import is_process_alive
            assert not is_process_alive(pid)
            # Heartbeat was cleaned up.
            assert not os.path.exists(heartbeat_path(tmp_path))
        finally:
            self._kill_if_alive(pid)

    def test_force_sends_sigkill(self, tmp_path):
        # Grandchild ignores SIGTERM so only --force (SIGKILL) can kill it.
        pid = self._spawn_detached(
            tmp_path,
            body="signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        )
        try:
            rc = stop_command(_args(tmp_path, force=True, timeout=5))
            assert rc == 0
            time.sleep(0.1)
            from camflow.engine.monitor import is_process_alive
            assert not is_process_alive(pid)
        finally:
            self._kill_if_alive(pid)
