"""Agent definition loader.

Reads agent definitions from `~/.claude/agents/<name>.md`. Each file
is a skill-style markdown document with YAML frontmatter:

    ---
    name: rtl-debugger
    description: RTL debug specialist for Verilog/SystemVerilog
    model: claude
    tools: [Read, Edit, Write, Bash, Grep, Glob]
    skills: [rtl-trace, vmod-edit, systematic-debugging]
    ---

    You are an RTL debug engineer. You analyze Verilog/SystemVerilog
    code, trace signal paths, identify root causes, and fix RTL bugs.

    ## Rules
    - Read the failing test output first
    - ...

The frontmatter fields are optional; missing fields fall back to
engine defaults (the engine decides the default model and lets the
harness use its full tool set). The body is the agent's system prompt
and is prepended to every task the agent runs.

`load_agent_definition(name)` returns a dict with keys:
    name, description, model, tools (list[str]), skills (list[str]),
    system_prompt (str)
…or None if the file does not exist. The dict is safe to pass through
to `agent_runner.start_agent` via the `agent_def` kwarg.
"""

from __future__ import annotations

import os
from typing import Optional

import yaml


AGENTS_DIR_ENV = "CAMFLOW_AGENTS_DIR"
DEFAULT_AGENTS_DIR = os.path.expanduser("~/.claude/agents")


def _agents_dir() -> str:
    """Resolve the agents directory. Tests override via env var."""
    return os.environ.get(AGENTS_DIR_ENV, DEFAULT_AGENTS_DIR)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a markdown file with YAML frontmatter into (fm, body).

    Returns ("", text) if the file has no frontmatter.
    """
    if not text.startswith("---"):
        return "", text
    # Split on the closing fence. Accept "\n---\n" or "\n---" at EOF.
    rest = text[3:]
    # Strip a single leading newline if present.
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end == -1:
        return "", text
    fm = rest[:end]
    body_start = end + len("\n---")
    # Skip the newline after the closing fence if present.
    if body_start < len(rest) and rest[body_start] == "\n":
        body_start += 1
    body = rest[body_start:]
    return fm, body


def load_agent_definition(name: str) -> Optional[dict]:
    """Load ~/.claude/agents/<name>.md and return a normalized dict.

    Returns None if the file does not exist. Raises ValueError if the
    file exists but the frontmatter is malformed — a corrupt agent file
    is an operator error worth surfacing, not silently ignoring.
    """
    if not name or not isinstance(name, str):
        return None
    # Guard against path traversal — agent names are flat identifiers.
    if "/" in name or ".." in name or name.startswith("."):
        raise ValueError(f"invalid agent name: {name!r}")

    path = os.path.join(_agents_dir(), f"{name}.md")
    if not os.path.isfile(path):
        return None

    with open(path, encoding="utf-8") as f:
        text = f.read()

    fm_text, body = _split_frontmatter(text)
    try:
        fm = yaml.safe_load(fm_text) if fm_text.strip() else {}
    except yaml.YAMLError as e:
        raise ValueError(f"agent '{name}': malformed frontmatter: {e}")
    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        raise ValueError(f"agent '{name}': frontmatter must be a mapping")

    tools = fm.get("tools") or []
    skills = fm.get("skills") or []
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]

    return {
        "name": fm.get("name", name),
        "description": fm.get("description", ""),
        "model": fm.get("model"),
        "tools": list(tools),
        "skills": list(skills),
        "system_prompt": body.strip(),
    }


def list_available_agents() -> list[dict]:
    """List every agent definition under the agents dir.

    Returns a list of dicts (same shape as load_agent_definition).
    Used by the Planner during the COLLECT phase to build an agent
    catalog for the prompt.
    """
    d = _agents_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".md"):
            continue
        name = fname[:-3]
        try:
            agent = load_agent_definition(name)
        except ValueError:
            # Skip malformed files rather than failing the whole scan.
            continue
        if agent:
            out.append(agent)
    return out
