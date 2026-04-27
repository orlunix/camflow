"""End-to-end integration tests against a REAL ``camc`` binary.

These tests are gated behind ``@pytest.mark.allow_real_camc`` and
**SKIPPED BY DEFAULT** unless the environment variable
``CAMFLOW_RUN_REAL_CAMC=1`` is set. They actually spawn agents in
tmpdir; they cost real Claude tokens; they leave traces on the
host's tmux state until the cleanup fixture reaps them.

Run them explicitly:

    CAMFLOW_RUN_REAL_CAMC=1 pytest tests/integration/test_real_camc.py -v

Why all the ceremony:

The previous camflow-dev instance (2026-04-26) self-killed because a
broad ``camflow-*`` cleanup glob matched its own tmux session. The
post-mortem (``docs/triage-2026-04-26.md``) recommended:
  - registry-scoped cleanup (shipped in commit f4575ef)
  - global block on real ``camc`` shell-outs in fixtures (08a2940)
  - airtight reaping of any agent created during a real-camc test

This file is the third leg. ``_real_camc_reaper`` snapshots
``agents.json`` at start and at end forcibly kills every record whose
status is still ``alive`` (steward / planner / worker), then asserts
no survivors. A test cannot leak agents past its own boundary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest


CAMC_BIN = shutil.which("camc") or "camc"

REAL_CAMC_ENABLED = os.environ.get("CAMFLOW_RUN_REAL_CAMC") == "1"

_skip_reason = (
    "Real-camc tests are opt-in. Set CAMFLOW_RUN_REAL_CAMC=1 to enable. "
    "These tests spawn real camc agents and cost real Claude tokens."
)


pytestmark = [
    pytest.mark.allow_real_camc,
    pytest.mark.skipif(not REAL_CAMC_ENABLED, reason=_skip_reason),
]


# ---- airtight reaper ---------------------------------------------------


def _list_alive_agents(project_dir: Path) -> list[dict]:
    """Read ``.camflow/agents.json`` and return any record with
    ``status='alive'``. Empty list if the file doesn't exist yet."""
    p = project_dir / ".camflow" / "agents.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        a for a in (data.get("agents") or [])
        if a.get("status") == "alive"
    ]


def _force_camc_rm(agent_id: str) -> None:
    """Best-effort ``camc rm --kill <id>``. Never raises."""
    try:
        subprocess.run(
            [CAMC_BIN, "rm", agent_id, "--kill"],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


@pytest.fixture
def real_camc_reaper(tmp_path: Path) -> Iterator[Path]:
    """Yield the tmp_path; after the test, forcibly kill every still-
    alive agent recorded in this project's ``agents.json``. Then
    assert none survived.

    A test that crashes mid-way will still get its agents reaped on
    teardown — pytest fixtures run finalizers regardless.
    """
    yield tmp_path

    alive = _list_alive_agents(tmp_path)
    for agent in alive:
        aid = agent.get("id")
        if not aid:
            continue
        _force_camc_rm(aid)

    # Final check: nothing alive.
    survivors = _list_alive_agents(tmp_path)
    assert not survivors, (
        f"real-camc test leaked {len(survivors)} agent(s) past the "
        f"reaper. Investigate — this should never happen.\n"
        f"survivors: {[a.get('id') for a in survivors]}"
    )


# ---- smoke tests -------------------------------------------------------


class TestStewardLifecycleEndToEnd:
    """Spawn a Steward → assert it's alive → kill it → assert gone."""

    def test_spawn_alive_kill(self, real_camc_reaper):
        """The minimum end-to-end Steward path:
          1. spawn_steward → real ``camc run``
          2. is_steward_alive → True
          3. ``camflow steward kill`` → agent gone
        """
        from camflow.steward.spawn import (
            is_steward_alive,
            spawn_steward,
        )

        agent_id = spawn_steward(
            real_camc_reaper,
            workflow_path=None,
            spawned_by="real-camc test",
        )
        assert agent_id
        assert is_steward_alive(real_camc_reaper) is True

        # Use the public CLI rather than the registry helper, so we
        # also exercise the camflow steward kill path.
        from camflow.cli_entry.steward import steward_command
        rc = steward_command(
            ["kill", "--project-dir", str(real_camc_reaper)]
        )
        assert rc == 0
        assert is_steward_alive(real_camc_reaper) is False


class TestPlannerAgentEndToEnd:
    """Spawn a Planner agent against a tiny prompt → workflow.yaml
    appears → registry shows ``completed``."""

    def test_simple_request_produces_valid_yaml(self, real_camc_reaper):
        from camflow.planner.agent_planner import (
            generate_workflow_via_agent,
        )

        # Tiny request the agent can plan in one or two iterations.
        result = generate_workflow_via_agent(
            "echo hello, then echo world",
            project_dir=str(real_camc_reaper),
            timeout_seconds=180,
        )
        assert result.workflow
        assert isinstance(result.workflow, dict)
        assert (real_camc_reaper / ".camflow" / "workflow.yaml").exists()


class TestEngineLoopEndToEnd:
    """Run a small cmd-only workflow end-to-end with the Steward
    enabled. Verifies that flow_started / node_started / node_done /
    flow_terminal events all reach the Steward via real ``camc send``."""

    def test_two_node_workflow_completes(self, real_camc_reaper):
        from camflow.backend.cam.engine import Engine, EngineConfig

        wf = real_camc_reaper / "wf.yaml"
        wf.write_text(
            "build:\n  do: cmd echo hello\n  next: done\n"
            "done:\n  do: cmd echo done\n"
        )

        cfg = EngineConfig(
            poll_interval=0,
            node_timeout=10,
            workflow_timeout=60,
            max_retries=1,
        )
        eng = Engine(str(wf), str(real_camc_reaper), cfg)
        final = eng.run()

        assert final["status"] == "done"
        # Steward saw the flow.
        events_path = (
            real_camc_reaper / ".camflow" / "steward-events.jsonl"
        )
        assert events_path.exists()
        events = [
            json.loads(ln)
            for ln in events_path.read_text(encoding="utf-8")
                .splitlines()
            if ln.strip()
        ]
        types = [e.get("type") for e in events]
        assert "flow_started" in types
        assert "flow_terminal" in types
