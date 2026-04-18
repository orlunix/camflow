"""cmd node runner — direct subprocess execution, no LLM.

Runs shell commands and determines success/fail from exit code.
Captures stdout (last 2000 chars) and stderr (last 500 chars) and
promotes them to state via `state_updates.last_cmd_output` /
`state_updates.last_cmd_stderr` so downstream nodes can template them.
"""

import subprocess

STDOUT_TAIL = 2000
STDERR_TAIL = 500


def _tail(text, n):
    if not text:
        return ""
    return text[-n:]


def run_cmd(command, cwd, timeout=120):
    """Execute a shell command and return a node result dict.

    Args:
        command: Shell command string
        cwd: Working directory
        timeout: Max seconds to wait

    Returns:
        Result dict with status, summary, output, state_updates, error.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        # Grab whatever partial output we got
        stdout_tail = _tail(e.stdout or "", STDOUT_TAIL)
        stderr_tail = _tail(e.stderr or "", STDERR_TAIL)
        return {
            "status": "fail",
            "summary": f"cmd timed out after {timeout}s",
            "output": {
                "exit_code": None,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            },
            "state_updates": {
                "last_cmd_output": stdout_tail,
                "last_cmd_stderr": stderr_tail,
            },
            "error": {"code": "CMD_TIMEOUT", "message": f"timeout after {timeout}s"},
        }
    except FileNotFoundError as e:
        return {
            "status": "fail",
            "summary": f"cmd not found: {e}",
            "output": {"exit_code": None, "stdout_tail": "", "stderr_tail": ""},
            "state_updates": {},
            "error": {"code": "CMD_NOT_FOUND", "message": str(e)},
        }
    except Exception as e:
        return {
            "status": "fail",
            "summary": f"cmd execution error: {e}",
            "output": {"exit_code": None, "stdout_tail": "", "stderr_tail": ""},
            "state_updates": {},
            "error": {"code": "CMD_ERROR", "message": str(e)},
        }

    stdout_tail = _tail(proc.stdout, STDOUT_TAIL)
    stderr_tail = _tail(proc.stderr, STDERR_TAIL)

    if proc.returncode == 0:
        return {
            "status": "success",
            "summary": "cmd succeeded (exit 0)",
            "output": {
                "exit_code": 0,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            },
            "state_updates": {
                "last_cmd_output": stdout_tail,
            },
            "error": None,
        }
    else:
        return {
            "status": "fail",
            "summary": f"cmd failed (exit {proc.returncode})",
            "output": {
                "exit_code": proc.returncode,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            },
            "state_updates": {
                "last_cmd_output": stdout_tail,
                "last_cmd_stderr": stderr_tail,
            },
            "error": {"code": "CMD_FAIL", "exit_code": proc.returncode},
        }
