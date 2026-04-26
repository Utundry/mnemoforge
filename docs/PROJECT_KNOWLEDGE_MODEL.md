# Unified Project Knowledge Model

Status: proposed  
Project: `supermemory`  
Priority: critical

Related follow-on specs:
- `docs/TASK_MEMORY_CAPTURE_PROTOCOL.md`
- `docs/GIT_FIRST_AUTODOCS_SPEC.md`
- `docs/REMOTE_SNAPSHOT_HELPER_SPEC.md`

## Problem

`supermemory` currently stores project understanding across multiple partially-overlapping layers:
- `project_docs` for component summaries
- file-backed living docs cache
- project laws in governed memory
- improvements in SQLite plus Qdrant history
- learning artifacts/runtime hints in Learning Ledger
- task memoirs as retrospective memories

These layers are individually useful, but together they fail the main architectural goal:

`an agent should retrieve project understanding from SuperMemory before reading source code`

Today the system has no single knowledge substrate that all of these layers project from or retrieve through.

## Goal

Define one unified memory-first knowledge model for project understanding.

This model must:
- treat project knowledge as first-class governed entities
- distinguish stable knowledge from evolutionary hypotheses
- support one main retrieval path for agents
- make autodocumentation a projection layer, not a parallel truth store
- support migration from current fragmented layers without preserving them as permanent architecture

## Architectural Thesis

The system needs one shared knowledge substrate with three semantic classes:

1. `Governed knowledge entities`
- stable project knowledge
- reviewed, versioned where needed
- eligible for direct agent retrieval

2. `Evolutionary candidates`
- observations, hypotheses, candidate patterns, candidate rules
- may influence review queues
- do not become project truth without governance

3. `Projection layers`
- human docs
- dashboard views
- markdown exports
- summaries and reports

Projection layers never become the source of truth.

## Core Principles

1. `Project-local first`
All new knowledge starts as project-scoped unless explicitly promoted.

2. `Knowledge first, docs second`
Documentation is rendered from knowledge entities; it is not the canonical store.

3. `User sovereignty over truth`
High-impact project truth requires explicit user confirmation.

4. `Curiosity creates candidates, not truth`
Learning outputs enter the system as reviewable material, not active law or fact.

5. `Mechanism is general-purpose`
No subject-domain semantics may be hardcoded into the knowledge model.

6. `Scope must be explicit`
Project, domain, principle, and meta knowledge must not be silently mixed.

## Target Entity Types

The unified substrate should support these first-class entity types.

### 1. `task`

Represents a unit of project work.

Required fields:
- `id`
- `project`
- `title`
- `description`
- `status`
- `created_at`
- `updated_at`
- `source`
- `tags`
- `topic_path`

Why it exists:
- anchor for task changes
- anchor for memoir generation
- anchor for decisions and outcomes

### 2. `task_change`

Structured delta recorded during work on a task.

The canonical checkpoint form is a `task_change` tagged `task_checkpoint`, which records planning, blocked, interrupted, handoff, and completion transitions without requiring a new entity class.

Required fields:
- `id`
- `project`
- `task_id`
- `change_type`
- `content`
- `why`
- `timestamp`
- `source`

Why it exists:
- memoirs currently degrade because this layer is mostly absent
- the system needs a normal form for “what changed and why”

### 3. `decision_memoir`

Retrospective knowledge derived from a task and its task changes.

Required fields:
- `id`
- `project`
- `task_id`
- `content`
- `quality_status`
- `generated_from`
- `timestamp`

Why it exists:
- preserves reasoning and tradeoffs
- feeds decision memory and self-documentation

Constraint:
- memoir quality must be explicit; weak/generated-without-context memoirs should not be treated as equal to grounded ones

### 4. `component`

Project component or subsystem summary.

Required fields:
- `id`
- `project`
- `name`
- `purpose`
- `implementation`
- `key_files`
- `endpoints`
- `status`
- `file_hash`
- `version_note`
- `updated_at`

Why it exists:
- gives agents architectural entry points without rereading code

### 5. `law`

Governed project law.

Required fields:
- `id`
- `project`
- `scope`
- `title`
- `statement`
- `rationale`
- `evidence`
- `status`
- `confirmed_by`
- `confirmed_at`
- `version`
- `supported_by`
- `supersedes`

Why it exists:
- normative layer for agents and project governance

### 6. `improvement`

Open or resolved project improvement.

Required fields:
- `id`
- `project`
- `title`
- `description`
- `status`
- `importance_score`
- `tags`
- `created_at`
- `resolved_at`
- `last_status_action*`

Why it exists:
- backlog of architectural and operational work
- source for pending project pressure, not just documentation

### 7. `runtime_hint`

User-confirmed lightweight project guidance or confirmed knowledge gap.

Required fields:
- `id`
- `project`
- `artifact_type`
- `artifact_scope`
- `status`
- `content`
- `action_type`
- `context_signature`
- `evidence_count`
- `approval metadata`

Why it exists:
- confirmed evolutionary knowledge should influence agent context before it matures into broader laws or canonicals

Constraint:
- active runtime hints require filtering; demo and weak legacy artifacts must not leak into main context unchecked

### 8. `doc_section`

Effective project documentation section.

Required fields:
- `id`
- `project`
- `section_name`
- `content`
- `status`
- `generated_from`
- `generated_at`

Why it exists:
- allows documentation to be retrieved as knowledge, not only as cached files

Constraint:
- `doc_section` is governed projection content, not an autonomous truth store

### 9. `canonical`

Promoted knowledge above project scope.

Required fields:
- `id`
- `scope`
- `topic_path`
- `content`
- `supports`
- `canonical_status`
- `candidate_revision`

Why it exists:
- enables reuse across projects and upward generalization

## Lifecycle Classes

Different entity types require different lifecycle semantics.

### A. Governed entities

Use `effective + candidate revision`.

Applies to:
- `law`
- `doc_section`
- `canonical`
- possibly `component` if summaries become governed rather than regenerated

Key transitions:
- `stage candidate`
- `apply candidate`
- `discard candidate`
- `suppress`
- `supersede`

### B. Evolutionary candidates

Use hypothesis/review lifecycle.

Applies to:
- learning candidates
- deferred findings
- candidate patterns
- candidate laws

Key transitions:
- `observed`
- `proposed`
- `reviewed`
- `user_confirmed`
- `rejected`
- `deferred`
- `promoted into governed entity`

### C. Operational entities

Track project organization and execution state.

Applies to:
- `task`
- `improvement`
- `task_change`

These are not candidate revisions; they are operational records with audit trails.

Improvement semantics:
- an `improvement` is a project-local idea, problem statement, or opportunity for better project behavior
- it may be decomposed into one task or a task set when implementation work starts
- it may later be reviewed for stage and effectiveness, but that review does not replace its role as the originating improvement signal
- if an improvement is effective, it should usually be reflected back into an operational instinct, routing hint, or tool guidance so it changes agent behavior and not only stored state

## Retrieval Model

There must be one primary retrieval path for agents:

`task -> project context assembly -> sufficient knowledge to start work`

The retrieval bundle should include, in order:

1. applicable active laws
2. relevant project components
3. relevant open improvements
4. relevant confirmed runtime hints
5. relevant decision memoirs
6. relevant effective doc sections
7. promoted canonicals only when local knowledge is weak or explicitly requested

The system must also return:
- confidence/coverage signals
- what is missing
- whether code inspection is recommended as fallback

### MCP Usage Layering

Keep MCP usage compact by default:

- start with the unified surface (`list_open_tasks`, `list_artifacts`, `enrich-task`, `review_improvement`)
- if the tool catalog is large or the agent is unsure, start with `normalize_mcp_intent`; otherwise start with `list_tool_families`, then `tool_family_tools`, and use `tool_recommend` when the next call is ambiguous
- prefer the canonical surface returned by `tool_recommend` before browsing individual tool families; it should keep the agent on a small set of universal entrypoints and only then fall back to deeper or specialized tools
- at task start and every meaningful stage transition, the agent should record a compact task checkpoint so planning, blockers, interruptions, and handoff do not vanish if the session ends unexpectedly
- if any MCP tool is marked `testing`, the agent should complete that tool's use-case with `tool_feedback` and include a compact evaluation envelope with scope, what_was_tested, expected_behavior, observed_behavior, friction, suggestion, and next_action when available; this is a phase-level rule, not a family-level exception. Testing tools are auto-seeded into lifecycle review and may later be promoted to `stable` or marked `deprecated` after enough time or feedback, with LLM review used only when the signal is ambiguous
- after completing a task or implementation, the agent should report the outcome back to SuperMemory with the linked improvement/task id and any relevant stage/verdict evidence; completion should not live only in chat
- pull deeper project knowledge only when the current bundle is missing a needed answer
- use specialized or lower-level surfaces only when the unified surface cannot express the request
- prefer short guidance in the default context and detailed retrieval on demand
- do not inspect project tables directly from agent workflows; table access is for maintenance and debugging, not for normal task inspection

## Projection Model

Human-facing documentation should be derived from the same substrate.

Primary projections:
- dashboard docs
- markdown status pages
- section views
- executive summaries

Required rule:
- if a projection becomes stale, it must be visibly stale
- projection freshness cannot silently diverge from current project knowledge

## Scope Model

Every knowledge entity must declare its scope:

`project -> family -> domain -> principle -> meta`

Rules:
- `project` is the default
- project docs must not silently include global content as if it were local
- promoted knowledge must remain explicitly marked as promoted

## Migration Mapping From Current Layers

### `project_docs`
Target: migrate into `component`

Reason:
- already semantically useful
- should remain a source layer, but under the unified entity model

### `docs_cache`
Target: remove as source of truth, keep only as projection cache

Reason:
- stale and contaminated
- useful as rendering cache only

### `laws`
Target: keep mechanism, migrate actual project truth into active law entities

Reason:
- mechanism is already close to target state
- current project data is underpopulated

### `improvements`
Target: keep and include in context assembly

Reason:
- already rich and operationally important
- currently missing from agent context

### `runtime hints / learning artifacts`
Target: keep selectively as `runtime_hint`

Reason:
- confirmed evolutionary knowledge is valuable
- current set requires filtering and quality control

### `task_memoir`
Target: keep concept, redesign upstream capture

Reason:
- concept is strong
- current pipeline depends on missing `task_change` and mixed storage assumptions

## Known Current Defects Driving This Redesign

- living docs drift from current component knowledge
- living docs store contaminated LLM output
- project context assembly ignores most project knowledge surfaces
- project law mechanism exists but current project has no active laws migrated into it
- confirmed runtime hints exist but are excluded from context
- runtime hint set contains demo and weak legacy artifacts
- memoir quality depends on upstream data that current workflow barely captures

These are redesign drivers, not isolated bugs.

## Implementation Direction

### WP2.1 Define canonical entity envelopes

Unify the minimum shared fields:
- `entity_type`
- `project`
- `scope`
- `status`
- `content / structured content`
- `evidence / supports`
- `review metadata`
- `timestamps`
- `topic_path`

### WP2.2 Define task and task_change as first-class entities

This is required before memoirs can become reliable.

### WP2.3 Build context assembly over unified entities

Replace current `laws + components only` behavior with layered retrieval.

### WP2.4 Rebuild autodocs as projection

`docs/status` should render from unified entities, not from an independent truth cache.

### WP2.5 Filter and curate runtime hints

Only retrieval-worthy active hints should enter project context.

## Non-Goals

- Do not preserve every current storage detail for compatibility.
- Do not treat docs cache as sacred infrastructure.
- Do not import weak memoirs or demo hints as high-trust knowledge.
- Do not collapse all entities into one shapeless record without semantics.

## Acceptance Criteria

1. One agent context path can retrieve laws, components, improvements, hints, memoirs, and docs from one unified model.
2. Living docs become a projection layer over that model.
3. `task` and `task_change` exist as explicit inputs to memoir generation.
4. Confirmed runtime hints can influence context, but only through filtered project-scoped inclusion.
5. Scope boundaries are explicit; project-local and promoted knowledge are not silently mixed.
6. Code reading becomes a fallback, not the default first step.
