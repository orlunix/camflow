"""Engine-side drain for ``.camflow/control.jsonl``.

The Steward (and human operators) push verb commands into the queue
via ``camflow ctl <verb>``. This module reads the queue at the top of
each engine tick, applies each command's effect on engine state, and
truncates the queue. Failures don't crash the engine — verb effects
are observability + control, not correctness.

Verbs handled (Phase B):
  pause          flip state.status running → waiting
  resume         flip state.status waiting → running
  kill-worker    SIGTERM state.current_agent_id (with 30s cooldown
                 per (flow_id, node_id, agent_id))
  spawn          force state.pc to args.node (override scheduler)
                 + record args.brief in state.spawned_brief
  skip           mark current pc completed, advance to next via
                 transition resolver

Each drained command emits a ``kind=control_resolution`` trace entry
(actor=engine, resolution="executed" / "skipped:<reason>") so the
audit trail shows BOTH the user's intent (control_command from ctl)
and the engine's action (this).

The 30s kill-worker cooldown lives in this module's process-local
state. That's fine because there's only ever one engine per project
and the lock guarantees that.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from camflow.backend.cam.tracer import build_event_entry
from camflow.backend.persistence import append_trace_atomic


CONTROL_QUEUE = "control.jsonl"
KILL_COOLDOWN_SECONDS = 30


# (flow_id, node_id, agent_id) -> last_kill_unix_ts
_KILL_COOLDOWN: dict[tuple[str | None, str | None, str | None], float] = {}


def _control_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / ".camflow" / CONTROL_QUEUE


def _trace_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / ".camflow" / "trace.log"


def _emit_resolution_trace(
    project_dir: str | os.PathLike,
    *,
    verb: str,
    args: dict[str, Any],
    actor: str,
    flow_id: str | None,
    resolution: str,
    detail: str | None = None,
) -> None:
    try:
        append_trace_atomic(
            str(_trace_path(project_dir)),
            build_event_entry(
                "control_resolution",
                actor="engine",
                flow_id=flow_id,
                ts=time.time(),
                verb=verb,
                args=args,
                resolution=resolution,
                issued_by=actor,
                detail=detail,
            ),
        )
    except Exception:
        pass


def _read_and_truncate_queue(
    project_dir: str | os.PathLike,
) -> list[dict[str, Any]]:
    """Atomic-ish read+truncate: read every entry, then truncate the
    file. Race window is tiny (single engine, single ctl writer per
    project) and worst-case we drop one command if a write lands
    between read and truncate. Engine treats commands as best-effort
    so this is acceptable."""
    p = _control_path(project_dir)
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if entries:
        try:
            p.write_text("", encoding="utf-8")
        except OSError:
            pass
    return entries


# ---- per-verb effects --------------------------------------------------


def _apply_pause(state: dict[str, Any]) -> str:
    if state.get("status") == "waiting":
        return "skipped:already_waiting"
    state["status"] = "waiting"
    state["paused_at"] = time.time()
    return "executed"


def _apply_resume(state: dict[str, Any]) -> str:
    if state.get("status") == "running":
        return "skipped:already_running"
    state["status"] = "running"
    state.pop("paused_at", None)
    return "executed"


def _apply_kill_worker(
    state: dict[str, Any],
    *,
    cleanup_agent: Callable[[str], None],
) -> str:
    aid = state.get("current_agent_id")
    if not aid:
        return "skipped:no_current_agent"
    flow_id = state.get("flow_id")
    node_id = state.get("pc")
    key = (flow_id, node_id, aid)
    now = time.time()
    last = _KILL_COOLDOWN.get(key)
    if last is not None and (now - last) < KILL_COOLDOWN_SECONDS:
        wait = KILL_COOLDOWN_SECONDS - int(now - last)
        return f"skipped:cooldown_{wait}s_remaining"
    _KILL_COOLDOWN[key] = now
    try:
        cleanup_agent(aid)
    except Exception:
        return "skipped:cleanup_failed"
    state["current_agent_id"] = None
    return "executed"


def _apply_spawn(state: dict[str, Any], args: dict[str, Any]) -> str:
    node = args.get("node")
    if not node:
        return "skipped:missing_node_arg"
    state["pc"] = node
    if args.get("brief"):
        state["spawned_brief"] = args["brief"]
    return "executed"


def _apply_skip(state: dict[str, Any], args: dict[str, Any]) -> str:
    """Mark the current pc as completed and let the engine's normal
    transition resolver pick the next node via the node's
    ``transitions`` / ``next``. The engine reads
    ``state.skip_current`` at the top of its tick and short-circuits
    the node body."""
    pc = state.get("pc")
    if not pc:
        return "skipped:no_current_pc"
    state["skip_current"] = {
        "node": pc,
        "reason": args.get("reason") or "ctl skip",
    }
    return "executed"


# ---- main entry --------------------------------------------------------


def drain_control_queue(
    project_dir: str | os.PathLike,
    state: dict[str, Any],
    *,
    cleanup_agent: Callable[[str], None] | None = None,
) -> int:
    """Read every queued command, apply its effect on ``state``, emit
    a paired ``control_resolution`` trace entry, and truncate the
    queue. Returns the number of commands processed.

    ``cleanup_agent`` is dependency-injected so unit tests can avoid
    real ``camc rm`` calls. Default uses
    ``agent_runner._cleanup_agent``.
    """
    if cleanup_agent is None:
        from camflow.backend.cam.agent_runner import _cleanup_agent
        cleanup_agent = _cleanup_agent

    entries = _read_and_truncate_queue(project_dir)
    for entry in entries:
        verb = entry.get("verb") or ""
        args = entry.get("args") or {}
        actor = entry.get("issued_by") or "user"
        flow_id = entry.get("flow_id") or state.get("flow_id")

        if verb == "pause":
            resolution = _apply_pause(state)
        elif verb == "resume":
            resolution = _apply_resume(state)
        elif verb == "kill-worker":
            resolution = _apply_kill_worker(
                state, cleanup_agent=cleanup_agent,
            )
        elif verb == "spawn":
            resolution = _apply_spawn(state, args)
        elif verb == "skip":
            resolution = _apply_skip(state, args)
        else:
            resolution = "skipped:unknown_verb"

        _emit_resolution_trace(
            project_dir,
            verb=verb,
            args=args,
            actor=actor,
            flow_id=flow_id,
            resolution=resolution,
        )
    return len(entries)


def reset_kill_cooldown_for_tests() -> None:
    """Test helper — clears the per-tuple cooldown table."""
    _KILL_COOLDOWN.clear()
