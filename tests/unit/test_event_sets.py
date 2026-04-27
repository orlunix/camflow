"""Drift catcher for the two closed event sets.

camflow has two distinct closed sets that name "events":

  * ``camflow.backend.cam.tracer.EVENT_KINDS``  — values of the
    ``kind`` field in trace.log (audit / timeline channel).

  * ``camflow.steward.events.EVENT_TYPES``      — the engine→Steward
    push types that arrive at the Steward as
    ``[CAMFLOW EVENT] {"type": "...", ...}``.

These two sets play different roles. A Steward push lands in trace.log
as ``kind="event_emitted"`` with the type carried in ``event_type`` —
NOT as its own kind. So the sets are intentionally different.

A few names overlap on purpose:
    flow_started, flow_terminal, flow_idle,
    compaction_detected, handoff_completed
…because these are project-level state transitions that warrant a
direct audit entry in addition to the Steward push. The current
implementation only wires the Steward push for these; the direct
audit emission is reserved for Phase B (see triage doc §5.2).

This file documents and protects those invariants:

  1. Every ``kind`` actually emitted via ``build_event_entry`` in the
     codebase must be in ``EVENT_KINDS``. (Catches typos.)
  2. Every event type actually emitted via ``emit`` must be in
     ``EVENT_TYPES``. (Catches typos.)
  3. The intentional overlap is exactly the documented set — no
     accidental new collisions slip in.
  4. Each set has a non-trivial size (sanity).
"""

from __future__ import annotations

import re
from pathlib import Path

from camflow.backend.cam.tracer import EVENT_KINDS, STEP_KIND
from camflow.steward.events import EVENT_TYPES


SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "camflow"


# Names that appear in BOTH sets on purpose — design §13.1 + §7.3.
# These are project-level state transitions: the engine pushes them
# to the Steward (events.EVENT_TYPES) AND records them as direct
# audit entries (tracer.EVENT_KINDS). The current implementation only
# wires the Steward push; direct audit emission lands in Phase B.
#
# ``compaction_detected`` and ``handoff_completed`` are tracer-only
# (the engine writes them when handing off Stewards; nobody pushes
# them to a Steward).
INTENTIONAL_OVERLAP = frozenset({
    "flow_started",
    "flow_terminal",
    "flow_idle",
})


# ---- 1. Sanity --------------------------------------------------------


def test_event_kinds_non_trivial():
    assert "agent_spawned" in EVENT_KINDS
    assert "event_emitted" in EVENT_KINDS
    assert STEP_KIND not in EVENT_KINDS  # step is its own thing
    assert len(EVENT_KINDS) > 10


def test_event_types_non_trivial():
    assert "node_done" in EVENT_TYPES
    assert "engine_resumed" in EVENT_TYPES
    assert len(EVENT_TYPES) > 5


# ---- 2. Intentional overlap is exactly what's documented --------------


def test_intentional_overlap_is_exact():
    actual_overlap = EVENT_KINDS & EVENT_TYPES
    assert actual_overlap == INTENTIONAL_OVERLAP, (
        "Overlap between tracer.EVENT_KINDS and steward.events.EVENT_TYPES "
        "drifted.\n"
        f"  expected: {sorted(INTENTIONAL_OVERLAP)}\n"
        f"  actual:   {sorted(actual_overlap)}\n"
        "If the new collision is intentional, update INTENTIONAL_OVERLAP "
        "in this test AND the cross-reference docstrings in "
        "tracer.py and steward/events.py. Otherwise rename one side."
    )


# ---- 3. Every emitted kind is in the closed set -----------------------


_BUILD_EVENT_ENTRY_KIND_RE = re.compile(
    r'build_event_entry\(\s*\n?\s*"(?P<kind>[a-z_]+)"',
    re.MULTILINE,
)


def _kinds_emitted_in_source() -> set[str]:
    """Scan the codebase for ``build_event_entry("<kind>", ...)`` calls
    and return every literal ``<kind>`` it finds.

    Only literal first-arg strings are caught; a few callers pass a
    variable (e.g. ``hooks.py`` uses ``trace_kind = "agent_completed"
    if success else "agent_failed"``). Those are exercised by
    ``test_registry_hooks.py``; here we check every literal use.
    """
    found: set[str] = set()
    for py in SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in _BUILD_EVENT_ENTRY_KIND_RE.finditer(text):
            found.add(m.group("kind"))
    return found


def test_every_literal_emitted_kind_is_known():
    actual = _kinds_emitted_in_source()
    # The literals we know are emitted today. If the scanner finds
    # anything missing from EVENT_KINDS, fail with a useful message.
    missing = actual - EVENT_KINDS
    assert not missing, (
        "build_event_entry called with kind(s) not in EVENT_KINDS: "
        f"{sorted(missing)}. Either add them to EVENT_KINDS in tracer.py "
        "or fix the typo at the call site."
    )
    # And we should at least see the kinds we hardcoded; this guards
    # the regex against a refactor that breaks the scan.
    assert "agent_spawned" in actual
    assert "event_emitted" in actual
    assert "control_command" in actual


# ---- 4. Every emitted Steward type is in the closed set --------------


_EMIT_TYPE_RE = re.compile(
    # ``emit(<arg1>, "<type>"`` where <arg1> is a single argument
    # expression (no commas, no closing paren — so we don't match
    # ``emit()`` followed by an unrelated quoted string later in the
    # same source).
    r"""(?:^|[^.\w])emit\(\s*[^,)]+,\s*\n?\s*['"](?P<typ>[a-z_]+)['"]""",
    re.MULTILINE,
)


def _types_emitted_in_source() -> set[str]:
    """Find every literal type passed as the second positional arg of
    ``emit(project_dir, "<type>", ...)`` in the steward path.

    Excludes test files and the emit() definition itself."""
    found: set[str] = set()
    for py in SRC_ROOT.rglob("*.py"):
        if py.name == "events.py" and "steward" in str(py):
            # Skip the emit() definition itself, but DO scan its
            # convenience wrappers (emit_flow_started etc.) by reading
            # only those lines that are inside the wrapper bodies.
            text = py.read_text(encoding="utf-8")
            # Just collect string literals after `emit(` calls inside
            # this file.
            for m in _EMIT_TYPE_RE.finditer(text):
                found.add(m.group("typ"))
            continue
        text = py.read_text(encoding="utf-8")
        for m in _EMIT_TYPE_RE.finditer(text):
            found.add(m.group("typ"))
    return found


def test_every_literal_emitted_type_is_known():
    actual = _types_emitted_in_source()
    missing = actual - EVENT_TYPES
    assert not missing, (
        "emit() called with type(s) not in EVENT_TYPES: "
        f"{sorted(missing)}. Either add them to EVENT_TYPES in "
        "steward/events.py or fix the typo at the call site."
    )
    # Sanity: these wrappers exist and the regex catches their
    # underlying emit() calls.
    assert "flow_started" in actual
    assert "node_done" in actual
    assert "engine_resumed" in actual
