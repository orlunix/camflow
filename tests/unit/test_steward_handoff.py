"""Phase B compaction handoff — archive old Steward + spawn fresh
with summary+archive carried over."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camflow import paths
from camflow.cli_entry import ctl_steward as ctl_steward_module
from camflow.cli_entry.ctl import dispatch
from camflow.registry import (
    get_agent,
    get_current_steward,
    register_agent,
    set_current_steward,
)
from camflow.steward.handoff import handoff_steward
from camflow.steward.spawn import STEWARD_POINTER_FILE


def _seed_steward(tmp_path, agent_id="steward-OLD1"):
    register_agent(tmp_path, {
        "id": agent_id,
        "role": "steward",
        "status": "alive",
        "spawned_at": "2026-04-26T10:00:00Z",
        "spawned_by": "test",
        "flows_witnessed": ["flow_a"],
    })
    set_current_steward(tmp_path, agent_id)
    pointer = tmp_path / ".camflow" / STEWARD_POINTER_FILE
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"agent_id": agent_id, "name": agent_id}))


def _seed_steward_files(tmp_path, *,
                        summary_text="OLD-SUMMARY",
                        archive_text="OLD-ARCHIVE",
                        prompt_text="OLD-PROMPT"):
    sdir = paths.steward_dir(tmp_path)
    (sdir / "summary.md").write_text(summary_text, encoding="utf-8")
    (sdir / "archive.md").write_text(archive_text, encoding="utf-8")
    (sdir / "prompt.txt").write_text(prompt_text, encoding="utf-8")


# ---- handoff -----------------------------------------------------------


class TestHandoffSteward:
    def test_no_current_steward_returns_none(self, tmp_path):
        result = handoff_steward(tmp_path, reason="test")
        assert result is None

    def test_archives_old_dir_and_spawns_fresh(self, tmp_path):
        _seed_steward(tmp_path)
        _seed_steward_files(tmp_path)

        new_id = handoff_steward(
            tmp_path,
            reason="test compaction",
            camc_runner=lambda name, pdir, prompt: "steward-NEW1",
            camc_remover=lambda aid: None,
        )
        assert new_id == "steward-NEW1"

        # Old steward marked handoff_archived with successor + reason.
        old = get_agent(tmp_path, "steward-OLD1")
        assert old["status"] == "handoff_archived"
        assert old["successor_id"] == "steward-NEW1"
        assert "archived_dir" in old
        assert old["handoff_reason"] == "test compaction"

        # New steward registered + pointer flipped.
        current = get_current_steward(tmp_path)
        assert current["id"] == "steward-NEW1"

        # Archive folder exists with the old prompt + (folded) archive.
        archive_root = paths.steward_dir(tmp_path) / "archive"
        children = list(archive_root.iterdir())
        assert len(children) == 1
        archived_prompt = children[0] / "prompt.txt"
        assert archived_prompt.exists()
        assert "OLD-PROMPT" in archived_prompt.read_text()

    def test_summary_folded_into_archive(self, tmp_path):
        _seed_steward(tmp_path)
        _seed_steward_files(
            tmp_path,
            summary_text="working memory at handoff time",
            archive_text="prior archive content",
        )
        handoff_steward(
            tmp_path,
            reason="x",
            camc_runner=lambda name, pdir, prompt: "steward-NEW2",
            camc_remover=lambda aid: None,
        )
        # archive.md now contains BOTH the prior content and a new
        # "Pre-handoff summary" section with the old summary.
        archive_text = paths.steward_archive_path(tmp_path).read_text()
        assert "prior archive content" in archive_text
        assert "Pre-handoff summary" in archive_text
        assert "working memory at handoff time" in archive_text

    def test_summary_reset_after_handoff(self, tmp_path):
        _seed_steward(tmp_path)
        _seed_steward_files(tmp_path, summary_text="will be folded")
        handoff_steward(
            tmp_path,
            reason="x",
            camc_runner=lambda name, pdir, prompt: "steward-NEW3",
            camc_remover=lambda aid: None,
        )
        new_summary = paths.steward_summary_path(tmp_path).read_text()
        assert "will be folded" not in new_summary
        assert "post-handoff" in new_summary


# ---- summarize verb ----------------------------------------------------


class TestSummarizeVerb:
    def setup_method(self):
        ctl_steward_module._register_all()

    def test_writes_summary_md(self, tmp_path):
        rc = dispatch(
            "summarize", ["test summary text"],
            project_dir=str(tmp_path),
        )
        assert rc == 0
        path = paths.steward_summary_path(tmp_path)
        assert path.exists()
        body = path.read_text()
        assert "test summary text" in body
        assert "Steward summary" in body

    def test_empty_text_returns_1(self, tmp_path, capsys):
        rc = dispatch(
            "summarize", [""],
            project_dir=str(tmp_path),
        )
        assert rc == 1


# ---- archive-summary verb ---------------------------------------------


class TestArchiveSummaryVerb:
    def setup_method(self):
        ctl_steward_module._register_all()

    def test_folds_summary_and_resets(self, tmp_path):
        sp = paths.steward_summary_path(tmp_path)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text("flow A working memory")

        rc = dispatch(
            "archive-summary",
            ["--label", "flow_a"],
            project_dir=str(tmp_path),
        )
        assert rc == 0

        ap = paths.steward_archive_path(tmp_path)
        assert ap.exists()
        archive_text = ap.read_text()
        assert "flow_a" in archive_text
        assert "flow A working memory" in archive_text

        new_summary = sp.read_text()
        assert "flow A working memory" not in new_summary
        assert "reset" in new_summary

    def test_no_summary_returns_1(self, tmp_path, capsys):
        rc = dispatch(
            "archive-summary", [],
            project_dir=str(tmp_path),
        )
        assert rc == 1


# ---- engine checkpoint emission --------------------------------------


def test_engine_checkpoint_threshold(tmp_path, monkeypatch):
    from camflow.backend.cam.engine import Engine, EngineConfig
    from camflow.steward import events as events_module

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        events_module, "emit",
        lambda pdir, etype, **kw: (
            captured.append((etype, kw)) or False
        ),
    )

    wf = tmp_path / "wf.yaml"
    wf.write_text("a:\n  do: cmd echo\n")
    cfg = EngineConfig(no_steward=False)
    eng = Engine(str(wf), str(tmp_path), cfg)
    eng.state = {"flow_id": "f", "pc": "a"}

    for _ in range(19):
        eng._maybe_emit_checkpoint()
    assert not any(c[0] == "checkpoint_now" for c in captured)
    eng._maybe_emit_checkpoint()
    assert any(c[0] == "checkpoint_now" for c in captured)


def test_engine_no_steward_short_circuits_checkpoint(tmp_path, monkeypatch):
    from camflow.backend.cam.engine import Engine, EngineConfig
    from camflow.steward import events as events_module

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        events_module, "emit",
        lambda pdir, etype, **kw: (
            captured.append((etype, kw)) or False
        ),
    )

    wf = tmp_path / "wf.yaml"
    wf.write_text("a:\n  do: cmd echo\n")
    cfg = EngineConfig(no_steward=True)
    eng = Engine(str(wf), str(tmp_path), cfg)
    eng.state = {"flow_id": "f", "pc": "a"}
    for _ in range(25):
        eng._maybe_emit_checkpoint()
    assert captured == []
