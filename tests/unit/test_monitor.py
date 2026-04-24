"""Unit tests for camflow.engine.monitor.

Covers:
  * HeartbeatThread writes the expected JSON shape and updates on tick.
  * load_heartbeat + is_stale play nice with both fresh and stale files.
  * is_process_alive returns True for self, False for a guaranteed-dead pid.
  * EngineLock blocks a second acquirer with the right holder pid.
  * EngineLock auto-heals stale lock files (dead holder pid).
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path

import pytest

from camflow.engine.monitor import (
    DEFAULT_STALE_THRESHOLD,
    EngineLock,
    EngineLockError,
    HeartbeatThread,
    _is_lock_stale,
    _parse_iso,
    _utcnow_iso,
    heartbeat_path,
    is_process_alive,
    is_stale,
    load_heartbeat,
    lock_path,
    write_heartbeat,
)

# A pid that (on Linux with default pid_max) cannot exist.
DEAD_PID = 4194301


# ---- time helpers ------------------------------------------------------


class TestTimeHelpers:
    def test_utcnow_iso_parses_back(self):
        ts = _utcnow_iso()
        parsed = _parse_iso(ts)
        assert parsed is not None
        # Should be within a second of "now".
        assert abs(time.time() - parsed) < 2

    def test_parse_iso_bad_input(self):
        assert _parse_iso("") is None
        assert _parse_iso("not-a-timestamp") is None
        assert _parse_iso(None) is None


# ---- heartbeat I/O -----------------------------------------------------


class TestHeartbeatIO:
    def test_write_and_load_roundtrip(self, tmp_path):
        path = heartbeat_path(tmp_path)
        write_heartbeat(path, {"pid": 42, "timestamp": _utcnow_iso(), "pc": "n1"})
        loaded = load_heartbeat(path)
        assert loaded["pid"] == 42
        assert loaded["pc"] == "n1"

    def test_load_missing_returns_none(self, tmp_path):
        assert load_heartbeat(heartbeat_path(tmp_path)) is None

    def test_is_stale_missing_is_stale(self):
        assert is_stale(None) is True

    def test_is_stale_fresh(self):
        hb = {"timestamp": _utcnow_iso()}
        assert is_stale(hb, threshold=60) is False

    def test_is_stale_old(self):
        # 10 minutes ago
        hb = {"timestamp": "2020-01-01T00:00:00Z"}
        assert is_stale(hb) is True

    def test_is_stale_malformed_ts(self):
        hb = {"timestamp": "garbage"}
        assert is_stale(hb) is True


# ---- process liveness --------------------------------------------------


class TestProcessLiveness:
    def test_self_is_alive(self):
        assert is_process_alive(os.getpid()) is True

    def test_none_is_not_alive(self):
        assert is_process_alive(None) is False

    def test_implausible_pid_is_not_alive(self):
        # 2**22 is well outside the Linux default pid range; if anything
        # occupies it we'll just have a flaky test on one machine.
        assert is_process_alive(4194301) is False


# ---- heartbeat thread --------------------------------------------------


class TestHeartbeatThread:
    def test_writes_snapshot_of_current_state(self, tmp_path):
        state = {"pc": "alpha", "iteration": 1, "current_agent_id": "abc123"}
        # short interval so the test is fast; stop() cuts the loop anyway
        thread = HeartbeatThread(str(tmp_path), lambda: state, interval=60)
        thread.start()
        try:
            # Wait for initial write
            deadline = time.time() + 2
            path = heartbeat_path(tmp_path)
            while not os.path.exists(path) and time.time() < deadline:
                time.sleep(0.05)
            assert os.path.exists(path), "heartbeat file not written"
            data = json.loads(open(path).read())
            assert data["pid"] == os.getpid()
            assert data["pc"] == "alpha"
            assert data["iteration"] == 1
            assert data["agent_id"] == "abc123"
            assert "timestamp" in data
            assert data["uptime_seconds"] >= 0
        finally:
            thread.stop()
            thread.join(timeout=2)

    def test_stop_removes_heartbeat_file(self, tmp_path):
        state = {"pc": "x"}
        thread = HeartbeatThread(str(tmp_path), lambda: state, interval=60)
        thread.start()
        path = heartbeat_path(tmp_path)
        deadline = time.time() + 2
        while not os.path.exists(path) and time.time() < deadline:
            time.sleep(0.05)
        assert os.path.exists(path)
        thread.stop()
        thread.join(timeout=2)
        assert not os.path.exists(path)

    def test_getter_returning_none_does_not_crash(self, tmp_path):
        thread = HeartbeatThread(str(tmp_path), lambda: None, interval=60)
        thread.start()
        try:
            deadline = time.time() + 2
            path = heartbeat_path(tmp_path)
            while not os.path.exists(path) and time.time() < deadline:
                time.sleep(0.05)
            assert os.path.exists(path)
            data = json.loads(open(path).read())
            assert data["pc"] is None
        finally:
            thread.stop()
            thread.join(timeout=2)


# ---- lock --------------------------------------------------------------


class TestEngineLock:
    def test_acquire_and_release(self, tmp_path):
        with EngineLock(str(tmp_path)) as lock:
            # Lock file exists and contains our pid.
            with open(lock.path) as f:
                assert int(f.read().strip()) == os.getpid()
        # Released → file removed.
        assert not os.path.exists(lock_path(tmp_path))

    def test_second_acquirer_blocks(self, tmp_path):
        first = EngineLock(str(tmp_path))
        first.acquire()
        try:
            second = EngineLock(str(tmp_path))
            with pytest.raises(EngineLockError) as exc:
                second.acquire()
            assert exc.value.holder_pid == os.getpid()
        finally:
            first.release()

    def test_reacquire_after_release(self, tmp_path):
        first = EngineLock(str(tmp_path))
        first.acquire()
        first.release()
        # Now a fresh lock should succeed.
        with EngineLock(str(tmp_path)):
            pass


# ---- stale-lock recovery ------------------------------------------------


def _plant_blocking_lock(tmp_path, pid_to_write: int | None):
    """Plant a lock file whose flock is held by a zombie fd.

    Returns the zombie fd so the caller can close it in teardown.
    Simulates the post-SIGKILL / NFS-stuck scenario: flock is still
    held at the kernel level, but the pid recorded in the file points
    at a dead process.
    """
    p = lock_path(tmp_path)
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    fd = open(p, "a+", encoding="utf-8")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    if pid_to_write is not None:
        fd.seek(0)
        fd.truncate()
        fd.write(str(pid_to_write))
        fd.flush()
    return fd


def _release_zombie(fd):
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fd.close()
    except OSError:
        pass


class TestIsLockStale:
    """Unit-level coverage of the decision function itself."""

    def test_missing_holder_is_not_stale(self, tmp_path):
        # Empty / unparseable pid → an acquirer is mid-write. Respect it.
        assert _is_lock_stale(None, str(tmp_path)) is False

    def test_live_holder_is_not_stale(self, tmp_path):
        assert _is_lock_stale(os.getpid(), str(tmp_path)) is False

    def test_dead_holder_no_heartbeat_is_stale(self, tmp_path):
        assert _is_lock_stale(DEAD_PID, str(tmp_path)) is True

    def test_dead_holder_with_live_heartbeat_is_not_stale(self, tmp_path):
        # Weird case: lock file has a wrong/stale pid, but the live
        # heartbeat says a different process is alive. Don't steal.
        write_heartbeat(
            heartbeat_path(tmp_path),
            {"pid": os.getpid(), "timestamp": _utcnow_iso()},
        )
        assert _is_lock_stale(DEAD_PID, str(tmp_path)) is False

    def test_dead_holder_with_fresh_dead_heartbeat_is_not_stale(self, tmp_path):
        # Holder just died: heartbeat pid matches lock pid and is recent.
        # Give the engine a grace window before auto-cleaning.
        write_heartbeat(
            heartbeat_path(tmp_path),
            {"pid": DEAD_PID, "timestamp": _utcnow_iso()},
        )
        assert _is_lock_stale(DEAD_PID, str(tmp_path)) is False

    def test_dead_holder_with_stale_dead_heartbeat_is_stale(self, tmp_path):
        write_heartbeat(
            heartbeat_path(tmp_path),
            {"pid": DEAD_PID, "timestamp": "2020-01-01T00:00:00Z"},
        )
        assert _is_lock_stale(DEAD_PID, str(tmp_path)) is True


class TestEngineLockStaleRecovery:
    """End-to-end: acquire() recovers from an abandoned lock file."""

    def test_steals_lock_with_dead_holder_no_heartbeat(self, tmp_path):
        zombie = _plant_blocking_lock(tmp_path, DEAD_PID)
        try:
            lock = EngineLock(str(tmp_path))
            lock.acquire()
            try:
                # Fresh lock file now records our pid.
                with open(lock.path) as f:
                    assert int(f.read().strip()) == os.getpid()
            finally:
                lock.release()
        finally:
            _release_zombie(zombie)

    def test_steals_lock_with_dead_holder_and_stale_heartbeat(self, tmp_path):
        zombie = _plant_blocking_lock(tmp_path, DEAD_PID)
        write_heartbeat(
            heartbeat_path(tmp_path),
            {"pid": DEAD_PID, "timestamp": "2020-01-01T00:00:00Z"},
        )
        try:
            with EngineLock(str(tmp_path)):
                pass  # acquire+release without raising is the assertion
        finally:
            _release_zombie(zombie)

    def test_refuses_to_steal_when_holder_is_alive(self, tmp_path):
        zombie = _plant_blocking_lock(tmp_path, os.getpid())
        try:
            lock = EngineLock(str(tmp_path))
            with pytest.raises(EngineLockError) as exc:
                lock.acquire()
            assert exc.value.holder_pid == os.getpid()
        finally:
            _release_zombie(zombie)

    def test_refuses_to_steal_on_empty_pid_file(self, tmp_path):
        # File exists + flocked, but pid wasn't written yet (mid-acquire).
        zombie = _plant_blocking_lock(tmp_path, None)
        try:
            lock = EngineLock(str(tmp_path))
            with pytest.raises(EngineLockError) as exc:
                lock.acquire()
            assert exc.value.holder_pid is None
        finally:
            _release_zombie(zombie)
