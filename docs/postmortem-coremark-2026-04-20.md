# CoreMark v2 Benchmark — Postmortem (2026-04-20)

**Project:** `/home/scratch.hren_gpu_3/coremark-bench/`
**Workflow:** `workflow-v2.yaml` (DSL v2)
**Wall-clock across all runs:** ~75 min engine time + investigation
**Final corrected score:** **1.7509 CoreMark/MHz** (2x the raw log
value of 0.8754 — matches the rn102g_fecs gold reference ~1.8 within
~3%)
**IPC (measured, timed region):** 0.507 — healthy for an in-order
RV32 scalar

This postmortem exists because the *journey* from "build fails" to
"confident score" exposed several planner and runtime gaps worth
fixing before the next hardware workflow. The score itself is fine;
the planning decisions that got us there are not.

---

## 1. Timeline — 32 steps, 6 classes of issue

From `camflow evolve report` on the project trace.log:

| Node                   | Runs | Success | Avg dur | Notes |
|------------------------|-----:|--------:|--------:|-------|
| `start`                |    1 |   100%  |   152 s | analyze DUT, choose TB strategy |
| `write_new_tb`         |    1 |   100%  |    90 s | wrote minimal TB from scratch |
| `build_clean_filelist` |    1 |   100%  |   157 s | clean -y paths, no tree TB reuse |
| `compile_tb`           |    1 |   100%  |   100 s | 13 compile errors auto-fixed |
| `wave_dump_debug`      |    2 |    50%  |   182 s | first pass missed imem_rd count |
| `smoke_test`           |    2 |    50%  |   174 s | trace.mon.log signal first wrong |
| `run_simv`             |    4 |   100%  |   516 s | 4x — looped with fix_tracer |
| `validate_trace`       |    3 |   100%  |     8 ms | shell check, 100-line floor passed |
| `validate_score_sanity`|  **5** |   **0%**  |     3 ms | ★ 500 K threshold too strict |
| `fix_tracer`           |    3 |   100%  |   140 s | invoked by validate gate failures |
| `parse_score`          |    3 |   100%  |    50 s | parsed 875.4 (raw, wrong) |
| `investigate_score`    |    1 |   100%  |   396 s | diagnosed the 2x gap |
| `report`               |    3 |    67%  |    89 s | final writes REPORT.md with fix |
| `done`                 |    2 |   100%  |     3 ms | terminal |

Overall: **32 steps, 75% step-level success, 100% workflow success**
(the failed steps all fed into a retry that eventually succeeded).

Six classes of issue surfaced, in the order they hit:

1. **Compile errors (13).** auto-fixed by `compile_tb` on first
   attempt. No workflow-level impact — this is exactly the pattern
   DSL v2 is designed for (agent iterates with the compile log in
   CONTEXT).
2. **Missing `-y` paths for generated `NV_GR_FECS_*` modules.**
   Resolved during `build_clean_filelist` by adding `outdir/.../rn102g_fecs`
   directories to the filelist. Cost: 1 retry.
3. **Wave dump signals wrong first pass.** `wave_dump_debug` ran
   twice — first attempt counted `imem_rd_acc` in the wrong
   hierarchical path. Cost: 1 retry.
4. **`trace.mon.log` retirement signal wired to the wrong flop.**
   `smoke_test` failed once and looped to `fix_tracer` three times.
   The tracer in the DUT (`simTop.u_dut.u_NV_RV_top.u_NV_RV_tracer`)
   only writes mnemonic lines when `+enable_riscv_trace` /
   `+enable_fecs_riscv_trace` is passed — which
   `build/run_sim.sh` intentionally omits for wall-clock. Cost:
   3 × 140 s = 7 min in fix_tracer plus 4 × run_simv.
5. **`validate_score_sanity` gate false-negative, 5×.** The 500 K
   threshold was set assuming a full trace.mon.log would carry every
   retired instruction for the 1.14 M cycle timed region. The full
   run (by design) produces an empty mnemonic trace file (see #4), so
   this gate can never pass. **Cost: 5 looped runs of
   run_simv / fix_tracer / parse_score before the loop was broken by
   hand-jumping `--from investigate_score`.** This is the single
   biggest planner error of the run.
6. **2x score gap (raw 0.875 vs. expected 1.8).** The root cause was
   a TB plusarg default — see §3. `investigate_score` diagnosed it
   in 7 min with pure read-only analysis; no re-compile or re-run
   was needed.

---

## 2. Score analysis — 875.4 raw → 1750.9 corrected

### Raw (face-value) score

`parse_score` read the simv log and found:
```
CoreMark Iterations: 1
CoreMark Total ticks: 1142275
```
→ `score_raw = 1 × 1e9 / 1,142,275 = 875.4 iter/sec = 0.8754 CoreMark/MHz`.

Half of the ~1.8 gold reference — obviously wrong.

### Corrected score

`investigate_score` cross-checked the iteration count against the
hex's compile flags and the start_time/stop_time DMEM stores:

- `tree/…/rv30/tb/benchmark/coremark/Makefile` compiles the hex
  with `-DITERATIONS=2 -DPERFORMANCE_RUN=1`. That's authoritative —
  the RTL actually ran 2 iterations.
- `build/run_sim.sh` does **not** pass `+ITER=2` on the simv
  command line.
- The TB (`build/tb/rv30_coremark_tb.sv` L91-94) hard-codes
  `iter_count_plusarg = 1` as the default and echoes it blindly on
  L459 as `"CoreMark Iterations: %0d"`. No correlation with the
  actual benchmark run.
- `start_time` fires once at tb_cycle 594,664; `stop_time` fires once
  at 1,736,939. The 1,142,275-cycle delta brackets **2** full
  iterations (plus the ramp-up iteration before `start_time`).

→ `score_corrected = 2 × 1e9 / 1,142,275 = 1750.9 iter/sec = 1.7509 CoreMark/MHz`.

### IPC cross-validation

Two independent methods, agreed to within 1%:

| Method | Retired | Cycles | IPC |
|---|---:|---:|---:|
| Full-timed-region observation (via `imem_rd_acc` × 1.126 fetch-to-retire ratio) | 578,775 | 1,142,275 | **0.507** |
| Narrow-window from short-run `trace.mon.log` (21,602 lines, continuous, no gaps) | 21,599 | 42,971 | **0.5027** |

Core is healthy. The 2x score gap was a **reporting bug in the TB**,
not a performance bug in the RTL.

---

## 3. Root causes (ordered by severity)

### RC1: TB plusarg default hard-codes Iterations=1
The dominant root cause of the score gap. Three-line fix candidates:

1. Change default: `iter_count_plusarg = 2;` at L93
2. Pass `+ITER=2` in `build/run_sim.sh`
3. Scrape CoreMark's own `ee_printf("Iterations : %lu\n", ...)` from
   the DMEM store observer (best — no hard-coding in the TB at all)

### RC2: Tracer requires plusargs that the full run omits
The built-in DUT tracer (`simTop.u_dut.u_NV_RV_top.u_NV_RV_tracer`)
only writes to `.rv32_trace.mon.log` when `+enable_riscv_trace` or
`+enable_fecs_riscv_trace` is passed. The full run skips these for
wall-clock. Upshot: we have a 178-byte full-run mnemonic trace (just
the header) and a 3.3 MB short-run trace from an earlier validation.

This isn't a bug — it's the designed behavior. But `validate_score_
sanity` was planned on the assumption that `trace.mon.log` covers
the full run. It never does.

### RC3: Boot sequence + tracer signal confusion
First `smoke_test` attempt wired `trace.mon.log` to the wrong flop
and `wave_dump_debug` first counted `imem_rd_acc` in the wrong path.
Both were caught by retries in the same node. These are routine
agent-debug loops — cost was bounded by each node's
`max_retries: 3`. No planner-level failure here.

---

## 4. What the planner should have done differently

Four concrete changes would have shortened this run from ~75 min to
~50 min and eliminated the five wasted `validate_score_sanity` loops.

### 4.1 Require `analyze_tb` before `validate_score_sanity`
The planner generated `validate_score_sanity` at plan time without
knowing how the TB counts iterations. A prerequisite `analyze_tb`
node (read the TB, identify which fields are hard-coded vs. plusarg
vs. computed) would have caught the `iter_count_plusarg = 1` default
BEFORE the validate gate was asked to pass 500 K lines.

Planner rule to add:
> Any node that parses or validates benchmark output MUST be
> preceded by a node that reads the TB and reports which fields are
> derived vs. constant. Never gate on a value whose computation
> the plan hasn't inspected.

### 4.2 Validate with a short run before committing to a full run
The current workflow has `smoke_test` (short sim to confirm tracer +
monitor) and `run_simv` (full 6-min sim). That's good — but
`smoke_test` only checks that logs get written, not that the
log CONTENT is what downstream gates will require. A dedicated
`validate_simv_output` node between `smoke_test` and `run_simv`
(extract score markers from a 90-s quick run, compare against
expected format) would have caught the `CoreMark Iterations: 1`
issue in under 2 minutes instead of waiting for `validate_score_
sanity` to fail 5× on the full run.

Planner rule to add:
> Before any simulation node with `timeout > 5 min`, insert a
> short-run validation that exercises the SAME score parser the
> full run will use, on a known-good quick vector, and asserts the
> parser's output matches an expected shape.

### 4.3 Investigate when observed ≠ expected, DON'T re-run
The workflow looped `run_simv → validate_score_sanity →
fix_tracer → run_simv` three times before we manually jumped to
`investigate_score`. Each loop cost ~10 min and burned retry
budget on the wrong problem (the tracer was fine — the plusarg was
hard-coded).

The planner should have generated an `investigate_score` node
automatically: any time `parse_score` succeeds but the result is
outside a plan-declared "expected range," route to investigation,
not to re-run.

Planner rule to add:
> Every `parse_score` / output-extraction node that produces a
> NUMERIC result MUST have an `expected_range` declaration. A
> result outside the range routes to an investigate node, never
> to a retry of the producing step.

### 4.4 Drop `validate_score_sanity` as a pure line-count gate
The 500 K threshold assumed the full run's trace.mon.log would
cover the whole 1.14 M cycle timed region. That assumption was
wrong (see RC2). A line-count gate on a file the full run doesn't
even produce is architecturally broken.

Replacement: `validate_score_range` — a plan-level range check
(e.g. `0.5 < state.coremark_per_mhz < 5.0`) that catches the 2x
error in one shell command, without touching trace.mon.log.

---

## 5. Lessons for future hardware workflows

These reinforce or extend the rules in `docs/strategy.md`.

### L1. Preflight on EVERY expensive node, no exceptions
`run_simv` has `preflight: test -x {{state.simv_path}} && test -s
{{state.hex_im_path}} && test -s {{state.hex_dm_path}}`. That saved
the 4th run_simv attempt — the simv binary was still there from the
previous iteration, so the node re-executed in seconds instead of
re-compiling.

Extending to the gaps:
- Every parse / validate node needs preflight on the input file's
  **non-emptiness** (not just existence). An empty file produces
  garbage parse results that waste downstream retries.

### L2. Verify the OUTCOME, not the OUTPUT (reinforced)
- `smoke_test` verified trace.mon.log was non-empty → success, but
  the trace content was wrong. Output exists ≠ outcome achieved.
- `validate_score_sanity` verified line count → a proxy for a
  proxy. The real outcome is "score is in the expected range"; the
  line count is a heuristic that happens to correlate with the
  outcome on FULL-trace runs, and breaks on run-scripts that
  disable the tracer for wall-clock.

Strategy doc § 3 already says this. This run shows the cost when
we forget.

### L3. Domain-specific planner rules (hardware) — expand
The current `DOMAIN_PACKS["hardware"]` says "analyze DUT interface
before writing TB; validate DUT alive before running benchmarks;
check memory width." Add:
- **Analyze TB before gating on TB output.** §4.1 above.
- **Range-check any numeric score against an `expected_range` the
  plan declares.** §4.3 above.
- **Never line-count a log file the production run doesn't guarantee
  to populate.** §4.4 above.
- **Two-stage sim: short validation, then full.** §4.2 above.

### L4. Investigate nodes are first-class, not escape hatches
Before this run, `investigate_score` was an ad-hoc hand-authored
node added mid-workflow when the loop got stuck. It produced the
highest-value output of the run (the 253-line INVESTIGATION.md that
pinpointed the root cause in 7 min). The planner should generate
these proactively at plan time, not wait for the human to notice.

### L5. `camflow evolve report` earns its keep on production traces
The timeline table in § 1 came from a single `camflow evolve report`
command. For a 32-step workflow across 4 retry loops, that rollup
is indispensable — no way to eyeball 32 trace entries and spot "the
5x validate_score_sanity failure" without it.

Roadmap implication: trace rollup → planner-feedback loop
(ideas-backlog #43) is high-leverage. This run's 5-loop waste is
exactly the pattern a mature rollup-to-planner pipeline should
have flagged before the 2nd loop.

---

## Cross-references

- `docs/strategy.md` — the rules this run tested
- `docs/lessons-2026-04-19.md` — lessons from the rv32-ecc-verify
  run; appended with Part 5 for today's CoreMark-specific findings
- `docs/ideas-backlog.md` — #36 (preflight), #41 (verify outcome
  not output), #43 (lesson feedback to planner)
- `/home/scratch.hren_gpu_3/coremark-bench/INVESTIGATION.md` — the
  actual investigate_score output (253 lines, evidence-backed)
- `/home/scratch.hren_gpu_3/coremark-bench/REPORT.md` — the final
  benchmark report with both raw and corrected scores
