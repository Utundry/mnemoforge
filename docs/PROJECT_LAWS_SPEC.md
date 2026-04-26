# Project Laws Specification

Status: draft
Scope: architecture target for `supermemory`

## Purpose

`supermemory` must support project-specific laws for agents.

A project law is a governed knowledge artifact that defines how agents should behave while working on a specific project.

This is a core system goal, not an optional note.

## What A Law Is

A law is:
- project-scoped by default
- explicit
- discoverable
- reviewable
- evidence-backed
- revisable

A law is not:
- a hidden system prompt assumption
- hardcoded subject-matter logic
- an unreviewed one-off heuristic
- a global truth by default

## Law Lifecycle

### 1. Proposal

A law begins as a project-local proposal derived from:
- repeated user instruction
- repeated successful practice
- repeated failure caused by a missing rule
- explicit architectural decision

### 2. Review

A proposed law must be reviewable by a human or a governed approval workflow.

Review checks:
- Is the law actually project-specific?
- Is it grounded in evidence?
- Is it too broad?
- Does it conflict with another law?
- Is it mechanism-level or knowledge-level?

### 3. Active Project Law

Once accepted, the law becomes active for that project.

Agents working on the project must be able to retrieve and apply it through project memory and MCP.

### 4. Revision

The law may later be:
- clarified
- narrowed
- superseded
- split into multiple laws
- merged with another law

### 5. Promotion

If similar laws recur across multiple projects, they may be promoted upward:

`project -> family -> domain -> principle -> meta`

Promotion must require evidence from multiple independent project contexts.

### 6. Suppression Or Rollback

If a law proves harmful, false, obsolete, or too narrow, it must be suppressible without deleting history.

## Minimum Required Metadata

At minimum, a project law should carry:
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

## Operational Requirement

Agents must be able to:
- discover active laws for the current project
- distinguish project-local laws from promoted general laws
- cite the evidence behind a law
- avoid treating project-local law as universal truth

## Design Constraint

Project laws must stay in the knowledge layer.

The mechanism may enforce how laws are stored, retrieved, reviewed, promoted, and suppressed.
The mechanism must not replace laws with hardcoded project-specific behavior.
