"""``.camflow/steward-config.yaml`` loader + per-verb autonomy resolver.

Phase B (design §7.6). The user puts a small YAML file in their
project to dial Steward's autonomy:

    autonomy: default          # cautious | default | bold

    overrides:
      kill-worker: confirm     # tighten this project
      spawn: autonomous        # loosen this one

    confirm:
      timeout_minutes: 30      # OQ-11 = B: timeout-deny (default 30)
      channel: chat            # 'chat' | 'inbox-only'

Three presets, per design §7.6:

  cautious  every mutating verb needs confirm. Only read-* /
            pause / resume / summarize / archive-summary /
            ask-user are autonomous.
  default   kill-worker / pause / resume / summarize /
            archive-summary / ask-user are autonomous; spawn /
            skip / replan need confirm.
  bold      every verb is autonomous.

Per-verb ``overrides`` win over the preset.

A *third* level — ``block`` — shows up when the user replies
``never`` to a confirm prompt: that verb is added to overrides as
``<verb>: block``. Anything blocked makes the dispatcher exit
non-zero with a "blocked by user" message.

Public API:

  load_config(project_dir)         -> AutonomyConfig
  effective_autonomy(verb, config) -> "autonomous" | "confirm" | "block"
  set_override(project_dir, verb, level)   # used by chat --pending 'never'
  confirm_timeout_minutes(config)  -> int

The module reads/writes ``steward-config.yaml`` atomically; if the
file is missing the ``default`` preset is used (so the trust model is
the spec's recommendation OQ-10 = A unless the user opts out).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


CONFIG_FILE = "steward-config.yaml"

PRESET_CAUTIOUS = "cautious"
PRESET_DEFAULT = "default"
PRESET_BOLD = "bold"
_VALID_PRESETS = (PRESET_CAUTIOUS, PRESET_DEFAULT, PRESET_BOLD)

LEVEL_AUTONOMOUS = "autonomous"
LEVEL_CONFIRM = "confirm"
LEVEL_BLOCK = "block"
_VALID_LEVELS = (LEVEL_AUTONOMOUS, LEVEL_CONFIRM, LEVEL_BLOCK)


# Per-preset autonomy mapping. Verbs not listed here default to
# ``confirm`` (cautious-most safe default for unknown verbs in any
# preset that hasn't explicitly classified them).
PRESET_TABLE: dict[str, dict[str, str]] = {
    PRESET_CAUTIOUS: {
        # Read verbs — always autonomous regardless of preset.
        "read-state": LEVEL_AUTONOMOUS,
        "read-trace": LEVEL_AUTONOMOUS,
        "read-events": LEVEL_AUTONOMOUS,
        "read-rationale": LEVEL_AUTONOMOUS,
        "read-registry": LEVEL_AUTONOMOUS,
        # The rest of the autonomous-in-cautious list per §7.6.
        "summarize": LEVEL_AUTONOMOUS,
        "archive-summary": LEVEL_AUTONOMOUS,
        "ask-user": LEVEL_AUTONOMOUS,
        "pause": LEVEL_AUTONOMOUS,
        "resume": LEVEL_AUTONOMOUS,
        # Everything mutating beyond the above needs confirm.
        "kill-worker": LEVEL_CONFIRM,
        "spawn": LEVEL_CONFIRM,
        "skip": LEVEL_CONFIRM,
        "replan": LEVEL_CONFIRM,
    },
    PRESET_DEFAULT: {
        "read-state": LEVEL_AUTONOMOUS,
        "read-trace": LEVEL_AUTONOMOUS,
        "read-events": LEVEL_AUTONOMOUS,
        "read-rationale": LEVEL_AUTONOMOUS,
        "read-registry": LEVEL_AUTONOMOUS,
        "summarize": LEVEL_AUTONOMOUS,
        "archive-summary": LEVEL_AUTONOMOUS,
        "ask-user": LEVEL_AUTONOMOUS,
        "pause": LEVEL_AUTONOMOUS,
        "resume": LEVEL_AUTONOMOUS,
        "kill-worker": LEVEL_AUTONOMOUS,    # autonomous in default
        "spawn": LEVEL_CONFIRM,
        "skip": LEVEL_CONFIRM,
        "replan": LEVEL_CONFIRM,
    },
    PRESET_BOLD: {
        # bold = everything autonomous.
        "read-state": LEVEL_AUTONOMOUS,
        "read-trace": LEVEL_AUTONOMOUS,
        "read-events": LEVEL_AUTONOMOUS,
        "read-rationale": LEVEL_AUTONOMOUS,
        "read-registry": LEVEL_AUTONOMOUS,
        "summarize": LEVEL_AUTONOMOUS,
        "archive-summary": LEVEL_AUTONOMOUS,
        "ask-user": LEVEL_AUTONOMOUS,
        "pause": LEVEL_AUTONOMOUS,
        "resume": LEVEL_AUTONOMOUS,
        "kill-worker": LEVEL_AUTONOMOUS,
        "spawn": LEVEL_AUTONOMOUS,
        "skip": LEVEL_AUTONOMOUS,
        "replan": LEVEL_AUTONOMOUS,
    },
}


DEFAULT_CONFIRM_TIMEOUT_MINUTES = 30


# ---- types --------------------------------------------------------------


@dataclass(frozen=True)
class AutonomyConfig:
    preset: str = PRESET_DEFAULT
    overrides: dict[str, str] = field(default_factory=dict)
    confirm_timeout_minutes: int = DEFAULT_CONFIRM_TIMEOUT_MINUTES
    confirm_channel: str = "chat"


# ---- I/O ----------------------------------------------------------------


def _config_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / ".camflow" / CONFIG_FILE


def load_config(project_dir: str | os.PathLike) -> AutonomyConfig:
    """Load the project's autonomy config, falling back to the default
    preset when the file is missing or malformed."""
    p = _config_path(project_dir)
    if not p.exists():
        return AutonomyConfig()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return AutonomyConfig()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return AutonomyConfig()
    if not isinstance(data, dict):
        return AutonomyConfig()

    preset = data.get("autonomy", PRESET_DEFAULT)
    if preset not in _VALID_PRESETS:
        preset = PRESET_DEFAULT

    raw_overrides = data.get("overrides") or {}
    overrides: dict[str, str] = {}
    if isinstance(raw_overrides, dict):
        for verb, lvl in raw_overrides.items():
            if not isinstance(verb, str):
                continue
            if lvl in _VALID_LEVELS:
                overrides[verb] = lvl

    confirm = data.get("confirm") or {}
    timeout = DEFAULT_CONFIRM_TIMEOUT_MINUTES
    channel = "chat"
    if isinstance(confirm, dict):
        if isinstance(confirm.get("timeout_minutes"), int) and confirm["timeout_minutes"] > 0:
            timeout = confirm["timeout_minutes"]
        if confirm.get("channel") in ("chat", "inbox-only"):
            channel = confirm["channel"]

    return AutonomyConfig(
        preset=preset,
        overrides=overrides,
        confirm_timeout_minutes=timeout,
        confirm_channel=channel,
    )


def write_config(
    project_dir: str | os.PathLike, config: AutonomyConfig,
) -> None:
    """Write the config to disk atomically (temp + rename)."""
    p = _config_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "autonomy": config.preset,
        "overrides": dict(config.overrides),
        "confirm": {
            "timeout_minutes": config.confirm_timeout_minutes,
            "channel": config.confirm_channel,
        },
    }
    text = yaml.safe_dump(
        payload, default_flow_style=False, sort_keys=False,
    )
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def set_override(
    project_dir: str | os.PathLike, verb: str, level: str,
) -> None:
    """Add or update a single per-verb override and persist."""
    if level not in _VALID_LEVELS:
        raise ValueError(
            f"level must be one of {_VALID_LEVELS}, got {level!r}"
        )
    cfg = load_config(project_dir)
    new_overrides = dict(cfg.overrides)
    new_overrides[verb] = level
    write_config(
        project_dir,
        AutonomyConfig(
            preset=cfg.preset,
            overrides=new_overrides,
            confirm_timeout_minutes=cfg.confirm_timeout_minutes,
            confirm_channel=cfg.confirm_channel,
        ),
    )


# ---- resolution --------------------------------------------------------


def effective_autonomy(
    verb: str, config: AutonomyConfig, *, default: str = LEVEL_CONFIRM,
) -> str:
    """Return the effective level for ``verb`` under this config.

    Resolution order:
      1. Per-verb override (explicit user choice — wins above all)
      2. Preset table for this preset
      3. ``default`` argument — caller-supplied fallback (typically the
         verb spec's default autonomy, so an unknown-to-config verb
         keeps its registered behaviour rather than being forced to
         confirm).
    """
    if verb in config.overrides:
        return config.overrides[verb]
    table = PRESET_TABLE.get(config.preset, {})
    if verb in table:
        return table[verb]
    return default


def confirm_timeout_minutes(config: AutonomyConfig) -> int:
    return config.confirm_timeout_minutes
