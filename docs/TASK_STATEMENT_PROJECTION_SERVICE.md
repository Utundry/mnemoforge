# Task Statement Projection Service

Status: proposed  
Project: `supermemory`  
Priority: high

## Why This Service Exists

Task framing mutates during real work.

A project may start with one problem statement and end with:
- narrowed scope
- new constraints
- revised success criteria
- rejected options
- deferred side findings

If this evolution lives only in chat or ad hoc notes, agents lose the current task definition and future sessions cannot explain why the task changed.

## Goal

Create a projection service that reconstructs the current task statement from stored task artifacts in `supermemory`.

This service should answer two different questions:

1. `What is the current task statement right now?`
2. `How did the task statement evolve from its original form, and why?`

## Architectural Position

This service is a projection layer, not a new source of truth.

It should read from:
- `task`
- `assumption`
- `constraint`
- `definition_of_done`
- `task_checkpoint` records stored as tagged `task_change`
- `decision_candidate`
- `chosen_decision`
- `task_change`
- `deferred_finding`
- `verification_result`
- `result_summary`
- `handoff_summary`

It should not invent task truth from chat alone.

## Primary Outputs

### 1. Current task statement

A compact machine-facing and human-facing summary of:
- current objective
- active scope
- explicit assumptions
- active constraints
- current done condition
- known blockers
- unresolved ambiguities

### 2. Task statement evolution

A structured change log from the initial statement to the current one:
- what changed
- when it changed
- why it changed
- which artifact justified the change
- whether the change was explicit or inferred

### 3. Confidence / quality report

A service-level assessment of whether the current statement is grounded enough:
- complete
- partial
- weak

## Required Views

### View A: `current`

Purpose:
- feed agents with the latest task framing before work continues

Output shape:
- task id
- current objective
- active assumptions
- active constraints
- done condition
- current priority
- open questions
- capture quality

### View B: `timeline`

Purpose:
- show how task framing evolved over time

Output shape:
- ordered framing deltas
- source artifact id
- timestamp
- reason for change
- confidence

### View C: `diff`

Purpose:
- compare `initial framing` vs `current framing`

Output shape:
- added scope
- removed scope
- changed constraints
- changed assumptions
- changed done criteria
- newly deferred work

## Model Discipline

This service should prefer cheap capture and cheap projection.

Default execution order:
1. deterministic assembly from existing task artifacts
2. local SLM for compact summary or framing normalization
3. cheap cloud fallback
4. strong reasoning model only for difficult consolidation or conflict explanation

Strong models should not be required just to answer:
- what is the current task
- what changed in scope
- what remains unresolved

## Mutation Rules

Task framing may change only through explicit evidence-bearing artifacts.

Examples:
- a new `constraint` can tighten scope
- a `chosen_decision` can replace an earlier option
- a `deferred_finding` can remove side work from current scope
- a `verification_result` can change done confidence

The projection service should preserve:
- `original framing`
- `current framing`
- `change trail`

It should never overwrite the original problem statement.

## Relationship To Memory-First Work

This service becomes valuable only if task capture is disciplined.

Therefore it depends on:
- `docs/TASK_MEMORY_CAPTURE_PROTOCOL.md`

The protocol ensures that task mutation is recorded as structured evidence.
This service turns that evidence into an up-to-date framing projection.

## MVP

The first version should support:
- reconstructing `current task statement`
- listing framing-relevant artifacts in chronological order
- generating a compact `framing evolution` summary
- surfacing missing capture when the task statement is weak

The first version does not need:
- full conflict arbitration
- law promotion
- cross-task synthesis

## Future Direction

Later, this can evolve into:
- automatic “current task brief” for every active task
- handoff-ready `continue here` framing blocks
- retrospective “how the task changed” reports
- comparison of operator intent vs final delivered scope
