"""Steward compaction handoff.

When a Steward's Claude Code session hits the context wall (compaction),
or when the engine simply finds the registered Steward dead at startup,
this module:

  1. Archives the old Steward's private directory to
     ``.camflow/steward/archive/<old-id>-<utc-iso>/`` so the
     conversation history, summary, and archive are preserved.
  2. Spawns a fresh Steward via ``spawn_steward``, with the OLD
     summary + archive injected into the boot pack so the new
     instance picks up the project memory.
  3. Updates ``agents.json``: old → ``handoff_archived``, new →
     ``alive`` and ``current_steward_id`` flips.
  4. Emits a ``kind=handoff_completed`` trace entry.

Public API:
    handoff_steward(project_dir, *, reason, ...) -> new_agent_id

Detection helpers (engine / watchdog calls these to decide):
    is_steward_responsive(...)  — TODO Phase B+: probe the agent
                                   via camc capture / response time
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from camflow import paths
from camflow.registry import (
    on_agent_handoff_archived,
    set_current_steward,
)
from camflow.steward.spawn import (
    STEWARD_ARCHIVE_FILE,
    STEWARD_SUMMARY_FILE,
    is_steward_alive,
    load_steward_pointer,
    spawn_steward,
)


def _utc_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def _archive_old_dir(
    project_dir: str | os.PathLike,
    old_agent_id: str,
    timestamp: str,
) -> Path:
    """Copy the live ``steward/`` directory's content (excluding the
    ``archive/`` subdir itself) into a frozen subfolder under
    ``steward/archive/<old-id>-<ts>/`` so future spawns start fresh
    while the prior Steward's state is preserved for audit / forensic
    debugging.
    """
    sdir = paths.steward_dir(project_dir)
    target = paths.steward_archive_subdir(
        project_dir, old_agent_id, timestamp,
    )

    for entry in sdir.iterdir():
        if entry.name == "archive":
            continue  # don't recurse into our own archive root
        dst = target / entry.name
        try:
            if entry.is_dir():
                shutil.copytree(entry, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, dst)
        except OSError:
            # Best-effort archive — failure to copy one file shouldn't
            # block the handoff.
            continue

    return target


def _reset_summary_with_carryover(
    project_dir: str | os.PathLike,
) -> None:
    """After archiving, reset ``summary.md`` to a clean slate so the
    fresh Steward starts with a minimal working memory and pulls
    long-term context from ``archive.md``."""
    summary_path = paths.steward_summary_path(project_dir)
    try:
        summary_path.write_text(
            "# Steward working memory (post-handoff)\n\n"
            "Previous summary moved into archive.md; archive entries "
            "cover everything before this handoff.\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _fold_summary_into_archive(
    project_dir: str | os.PathLike, handoff_iso: str,
) -> None:
    """Append the contents of ``summary.md`` to ``archive.md`` with a
    section header so the new Steward can read its predecessor's
    last-known working memory."""
    summary = paths.steward_summary_path(project_dir)
    archive = paths.steward_archive_path(project_dir)
    try:
        if summary.exists():
            summary_text = summary.read_text(encoding="utf-8")
        else:
            summary_text = ""
    except OSError:
        summary_text = ""

    if not summary_text.strip():
        return

    section = (
        f"\n\n## Pre-handoff summary ({handoff_iso})\n\n"
        f"{summary_text.strip()}\n"
    )
    try:
        with open(archive, "a", encoding="utf-8") as f:
            f.write(section)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def handoff_steward(
    project_dir: str | os.PathLike,
    *,
    reason: str,
    workflow_path: str | os.PathLike | None = None,
    spawned_by: str = "compaction-handoff",
    camc_runner: Callable[[str, str, str], str] | None = None,
    camc_remover: Callable[[str], None] | None = None,
) -> str | None:
    """Archive the current Steward and spawn a fresh one with its
    summary + archive carried over.

    Returns the new agent id, or ``None`` if there was no current
    Steward to hand off (caller should just call ``spawn_steward``
    directly in that case).
    """
    pdir = str(Path(project_dir).resolve())
    pointer = load_steward_pointer(pdir)
    if not pointer or not pointer.get("agent_id"):
        return None

    old_agent_id = pointer["agent_id"]
    handoff_iso = _utc_iso()

    # 1. Fold the current summary into archive.md so the new boot
    #    pack sees the predecessor's last-known thinking.
    _fold_summary_into_archive(pdir, handoff_iso)

    # 2. Archive the live directory's contents to a frozen subfolder.
    archive_target = _archive_old_dir(pdir, old_agent_id, handoff_iso)

    # 3. Reset summary.md so the new instance starts with a clean
    #    working memory. archive.md continues to grow.
    _reset_summary_with_carryover(pdir)

    # 4. Best-effort kill of the old camc session (it's already
    #    compacted / dead in the trigger case, but call rm anyway to
    #    clean up the camc registry).
    if camc_remover is not None:
        try:
            camc_remover(old_agent_id)
        except Exception:
            pass
    else:
        try:
            from camflow.cli_entry.steward import _camc_rm
            _camc_rm(old_agent_id, kill=True)
        except Exception:
            pass

    # 5. Mark the old agent as handoff_archived in the registry.
    try:
        on_agent_handoff_archived(
            pdir,
            agent_id=old_agent_id,
            successor_id=None,  # will be filled in after spawn
            memory_carried=[
                str(paths.steward_summary_path(pdir)),
                str(paths.steward_archive_path(pdir)),
            ],
        )
    except Exception:
        pass

    # 6. Spawn the fresh Steward. The boot pack assembly in spawn.py
    #    reads workflow + plan-rationale; the carried-over archive.md
    #    sits next to it for the new agent to read.
    new_agent_id = spawn_steward(
        pdir,
        workflow_path=workflow_path,
        spawned_by=spawned_by,
        camc_runner=camc_runner,
    )

    # 7. Update the predecessor's record with the actual successor id.
    try:
        from camflow.registry import update_agent_status
        update_agent_status(
            pdir,
            old_agent_id,
            "handoff_archived",
            successor_id=new_agent_id,
            archived_dir=str(archive_target),
            archived_at=handoff_iso,
            handoff_reason=reason,
        )
    except Exception:
        pass

    return new_agent_id
