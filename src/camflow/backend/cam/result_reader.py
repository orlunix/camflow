"""Result reader — reads .camflow/node-result.json written by the agent.

Returns a normalized node result dict, or a fail result if the file
is missing or malformed.
"""

import json
import os

REQUIRED_KEYS = {"status", "summary"}


def read_node_result(project_dir):
    """Read and validate the node result file.

    Args:
        project_dir: Project root directory

    Returns:
        Node result dict with at least: status, summary, state_updates, error.
    """
    result_path = os.path.join(project_dir, ".camflow", "node-result.json")

    if not os.path.exists(result_path):
        return {
            "status": "fail",
            "summary": "agent did not write result file",
            "state_updates": {},
            "error": f"missing: {result_path}",
        }

    try:
        with open(result_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {
            "status": "fail",
            "summary": "malformed result file",
            "state_updates": {},
            "error": str(e),
        }

    if not isinstance(result, dict):
        return {
            "status": "fail",
            "summary": "result is not a JSON object",
            "state_updates": {},
            "error": f"got {type(result).__name__}",
        }

    missing = REQUIRED_KEYS - set(result.keys())
    if missing:
        return {
            "status": "fail",
            "summary": f"result missing keys: {missing}",
            "state_updates": {},
            "error": f"missing: {missing}",
        }

    # Normalize: ensure optional fields have defaults
    result.setdefault("state_updates", {})
    result.setdefault("error", None)

    return result


def clear_node_result(project_dir):
    """Remove the result file before a new node execution."""
    result_path = os.path.join(project_dir, ".camflow", "node-result.json")
    if os.path.exists(result_path):
        os.remove(result_path)
