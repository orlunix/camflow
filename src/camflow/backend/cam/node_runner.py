"""Node runner — standalone dispatcher (not used by Engine class directly).

Engine has its own inlined dispatcher because it needs to save state
between start_agent and finalize_agent for orphan handling. This module
is kept as a convenience for simple callers that don't need orphan
tracking.

Routes based on the `do` field:
  - cmd <command>    → run_cmd (direct subprocess, no LLM)
  - agent <name>     → run_agent (camc)
  - subagent <name>  → run_agent (camc)
  - skill <name>     → run_agent (camc) — skills run as agent in CAM phase
"""

from camflow.backend.cam.agent_runner import run_agent
from camflow.backend.cam.cmd_runner import run_cmd
from camflow.backend.cam.prompt_builder import build_prompt


def run_node(node_id, node, state, project_dir, timeout=600, poll_interval=5):
    """Execute a single workflow node. Returns only the result dict.

    For full info (agent_id, completion_signal), use Engine or call
    start_agent/finalize_agent directly.
    """
    do = node.get("do", "")

    if do.startswith("cmd "):
        command = do[4:]
        return run_cmd(command, project_dir, timeout=timeout)

    if (do.startswith("agent ") or do.startswith("subagent ")
            or do.startswith("skill ")):
        prompt = build_prompt(node_id, node, state)
        result, _agent_id, _signal = run_agent(
            node_id, prompt, project_dir,
            timeout=timeout, poll_interval=poll_interval,
        )
        return result

    return {
        "status": "fail",
        "summary": f"unknown node type: {do}",
        "state_updates": {},
        "error": {"code": "UNKNOWN_NODE_TYPE", "do": do},
    }
