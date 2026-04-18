"""Trace entry builder.

Produces a self-contained dict for one step of engine execution.
Written to trace.log as one JSONL line per step.

Entry schema (see docs/cam-phase-plan.md §7.1):
  step, ts_start, ts_end, duration_ms, node_id, do, attempt, is_retry,
  retry_mode, input_state, node_result, output_state, transition,
  agent_id, exec_mode, completion_signal, lesson_added, event
"""

import copy
from datetime import datetime, timezone


def _utc_iso(ts_float):
    """Convert a Unix timestamp (float) to ISO 8601 UTC with millisecond precision."""
    dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
    # format with milliseconds, drop microsecond tail
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def build_trace_entry(
    step,
    node_id,
    node,
    input_state,
    node_result,
    output_state,
    transition,
    ts_start,
    ts_end,
    attempt=1,
    is_retry=False,
    retry_mode=None,
    agent_id=None,
    exec_mode="cmd",
    completion_signal=None,
    lesson_added=None,
    event=None,
):
    """Build a single trace entry.

    Deep-copies `input_state` and `output_state` so later mutations
    don't corrupt the recorded snapshot.
    """
    return {
        "step": step,
        "ts_start": _utc_iso(ts_start),
        "ts_end": _utc_iso(ts_end),
        "duration_ms": int((ts_end - ts_start) * 1000),
        "node_id": node_id,
        "do": node.get("do", ""),
        "attempt": attempt,
        "is_retry": is_retry,
        "retry_mode": retry_mode,
        "input_state": copy.deepcopy(input_state),
        "node_result": copy.deepcopy(node_result) if node_result is not None else None,
        "output_state": copy.deepcopy(output_state),
        "transition": copy.deepcopy(transition) if transition is not None else None,
        "agent_id": agent_id,
        "exec_mode": exec_mode,
        "completion_signal": completion_signal,
        "lesson_added": lesson_added,
        "event": event,
    }
