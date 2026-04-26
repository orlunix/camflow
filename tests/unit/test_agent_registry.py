"""Unit tests for camflow.registry.agents."""

import json
from pathlib import Path

import pytest

from camflow.registry import (
    REGISTRY_VERSION,
    get_agent,
    get_current_steward,
    list_agents,
    load_registry,
    register_agent,
    registry_path,
    set_current_steward,
    update_agent_status,
)


# ---- helpers ------------------------------------------------------------


def _worker(agent_id="camflow-build-a1b2c3", **overrides):
    base = {
        "id": agent_id,
        "role": "worker",
        "status": "alive",
        "spawned_at": "2026-04-26T10:00:00Z",
        "spawned_by": "engine (flow_001 step 1)",
        "flow_id": "flow_001",
        "node_id": "build",
    }
    base.update(overrides)
    return base


def _steward(agent_id="steward-7c2a", **overrides):
    base = {
        "id": agent_id,
        "role": "steward",
        "status": "alive",
        "spawned_at": "2026-04-26T10:00:00Z",
        "spawned_by": "camflow run (smooth)",
    }
    base.update(overrides)
    return base


# ---- load_registry ------------------------------------------------------


class TestLoadRegistry:
    def test_missing_file_returns_empty(self, tmp_path):
        reg = load_registry(tmp_path)
        assert reg["version"] == REGISTRY_VERSION
        assert reg["agents"] == []
        assert reg["current_steward_id"] is None
        # project_dir is recorded as the absolute path
        assert reg["project_dir"] == str(tmp_path.resolve())

    def test_round_trip(self, tmp_path):
        register_agent(tmp_path, _worker())
        reg = load_registry(tmp_path)
        assert len(reg["agents"]) == 1
        assert reg["agents"][0]["id"] == "camflow-build-a1b2c3"

    def test_load_fills_missing_keys_for_old_files(self, tmp_path):
        """A registry written by a future/older version with missing keys
        loads cleanly and is augmented in memory."""
        path = registry_path(tmp_path)
        Path(path).parent.mkdir(parents=True)
        # Write a registry missing current_steward_id.
        Path(path).write_text(
            json.dumps({"version": 1, "project_dir": str(tmp_path), "agents": []})
        )
        reg = load_registry(tmp_path)
        assert reg["current_steward_id"] is None


# ---- register_agent -----------------------------------------------------


class TestRegisterAgent:
    def test_basic_append(self, tmp_path):
        register_agent(tmp_path, _worker())
        register_agent(tmp_path, _worker("camflow-test-x1y2z3", node_id="test"))
        reg = load_registry(tmp_path)
        ids = [a["id"] for a in reg["agents"]]
        assert ids == ["camflow-build-a1b2c3", "camflow-test-x1y2z3"]

    def test_duplicate_id_rejected(self, tmp_path):
        register_agent(tmp_path, _worker())
        with pytest.raises(ValueError, match="already in registry"):
            register_agent(tmp_path, _worker())

    def test_missing_id_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="requires 'id'"):
            register_agent(tmp_path, {"role": "worker", "status": "alive"})

    def test_invalid_role_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="agent role must be one of"):
            register_agent(
                tmp_path,
                {"id": "x", "role": "wizard", "status": "alive"},
            )

    def test_invalid_status_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="agent status must be one of"):
            register_agent(
                tmp_path,
                {"id": "x", "role": "worker", "status": "running"},
            )

    def test_atomic_write_no_partial_files(self, tmp_path):
        register_agent(tmp_path, _worker())
        # No tmp file lingering after success.
        camflow_dir = tmp_path / ".camflow"
        leftovers = [
            p for p in camflow_dir.iterdir() if p.name.startswith("agents.json.tmp.")
        ]
        assert leftovers == []


# ---- update_agent_status ------------------------------------------------


class TestUpdateAgentStatus:
    def test_flip_alive_to_completed(self, tmp_path):
        register_agent(tmp_path, _worker())
        update_agent_status(
            tmp_path,
            "camflow-build-a1b2c3",
            "completed",
            completed_at="2026-04-26T10:04:30Z",
            duration_ms=47000,
        )
        agent = get_agent(tmp_path, "camflow-build-a1b2c3")
        assert agent["status"] == "completed"
        assert agent["completed_at"] == "2026-04-26T10:04:30Z"
        assert agent["duration_ms"] == 47000

    def test_unknown_id_raises(self, tmp_path):
        with pytest.raises(KeyError, match="not in registry"):
            update_agent_status(tmp_path, "nope", "completed")

    def test_invalid_status_rejected(self, tmp_path):
        register_agent(tmp_path, _worker())
        with pytest.raises(ValueError, match="status must be one of"):
            update_agent_status(tmp_path, "camflow-build-a1b2c3", "bogus")

    def test_killed_with_reason(self, tmp_path):
        register_agent(tmp_path, _worker())
        update_agent_status(
            tmp_path,
            "camflow-build-a1b2c3",
            "killed",
            killed_at="2026-04-26T10:08:11Z",
            killed_by="steward-7c2a",
            killed_reason="stuck on compaction",
        )
        agent = get_agent(tmp_path, "camflow-build-a1b2c3")
        assert agent["status"] == "killed"
        assert agent["killed_by"] == "steward-7c2a"


# ---- get_agent / list_agents -------------------------------------------


class TestQueries:
    def test_get_agent_returns_none_when_missing(self, tmp_path):
        assert get_agent(tmp_path, "nope") is None

    def test_list_agents_no_filter(self, tmp_path):
        register_agent(tmp_path, _steward())
        register_agent(tmp_path, _worker())
        agents = list_agents(tmp_path)
        roles = [a["role"] for a in agents]
        assert roles == ["steward", "worker"]

    def test_list_agents_filter_by_role(self, tmp_path):
        register_agent(tmp_path, _steward())
        register_agent(tmp_path, _worker())
        register_agent(tmp_path, _worker("camflow-test-x1y2z3", node_id="test"))
        workers = list_agents(tmp_path, role="worker")
        assert len(workers) == 2
        assert all(a["role"] == "worker" for a in workers)

    def test_list_agents_filter_by_status(self, tmp_path):
        register_agent(tmp_path, _worker())
        register_agent(tmp_path, _worker("camflow-test-x1y2z3", node_id="test"))
        update_agent_status(tmp_path, "camflow-build-a1b2c3", "completed")
        alive = list_agents(tmp_path, status="alive")
        assert len(alive) == 1
        assert alive[0]["id"] == "camflow-test-x1y2z3"

    def test_list_agents_filter_combined(self, tmp_path):
        register_agent(tmp_path, _steward())
        register_agent(tmp_path, _worker())
        register_agent(tmp_path, _worker("camflow-test-x1y2z3", node_id="test"))
        update_agent_status(tmp_path, "camflow-build-a1b2c3", "killed")
        alive_workers = list_agents(tmp_path, role="worker", status="alive")
        assert len(alive_workers) == 1
        assert alive_workers[0]["id"] == "camflow-test-x1y2z3"


# ---- current_steward_id pointer ----------------------------------------


class TestCurrentSteward:
    def test_set_and_get(self, tmp_path):
        register_agent(tmp_path, _steward())
        set_current_steward(tmp_path, "steward-7c2a")
        sw = get_current_steward(tmp_path)
        assert sw is not None
        assert sw["id"] == "steward-7c2a"

    def test_set_unknown_id_raises(self, tmp_path):
        with pytest.raises(KeyError, match="not in registry"):
            set_current_steward(tmp_path, "steward-ghost")

    def test_clear_with_none(self, tmp_path):
        register_agent(tmp_path, _steward())
        set_current_steward(tmp_path, "steward-7c2a")
        set_current_steward(tmp_path, None)
        reg = load_registry(tmp_path)
        assert reg["current_steward_id"] is None
        assert get_current_steward(tmp_path) is None

    def test_handoff_replaces_current(self, tmp_path):
        register_agent(tmp_path, _steward("steward-7c2a"))
        set_current_steward(tmp_path, "steward-7c2a")

        # Compaction handoff: archive old, register new, point to new.
        update_agent_status(
            tmp_path,
            "steward-7c2a",
            "handoff_archived",
            archived_at="2026-04-26T11:00:00Z",
        )
        register_agent(tmp_path, _steward("steward-7c2a-v2"))
        set_current_steward(tmp_path, "steward-7c2a-v2")

        old = get_agent(tmp_path, "steward-7c2a")
        assert old["status"] == "handoff_archived"

        sw = get_current_steward(tmp_path)
        assert sw["id"] == "steward-7c2a-v2"
        assert sw["status"] == "alive"


# ---- file location -----------------------------------------------------


def test_registry_path(tmp_path):
    p = registry_path(tmp_path)
    assert p.endswith("/.camflow/agents.json")


def test_registry_file_is_under_dot_camflow(tmp_path):
    register_agent(tmp_path, _worker())
    assert (tmp_path / ".camflow" / "agents.json").exists()
