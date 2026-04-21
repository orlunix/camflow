"""Shared persistence utilities for backends.

Atomic JSON state file + fsync'd JSONL append-only trace log.
"""

import json
import os
from pathlib import Path


def _json_default(obj):
    """Fallback encoder for values json.dumps can't handle natively.

    Agent results and subprocess-captured outputs occasionally include
    raw ``bytes`` (e.g. a ``TimeoutExpired`` before ``text=True`` kicks
    in, or a tool returning binary) that leak into state. Crashing the
    whole engine on an unserializable field loses all previous progress.
    Prefer best-effort stringification over a hard failure.
    """
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8", errors="replace")
        except Exception:
            return repr(obj)
    if isinstance(obj, set):
        return sorted(obj)
    # Preserve the standard "not serializable" error for genuinely
    # unexpected types — callers rely on it as a signal.
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def ensure_parent(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _fsync_dir(path):
    """fsync the directory containing `path` so renames are durable."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    try:
        fd = os.open(parent, os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_state(path, state):
    """Non-atomic write — kept for backward compatibility. Prefer save_state_atomic."""
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=_json_default)


def save_state_atomic(path, state):
    """Atomically write state to `path`.

    Procedure:
      1. Write to `path.tmp.<pid>`
      2. fsync the tmp file
      3. os.rename(tmp, path) — atomic on POSIX
      4. fsync the parent directory

    If any step raises, the original `path` (if any) remains intact.
    """
    ensure_parent(path)
    tmp = f"{path}.tmp.{os.getpid()}"

    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=_json_default)
            f.flush()
            os.fsync(f.fileno())

        os.rename(tmp, path)
        _fsync_dir(path)
    except Exception:
        # Clean up temp file on any failure
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def load_state(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def append_trace(path, entry):
    """Non-atomic append — kept for backward compatibility. Prefer append_trace_atomic."""
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=_json_default) + "\n")


def append_trace_atomic(path, entry):
    """Append one JSON line to trace, flushed and fsync'd before returning."""
    ensure_parent(path)
    line = json.dumps(entry, ensure_ascii=False, default=_json_default) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def load_trace(path):
    """Load JSONL trace. Skips (with warning) any trailing malformed line."""
    items = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    # Trailing malformed line from a crash; skip and move on
                    continue
    except FileNotFoundError:
        pass
    return items
