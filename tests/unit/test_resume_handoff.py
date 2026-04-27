"""Phase B: engine resume goes through the handoff path when a dead
Steward is registered, so working memory survives engine restarts.

Three branches of ``_ensure_steward``:
  - LIVE                → reattach (no-op).
  - POINTER + DEAD      → handoff (archive old, spawn fresh, carry
                          summary+archive over).
  - NO POINTER          → fresh spawn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camflow import paths
from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.registry import (
    get_agent,
    register_agent,
    set_current_steward,
)
from camflow.steward.spawn import STEWARD_POINTER_FILE


def _seed_workflow(tmp_path):
    wf = tmp_path / "wf.yaml"
    wf.write_text("a:\n  do: cmd echo\n")
    return wf


def _make_engine(tmp_path, *, no_steward=False) -> Engine:
    wf = _seed_workflow(tmp_path)
    cfg = EngineConfig(no_steward=no_steward)
    eng = Engine(str(wf), str(tmp_path), cfg)
    eng.state = {"flow_id": "flow_xx"}
    return eng


def _seed_dead_steward(tmp_path, agent_id="steward-OLD"):
    register_agent(tmp_path, {
        "id": agent_id,
        "role": "steward",
        "status": "alive",  # registry says alive but camc disagrees
        "spawned_at": "2026-04-26T10:00:00Z",
        "spawned_by": "test",
    })
    set_current_steward(tmp_path, agent_id)
    pointer = tmp_path / ".camflow" / STEWARD_POINTER_FILE
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"agent_id": agent_id, "name": agent_id}))
    sdir = paths.steward_dir(tmp_path)
    (sdir / "summary.md").write_text("dead-steward working memory")
    (sdir / "archive.md").write_text("dead-steward archived flows")
    (sdir / "prompt.txt").write_text("dead-steward prompt")


# ---- LIVE branch -------------------------------------------------------


def test_alive_steward_skips_handoff(tmp_path, monkeypatch):
    _seed_dead_steward(tmp_path)
    # Force is_steward_alive → True.
    from camflow.backend.cam import engine as engine_module
    monkeypatch.setattr(
        engine_module, "is_steward_alive",
        lambda *a, **k: True,
    )

    handoff_calls = []
    spawn_calls = []
    monkeypatch.setattr(
        "camflow.steward.handoff.handoff_steward",
        lambda *a, **k: handoff_calls.append(k) or "shouldn-not-be-called",
    )
    monkeypatch.setattr(
        engine_module, "spawn_steward",
        lambda *a, **k: spawn_calls.append(k) or "shouldn-not-be-called",
    )

    eng = _make_engine(tmp_path)
    eng._ensure_steward()
    assert handoff_calls == []
    assert spawn_calls == []


# ---- POINTER + DEAD branch (handoff) ---------------------------------


def test_dead_steward_triggers_handoff(tmp_path, monkeypatch):
    _seed_dead_steward(tmp_path)
    # is_steward_alive → False.
    from camflow.backend.cam import engine as engine_module
    monkeypatch.setattr(
        engine_module, "is_steward_alive",
        lambda *a, **k: False,
    )

    captured: dict = {}
    def fake_handoff(project_dir, *, reason, workflow_path, spawned_by, **kw):
        captured["project_dir"] = str(project_dir)
        captured["reason"] = reason
        captured["spawned_by"] = spawned_by
        return "steward-NEW1"

    monkeypatch.setattr(
        "camflow.steward.handoff.handoff_steward", fake_handoff,
    )

    spawn_calls = []
    monkeypatch.setattr(
        engine_module, "spawn_steward",
        lambda *a, **k: spawn_calls.append(k),
    )

    eng = _make_engine(tmp_path)
    eng._ensure_steward()
    assert captured["reason"] == "dead steward at engine startup"
    assert "handoff" in captured["spawned_by"]
    # Plain spawn was NOT called; handoff was.
    assert spawn_calls == []


# ---- NO POINTER branch (fresh spawn) ---------------------------------


def test_no_pointer_triggers_fresh_spawn(tmp_path, monkeypatch):
    # No prior pointer: project never had a Steward.
    from camflow.backend.cam import engine as engine_module

    captured: dict = {}
    def fake_spawn(project_dir, *, workflow_path, spawned_by, **kw):
        captured["called"] = True
        captured["spawned_by"] = spawned_by
        return "steward-FIRST"

    handoff_calls = []
    monkeypatch.setattr(
        engine_module, "spawn_steward", fake_spawn,
    )
    monkeypatch.setattr(
        "camflow.steward.handoff.handoff_steward",
        lambda *a, **k: handoff_calls.append(k),
    )
    # is_steward_alive → False (no pointer either).
    monkeypatch.setattr(
        engine_module, "is_steward_alive",
        lambda *a, **k: False,
    )

    eng = _make_engine(tmp_path)
    eng._ensure_steward()
    assert captured.get("called") is True
    # Handoff path was NOT called — there was nothing to hand off.
    assert handoff_calls == []


# ---- --no-steward short-circuits all three ---------------------------


def test_no_steward_flag_short_circuits(tmp_path, monkeypatch):
    _seed_dead_steward(tmp_path)
    from camflow.backend.cam import engine as engine_module

    spawn_calls = []
    handoff_calls = []
    monkeypatch.setattr(
        engine_module, "spawn_steward",
        lambda *a, **k: spawn_calls.append(k),
    )
    monkeypatch.setattr(
        "camflow.steward.handoff.handoff_steward",
        lambda *a, **k: handoff_calls.append(k),
    )

    eng = _make_engine(tmp_path, no_steward=True)
    eng._ensure_steward()
    assert spawn_calls == []
    assert handoff_calls == []
