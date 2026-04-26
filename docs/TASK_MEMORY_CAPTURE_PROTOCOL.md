# Task Memory Capture Protocol

Status: proposed  
Project: `supermemory`  
Priority: critical

## Problem

`memory-first` retrieval degrades when task work is captured only as final summaries, ad hoc notes, or long chat traces.

The system needs a disciplined way for agents to record what happened during a task without depending on a strong reasoning model for every write.

## Goal

Define the minimum capture protocol that every agent task should follow.

The protocol must:
- capture task progress across the full task lifecycle
- preserve the most recent checkpoint so a later agent can recover work after interruption or termination
- separate `project truth` from `session scratchpad`
- prefer cheap/local structured capture over expensive synthesis
- reserve strong reasoning models for conflict resolution, consolidation, and promotion
- make handoff, enrich-task, and memoir generation depend on recorded task artifacts rather than chat reconstruction

Related projection:
- `docs/TASK_STATEMENT_PROJECTION_SERVICE.md`

## Capture Tiers

### 1. Hot-path capture

Written synchronously by the main agent while the task is active.

Must be cheap, structured, and minimal.

Allowed writers:
- main agent
- local SLM helper

Target latency:
- small enough to run during normal work without noticeable interruption

### 2. Background capture

Written asynchronously after a task step or checkpoint.

Used for:
- normalization
- summarization of recent task changes
- extraction of deferred findings
- lightweight handoff preparation

Preferred executor:
- local SLM or other cheap tier

### 3. Governed synthesis

Used only when the system needs to:
- consolidate many records
- resolve conflicts
- promote high-impact project truth
- generate durable memoir/doc projections

Preferred executor:
- stronger cloud model only when cheaper tiers are insufficient

## Required Task Stages

Every task should produce artifacts for these stages.

### 1. Task framing

Required artifacts:
- `task`
- `assumption`
- `constraint`
- `definition_of_done`
- `task_checkpoint`

Minimum fields:
- task title
- project
- current phase
- explicit assumptions
- explicit constraints
- done condition

### 2. Planning

Required artifacts:
- `decision_candidate` when alternatives exist
- `risk_note` for meaningful tradeoffs
- `chosen_decision` once direction is selected
- `task_checkpoint`

Minimum rule:
- if the path is non-trivial or non-reversible, the chosen decision must be recorded explicitly

### 3. Execution

Required artifacts:
- `task_change`
- `code_link`
- `deferred_finding` when something important is discovered but not solved now
- `task_checkpoint`

Minimum rule:
- every meaningful implementation step should leave at least one `task_change`
- every decision or change tied to code should carry a code reference when available
- task checkpoints may be stored as `task_change` records tagged `task_checkpoint`

### 4. Verification

Required artifacts:
- `verification_result`
- `remaining_risk` when tests or checks are partial
- `task_checkpoint`

Minimum rule:
- completion without verification data is considered incomplete capture

### 5. Closure or handoff

Required artifacts:
- `result_summary`
- `handoff_summary` if work is paused or delegated
- `decision_memoir_candidate` for completed meaningful work
- `task_checkpoint`

Minimum rule:
- every paused or finished task must leave a clear `continue here` trail

## Artifact Classes

First MVP artifact set:
- `task`
- `assumption`
- `constraint`
- `definition_of_done`
- `task_checkpoint`
- `decision_candidate`
- `chosen_decision`
- `task_change`
- `code_link`
- `deferred_finding`
- `verification_result`
- `remaining_risk`
- `result_summary`
- `handoff_summary`

These are enough to discipline capture without introducing the full long-term entity surface at once.

## Truth Boundary

Default rule:
- task-lifecycle records are not automatically `project truth`

Promotion rules:
- `assumption`, `decision`, `constraint`, or `risk` may influence project truth only after governance or explicit user confirmation where impact is high

Scratchpad rules:
- temporary planning material may exist, but it must remain queryable as scratchpad and must not silently appear as active law or stable project fact

## Model Discipline

Default execution order:
1. structured write by main agent
2. local SLM fills or normalizes missing fields
3. cheap cloud fallback if local fails
4. strong reasoning model only for governed synthesis

Strong reasoning models should not be the default writer for:
- assumptions
- task changes
- deferred findings
- handoff summaries
- routine verification notes

## Enforcement Direction

The protocol should eventually be enforced through:
- task lifecycle hooks
- handoff close/pause checks
- enrich-task quality checks
- memoir generation preconditions
- dashboards showing capture completeness

## MVP Implementation Order

1. Add the protocol as an explicit project artifact.
2. Define storage schema and lifecycle semantics for the MVP artifact set.
3. Add cheap/local background capture for missing task artifacts.
4. Make handoff and enrich-task depend on these artifacts.
5. Add completeness checks and conflict-aware promotion later.
