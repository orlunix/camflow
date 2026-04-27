"""Phase B autonomy config — steward-config.yaml + effective_autonomy."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from camflow.steward.autonomy import (
    DEFAULT_CONFIRM_TIMEOUT_MINUTES,
    LEVEL_AUTONOMOUS,
    LEVEL_BLOCK,
    LEVEL_CONFIRM,
    PRESET_BOLD,
    PRESET_CAUTIOUS,
    PRESET_DEFAULT,
    AutonomyConfig,
    effective_autonomy,
    load_config,
    set_override,
    write_config,
)


# ---- load defaults when missing -----------------------------------------


class TestLoadDefaults:
    def test_no_file_returns_default_preset(self, tmp_path):
        cfg = load_config(tmp_path)
        assert cfg.preset == PRESET_DEFAULT
        assert cfg.overrides == {}
        assert cfg.confirm_timeout_minutes == DEFAULT_CONFIRM_TIMEOUT_MINUTES
        assert cfg.confirm_channel == "chat"

    def test_malformed_yaml_returns_default(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "steward-config.yaml").write_text(
            "foo: [unterminated\n"
        )
        cfg = load_config(tmp_path)
        assert cfg.preset == PRESET_DEFAULT

    def test_unknown_preset_returns_default(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "steward-config.yaml").write_text(
            "autonomy: insanely-bold\n"
        )
        cfg = load_config(tmp_path)
        assert cfg.preset == PRESET_DEFAULT


# ---- presets -----------------------------------------------------------


class TestPresetEffectiveLevels:
    def test_default_preset_kill_worker_autonomous(self):
        cfg = AutonomyConfig(preset=PRESET_DEFAULT)
        assert effective_autonomy("kill-worker", cfg) == LEVEL_AUTONOMOUS

    def test_default_preset_spawn_confirm(self):
        cfg = AutonomyConfig(preset=PRESET_DEFAULT)
        assert effective_autonomy("spawn", cfg) == LEVEL_CONFIRM
        assert effective_autonomy("skip", cfg) == LEVEL_CONFIRM
        assert effective_autonomy("replan", cfg) == LEVEL_CONFIRM

    def test_cautious_kills_need_confirm(self):
        cfg = AutonomyConfig(preset=PRESET_CAUTIOUS)
        assert effective_autonomy("kill-worker", cfg) == LEVEL_CONFIRM
        # Pause/resume stay autonomous even in cautious.
        assert effective_autonomy("pause", cfg) == LEVEL_AUTONOMOUS
        assert effective_autonomy("resume", cfg) == LEVEL_AUTONOMOUS

    def test_bold_everything_autonomous(self):
        cfg = AutonomyConfig(preset=PRESET_BOLD)
        for verb in ("pause", "resume", "kill-worker", "spawn", "skip", "replan"):
            assert effective_autonomy(verb, cfg) == LEVEL_AUTONOMOUS

    def test_unknown_verb_falls_back_to_confirm(self):
        cfg = AutonomyConfig(preset=PRESET_DEFAULT)
        assert effective_autonomy("hypothetical-future-verb", cfg) == LEVEL_CONFIRM


# ---- per-verb overrides ----------------------------------------------


class TestOverrides:
    def test_override_wins_over_preset(self):
        cfg = AutonomyConfig(
            preset=PRESET_DEFAULT,
            overrides={"kill-worker": LEVEL_CONFIRM},
        )
        # Default preset has kill-worker autonomous; override flips it.
        assert effective_autonomy("kill-worker", cfg) == LEVEL_CONFIRM

    def test_override_loosens_too(self):
        cfg = AutonomyConfig(
            preset=PRESET_CAUTIOUS,
            overrides={"spawn": LEVEL_AUTONOMOUS},
        )
        assert effective_autonomy("spawn", cfg) == LEVEL_AUTONOMOUS

    def test_set_override_persists_to_disk(self, tmp_path):
        set_override(tmp_path, "kill-worker", LEVEL_CONFIRM)
        # Reload — the override survived.
        cfg = load_config(tmp_path)
        assert cfg.overrides == {"kill-worker": LEVEL_CONFIRM}

    def test_set_override_invalid_level_raises(self, tmp_path):
        with pytest.raises(ValueError, match="level must be one of"):
            set_override(tmp_path, "kill-worker", "bogus")

    def test_set_override_preserves_other_fields(self, tmp_path):
        write_config(
            tmp_path,
            AutonomyConfig(
                preset=PRESET_BOLD,
                overrides={"existing": LEVEL_CONFIRM},
                confirm_timeout_minutes=60,
                confirm_channel="inbox-only",
            ),
        )
        set_override(tmp_path, "new-verb", LEVEL_BLOCK)
        cfg = load_config(tmp_path)
        assert cfg.preset == PRESET_BOLD
        assert cfg.confirm_timeout_minutes == 60
        assert cfg.confirm_channel == "inbox-only"
        assert cfg.overrides == {
            "existing": LEVEL_CONFIRM,
            "new-verb": LEVEL_BLOCK,
        }


# ---- block level -------------------------------------------------------


class TestBlock:
    def test_block_level_propagates(self):
        cfg = AutonomyConfig(
            preset=PRESET_DEFAULT,
            overrides={"replan": LEVEL_BLOCK},
        )
        assert effective_autonomy("replan", cfg) == LEVEL_BLOCK


# ---- confirm-config fields --------------------------------------------


class TestConfirmConfig:
    def test_custom_timeout_loaded(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "steward-config.yaml").write_text(
            yaml.safe_dump({
                "autonomy": "default",
                "confirm": {"timeout_minutes": 60, "channel": "inbox-only"},
            })
        )
        cfg = load_config(tmp_path)
        assert cfg.confirm_timeout_minutes == 60
        assert cfg.confirm_channel == "inbox-only"

    def test_invalid_channel_falls_back_to_chat(self, tmp_path):
        (tmp_path / ".camflow").mkdir()
        (tmp_path / ".camflow" / "steward-config.yaml").write_text(
            yaml.safe_dump({
                "autonomy": "default",
                "confirm": {"channel": "carrier-pigeon"},
            })
        )
        cfg = load_config(tmp_path)
        assert cfg.confirm_channel == "chat"


# ---- write_config round-trip -----------------------------------------


def test_write_config_round_trip(tmp_path):
    cfg = AutonomyConfig(
        preset=PRESET_BOLD,
        overrides={"kill-worker": LEVEL_BLOCK, "spawn": LEVEL_AUTONOMOUS},
        confirm_timeout_minutes=15,
        confirm_channel="inbox-only",
    )
    write_config(tmp_path, cfg)
    loaded = load_config(tmp_path)
    assert loaded.preset == PRESET_BOLD
    assert loaded.overrides == {
        "kill-worker": LEVEL_BLOCK,
        "spawn": LEVEL_AUTONOMOUS,
    }
    assert loaded.confirm_timeout_minutes == 15
    assert loaded.confirm_channel == "inbox-only"


# ---- integration with ctl dispatch ----------------------------------


class TestCtlDispatchUsesConfig:
    def test_block_overrides_dispatcher(self, tmp_path, capsys):
        from camflow.cli_entry.ctl import dispatch
        # Ensure verbs registered.
        from camflow.cli_entry.ctl_mutate import _register_all
        _register_all()

        set_override(tmp_path, "pause", LEVEL_BLOCK)
        rc = dispatch("pause", [], project_dir=str(tmp_path))
        assert rc == 1
        assert "blocked by project config" in capsys.readouterr().err

    def test_promotion_to_autonomous_runs_handler(self, tmp_path, capsys):
        """If a confirm verb is promoted to autonomous via override
        and has a handler, the handler runs inline."""
        from camflow.cli_entry.ctl import dispatch
        from camflow.cli_entry.ctl_mutate import _register_all
        _register_all()

        # spawn is normally confirm; promote it. spawn has no handler
        # currently, so the dispatcher falls back to confirm flow with
        # a warning. That's the documented behavior.
        set_override(tmp_path, "spawn", LEVEL_AUTONOMOUS)
        rc = dispatch(
            "spawn", ["--node", "build"],
            project_dir=str(tmp_path),
        )
        # Falls back to confirm queue rather than crashing.
        assert rc == 0
        err = capsys.readouterr().err
        assert "Falling back to confirm" in err

    def test_demotion_to_confirm_queues_to_pending(self, tmp_path):
        """Demoting an autonomous verb to confirm via override sends
        the next call to the pending queue."""
        from camflow.cli_entry.ctl import dispatch
        from camflow.cli_entry.ctl_mutate import _register_all
        _register_all()

        set_override(tmp_path, "kill-worker", LEVEL_CONFIRM)
        rc = dispatch(
            "kill-worker", ["--reason", "x"],
            project_dir=str(tmp_path),
        )
        assert rc == 0
        pending = (tmp_path / ".camflow" / "control-pending.jsonl")
        assert pending.exists()
        assert "kill-worker" in pending.read_text()
        # Approved queue is empty because the override demoted to confirm.
        approved = tmp_path / ".camflow" / "control.jsonl"
        if approved.exists():
            assert approved.read_text() == ""
