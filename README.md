# cam-flow

`cam-flow` is a lightweight stateful workflow engine for agent execution.

It is designed for workflows that are **not** well modeled as pure DAGs:

- loopable agent flows
- retry and recovery
- wait / human approval / resume
- structured handoff and checkpointing
- deterministic runtime control around `skill`, `cmd`, and `agent`

## Core idea

Top-level workflow execution is modeled as a **stateful graph workflow**.

- Nodes run `skill`, `cmd`, or `agent`
- Transitions are controlled by a small DSL
- Runtime owns `pc`, `status`, `resume_pc`, `memory`, and `trace`
- Logs and artifacts are stored separately from trace
- Handoff/checkpoint is a first-class pattern for resume and context compression

## Project status

This repository currently contains:

- project specifications
- a minimal runtime skeleton
- a runnable demo shape
- a roadmap for v0.1 and beyond

## Repository layout

```text
cam-flow/
  README.md
  spec/
    runtime-state-machine.md
    node-output-contract.md
    transition-resolution.md
    dsl-node-schema.md
    trace-schema.md
    runtime-skeleton.md
  engine/
    __init__.py
    state.py
    executor.py
    resolver.py
    trace.py
    logsink.py
  demo/
    workflow.yaml
    demo.py
  cli/
    main.py
```

## Design decisions

### Chosen

- stateful graph workflow
- small DSL: `do`, `if`, `goto`, `set`, `fail`
- executor types: `skill`, `cmd`, `agent`
- append-only trace
- memory / trace / artifact separation
- explicit handoff/checkpoint node pattern
- runtime-controlled workflow state

### Not in v0.1

- parallel execution
- BPMN / visual-first modeling
- distributed workers
- complex expression language
- heavy policy system

## v0.1 goals

- parse simple workflow definitions
- run `cmd`, `skill`, `agent`
- normalize node output contract
- resolve transitions deterministically
- persist runtime state
- append trace each step
- support wait and resume
- support loop + handoff

## Immediate to-do

- [ ] implement minimal runtime loop
- [ ] implement transition resolver from spec
- [ ] implement trace writer
- [ ] implement log/artifact path allocation
- [ ] add YAML loader and validator
- [ ] wire demo to the runtime skeleton
- [ ] add a real Claude-backed executor adapter

## Near-term to-do

- [ ] retry budget and backoff policy
- [ ] CLI inspect / resume / override
- [ ] handoff templates
- [ ] side-effect / commit boundary
- [ ] idempotency for effectful nodes

## Long-term direction

`cam-flow` is intended to become the advanced orchestration kernel for CAM-style agent systems:

- DAG remains useful for simple dependency scheduling
- `cam-flow` handles loopable, resumable, human-in-the-loop agent execution

## First success criterion

A first milestone is successful when this workflow can run end-to-end:

```text
start -> test -> analyze -> handoff -> approve(wait) -> resume -> fix -> test -> done
```

with:

- persisted runtime state
- append-only trace
- log/artifact refs
- handoff artifact
- working wait/resume path
