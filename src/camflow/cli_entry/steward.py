"""``camflow steward`` — lifecycle management for the project's Steward.

Three subverbs in Phase A:

    camflow steward status     pretty status of the project's Steward
    camflow steward kill       stop + remove the Steward (project memory
                               is preserved on disk; only the live agent
                               session ends)
    camflow steward restart    kill, then spawn-fresh on the same project

By design (§7.2 / §11), the Steward is project-scoped — a flow ending
or ``camflow stop`` does NOT touch it. ``steward kill`` is the explicit
human action that ends a Steward.

All three subverbs operate on the project at ``--project-dir`` (default
cwd) and exit with a clear status code so they can be scripted:

    0 — success
    1 — error (no project, no Steward, or operation failed)
    2 — Steward is dead but pointer file still references it
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from camflow.registry import (
    get_current_steward,
    set_current_steward,
    update_agent_status,
)
from camflow.steward.spawn import (
    is_steward_alive,
    load_steward_pointer,
    spawn_steward,
)


CAMC_BIN = shutil.which("camc") or "camc"


# ---- helpers ------------------------------------------------------------


def _resolve_project_dir(explicit: str | None) -> str:
    return os.path.abspath(explicit) if explicit else os.getcwd()


def _fmt_age(spawned_at: str | None) -> str:
    if not spawned_at:
        return "(unknown)"
    from datetime import datetime, timezone

    try:
        spawned = datetime.fromisoformat(spawned_at.replace("Z", "+00:00"))
    except ValueError:
        return "(unparseable)"
    delta = datetime.now(timezone.utc) - spawned
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _camc_rm(agent_id: str, kill: bool = True) -> bool:
    """Best-effort ``camc rm`` for a Steward id. Returns True on exit 0."""
    args = [CAMC_BIN, "rm", agent_id]
    if kill:
        args.append("--kill")
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---- status -------------------------------------------------------------


def _do_status(args: argparse.Namespace) -> int:
    project_dir = _resolve_project_dir(args.project_dir)
    pointer = load_steward_pointer(project_dir)

    if not pointer or not pointer.get("agent_id"):
        print("Steward: NONE (no .camflow/steward.json)", file=sys.stderr)
        print("Project:", project_dir)
        return 1

    agent_id = pointer["agent_id"]
    alive = is_steward_alive(project_dir)

    state = "ALIVE" if alive else "DEAD"
    age = _fmt_age(pointer.get("spawned_at"))
    print(f"Steward: {state} ({agent_id}, born {age})")
    print(f"Project: {project_dir}")
    print(f"Prompt:  {pointer.get('prompt_file', '(none)')}")
    print(f"Summary: {pointer.get('summary_path', '(none)')}")
    print(f"Archive: {pointer.get('archive_path', '(none)')}")

    record = get_current_steward(project_dir)
    if record:
        flows = record.get("flows_witnessed") or []
        print(f"Flows witnessed: {len(flows)}")
        if flows:
            recent = flows[-3:] if len(flows) > 3 else flows
            print(f"  recent: {', '.join(recent)}")

    return 0 if alive else 2


# ---- kill ---------------------------------------------------------------


def _clear_pointer(project_dir: str) -> None:
    """Delete the steward.json pointer so subsequent ``camflow run`` knows
    to spawn fresh. Keeps summary / archive / prompt files on disk —
    they're project memory."""
    p = Path(project_dir) / ".camflow" / "steward.json"
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def _do_kill(args: argparse.Namespace) -> int:
    project_dir = _resolve_project_dir(args.project_dir)
    pointer = load_steward_pointer(project_dir)
    if not pointer or not pointer.get("agent_id"):
        print(
            "camflow steward kill: no Steward registered for project "
            f"{project_dir}",
            file=sys.stderr,
        )
        return 1

    agent_id = pointer["agent_id"]
    alive = is_steward_alive(project_dir)

    if alive:
        # Phase A has no graceful "write your final summary" path yet
        # (Phase B brings the chat/inbox flow). For now we just
        # ``camc rm --kill``.
        ok = _camc_rm(agent_id, kill=True)
        if not ok:
            print(
                f"camflow steward kill: camc rm {agent_id} failed; "
                "Steward may still be alive",
                file=sys.stderr,
            )
            return 1
        print(f"Steward {agent_id} killed.")
    else:
        print(f"Steward {agent_id} was already dead; clearing pointer.")

    # Flip registry status (best-effort) and clear pointer regardless.
    try:
        update_agent_status(
            project_dir, agent_id, "killed",
            killed_by="user (camflow steward kill)",
            killed_reason="explicit human kill",
        )
    except (KeyError, ValueError):
        pass
    try:
        set_current_steward(project_dir, None)
    except (KeyError, ValueError):
        pass
    _clear_pointer(project_dir)
    return 0


# ---- restart ------------------------------------------------------------


def _do_restart(args: argparse.Namespace) -> int:
    project_dir = _resolve_project_dir(args.project_dir)

    # Best-effort kill if there is a current one.
    pointer = load_steward_pointer(project_dir)
    if pointer and pointer.get("agent_id"):
        rc = _do_kill(args)
        if rc != 0:
            print(
                "camflow steward restart: kill failed; not respawning",
                file=sys.stderr,
            )
            return rc
        # Tiny grace period so camc reflects the rm before we spawn.
        time.sleep(0.5)

    # Workflow path is optional — restart from a project that has been
    # used before will find one in .camflow/ if present, else None.
    workflow_path = args.workflow or _detect_workflow(project_dir)

    try:
        agent_id = spawn_steward(
            project_dir,
            workflow_path=workflow_path,
            spawned_by="user (camflow steward restart)",
        )
    except Exception as exc:
        print(
            f"camflow steward restart: spawn failed: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"Steward {agent_id} respawned for project {project_dir}.")
    return 0


def _detect_workflow(project_dir: str) -> str | None:
    """If ``.camflow/workflow.yaml`` exists, return it. Else None."""
    p = Path(project_dir) / ".camflow" / "workflow.yaml"
    return str(p) if p.is_file() else None


# ---- CLI hookup ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="camflow steward",
        description="Manage the project's Steward agent.",
    )
    sub = p.add_subparsers(dest="subverb", required=True)

    sp = sub.add_parser("status", help="show Steward state")
    sp.add_argument("--project-dir", "-p", default=None)
    sp.set_defaults(func=_do_status)

    kp = sub.add_parser("kill", help="stop and unregister the Steward")
    kp.add_argument("--project-dir", "-p", default=None)
    kp.set_defaults(func=_do_kill)

    rp = sub.add_parser(
        "restart",
        help="kill the current Steward and spawn a fresh one",
    )
    rp.add_argument("--project-dir", "-p", default=None)
    rp.add_argument(
        "--workflow",
        default=None,
        help="Workflow path passed into the new Steward's boot pack "
             "(default: .camflow/workflow.yaml if present)",
    )
    rp.set_defaults(func=_do_restart)

    return p


def steward_command(argv: list[str]) -> int:
    """Entry point used by ``camflow.cli_entry.main``."""
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)
    return int(args.func(args))


def steward_status_for_status_command(project_dir: str) -> dict[str, Any]:
    """Helper for ``camflow status`` — return a small dict describing
    the current Steward (if any) without any printing.

    Returns:
        ``{"present": False}``                                       — no pointer
        ``{"present": True, "agent_id": ..., "alive": True/False,
           "spawned_at": ..., "age": "12m ago"}``
    """
    pointer = load_steward_pointer(project_dir)
    if not pointer or not pointer.get("agent_id"):
        return {"present": False}
    return {
        "present": True,
        "agent_id": pointer["agent_id"],
        "alive": is_steward_alive(project_dir),
        "spawned_at": pointer.get("spawned_at"),
        "age": _fmt_age(pointer.get("spawned_at")),
    }
