"""Unit tests for backend.cam.cmd_runner."""

from camflow.backend.cam.cmd_runner import _coerce_text, _tail, run_cmd


class TestCoerceText:
    def test_bytes_are_decoded(self):
        assert _coerce_text(b"hi") == "hi"

    def test_bytearray_is_decoded(self):
        assert _coerce_text(bytearray(b"ok")) == "ok"

    def test_invalid_utf8_bytes_are_replaced_not_raised(self):
        out = _coerce_text(b"partial\xff")
        assert isinstance(out, str)
        assert "partial" in out

    def test_none_becomes_empty_str(self):
        assert _coerce_text(None) == ""

    def test_str_passes_through(self):
        assert _coerce_text("already text") == "already text"


def test_tail_survives_bytes():
    """Regression: TimeoutExpired.stdout is bytes even when the original
    subprocess.run was text=True — _tail used to crash with
    'bytes is not subscriptable' style errors. Now it coerces first."""
    assert _tail(b"abcdefghij", 3) == "hij"


def test_success(tmp_path):
    r = run_cmd("echo hello", str(tmp_path))
    assert r["status"] == "success"
    assert r["output"]["exit_code"] == 0
    assert "hello" in r["output"]["stdout_tail"]
    assert "hello" in r["state_updates"]["last_cmd_output"]
    assert r["error"] is None


def test_failure(tmp_path):
    r = run_cmd("false", str(tmp_path))
    assert r["status"] == "fail"
    assert r["output"]["exit_code"] == 1
    assert r["error"]["code"] == "CMD_FAIL"


def test_stderr_captured(tmp_path):
    r = run_cmd("echo boom >&2; exit 1", str(tmp_path))
    assert r["status"] == "fail"
    assert "boom" in r["output"]["stderr_tail"]
    assert "boom" in r["state_updates"]["last_cmd_stderr"]


def test_stdout_truncation(tmp_path):
    # Print 5000 chars; tail is capped at 2000
    r = run_cmd('python3 -c "print(\'x\'*5000)"', str(tmp_path))
    assert r["status"] == "success"
    assert len(r["output"]["stdout_tail"]) == 2000
    assert len(r["state_updates"]["last_cmd_output"]) == 2000


def test_timeout(tmp_path):
    r = run_cmd("sleep 10", str(tmp_path), timeout=1)
    assert r["status"] == "fail"
    assert r["error"]["code"] == "CMD_TIMEOUT"
