"""Unit tests for backend.cam.agent_loader."""

import pytest

from camflow.backend.cam import agent_loader
from camflow.backend.cam.agent_loader import (
    AGENTS_DIR_ENV,
    list_available_agents,
    load_agent_definition,
)


def _write_agent(dir_path, name, content):
    f = dir_path / f"{name}.md"
    f.write_text(content)
    return f


def test_load_full_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    _write_agent(tmp_path, "rtl-debugger", """\
---
name: rtl-debugger
description: RTL debug specialist for Verilog/SystemVerilog
model: claude
tools: [Read, Edit, Write, Bash, Grep, Glob]
skills: [rtl-trace, vmod-edit]
---

You are an RTL debug engineer. You fix RTL bugs.
""")
    agent = load_agent_definition("rtl-debugger")
    assert agent is not None
    assert agent["name"] == "rtl-debugger"
    assert agent["description"].startswith("RTL debug")
    assert agent["model"] == "claude"
    assert agent["tools"] == ["Read", "Edit", "Write", "Bash", "Grep", "Glob"]
    assert agent["skills"] == ["rtl-trace", "vmod-edit"]
    assert "RTL debug engineer" in agent["system_prompt"]
    assert "---" not in agent["system_prompt"]


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    assert load_agent_definition("nope") is None


def test_load_no_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    _write_agent(tmp_path, "plain", "Just a plain prompt.\n")
    agent = load_agent_definition("plain")
    assert agent is not None
    assert agent["name"] == "plain"      # filename fallback
    assert agent["tools"] == []
    assert agent["skills"] == []
    assert agent["system_prompt"].startswith("Just a plain prompt")


def test_load_malformed_frontmatter_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    _write_agent(tmp_path, "bad", "---\nname: [unterminated\n---\nbody\n")
    with pytest.raises(ValueError):
        load_agent_definition("bad")


def test_load_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    with pytest.raises(ValueError):
        load_agent_definition("../etc/passwd")


def test_list_available_agents(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    _write_agent(tmp_path, "a", "---\nname: a\ndescription: one\n---\nbody-a")
    _write_agent(tmp_path, "b", "---\nname: b\ndescription: two\n---\nbody-b")
    # A malformed file should be skipped, not crash the scan.
    _write_agent(tmp_path, "bad", "---\nname: [broken\n---\nnope")
    agents = list_available_agents()
    names = [a["name"] for a in agents]
    assert "a" in names
    assert "b" in names
    assert "bad" not in names


def test_list_available_agents_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path / "does-not-exist"))
    assert list_available_agents() == []


def test_string_list_coercion(tmp_path, monkeypatch):
    """tools/skills may be written as a comma-separated string — coerce to list."""
    monkeypatch.setenv(AGENTS_DIR_ENV, str(tmp_path))
    _write_agent(tmp_path, "x", """\
---
name: x
tools: "Read, Edit, Bash"
skills: "skill-a, skill-b"
---

prompt
""")
    agent = load_agent_definition("x")
    assert agent["tools"] == ["Read", "Edit", "Bash"]
    assert agent["skills"] == ["skill-a", "skill-b"]
