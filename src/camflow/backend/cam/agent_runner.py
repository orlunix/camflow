"""Agent node runner — executes nodes via camc.

Dual-signal completion detection:
  PRIMARY:   existence of `.camflow/node-result.json` (file-first)
  SECONDARY: camc --json status <id> reports terminal state

Lifecycle per agent node:
  1. Clear old result file
  2. Write full prompt to `.camflow/node-prompt.txt` (tmux paste breaks on long prompts)
  3. camc run --name "camflow-<node>" --path <dir> --auto-exit "Read .camflow/node-prompt.txt..."
  4. Poll both signals until one fires (or timeout)
  5. Read node-result.json — may be missing if agent crashed; caller handles that
  6. camc rm <id> --force
"""

import json
import os
import re
import subprocess
import time

from camflow.backend.cam.result_reader import clear_node_result, read_node_result


PROMPT_FILE = ".camflow/node-prompt.txt"
RESULT_FILE = ".camflow/node-result.json"
CAPTURE_LINES = 50  # lines to grab via `camc capture` on crash


# ---- subprocess helpers --------------------------------------------------


def _parse_agent_id(output):
    """Extract agent ID from `camc run` stdout."""
    m = re.search(r"agent\s+([0-9a-f]{6,12})", output)
    if m:
        return m.group(1)
    m = re.search(r"ID:\s+([0-9a-f]{6,12})", output)
    if m:
        return m.group(1)
    return None


def _get_agent_status(agent_id):
    """Return status dict via `camc --json status <id>`, or None if not found."""
    try:
        proc = subprocess.run(
            ["camc", "--json", "status", agent_id],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None


def _capture_screen(agent_id, lines=CAPTURE_LINES):
    """Grab last N lines of agent screen via `camc capture`. Returns empty string on failure."""
    try:
        proc = subprocess.run(
            ["camc", "capture", agent_id, "-n", str(lines)],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass
    return ""


def _cleanup_agent(agent_id):
    """Best-effort remove agent from camc registry."""
    try:
        subprocess.run(
            ["camc", "rm", agent_id, "--force"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _send_key(agent_id, key):
    """Send a special key (e.g. Enter) to the agent's tmux session."""
    try:
        subprocess.run(
            ["camc", "key", agent_id, "--key", key],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _kick_prompt(agent_id, max_wait=30, poll=1):
    """Submit the queued prompt by sending Enter once the TUI is ready.

    `camc run "<prompt>"` pastes the prompt into the Claude Code TUI input
    box but does NOT submit it (no trailing newline). Without this kick the
    agent sits at the prompt forever.

    Strategy:
      1. Poll the screen until we see the prompt char (❯ / > / ›) on the
         last 5 lines AND text after it (the queued prompt is visible).
      2. Send Enter once to submit.
      3. If 30 s pass without seeing the prompt, send Enter anyway
         (best-effort fallback).

    Idempotent in the sense that if the agent has already moved past the
    initial prompt, the extra Enter is harmless.
    """
    deadline = time.time() + max_wait
    submitted = False
    while time.time() < deadline:
        time.sleep(poll)
        screen = _capture_screen(agent_id, lines=20)
        if not screen:
            continue
        last_lines = [ln for ln in screen.strip().split("\n")[-5:] if ln.strip()]
        # Look for a line that begins with the TUI prompt char and has text after it
        for ln in last_lines:
            stripped = ln.lstrip()
            if not stripped:
                continue
            if stripped[0] in ("❯", ">", "›") and len(stripped) > 2:
                _send_key(agent_id, "Enter")
                submitted = True
                break
        if submitted:
            return
    # Fallback: send Enter anyway in case the prompt is there but our
    # detection failed.
    _send_key(agent_id, "Enter")


# ---- core polling --------------------------------------------------------


def _wait_for_completion(agent_id, result_path, timeout, poll_interval):
    """Poll both file and camc status until one signals completion.

    Returns:
        (reason, detail)
        reason is one of:
          "file_appeared"      — primary signal; result file exists
          "status_terminal"    — camc status is completed/stopped/failed
          "status_idle_stable" — agent reported idle for 3 consecutive polls (flaky signal)
          "agent_gone"         — camc says agent no longer in registry
          "timeout"            — neither signal fired within timeout
    """
    deadline = time.time() + timeout
    idle_streak = 0

    while time.time() < deadline:
        time.sleep(poll_interval)

        # PRIMARY: result file exists
        if os.path.exists(result_path):
            time.sleep(1)  # small grace for fsync to complete
            return ("file_appeared", None)

        # SECONDARY: camc status
        status_info = _get_agent_status(agent_id)
        if status_info is None:
            return ("agent_gone", None)

        status = status_info.get("status")
        state = status_info.get("state")

        if status in ("completed", "stopped", "failed"):
            time.sleep(2)  # grace for result file to appear after status flip
            return ("status_terminal", status)

        if state == "idle":
            idle_streak += 1
            if idle_streak >= 3:
                return ("status_idle_stable", None)
        else:
            idle_streak = 0

    return ("timeout", None)


# ---- public API ----------------------------------------------------------


def start_agent(node_id, prompt, project_dir):
    """Start a camc agent for a node.

    Writes the prompt to `.camflow/node-prompt.txt` (because tmux paste
    corrupts long multiline prompts), then runs camc with a short instruction.

    Returns:
        agent_id (str) on success, or raises RuntimeError with a useful message.
    """
    camflow_dir = os.path.join(project_dir, ".camflow")
    os.makedirs(camflow_dir, exist_ok=True)

    clear_node_result(project_dir)

    prompt_path = os.path.join(camflow_dir, "node-prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    short_prompt = (
        "Read the file .camflow/node-prompt.txt and follow ALL instructions inside it exactly. "
        "The file contains your complete task and output requirements."
    )

    try:
        proc = subprocess.run(
            [
                "camc", "run",
                "--name", f"camflow-{node_id}",
                "--path", project_dir,
                "--auto-exit",
                short_prompt,
            ],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        raise RuntimeError(f"camc run failed to launch: {e}")

    if proc.returncode != 0:
        raise RuntimeError(
            f"camc run returned {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )

    agent_id = _parse_agent_id(proc.stdout)
    if not agent_id:
        raise RuntimeError(f"could not parse agent ID from camc output: {proc.stdout[:500]}")

    # camc pastes the prompt into the TUI input box but doesn't submit it;
    # send Enter once the TUI is ready so the agent actually starts working.
    _kick_prompt(agent_id)

    return agent_id


def finalize_agent(agent_id, completion_signal, project_dir, cleanup=True):
    """Read the node result, optionally grab screen capture on failure, and cleanup.

    Returns:
        Node result dict (possibly a synthesized fail result).
    """
    result_path = os.path.join(project_dir, RESULT_FILE)
    result = read_node_result(project_dir)

    # If result file missing, classify the failure based on completion_signal
    if result.get("status") == "fail" and "did not write result" in (result.get("summary") or ""):
        if completion_signal == "timeout":
            capture = _capture_screen(agent_id)
            result = {
                "status": "fail",
                "summary": f"agent timed out, no result file",
                "state_updates": {},
                "output": {"screen_capture": capture[-2000:] if capture else ""},
                "error": {"code": "AGENT_TIMEOUT", "completion_signal": completion_signal},
            }
        elif completion_signal == "agent_gone":
            capture = _capture_screen(agent_id)  # likely empty, but try
            result = {
                "status": "fail",
                "summary": "agent vanished before writing result",
                "state_updates": {},
                "output": {"screen_capture": capture[-2000:] if capture else ""},
                "error": {"code": "AGENT_CRASH", "completion_signal": completion_signal},
            }
        elif completion_signal == "status_idle_stable":
            capture = _capture_screen(agent_id)
            result = {
                "status": "fail",
                "summary": "agent idle but no result (camc status may be stale)",
                "state_updates": {},
                "output": {"screen_capture": capture[-2000:] if capture else ""},
                "error": {"code": "AGENT_CRASH", "completion_signal": completion_signal},
            }
        elif completion_signal == "status_terminal":
            capture = _capture_screen(agent_id)
            result = {
                "status": "fail",
                "summary": "agent terminated without writing result file",
                "state_updates": {},
                "output": {"screen_capture": capture[-2000:] if capture else ""},
                "error": {"code": "PARSE_ERROR", "completion_signal": completion_signal},
            }

    if cleanup:
        if completion_signal == "timeout":
            # Try graceful stop first
            try:
                subprocess.run(["camc", "stop", agent_id], capture_output=True, timeout=5)
            except Exception:
                pass
            time.sleep(1)
        _cleanup_agent(agent_id)

    return result


def run_agent(node_id, prompt, project_dir, timeout=600, poll_interval=5):
    """Execute an agent node end-to-end.

    Args:
        node_id: workflow node name (used for agent naming)
        prompt: complete prompt (caller may use build_prompt or build_retry_prompt)
        project_dir: working directory for the agent
        timeout: max seconds to wait for completion
        poll_interval: seconds between polls

    Returns:
        (result_dict, agent_id, completion_signal)
        - result_dict conforms to node-result schema (possibly synthesized fail)
        - agent_id is the camc agent ID (or None if launch failed)
        - completion_signal is one of: file_appeared, status_terminal,
          status_idle_stable, agent_gone, timeout, launch_failed
    """
    try:
        agent_id = start_agent(node_id, prompt, project_dir)
    except RuntimeError as e:
        return (
            {
                "status": "fail",
                "summary": f"failed to launch agent: {e}",
                "state_updates": {},
                "error": {"code": "CAMC_ERROR", "message": str(e)},
            },
            None,
            "launch_failed",
        )

    result_path = os.path.join(project_dir, RESULT_FILE)
    completion_signal, _detail = _wait_for_completion(
        agent_id, result_path, timeout, poll_interval
    )
    result = finalize_agent(agent_id, completion_signal, project_dir)
    return (result, agent_id, completion_signal)
