"""Memory store.

Implements: spec/memory.md

State carries a `lessons` list (max 10, FIFO-pruned, deduped by exact string).
Agents can add lessons via `state_updates.new_lesson`; the engine applies
dedup + pruning on top of what the agent wrote.
"""

MAX_LESSONS = 10


def init_memory():
    """Initial memory struct. Kept for compatibility with older callers."""
    return {
        "summaries": [],
        "lessons": [],
    }


def add_summary(memory, text):
    if text:
        memory["summaries"].append(text)
    return memory


def add_lesson(memory, lesson):
    """Legacy: append without dedup/prune."""
    if lesson:
        memory["lessons"].append(lesson)
    return memory


def add_lesson_deduped(lessons, lesson, max_lessons=MAX_LESSONS):
    """Append `lesson` to `lessons` (a list) with exact-string dedup + FIFO prune.

    Args:
        lessons: existing list of strings. Mutated in place AND returned.
        lesson: string to add. Empty/None is a no-op.
        max_lessons: cap for the list length.

    Returns:
        The (possibly-pruned) lessons list. Returns the same list object passed in.
    """
    if not lesson:
        return lessons

    lesson = lesson.strip()
    if not lesson:
        return lessons

    if lesson in lessons:
        # Already known — no-op
        return lessons

    lessons.append(lesson)

    while len(lessons) > max_lessons:
        lessons.pop(0)  # FIFO — drop oldest

    return lessons


def prune_lessons(lessons, max_lessons=MAX_LESSONS):
    """Prune a lessons list down to max_lessons by dropping oldest entries."""
    while len(lessons) > max_lessons:
        lessons.pop(0)
    return lessons
