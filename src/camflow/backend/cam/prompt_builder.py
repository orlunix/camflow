"""Prompt builder for CAM backend.

Produces the complete prompt shipped to the agent:
  1. Lessons + last_failure block (if any) — context from past runs
  2. Task ({{state.xxx}} resolved)
  3. Output contract (write .camflow/node-result.json)

Two entry points:
  build_prompt(node_id, node, state)                 — first attempt
  build_retry_prompt(node_id, node, state, attempt)  — retry with RETRY banner
"""

from camflow.engine.input_ref import resolve_refs


RESULT_CONTRACT = """

--- IMPORTANT: Output Contract ---

When you have completed the task above, you MUST write your result to the file `.camflow/node-result.json`.

First create the directory if it doesn't exist:
  mkdir -p .camflow

Then write the result file with this exact JSON structure:
```json
{
  "status": "success",
  "summary": "One sentence describing what you did",
  "state_updates": {},
  "error": null
}
```

Rules:
- "status" must be "success" or "fail"
- "summary" must be a brief one-sentence description
- "state_updates" is a dict of key-value pairs to pass to downstream nodes
  - On failure: include {"error": "what went wrong"}
  - On success: include any useful info for the next node
- "error" should be null on success, or an error description on failure
- If you learned something non-obvious, add {"new_lesson": "the insight"} to state_updates

This file is how the workflow engine knows you finished and what happened.
You MUST write this file before you stop working.
"""


def _lessons_block(state):
    """Render the `lessons` list as a numbered list. Empty if no lessons."""
    lessons = state.get("lessons") or []
    if not lessons:
        return ""

    lines = ["--- Previous lessons (apply these to your work) ---"]
    for i, lesson in enumerate(lessons, 1):
        lines.append(f"{i}. {lesson}")
    return "\n".join(lines) + "\n"


def _failure_block(state):
    """Render the `last_failure` context block. Empty if no failure pending."""
    failure = state.get("last_failure")
    if not failure:
        return ""

    lines = []
    node_id = failure.get("node_id", "?")
    attempt = failure.get("attempt_count", 1)
    summary = failure.get("summary", "")

    lines.append(f"--- Last failure in node '{node_id}' (attempt {attempt}) ---")
    if summary:
        lines.append(summary)

    stdout_tail = failure.get("stdout_tail", "")
    stderr_tail = failure.get("stderr_tail", "")

    if stdout_tail:
        lines.append("")
        lines.append("Test / command output (last chars):")
        lines.append(stdout_tail)

    if stderr_tail:
        lines.append("")
        lines.append("Stderr:")
        lines.append(stderr_tail)

    lines.append("")
    lines.append("If this is a retry, try a DIFFERENT approach than your previous attempt.")
    return "\n".join(lines) + "\n"


def _context_block(state):
    """Combine lessons and failure blocks (whichever are present)."""
    parts = []
    lb = _lessons_block(state)
    if lb:
        parts.append(lb)
    fb = _failure_block(state)
    if fb:
        parts.append(fb)
    return "\n".join(parts)


def build_prompt(node_id, node, state):
    """Build a normal (first-attempt) prompt for a workflow node."""
    task = resolve_refs(node.get("with", ""), state)
    context = _context_block(state)

    sections = [f"You are executing workflow node '{node_id}'."]
    if context:
        sections.append(context)
    sections.append("Task:")
    sections.append(task)
    sections.append(RESULT_CONTRACT)

    return "\n\n".join(sections)


def build_retry_prompt(node_id, node, state, attempt, max_attempts=3, previous_summary=None):
    """Build a retry prompt with an explicit RETRY banner.

    Args:
        node_id: Current node
        node: Node dict
        state: Current workflow state (should already have last_failure set)
        attempt: Current attempt number (1-indexed; 2 = second try)
        max_attempts: Maximum attempts allowed
        previous_summary: Optional one-liner about what the prior attempt did

    Returns:
        Complete prompt string with RETRY banner prepended.
    """
    banner_lines = [
        f"!!! RETRY — ATTEMPT {attempt} OF {max_attempts} !!!",
    ]
    if previous_summary:
        banner_lines.append(f"Previous attempt summary: {previous_summary}")
    banner_lines.append(
        "Your previous approach did not work. Read the failure context below. "
        "Try a DIFFERENT approach or address a DIFFERENT aspect of the problem."
    )

    banner = "\n".join(banner_lines)
    normal = build_prompt(node_id, node, state)
    return banner + "\n\n" + normal
