"""``camflow plan-tool`` — internal CLI tools the Planner agent calls.

The Planner agent (``planner-<id>`` in camc) doesn't have direct Python
access to camflow's validators. It runs them by shelling out to these
subcommands from inside its own session:

    camflow plan-tool validate <yaml-path>      — DSL + plan-quality
    camflow plan-tool write <yaml-path>          — atomic write from stdin

``validate`` exits 0 on a clean plan, 1 on any error; warnings are
non-fatal but reported. Output is JSON on stdout so the agent can
parse it programmatically:

    {
      "ok": true,
      "errors": [],
      "warnings": ["node 'fix' has no verify command"]
    }

``write`` reads YAML text from stdin and writes it atomically to the
target path. Refuses to write if the YAML doesn't parse, doesn't pass
DSL validation, or the target lives outside the project's ``.camflow/``
directory (cheap safety so a buggy agent doesn't trash the project).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

from camflow.engine.dsl import validate_workflow as validate_dsl
from camflow.planner.validator import validate_plan_quality


# ---- helpers ------------------------------------------------------------


def _load_yaml(path: str) -> tuple[dict | None, str | None]:
    """Read + parse a YAML file. Returns ``(workflow, error_msg)``."""
    p = Path(path)
    if not p.exists():
        return None, f"file not found: {path}"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read {path}: {exc}"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"invalid YAML: {exc}"
    if not isinstance(data, dict):
        return None, (
            f"top-level must be a mapping; got {type(data).__name__}"
        )
    return data, None


def _within_project_camflow_dir(path: str, project_dir: str) -> bool:
    """True iff ``path`` resolves under ``<project_dir>/.camflow/``.

    Defense against a buggy agent passing a path that escapes the
    project sandbox (e.g. ``../../etc/whatever.yaml``).
    """
    target = Path(path).resolve()
    safe_root = (Path(project_dir) / ".camflow").resolve()
    try:
        target.relative_to(safe_root)
        return True
    except ValueError:
        return False


# ---- validate -----------------------------------------------------------


def _do_validate(args: argparse.Namespace) -> int:
    workflow, parse_err = _load_yaml(args.path)
    if parse_err is not None:
        sys.stdout.write(json.dumps({
            "ok": False,
            "errors": [parse_err],
            "warnings": [],
        }) + "\n")
        return 1

    ok, dsl_errors = validate_dsl(workflow)
    if not ok:
        sys.stdout.write(json.dumps({
            "ok": False,
            "errors": list(dsl_errors),
            "warnings": [],
        }) + "\n")
        return 1

    quality_errors, quality_warnings = validate_plan_quality(workflow)
    has_errors = bool(quality_errors)
    sys.stdout.write(json.dumps({
        "ok": not has_errors,
        "errors": list(quality_errors),
        "warnings": list(quality_warnings),
    }) + "\n")
    return 1 if has_errors else 0


# ---- write --------------------------------------------------------------


def _do_write(args: argparse.Namespace) -> int:
    project_dir = (
        os.path.abspath(args.project_dir) if args.project_dir
        else os.getcwd()
    )

    if not _within_project_camflow_dir(args.path, project_dir):
        sys.stderr.write(
            "camflow plan-tool write: refusing — target must live under "
            f"{project_dir}/.camflow/\n"
        )
        return 1

    text = sys.stdin.read()
    if not text.strip():
        sys.stderr.write("camflow plan-tool write: stdin is empty\n")
        return 1

    # Validate before writing so we never persist a broken plan.
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        sys.stderr.write(f"camflow plan-tool write: invalid YAML: {exc}\n")
        return 1
    if not isinstance(data, dict):
        sys.stderr.write(
            "camflow plan-tool write: top-level must be a mapping\n"
        )
        return 1
    ok, errors = validate_dsl(data)
    if not ok:
        sys.stderr.write(
            "camflow plan-tool write: DSL validation failed:\n  - "
            + "\n  - ".join(errors)
            + "\n"
        )
        return 1

    target = Path(args.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write via temp + rename in the same directory.
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=target.name + ".tmp.",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    sys.stdout.write(f"wrote {target}\n")
    return 0


# ---- CLI hookup ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="camflow plan-tool",
        description=(
            "Internal tools the Planner agent calls during its "
            "self-validate / write loop."
        ),
    )
    sub = p.add_subparsers(dest="subverb", required=True)

    vp = sub.add_parser(
        "validate",
        help="Run DSL + plan-quality validators on a workflow.yaml",
    )
    vp.add_argument("path", help="Path to the yaml file to validate.")
    vp.set_defaults(func=_do_validate)

    wp = sub.add_parser(
        "write",
        help="Atomic write yaml from stdin to a path under .camflow/",
    )
    wp.add_argument("path", help="Target path (must live under .camflow/).")
    wp.add_argument(
        "--project-dir", "-p", default=None,
        help="Project directory (default: cwd). Used to enforce the "
             "sandbox check.",
    )
    wp.set_defaults(func=_do_write)

    return p


def plan_tool_command(argv: list[str]) -> int:
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)
    return int(args.func(args))
