# Task: Constitutional Kernel And First-Class Project Laws

Status: proposed
Project: `supermemory`
Priority: critical

## Problem

`supermemory` is a general-purpose system for helping agents work on any project in any subject area.
It must accumulate, systematize, document, and safely reuse knowledge gathered during work.

The current architectural gap is not only that project laws are weakly modeled.
The deeper gap is that the system has not clearly separated:
- what may be hardcoded as bootstrap mechanism
- what must be learned as project knowledge
- what may become promoted general knowledge only after evidence and review

Without that separation, the system drifts into one of two failure modes:
- overfitting the mechanism to the current project's subject matter
- refusing to define any bootstrap principles at all, leaving self-learning unguided

Both are wrong.

## Goal

Define and implement a small constitutional kernel for `supermemory`, then build first-class project laws on top of it as governed knowledge entities.

The kernel may hardcode only fundamental organs and reflexes of the system.
It must never hardcode subject-domain knowledge, project-specific conclusions, or operational content for a particular stack.

## Architectural Thesis

The system needs three layers:

1. `Constitutional kernel`
Hardcoded, minimal, domain-agnostic.
This is the equivalent of organs and reflexes.

2. `Governed project knowledge`
Project-scoped by default.
Includes laws, improvements, self-documentation, runbooks, skills, and decisions.

3. `Promoted general knowledge`
Raised upward from project knowledge only after repeated evidence and explicit governance.

## Constitutional Kernel

The following may be hardcoded as bootstrap principles:
- project-local first
- evidence before promotion
- dangerous high-impact change requires explicit approval
- user is the final authority on project truth
- curiosity may generate candidates, not truth
- knowledge entities have governed lifecycle
- MCP is the operational surface for agents
- RepRap is a normal case, not a privileged exception

Everything else must be learned, proposed, reviewed, or promoted through the knowledge layer.

## Core Invariants

1. Project knowledge starts local.
No law, improvement, doc, skill, or pattern starts as universal truth.

2. Subject knowledge does not belong in mechanism code.
The kernel may define how knowledge is governed, never what a specific domain means.

3. The user is sovereign over project truth.
Only explicitly user-confirmed knowledge may become active project truth when it affects agent behavior or high-impact action.

4. Curiosity creates candidates.
Curiosity may surface unknowns, gaps, hypotheses, and proposals, but may not silently promote them to truth.

5. Confirmation may happen inline.
The system may ask for confirmation during the working conversation, but only explicit approval counts as confirmation.

6. Governance outranks confidence.
Model confidence, repetition, similarity, or clustering may justify review, but not autonomous truth-making.

7. Promotion is earned.
Project-local knowledge may be promoted only through repeated independent evidence and governed review.

8. RepRap follows the same rules.
`supermemory` must consume the same project-law and governance mechanisms it provides to any other project.

## Truth And Candidate Model

The system must distinguish at least these states:
- `observed`
- `proposed`
- `reviewed`
- `user_confirmed`
- `active`
- `suppressed`
- `superseded`
- `archived`

Rules:
- observations and curiosity outputs are not truth
- candidate laws, docs, and improvements are not truth
- only explicit user confirmation may activate project truth
- suppression and rollback must preserve history

## Scope Model

Knowledge must move through explicit scopes:

`project -> family -> domain -> principle -> meta`

Requirements:
- every law belongs to a project by default
- upward movement requires independent supporting evidence
- promotion must never happen from one project or one session alone

## Required Capabilities

### 1. First-Class Laws

The system must represent a law with at least:
- `project`
- `scope`
- `title`
- `statement`
- `rationale`
- `evidence`
- `status`
- `version`
- `supersedes`
- `supported_by`
- `confirmed_by`
- `confirmed_at`

### 2. Governance Lifecycle

The system must support:
- proposal
- inline confirmation request
- explicit user confirmation
- activation
- revision
- suppression
- rollback
- promotion
- conflict handling

### 3. Agent Retrieval

Agents must be able to:
- retrieve active project laws through MCP and project context
- distinguish project-local laws from promoted laws
- see whether a law is merely proposed or user-confirmed
- cite the governing law they are following

### 4. Curiosity With Restraint

The system must be able to:
- detect relevant knowledge gaps
- propose candidate laws or missing skills
- request confirmation when useful to the current task
- avoid converting curiosity into automatic truth

### 5. Project Context Assembly

Project context must integrate:
- active laws
- relevant project documentation
- relevant improvements
- related prior experience

The agent should receive this through one operational path, not by stitching repo-specific files together.

## Non-Goals

1. Do not hardcode domain vocabularies or project-specific semantic rules into the kernel.
2. Do not treat unreviewed LLM output as project truth.
3. Do not use hidden prompt text as a substitute for governed laws.
4. Do not create special-case law handling for `supermemory` that other projects cannot use.
5. Do not auto-promote project knowledge to broader scopes from a single project or a single conversation.

## Acceptance Criteria

1. The constitutional kernel is documented as a small set of bootstrap principles and does not include subject-domain content.
2. A project can store and retrieve multiple laws as governed knowledge entities.
3. A law that affects agent behavior cannot become active without explicit user confirmation.
4. The system can ask for inline confirmation during a working conversation and record the result.
5. An agent can retrieve active laws for a project through MCP and project-context assembly without reading repo-specific markdown files.
6. The system can distinguish:
   - observed candidate
   - proposed law
   - user-confirmed active law
   - promoted broader-scope law
7. Promotion requires evidence beyond a single project instance.
8. `supermemory` itself can use the same mechanism in RepRap mode without privileged shortcuts.

## Work Packages

### WP1. Constitutional Specification

Define the minimal hardcoded kernel:
- organs
- reflexes
- approval boundaries
- curiosity boundaries

### WP2. Law Entity And Lifecycle

Complete the first-class `project law` model:
- status model
- user confirmation fields
- audit trail
- revision and suppression semantics

### WP3. Confirmation Mechanics

Define how inline confirmation works in agent workflows:
- candidate presentation
- explicit approval detection
- state transition to active truth

### WP4. Context Integration

Make laws part of the normal project context surface alongside docs and improvements.

### WP5. Promotion Governance

Define evidence thresholds and review rules for raising knowledge from project to broader scopes.

### WP6. RepRap Validation

Validate the whole path on `supermemory` itself without introducing special handling for this repo.

## Senior Engineering Constraint

Solve this from the general mechanism outward.

Correct order:
- define the constitutional kernel
- define governed lifecycle and truth boundaries
- define law and confirmation mechanics
- integrate into MCP and project context
- validate with `supermemory` as just another project

Incorrect order:
- patch current behavior for one repo
- invent domain-specific exceptions
- rely on green tests for hardcoded examples
- call the overfit result "architecture"
