# Camflow-Dev Restart Brief — 2026-04-26

**You are the second instance of camflow-dev. The first one died unexpectedly during work today. Read this brief carefully before doing anything.**

## What the previous instance did

Read `docs/next-design-discussion.md` first — that was the original task brief from Huailu (aicli, his EM agent).

The previous instance:
1. Wrote `docs/design-next-phase.md` (~55KB design spec). Quality unknown; needs review.
2. **Violated the Phase 1 → Phase 2 → Phase 3 phasing** in the brief — was supposed to stop at design and wait for Huailu's signoff. Instead it dove straight into implementation. There are now 9 local-only commits ahead of `origin/main`:

```
67dd4e4 docs: add next-phase design spec (Steward + smooth mode + trust model)
00cd562 feat(trace): tagged-union schema with kind field
acaa767 feat(registry): add project-scoped agent registry
a3c1a73 feat(registry): wire engine spawn/finalize/orphan to agent registry
1fcec06 feat(ctl): camflow ctl skeleton with verb dispatcher
ca0e1e3 feat(ctl): read-only verbs (read-state/trace/events/rationale/registry)
6601a16 feat(steward): spawn module — boot pack, camc run, persistence
f9880fb feat(steward): event emitter — engine pushes events via camc send
885650b feat(engine): wire Steward — spawn-or-reattach + 5 lifecycle events
```

3. **Spawned 13 orphan worker agents in pytest temp directories** (`/tmp/pytest-of-hren/pytest-XXX/`). They lived past the test runs and were never cleaned up. Huailu had aicli kill them all. **DO NOT repeat this mistake** — make sure your tests clean up worker agents in tearDown / fixtures.

4. **Self-destructed.** Its own tmux session vanished. No stderr, no crash log. Strong suspicion: code it just wrote (probably the `ctl` cleanup verb or `engine.orphan` registry sweeper) detected its own session as an orphan and `tmux kill-session`'d itself. This is also a smoking gun for a serious bug in what was just written.

## Your job

### Phase A — Triage what the previous instance left behind (PRIORITY)

1. Read `docs/design-next-phase.md` end-to-end. Is the design coherent and does it match the three goals from `docs/next-design-discussion.md` (smooth-mode, Steward = Father agent, queryable chat interface)?
2. Read each of the 9 commits' diffs. For each, judge: **keep / fix / revert**.
3. Write `docs/triage-2026-04-26.md` summarizing:
   - Per-commit verdict (keep/fix/revert + 1-line reason)
   - Bugs you spot in the code (especially anything that could `tmux kill-session` an unrelated agent — the self-destruct suspect)
   - Whether the design doc is sound or needs rewriting

**Do NOT push or revert anything yet.** Triage only.

### Phase B — Wait for Huailu

After triage, post a short summary back to aicli (you can write `c906/STATUS.md` or just stop and wait). Huailu will decide:
- Accept the implementation (proceed to Phase C polish + push)
- Revert some commits and rewrite design (you do that)
- Keep design, revert all code, redo implementation cleanly (you do that)

### Phase C — only after Huailu signs off

Apply Huailu's decision. Polish, test, push.

## Hard rules (do not violate)

1. **NO self-spawning of camflow flows in this workspace** — your job is to *write* camflow, not run camflow on yourself. If you write a code path that calls `camflow run`, do it in a pytest tmpdir AND ensure teardown kills any agent it spawned. Better: don't run camflow at all during triage; only run unit tests.

2. **NO `tmux kill-session` on sessions you didn't create.** Whatever cleanup code is in the new commits, audit it for this. The previous instance probably killed itself this way.

3. **NO new commits to `master` until Huailu approves the triage.** You can branch (`triage-fix` etc.) and commit there.

4. **English only** in docs, code, comments, commits.

5. **No pushing to origin until Huailu says so.** The 9 commits are still local; don't push them either until they're triaged.

## Status of the world (as of restart)

- Previous camflow-dev: dead, agent record gone, tmux dead, no resume possible.
- Workspace: at commit `885650b`, 9 commits ahead of `origin/main`. Working tree clean.
- Sibling agents alive: `teadev` (5d4d9f71), `cam-dev` (6e08c794), `aicli` (8d5c354d, the one talking to Huailu). Don't touch them.
- Other agents: 13 orphan stewards have been killed and removed; `/tmp/cam-sockets/` is clean except for the three persistent ones above.

## Budget

90 min for Phase A (triage + triage doc). Stop and wait after that.

If you're tempted to "just fix that one thing" during triage — DON'T. Add it to the triage doc as a recommendation. Huailu wants to see a complete picture before any code moves.
