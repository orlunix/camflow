"""Mutating ``camflow ctl`` verbs (Phase B).

Five verbs that influence a running engine:

  pause        engine: status running → waiting    (autonomous)
  resume       engine: status waiting → running    (autonomous)
  kill-worker  engine: SIGTERM the current worker  (autonomous, with cooldown)
  spawn        engine: force pc to the given node  (confirm — design §7.5)
  skip         engine: mark current node done       (confirm)

All five are uniformly queued to ``.camflow/control.jsonl`` (autonomous)
or ``.camflow/control-pending.jsonl`` (confirm — Phase B confirm flow,
not yet drained until the user approves). The engine drains
``control.jsonl`` at the top of each tick; see
``camflow.backend.cam.control_drain``.

The ``autonomous`` / ``confirm`` distinction is about USER APPROVAL,
not about who executes the verb — the engine is always the executor.
A future autonomy-config (B2) lets the user override per-verb.
"""

from __future__ import annotations

import argparse

from camflow.cli_entry.ctl import (
    AUTONOMY_AUTONOMOUS,
    AUTONOMY_CONFIRM,
    VerbSpec,
    queue_approved,
    register_verb,
)
import os
import sys
import time


def _actor() -> str:
    return os.environ.get("CAMFLOW_CTL_ACTOR", "user")


def _flow_id() -> str | None:
    return os.environ.get("CAMFLOW_CTL_FLOW_ID")


# ---- pause / resume -----------------------------------------------------


def _do_pause(args: argparse.Namespace, project_dir: str) -> int:
    queue_approved(
        project_dir,
        verb="pause",
        args={"reason": args.reason},
        issued_by=_actor(),
        flow_id=_flow_id(),
    )
    sys.stdout.write(
        "pause queued; engine will flip to status=waiting on its next tick.\n"
    )
    return 0


def _add_pause_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--reason", default="user requested pause",
        help="Free-text reason recorded in trace.log.",
    )


def _do_resume(args: argparse.Namespace, project_dir: str) -> int:
    queue_approved(
        project_dir,
        verb="resume",
        args={"reason": args.reason},
        issued_by=_actor(),
        flow_id=_flow_id(),
    )
    sys.stdout.write(
        "resume queued; engine will flip to status=running on its next tick.\n"
    )
    return 0


def _add_resume_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--reason", default="user requested resume",
        help="Free-text reason recorded in trace.log.",
    )


# ---- kill-worker --------------------------------------------------------


def _do_kill_worker(args: argparse.Namespace, project_dir: str) -> int:
    """Queue a kill of the engine's current worker. The engine's drainer
    looks up state.current_agent_id and runs ``camc rm --kill`` on it
    if it's still alive. A 30s per-(flow, node, agent) cooldown
    enforced by the drainer prevents Steward kill loops (design §7.6).
    """
    queue_approved(
        project_dir,
        verb="kill-worker",
        args={"reason": args.reason},
        issued_by=_actor(),
        flow_id=_flow_id(),
    )
    sys.stdout.write(
        "kill-worker queued; engine will SIGTERM the current worker on "
        "its next tick (subject to 30s cooldown).\n"
    )
    return 0


def _add_kill_worker_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--reason", required=True,
        help="Why this worker is being killed (recorded in trace.log).",
    )


# ---- spawn (confirm) ----------------------------------------------------


def _add_spawn_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--node", required=True,
        help="Node id to force-spawn next.",
    )
    p.add_argument(
        "--brief", default=None,
        help="Free-text brief passed to the spawned worker so it knows "
             "what the prior attempt achieved.",
    )
    p.add_argument(
        "--reason", default="steward-requested spawn",
        help="Why the spawn is needed (recorded in trace.log).",
    )


# spawn handler is unused — confirm verbs are auto-queued by the
# dispatcher to control-pending.jsonl. We still register a placeholder
# add_args so the per-verb sub-parser knows the flag schema.


# ---- skip (confirm) -----------------------------------------------------


def _add_skip_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--reason", required=True,
        help="Why the current node is being skipped (recorded in trace.log).",
    )


# ---- registration ------------------------------------------------------


def _register_all() -> None:
    """Register every mutating verb. Idempotent for tests that
    clear+repopulate the registry."""
    register_verb(VerbSpec(
        name="pause",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_do_pause,
        add_args=_add_pause_args,
        help="queue: engine flips status to waiting",
    ), replace=True)
    register_verb(VerbSpec(
        name="resume",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_do_resume,
        add_args=_add_resume_args,
        help="queue: engine flips status to running",
    ), replace=True)
    register_verb(VerbSpec(
        name="kill-worker",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_do_kill_worker,
        add_args=_add_kill_worker_args,
        help="queue: engine kills the current worker (30s cooldown)",
    ), replace=True)
    register_verb(VerbSpec(
        name="spawn",
        autonomy=AUTONOMY_CONFIRM,
        # confirm verbs don't need a handler — dispatcher queues them.
        add_args=_add_spawn_args,
        help="queue (CONFIRM): force-spawn a node next",
    ), replace=True)
    register_verb(VerbSpec(
        name="skip",
        autonomy=AUTONOMY_CONFIRM,
        add_args=_add_skip_args,
        help="queue (CONFIRM): mark current node done and advance",
    ), replace=True)


_register_all()
