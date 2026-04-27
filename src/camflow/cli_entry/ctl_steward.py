"""Phase B Steward-side ``camflow ctl`` verbs.

  summarize "<text>"     write current working memory to
                         .camflow/steward/summary.md
  archive-summary        fold current summary.md into archive.md
                         and reset summary.md to a clean slate

Both are autonomous (no user approval), file-only effects (no engine
queue). The Steward calls them in response to ``checkpoint_now`` and
``flow_idle`` events respectively.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from camflow import paths
from camflow.cli_entry.ctl import (
    AUTONOMY_AUTONOMOUS,
    VerbSpec,
    register_verb,
)


def _utc_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


# ---- summarize ---------------------------------------------------------


def _do_summarize(args: argparse.Namespace, project_dir: str) -> int:
    text = args.text
    if text is None:
        text = sys.stdin.read()
    if not text.strip():
        sys.stderr.write("camflow ctl summarize: empty text\n")
        return 1

    summary_path = paths.steward_summary_path(project_dir)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    header = f"# Steward summary (last update {_utc_iso()})\n\n"
    summary_path.write_text(header + text.rstrip() + "\n", encoding="utf-8")
    sys.stdout.write(f"wrote {summary_path}\n")
    return 0


def _add_summarize_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "text",
        nargs="?",
        default=None,
        help="Summary text. Omit to read from stdin.",
    )


# ---- archive-summary --------------------------------------------------


def _do_archive_summary(args: argparse.Namespace, project_dir: str) -> int:
    summary_path = paths.steward_summary_path(project_dir)
    archive_path = paths.steward_archive_path(project_dir)

    if not summary_path.exists():
        sys.stderr.write(
            "camflow ctl archive-summary: no summary.md to archive\n"
        )
        return 1

    try:
        summary_text = summary_path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(
            f"camflow ctl archive-summary: could not read summary: {exc}\n"
        )
        return 1
    if not summary_text.strip():
        sys.stderr.write(
            "camflow ctl archive-summary: summary is empty, nothing to fold\n"
        )
        return 1

    section_label = args.label or _utc_iso()
    section = (
        f"\n\n## Archived flow ({section_label})\n\n"
        f"{summary_text.strip()}\n"
    )
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with open(archive_path, "a", encoding="utf-8") as f:
        f.write(section)
        f.flush()
        os.fsync(f.fileno())

    # Reset summary to a clean slate so the next flow's working
    # memory doesn't drag the old one along.
    summary_path.write_text(
        f"# Steward working memory (reset {_utc_iso()})\n\n"
        f"Previous content folded into archive.md.\n",
        encoding="utf-8",
    )
    sys.stdout.write(
        f"folded {summary_path.name} into {archive_path.name}\n"
    )
    return 0


def _add_archive_summary_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--label", default=None,
        help="Section label in archive.md (default: current UTC time).",
    )


# ---- registration ------------------------------------------------------


def _register_all() -> None:
    register_verb(VerbSpec(
        name="summarize",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_do_summarize,
        add_args=_add_summarize_args,
        help="write text to .camflow/steward/summary.md",
    ), replace=True)
    register_verb(VerbSpec(
        name="archive-summary",
        autonomy=AUTONOMY_AUTONOMOUS,
        handler=_do_archive_summary,
        add_args=_add_archive_summary_args,
        help="fold summary.md into archive.md; reset summary",
    ), replace=True)


_register_all()
