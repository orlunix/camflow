"""Progress reporting — stdout line + .camflow/progress.json.

External tools can poll .camflow/progress.json to watch engine status.
"""

import json
import os
import time


def write_progress(project_dir, step, pc, node_exec_count, attempt, max_retries,
                   node_started_at, workflow_started_at):
    """Write .camflow/progress.json atomically-ish (one writer assumed)."""
    camflow_dir = os.path.join(project_dir, ".camflow")
    os.makedirs(camflow_dir, exist_ok=True)
    path = os.path.join(camflow_dir, "progress.json")

    now = time.time()
    data = {
        "step": step,
        "pc": pc,
        "node_execution_count": node_exec_count,
        "attempt": attempt,
        "max_retries": max_retries,
        "elapsed_seconds": int(now - node_started_at) if node_started_at else 0,
        "workflow_elapsed": int(now - workflow_started_at) if workflow_started_at else 0,
        "ts": now,
    }

    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp, path)


def format_progress_line(step, pc, node_exec, attempt, max_retries, exec_mode, elapsed):
    """One-line progress for stdout."""
    return (
        f"[{step}] {pc} (exec {node_exec}, attempt {attempt}/{max_retries}) "
        f"— {exec_mode} — {elapsed}s elapsed"
    )
