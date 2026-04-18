"""Error classification.

Implements: spec/error-classifier.md

Two classifications:

1. classify_error(raw_output, parse_ok, result) → error dict or None
   Used by the engine to label what went wrong.

2. retry_mode(error) → "transient" | "task" | "none"
   - transient: infrastructure failure, same prompt will likely work on retry
   - task: agent completed but logic failed, retry needs context-aware prompt
   - none: no error

Taxonomy:
  PARSE_ERROR, AGENT_TIMEOUT, AGENT_CRASH, CAMC_ERROR  → transient
  NODE_FAIL, CMD_FAIL, CMD_TIMEOUT                     → task
"""

TRANSIENT_CODES = {"PARSE_ERROR", "AGENT_TIMEOUT", "AGENT_CRASH", "CAMC_ERROR"}
TASK_CODES = {"NODE_FAIL", "CMD_FAIL", "CMD_TIMEOUT", "CMD_NOT_FOUND", "CMD_ERROR"}


def classify_error(raw_output, parse_ok, result=None):
    """Classify an agent-node error.

    Used for agent nodes where we have raw output + parse success flag + parsed result.
    cmd nodes skip this and set the error dict directly in run_cmd.
    """
    if not parse_ok:
        return {
            "code": "PARSE_ERROR",
            "retryable": True,
            "reason": "agent output could not be parsed as JSON",
        }

    if result and result.get("status") == "fail":
        return {
            "code": "NODE_FAIL",
            "retryable": True,
            "reason": result.get("summary", "node returned fail"),
        }

    return None


def retry_mode(error):
    """Return the retry mode for an error dict.

    Args:
        error: error dict with a "code" key, or None.

    Returns:
        "transient" | "task" | "none"
    """
    if error is None:
        return "none"

    code = error.get("code")
    if code in TRANSIENT_CODES:
        return "transient"
    if code in TASK_CODES:
        return "task"
    # Unknown code — default to task (safer: will inject context rather than blind retry)
    return "task"
