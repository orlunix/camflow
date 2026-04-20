"""Node runner — standalone dispatcher (not used by Engine class directly).

Engine has its own inlined dispatcher because it needs to save state
between start_agent and finalize_agent for orphan handling. This module
is kept as a convenience for simple callers that don't need orphan
tracking.

DSL v2 routing (2026-04-19):
  - `shell <cmd>` / `cmd <cmd>`     → run_cmd (direct subprocess, no LLM)
  - `agent <name>` / `subagent <name>`
                                    → load ~/.claude/agents/<name>.md,
                                      run_agent with persona prepended
  - `skill <name>`                  → run_agent with "invoke skill" task
  - `<anything else>`               → run_agent with the text as an
                                      inline prompt (no agent_def)
"""

from camflow.backend.cam.agent_loader import load_agent_definition
from camflow.backend.cam.agent_runner import run_agent
from camflow.backend.cam.cmd_runner import run_cmd
from camflow.backend.cam.prompt_builder import build_prompt
from camflow.engine.dsl import classify_do


def run_node(node_id, node, state, project_dir, timeout=600, poll_interval=5):
    """Execute a single workflow node. Returns only the result dict.

    For full info (agent_id, completion_signal), use Engine or call
    start_agent/finalize_agent directly.
    """
    do = node.get("do", "")
    kind, body = classify_do(do)

    if kind == "shell":
        return run_cmd(body, project_dir, timeout=timeout)

    if kind == "agent":
        agent_def = _resolve_agent_def(body)
        prompt = build_prompt(node_id, node, state, agent_def=agent_def)
        result, _agent_id, _signal = run_agent(
            node_id, prompt, project_dir,
            timeout=timeout, poll_interval=poll_interval,
        )
        return result

    if kind == "skill":
        # Skills run inside an agent session in CAM phase — invoke the
        # named skill as the first task, then let the agent carry on
        # with whatever `with` specified.
        skill_task = (
            f"Invoke the skill named '{body}' and follow its instructions. "
        )
        original_task = node.get("with", "")
        inline = skill_task + (original_task or "")
        prompt = build_prompt(node_id, node, state, inline_task=inline)
        result, _agent_id, _signal = run_agent(
            node_id, prompt, project_dir,
            timeout=timeout, poll_interval=poll_interval,
        )
        return result

    if kind == "inline":
        # `do` itself is the free-text prompt. No agent_def, no named
        # skill — anonymous default agent.
        prompt = build_prompt(node_id, node, state, inline_task=body)
        result, _agent_id, _signal = run_agent(
            node_id, prompt, project_dir,
            timeout=timeout, poll_interval=poll_interval,
        )
        return result

    return {
        "status": "fail",
        "summary": f"invalid node do value: {do!r} — {body}",
        "state_updates": {},
        "error": {"code": "INVALID_DO", "do": do},
    }


def _resolve_agent_def(name):
    """Look up an agent definition, tolerating the legacy 'claude' sentinel.

    Old workflows used `do: agent claude` as an anonymous marker. That's
    not a real agent file, so return None (anonymous default) instead
    of erroring.
    """
    if name == "claude":
        return None
    try:
        return load_agent_definition(name)
    except ValueError:
        return None
