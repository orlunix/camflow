# cam-flow: Possible Improvements (from production runs 2026-04-19/20)

Based on: ECC Verification, CoreMark Benchmark, BPU Enhancement workflows.

---

## Priority A: Should do soon

### A1. Engine silent fail → notification + clear reason
Engine stops with just `status: failed` in state.json. No notification, no explanation.
- state.json should include `stop_reason` and `suggestion`
- Optional webhook notification (Teams via messaging skill)
- `camflow status` should show WHY it stopped, not just "failed"

### A2. Handoff prompt between agents
Agents pass only a one-line summary. Next agent lacks detailed context.
- Add `handoff` field to node-result.json (detailed paragraph)
- state_enricher stores last_handoff
- prompt_builder injects into CONTEXT block
- **STATUS: implementing now**

### A3. Brainstorm on repeated failure
Engine hits max_node_executions and dies. Should auto-brainstorm instead.
- Spawn brainstorm agent with all failure summaries
- Agent analyzes pattern, recommends new strategy
- Engine resets count, retries with new strategy
- Only truly fails after brainstorm + second round exhausted
- **STATUS: implementing now**

### A4. Heartbeat for long-running nodes
No way to know if engine or simv is alive vs hung during long sims.
- Engine writes heartbeat timestamp to state.json every 30 seconds
- `camflow status` shows "last heartbeat 15s ago" vs "last heartbeat 10m ago (possibly dead)"

### A5. Structured attempted_strategies in state
Agents in a retry loop don't know what previous agents already tried.
- state.attempted_strategies: list of {approach, result, why_failed}
- state_enricher appends on each fail
- prompt_builder renders "DO NOT repeat these approaches: ..."

---

## Priority B: Should do this month

### B1. Domain-specific planner rules
Generic planner misses hardware-specific requirements (analyze DUT before build, validate before sim, check memory width).
- Hardware rules: analyze_dut → validate_boot → validate_ipc → full_run
- Software rules: run one test → full suite → deploy
- Planner loads rule pack based on workflow domain

### B2. Self-adaptive node timeout
First build takes 34 min, second takes 30 sec. But timeout stays 3600s for both.
- Engine tracks historical duration per node_id from trace
- Auto-sets timeout = 2x historical average
- First run uses configured default

### B3. Workflow version snapshot
We changed workflow.yaml mid-run multiple times. Trace doesn't record which version.
- Engine copies workflow.yaml to .camflow/workflow.snapshot.yaml at startup
- Trace entries reference the snapshot hash
- `camflow diff <run1> <run2>` can compare workflow versions

### B4. Cost tracking
55 iterations spawning agents. No idea of total cost.
- Trace entries include token_usage (already in eval fields but not populated from camc)
- Engine aggregates total_tokens, estimated_cost in state
- `camflow status` shows cost so far

### B5. Auto-resume via camc monitor
Engine crash → human must resume. Should be automatic.
- camc monitor detects engine process died
- Auto-runs `camflow resume workflow.yaml --retry`
- Max 3 auto-resumes before giving up
- Or use camflow.toml adapter so camc manages engine like any agent

### B6. Cross-workflow knowledge sharing
CoreMark learned "+enable_fecs_riscv_trace". BPU rediscovered same thing.
- Workspace-level lessons.json shared across workflows in same project
- Or project-level CLAUDE.md accumulates lessons automatically
- `camflow evolve` can merge lessons from multiple trace.logs

---

## Priority C: Nice to have

### C1. Smarter preflight with state comparison
Current preflight is static shell cmd. Some checks need state comparison (IPC > baseline_IPC).
- Support `preflight: "{{state.bpu_ipc}} > {{state.baseline_ipc}}"` as expression
- Or support `preflight_script:` that gets state vars as env vars

### C2. verify checks outcome not output
Current verify: `test -f simv` (file exists). Should be: `test -s trace.mon.log` (execution happened).
- Planner rule: "verify must check the GOAL was achieved, not just that a file was produced"
- Auto-suggest verify based on node type (build → binary exists + compiles, sim → trace non-empty)

### C3. Node execution replay
Can we replay a single node from trace without re-running the whole workflow?
- `camflow replay --step 5` reads input_state from trace, runs node, compares output
- Useful for debugging: "what would happen if I re-ran step 5 with current code?"

### C4. Parallel-safe state updates
Currently state.json is read-modify-write. If parallel nodes (future) both update state, last write wins.
- Use per-node output files, engine merges
- Or use lock file

### C5. Agent performance profiling
Which agents are slow? Which waste tokens? Which have high retry rates?
- `camflow evolve report` already does some of this
- Add per-agent-type stats: "rtl-debugger averages 3.2 min, build-engineer averages 0.5 min"

### C6. Workflow templates
Common patterns (fix-test loop, build-validate-run, analyze-plan-execute) become reusable templates.
- `camflow new --template fix-test-loop --project /path`
- Templates carry preflight, verify, methodology defaults

### C7. Conditional node skip
Some nodes are only needed in certain conditions.
- `skip_if: "{{state.bpu_score}} > 2.0"` — already good enough, skip optimization
- Avoids wasted runs when goal already achieved

### C8. Multi-model routing
Use Opus for brainstorm/analyze, Haiku for simple fix/build tasks.
- `model: haiku` in node config
- Planner assigns model based on node complexity
- Saves cost on simple nodes

---

## Lessons that apply to all improvements

1. **Preflight principle**: cheap check before expensive operation. Universal.
2. **verify checks outcome not output**: did we achieve the goal, not just produce a file.
3. **handoff > summary**: detailed context for next agent, not just a sentence.
4. **brainstorm > hard fail**: analyze failure pattern before giving up.
5. **domain knowledge upfront**: interview user deeply, put everything in CLAUDE.md before starting.
6. **one node = one verifiable deliverable**: smaller nodes = faster feedback.
