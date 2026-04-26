"""Steward agent — project-scoped persistent overseer.

The Steward is one camc agent per project (``.camflow/`` directory).
Born with the first flow, lives across all subsequent flows in the
project, **never auto-exits**. Only a human kills it (via
``camflow steward kill`` or ``camc rm``).

See ``docs/design-next-phase.md`` §7 for the full design. This package
ships the lifecycle pieces:

  - ``spawn``  — boot-pack assembly + ``camc run``
  - persistence — ``.camflow/steward.json`` pointer file
  - status     — alive/dead probe via camc

Event emission (engine → Steward), the autonomy config, the compaction
handoff, and the ``camflow chat``/``ctl`` interfaces live alongside
this package or under ``cli_entry/``.
"""

from camflow.steward.spawn import (
    STEWARD_POINTER_FILE,
    is_steward_alive,
    load_steward_pointer,
    spawn_steward,
)

__all__ = [
    "STEWARD_POINTER_FILE",
    "is_steward_alive",
    "load_steward_pointer",
    "spawn_steward",
]
