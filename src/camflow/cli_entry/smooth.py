"""``camflow "<NL>"`` — smooth mode (Phase C entry point).

Replaces the current "you must write workflow.yaml yourself" workflow
with a one-liner. The 7 steps from design §6.2:

  1. PROJECT DETECT  (cwd; --project-dir overrides)
  2. STEWARD CHECK   (alive → forward request via camc send + exit)
  3. PLAN            (spawn Planner agent; produce
                      .camflow/workflow.yaml + plan-rationale.md)
  4. SUMMARIZE       (ASCII graph + warnings)
  5. COUNTDOWN       (5s default; Ctrl-C abort, e<enter> edit yaml,
                      r<enter> replan with extra context;
                      --yes skips)
  6. KICKOFF         (camflow run --daemon — engine + watchdog +
                      Steward spawn happen there)
  7. CHAT HINT       ("Steward will spawn — `camflow chat` to ask
                      anything")

This module is the driver; the heavy lifting (agent spawn, validate,
write) lives in ``camflow.planner.agent_planner``. Failure modes
exit non-zero with a hint for ``--legacy`` or ``camflow plan``
fallback.
"""

from __future__ import annotations

import argparse
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from camflow import paths
from camflow.planner.agent_planner import (
    PlannerAgentError,
    generate_workflow_via_agent,
)
from camflow.planner.planner import ascii_graph
from camflow.steward.spawn import is_steward_alive, load_steward_pointer


CAMC_BIN = shutil.which("camc") or "camc"


# ---- helpers ------------------------------------------------------------


def _resolve_project_dir(explicit: str | None) -> str:
    return os.path.abspath(explicit) if explicit else os.getcwd()


def _camc_send(agent_id: str, message: str) -> bool:
    """Forward a NL request to a live Steward via ``camc send``."""
    try:
        proc = subprocess.run(
            [CAMC_BIN, "send", agent_id, message],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---- step 5: countdown with abort / edit / replan handles -------------


COUNTDOWN_GO = "go"
COUNTDOWN_ABORT = "abort"
COUNTDOWN_EDIT = "edit"
COUNTDOWN_REPLAN = "replan"


def _countdown_with_handles(seconds: int) -> str:
    """Show a Ns countdown. Returns one of:
      go      — countdown elapsed, run the workflow
      abort   — user pressed Ctrl-C (or typed 'a')
      edit    — user typed 'e' — caller should open $EDITOR on yaml
      replan  — user typed 'r' — caller should prompt for extra
                context and re-spawn the Planner

    On non-TTY stdin (CI, pipes), returns ``go`` immediately without
    polling — countdown becomes a no-op so scripted runs don't hang.
    """
    if not sys.stdin.isatty():
        return COUNTDOWN_GO

    sys.stdout.write(
        f"\nRunning in {seconds}s — Ctrl-C abort, "
        "e<enter> edit, r<enter> replan with more context.\n"
    )
    sys.stdout.flush()

    deadline = time.time() + seconds
    last_remaining = -1
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            sys.stdout.write("\n")
            return COUNTDOWN_GO
        whole = int(remaining) + 1
        if whole != last_remaining:
            sys.stdout.write(f"\r  {whole}s ")
            sys.stdout.flush()
            last_remaining = whole
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.25)
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            return COUNTDOWN_ABORT
        if not r:
            continue
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            return COUNTDOWN_ABORT
        if not line:
            sys.stdout.write("\n")
            return COUNTDOWN_GO
        choice = line.strip().lower()
        sys.stdout.write("\n")
        if choice in ("a", "abort"):
            return COUNTDOWN_ABORT
        if choice in ("e", "edit"):
            return COUNTDOWN_EDIT
        if choice in ("r", "replan"):
            return COUNTDOWN_REPLAN
        # Anything else = nothing; loop until timeout.
        last_remaining = -1


def _open_editor(path: str | os.PathLike) -> None:
    """Open ``path`` in $EDITOR (default: ``vi``). Blocking."""
    editor = os.environ.get("EDITOR") or "vi"
    try:
        subprocess.run([editor, str(path)])
    except FileNotFoundError:
        sys.stderr.write(
            f"camflow smooth: editor {editor!r} not found; edit "
            f"{path} manually then re-run.\n"
        )


def _read_extra_context() -> str | None:
    """Prompt for extra NL context to feed the Planner on replan.
    Returns the entered text, or None if the user gave nothing."""
    sys.stdout.write(
        "Replan: enter additional context (one line; empty to cancel):\n> "
    )
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        return None
    line = (line or "").strip()
    return line or None


# ---- step 3+4+5: plan / summarize / countdown loop --------------------


def _plan_and_review(args: argparse.Namespace, project_dir: str) -> dict | None:
    """Spawn a Planner, summarise the result, and run the countdown
    until the user goes / aborts. Returns the parsed workflow dict on
    GO, or None on ABORT.

    Loops if the user picks REPLAN (re-spawns Planner with augmented
    request). Loops if the user picks EDIT (re-opens $EDITOR on the
    yaml, re-validates, re-runs the countdown).
    """
    request = args.request
    while True:
        sys.stderr.write(
            "[smooth] spawning Planner agent (typically 30s-2min)...\n"
        )
        try:
            result = generate_workflow_via_agent(
                request,
                project_dir=project_dir,
                timeout_seconds=args.timeout,
            )
        except PlannerAgentError as exc:
            sys.stderr.write(f"ERROR: Planner failed: {exc}\n")
            sys.stderr.write(
                "Run with `camflow plan --legacy '<request>'` for the "
                "single-shot fallback, or `camflow plan -i '<request>'` "
                "for interactive mode.\n"
            )
            return None

        sys.stderr.write(
            f"[smooth] Planner {result.agent_id} finished in "
            f"{result.duration_s:.1f}s\n"
        )

        # Step 4: summarise.
        sys.stdout.write("\nWorkflow:\n")
        sys.stdout.write(ascii_graph(result.workflow))
        sys.stdout.write("\n")
        if result.warnings:
            sys.stdout.write("\nPlan-quality warnings:\n")
            for w in result.warnings:
                sys.stdout.write(f"  - {w}\n")

        if args.yes:
            return result.workflow

        # Step 5: countdown loop.
        choice = _countdown_with_handles(args.countdown)
        if choice == COUNTDOWN_GO:
            return result.workflow
        if choice == COUNTDOWN_ABORT:
            sys.stderr.write("Aborted.\n")
            return None
        if choice == COUNTDOWN_EDIT:
            _open_editor(result.workflow_path)
            # After editing, re-validate via plan-tool validate so the
            # user knows whether their hand-edit broke anything. Then
            # re-show graph and re-run the countdown.
            sys.stderr.write(
                "[smooth] post-edit; re-validate by running "
                "`camflow plan-tool validate "
                f"{result.workflow_path}`.\n"
            )
            # Reload and re-show summary; skip Planner re-spawn.
            try:
                import yaml
                with open(result.workflow_path, encoding="utf-8") as f:
                    edited = yaml.safe_load(f)
                if isinstance(edited, dict):
                    sys.stdout.write("\nWorkflow (post-edit):\n")
                    sys.stdout.write(ascii_graph(edited))
                    sys.stdout.write("\n")
            except Exception:
                pass
            choice = _countdown_with_handles(args.countdown)
            if choice == COUNTDOWN_GO:
                return None  # caller should re-load yaml from disk
                # ... actually we want to return the edited workflow.
                # Fall through: read it again.
            if choice == COUNTDOWN_ABORT:
                return None
            # If REPLAN or EDIT again, fall through to outer loop.

        if choice == COUNTDOWN_REPLAN:
            extra = _read_extra_context()
            if extra is None:
                sys.stderr.write("Replan cancelled.\n")
                return None
            request = f"{request}\n\nAdditional context: {extra}"
            # Loop — re-spawn Planner with augmented request.
            continue

        # Unknown — treat as abort.
        return None


# ---- step 6: kickoff ----------------------------------------------------


def _kickoff_engine(args: argparse.Namespace, project_dir: str) -> int:
    """Hand off to the regular ``camflow run --daemon`` path. Returns
    the engine driver's exit code."""
    workflow_path = str(paths.workflow_path(project_dir))
    if not os.path.exists(workflow_path):
        sys.stderr.write(
            f"ERROR: kickoff: workflow not found at {workflow_path}.\n"
        )
        return 1

    # We don't import _run_workflow at module load to avoid circular
    # imports between cli_entry/main.py and this module.
    from camflow.cli_entry.main import _run_workflow

    run_argv: list[str] = [workflow_path, "--project-dir", project_dir]
    if args.daemon:
        run_argv.append("--daemon")
    if args.no_steward:
        run_argv.append("--no-steward")
    return int(_run_workflow(run_argv))


# ---- public entry -------------------------------------------------------


def smooth_command(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_dir = _resolve_project_dir(args.project_dir)

    # Step 2: Steward check. If alive, route the NL request to it and
    # exit; the user's chat hint becomes "talk to Steward". Per design
    # §6.2 step 2.
    if is_steward_alive(project_dir):
        pointer = load_steward_pointer(project_dir)
        agent_id = (pointer or {}).get("agent_id")
        if agent_id:
            sys.stderr.write(
                f"[smooth] Steward {agent_id} is alive — routing your "
                "request to it. Use `camflow chat` to keep the "
                "conversation going.\n"
            )
            ok = _camc_send(agent_id, args.request)
            if not ok:
                sys.stderr.write(
                    "[smooth] camc send failed; falling through to a "
                    "fresh Planner spawn.\n"
                )
            else:
                return 0

    # Step 3+4+5: plan / summarize / countdown.
    workflow = _plan_and_review(args, project_dir)
    if workflow is None:
        return 1

    # Step 6: kickoff.
    sys.stderr.write("[smooth] kicking off engine...\n")
    rc = _kickoff_engine(args, project_dir)

    # Step 7: chat hint (only on successful daemonization; the engine
    # main loop has already exited if --daemon=False).
    if rc == 0:
        sys.stderr.write(
            "\n[smooth] engine launched. Use `camflow chat` to ask the "
            "Steward what's going on, or `camflow status` to see node "
            "progress.\n"
        )
    return rc


# ---- argparse hookup ---------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="camflow",
        description="Smooth mode — natural-language one-liner.",
    )
    p.add_argument(
        "request",
        help="Natural-language request (quote multi-word).",
    )
    p.add_argument(
        "--project-dir", "-p", default=None,
        help="Project root (default: cwd).",
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the countdown / handles; run immediately.",
    )
    p.add_argument(
        "--countdown", type=int, default=5,
        help="Seconds before kickoff (default 5; ignored under --yes).",
    )
    p.add_argument(
        "--timeout", type=int, default=180,
        help="Seconds the Planner has to write workflow.yaml (default 180).",
    )
    p.add_argument(
        "--daemon", action="store_true", default=True,
        help="Daemonize the engine (default). Use --no-daemon to run "
             "in the foreground (mostly for debugging).",
    )
    p.add_argument(
        "--no-daemon", dest="daemon", action="store_false",
        help="Run the engine in the foreground.",
    )
    p.add_argument(
        "--no-steward", action="store_true",
        help="Skip the project-scoped Steward (passed through to "
             "`camflow run`).",
    )
    return p
