# Next-Phase CamFlow Design — Discussion Brief

**Date**: 2026-04-25
**Author**: Huailu (via aicli)
**Status**: Open for design — DO NOT IMPLEMENT YET. Write a design doc first.

---

## Where we are

Watchdog is done and shipped:
- Commits `e9bb761` → `3b0f1f5` on origin/main
- Engine + Watchdog dual-process pattern works
- Heartbeat, lock, stale auto-heal, max-restart all in
- `camflow status` shows watchdog state

Read `docs/architecture.md`, `docs/strategy.md`, `docs/roadmap.md`,
`docs/self-monitoring.md` first to refresh context before reading the
rest of this brief.

---

## Three design ideas Huailu wants explored together

These came out of a chat between Huailu and his EM agent (aicli) on
2026-04-25. They are interconnected, not independent — design the
union, not three separate features.

### Idea 1 — Default smooth mode (camc-like UX)

Pain point: writing `workflow.yaml` is heavy. Should be optional,
not the default.

Target UX:

```
camflow "build watchdog feature with tests, deploy to PDX-098"
   # default: planner agent decomposes → workflow.yaml → CLAUDE.md
   #          → per-node prompts → auto-runs

camflow run my.yaml         # explicit yaml — old behavior
camflow plan -i "..."       # interactive: review/edit plan before run
```

Open question: should the planner show plan summary + N-second
countdown before running (so user can Ctrl-C), or run immediately?
Huailu leans toward "show summary + countdown".

### Idea 2 — Father / worker agent hierarchy

Today: `camflow run` spawns workers via camc, no overseer.

Proposal: every flow starts with **one persistent Father Agent** plus
its workers.

- Father is event-driven (wakes on `child done`, `child failed`,
  `heartbeat stale`, `user message`). Sleeps otherwise → near-zero
  idle cost.
- Father does NOT read raw logs. Engine surfaces structured events
  to it (`node X done, output: Y`, `node X failed: msg`).
- Father holds: goal, plan, decision history, why each rerouting
  happened.
- Father's tools: `replan(reason)`, `kill_child(id)`,
  `spawn_child(node)`, `ask_user(question)`, `summarize_to_user()`.

Layering (proposal):

```
User ←→ Father (LLM) ←→ Engine (Python, deterministic) ←→ Workers (camc agents)
                              ↑
                          Watchdog (existing)
```

Engine stays in charge of locks, scheduling, state — those must be
deterministic. Father is the LLM-driven PM layer above.

### Idea 3 — Father as queryable chat interface

This is the **real justification** for Father.

Today, to ask "what is flow X doing right now?" you have to read raw
log / parse JSON / interpret `camflow status`. There is no
human-friendly query interface.

With Father:

```
camc send <father> "现在状况？"
   → Father summarizes: 3 nodes done, node 4 retrying due to
     compile error, ETA 20m

camc send <father> "为啥节点3挂了？"
   → Father gives the actual cause + its decision: "worker reported
     OOM, I downgraded to fewer parallel jobs and restarted"

camc send <father> "把节点3 kill 重启"
   → Father takes the operator command via tool

camc send <father> "在 PDX-098 上加一台 build 节点"
   → Father modifies plan via tool
```

Father appears in `camc list` like any other agent. You can talk to
it as if it were a normal chat agent.

**Bonus benefit — solves compaction pain**: workers get compacted
and lose context all the time (we hit this 3 times today on the
c906-fix agent). Father is the long-term memory of the flow — when
a worker is compacted, Father can re-brief it.

---

## Reference frameworks (for inspiration, not copy)

Findings from a quick web survey done 2026-04-25:

- **CrewAI** — hierarchical Manager-Agent pattern. Closest to the
  Father design. Manager doesn't do tasks, only delegates and
  reads outputs. Worth borrowing the pattern.
- **MetaGPT** — SOP-driven roles (PM, Architect, Engineer, QA).
  Heavier than what we need but the role-separation idea is sound.
- **Hermes (Nous Research, Feb 2026)** — currently weaker than our
  CAM stack on orchestration (single parent + ephemeral workers,
  no inter-worker comm, no DAG). Their roadmap (Issue #344) is
  evolving toward what we already have. Real Hermes
  differentiator is **self-improving skill loop** — interesting
  for skillm later, NOT relevant to Father.
- **AutoGen** — flat, every agent sees full conversation. Not
  hierarchical enough.
- **LangGraph** — deterministic graph, no LLM PM. Engine-only,
  no Father.

Net: CAM is already ahead of Hermes on orchestration. CrewAI's
Manager pattern is the closest reference for Father. Don't try to
clone Hermes Issue #344 — it's vapor.

---

## What I want from you (camflow-dev agent)

**DO NOT** start coding. The first deliverable is a design spec.

### Phase 1 — Design spec (this is your current task)

Write `docs/design-next-phase.md` covering:

1. **Default-smooth-mode UX** — exact command surface, planner agent
   prompt sketch, where plan output lives, how user reviews/edits
2. **Father agent**
   - Concrete role definition (what it does / does NOT do)
   - Event interface from engine (what events, what shape)
   - Tool surface (the verbs Father has)
   - When it sleeps vs wakes (event-driven, not polling)
   - Lifecycle: spawned by `camflow run`, dies when flow done +
     archived
   - How its session is managed (it WILL grow long; what's the
     compact / handoff strategy)
3. **Father-as-chat-interface**
   - How it appears in `camc list`
   - How `camc send` routes to it
   - What kinds of questions/commands it answers (worked
     examples)
4. **Multi-flow scenario** — 3 flows running simultaneously: 1
   mega-Father or 3 independent Fathers? (Huailu leans 3
   independent; justify or push back)
5. **Open questions** — list anything you can't decide alone, with
   options + your recommendation. Huailu will pick.

Cite `architecture.md` / `strategy.md` for any change that touches
existing modules. Reuse vocabulary from those docs where
possible.

### Phase 2 — only after Huailu signs off the spec

Implementation plan + breakdown into commits.

### Phase 3 — implementation
You write code, tests, deploy via `scripts/release.sh`, push to origin.

**Budget for Phase 1**: 60 minutes. If something is unclear, write
"open question" instead of guessing.

---

## Constraints / lessons from past mistakes

- **English only** in docs/comments/changelogs (multinational team)
- **Don't break the watchdog** — it just shipped, must keep working
  during this refactor
- **Engine stays deterministic** — Father is additive, not a
  replacement for Engine's locking/scheduling
- **Don't over-design** — Huailu prefers iterative. v0 of Father
  can be very dumb; we'll grow it
- **Session compaction is real** — design Father with the
  assumption it WILL hit the context wall and need to handoff
  itself
