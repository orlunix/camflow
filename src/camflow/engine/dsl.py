"""DSL parser and validator.

Implements: spec/dsl.md

DSL v2 (2026-04-19) node `do` forms:
  - `shell <command>`   — system command (preferred spelling)
  - `cmd <command>`     — legacy alias for `shell`, still accepted
  - `agent <name>`      — named sub-agent from ~/.claude/agents/<name>.md
  - `subagent <name>`   — legacy alias for `agent`
  - `skill <name>`      — invoke an installed skill
  - `<free text>`       — inline prompt, runs in an anonymous default
                          agent; any string not matching a keyword
                          above is treated as an inline prompt
"""

import yaml

NODE_FIELDS = {
    "do", "with", "next", "transitions", "set",
    # Plan-level overrides — see docs/architecture.md (Plan vs Runtime boundary)
    "methodology",     # explicit methodology label; overrides keyword router
    "preflight",       # cmd run BEFORE the node body; skip body on non-zero exit
    "verify",          # cmd run after agent success; fails the node if exit != 0
    "escalation_max",  # cap the escalation level at this node (0..4)
    "max_retries",     # per-node retry budget override
    "allowed_tools",   # per-node tool scoping (§5.3 HQ.3)
    "timeout",         # per-node timeout in seconds
    # Agent-definition override (DSL v2)
    "model",           # override the model declared in the agent definition
}
# Reserved keywords at the front of `do`. Anything else is an inline prompt.
EXECUTOR_KEYWORDS = {"shell", "cmd", "agent", "subagent", "skill"}
# Kept for back-compat with callers that import this name; DSL v2 no
# longer uses it for validation because inline prompts have no keyword.
EXECUTOR_TYPES = EXECUTOR_KEYWORDS


def load_workflow(path):
    """Load a workflow definition from YAML."""
    with open(path) as f:
        return yaml.safe_load(f)


def classify_do(do):
    """Classify a `do` string into (kind, body).

    kind ∈ {"shell", "agent", "skill", "inline", "invalid"}. `subagent`
    collapses to `agent`, `cmd` collapses to `shell`. Inline means a
    free-text prompt with no keyword prefix. Invalid means the value
    is not a non-empty string or starts with a known keyword but has
    no body (e.g. bare "agent" with no name, bare "shell" with no cmd).

    Returns ("invalid", reason) on malformed input.
    """
    if not isinstance(do, str) or not do.strip():
        return ("invalid", "empty do")
    stripped = do.strip()
    parts = stripped.split(None, 1)
    head = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    if head in EXECUTOR_KEYWORDS:
        if not body:
            return ("invalid", f"'{head}' requires a value after the keyword")
        if head in {"shell", "cmd"}:
            return ("shell", body)
        if head in {"agent", "subagent"}:
            return ("agent", body)
        if head == "skill":
            return ("skill", body)
    # Free-text inline prompt — no keyword prefix.
    return ("inline", stripped)


def validate_node(node_id, node):
    errors = []

    if not isinstance(node, dict):
        return False, [f"node '{node_id}' is not a dict"]

    unknown = set(node.keys()) - NODE_FIELDS
    if unknown:
        errors.append(f"node '{node_id}': unknown fields {unknown}")

    if "do" not in node:
        errors.append(f"node '{node_id}': missing required field 'do'")
    else:
        kind, body = classify_do(node["do"])
        if kind == "invalid":
            errors.append(f"node '{node_id}': invalid 'do' — {body}")

    model = node.get("model")
    if model is not None and not isinstance(model, str):
        errors.append(f"node '{node_id}': 'model' must be a string")

    preflight = node.get("preflight")
    if preflight is not None and (not isinstance(preflight, str) or not preflight.strip()):
        errors.append(f"node '{node_id}': 'preflight' must be a non-empty shell string")

    transitions = node.get("transitions")
    if transitions is not None:
        if not isinstance(transitions, list):
            errors.append(f"node '{node_id}': transitions must be a list")
        else:
            for i, rule in enumerate(transitions):
                if "if" not in rule or "goto" not in rule:
                    errors.append(f"node '{node_id}': transition[{i}] must have 'if' and 'goto'")

    return len(errors) == 0, errors


def validate_workflow(workflow):
    errors = []

    if not isinstance(workflow, dict):
        return False, ["workflow is not a dict"]

    if not workflow:
        errors.append("workflow has no nodes")

    # Historical constraint: if there's no node named 'start', the engine
    # falls back to the FIRST node in declaration order (Python dicts
    # preserve insertion order since 3.7). We no longer hard-require a
    # node literally named 'start' — real workflows often start with
    # something like `setup-tree` or `analyze`.

    for node_id, node in workflow.items():
        valid, node_errors = validate_node(node_id, node)
        errors.extend(node_errors)

        if isinstance(node, dict):
            next_target = node.get("next")
            if next_target and next_target not in workflow:
                errors.append(f"node '{node_id}': next target '{next_target}' does not exist")

            for rule in (node.get("transitions") or []):
                goto = rule.get("goto")
                if goto and goto not in workflow:
                    errors.append(f"node '{node_id}': goto target '{goto}' does not exist")

    return len(errors) == 0, errors
