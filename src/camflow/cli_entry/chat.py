"""``camflow chat`` — talk to the project's Steward.

Phase A surface (smaller than design §8.2 — we only ship what's
backed by working plumbing in this phase):

    camflow chat "现在状况?"        one-shot send + brief reply note
    camflow chat                    same but reads message from stdin
    camflow chat --history          print recent (user, steward) turns
                                    from .camflow/steward-events.jsonl
                                    (mirror of every event the engine
                                    sent) and from steward-history.log
                                    when present.

Deferred to Phase B:

    --inbox             — needs ``ask-user`` queue (Phase B mutating verb)
    --pending           — needs the ``confirm`` queue (Phase B autonomy)
    --all               — multi-project fan-out

Resolution order for "current Steward":

    1. ``--project-dir`` flag → ``<project>/.camflow/steward.json``
    2. cwd has ``.camflow/steward.json`` → use it
    3. otherwise: exit 1 with a hint
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from camflow.steward.spawn import is_steward_alive, load_steward_pointer


CAMC_BIN = shutil.which("camc") or "camc"


# ---- helpers ------------------------------------------------------------


def _resolve_project_dir(explicit: str | None) -> str:
    return os.path.abspath(explicit) if explicit else os.getcwd()


def _resolve_steward(project_dir: str) -> str | None:
    pointer = load_steward_pointer(project_dir)
    if pointer and pointer.get("agent_id"):
        return pointer["agent_id"]
    return None


def _camc_send(agent_id: str, message: str) -> bool:
    """Send a user message via ``camc send <id> <text>``.

    User messages do NOT carry the ``[CAMFLOW EVENT]`` prefix — the
    Steward's prompt distinguishes them by absence of the prefix.
    """
    try:
        proc = subprocess.run(
            [CAMC_BIN, "send", agent_id, message],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---- one-shot send ------------------------------------------------------


def _do_send(args: argparse.Namespace) -> int:
    project_dir = _resolve_project_dir(args.project_dir)
    agent_id = _resolve_steward(project_dir)
    if agent_id is None:
        print(
            "camflow chat: no Steward registered for this project. "
            "Run `camflow run <yaml>` once to spawn one, or use "
            "`--project-dir` to point at another project.",
            file=sys.stderr,
        )
        return 1

    if not is_steward_alive(project_dir):
        print(
            f"camflow chat: Steward {agent_id} is dead. "
            "Use `camflow steward restart` to bring it back.",
            file=sys.stderr,
        )
        return 1

    message = args.message
    if message is None:
        # Read from stdin (one block, allows multi-line via heredoc).
        message = sys.stdin.read().rstrip("\n")
    if not message:
        print(
            "camflow chat: empty message; nothing to send.",
            file=sys.stderr,
        )
        return 1

    ok = _camc_send(agent_id, message)
    if not ok:
        print(
            f"camflow chat: camc send to {agent_id} failed.",
            file=sys.stderr,
        )
        return 1

    print(f"sent to {agent_id}.")
    print(
        "Steward replies asynchronously inside its tmux session. "
        f"Use `camc capture {agent_id}` to read its current screen, "
        "or `camflow chat --history` to see recent turns."
    )
    return 0


# ---- history ------------------------------------------------------------


def _read_event_tail(project_dir: str, n: int) -> list[dict[str, Any]]:
    p = Path(project_dir) / ".camflow" / "steward-events.jsonl"
    if not p.exists():
        return []
    lines = [
        ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    out: list[dict[str, Any]] = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _do_history(args: argparse.Namespace) -> int:
    project_dir = _resolve_project_dir(args.project_dir)
    agent_id = _resolve_steward(project_dir)
    if agent_id is None:
        print(
            "camflow chat --history: no Steward for this project.",
            file=sys.stderr,
        )
        return 1

    events = _read_event_tail(project_dir, args.tail)
    if not events:
        print("(no events recorded yet)")
        return 0

    print(f"Last {len(events)} engine→Steward events for {agent_id}:")
    print()
    for ev in events:
        ts = ev.get("ts", "")
        kind = ev.get("type", ev.get("kind", "?"))
        flow = ev.get("flow_id") or "-"
        node = ev.get("node") or "-"
        summary = ev.get("summary") or ev.get("status") or ""
        print(f"  {ts}  {kind:<14}  flow={flow}  node={node}  {summary}")
    return 0


# ---- CLI hookup ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="camflow chat",
        description="Talk to this project's Steward agent.",
    )
    p.add_argument(
        "message",
        nargs="?",
        default=None,
        help="Message to send. Omit to read from stdin.",
    )
    p.add_argument(
        "--project-dir", "-p", default=None,
        help="Project directory (default: cwd).",
    )
    p.add_argument(
        "--history", action="store_true",
        help="Print recent engine→Steward events instead of sending.",
    )
    p.add_argument(
        "--tail", type=int, default=20,
        help="With --history, number of events to show (default: 20).",
    )
    return p


def chat_command(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.history:
        return int(_do_history(args))
    return int(_do_send(args))
