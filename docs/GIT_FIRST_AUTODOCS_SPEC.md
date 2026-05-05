# Git-First Autodocs Spec

Status: proposed  
Project: `mnemoforge`  
Priority: high

Related specs:
- `docs/PROJECT_KNOWLEDGE_MODEL.md`
- `docs/EXTERNAL_PROJECT_ROADMAP.md`
- `docs/TASK_MEMORY_CAPTURE_PROTOCOL.md`
- `docs/REMOTE_SNAPSHOT_HELPER_SPEC.md`

## Problem

Background AI autodocumentation is less trustworthy when it reads an ambiguous live workspace as if that state were canonical project truth.

This creates several problems:
- the same job can produce different docs for the same project depending on local uncommitted state
- rebuild cost is higher because the system cannot safely reason from diffs
- docs are harder to verify because they are not anchored to an explicit code snapshot
- candidate and stable knowledge are mixed too early

## Goal

Define a `git-first` source contract for background autodocumentation.

The system should:
- treat `git` snapshots as the default canonical source for background code-derived docs
- keep uncommitted workspace state as a separate `workspace overlay`, not as silent truth
- support incremental rebuild from commit diffs
- preserve traceability from docs and component knowledge back to `repo/branch/commit_sha`
- reduce token usage by avoiding full-tree re-analysis when only a small diff changed

## Non-Goal

This spec does not make `git` the only source of project knowledge.

It does not replace:
- task memory
- laws
- runtime hints
- memoirs
- improvements

It only changes how background code-derived autodocs should be sourced and stabilized.

## Architectural Thesis

For background AI autodocumentation, the correct source hierarchy is:

1. `git snapshot baseline`
- canonical source for code-derived project docs
- explicit `repo`, `branch`, `commit_sha`

2. `workspace overlay`
- optional candidate layer for uncommitted local changes
- never silently promoted to canonical docs

3. `human or governed project memory`
- task changes, decisions, laws, improvements, and reviewed hints remain memory-first inputs
- they may influence doc synthesis, but they do not replace snapshot provenance

## Source Modes

The system already carries `source_mode`.

This spec fixes the intended semantics:

### 1. `workspace`

Use only when:
- git metadata is unavailable
- the project is intentionally local-only
- the user explicitly chooses workspace mode

Tradeoff:
- lower trust
- weaker reproducibility

### 2. `git_snapshot`

Default for background autodocs when git metadata is available.

Required metadata:
- `repo`
- `branch`
- `commit_sha`

Optional metadata:
- `diff_summary`
- parent commit
- dirty workspace flag

### 3. `github_pr`

Same trust class as `git_snapshot`, but anchored to a PR ref and diff context.

### 4. `archive_bundle`

Fallback for remote or exported source packages when git is unavailable but a stable artifact exists.

## Core Invariants

### 1. Canonical autodocs must be snapshot-anchored

Every effective code-derived doc projection must be attributable to one explicit snapshot.

Minimum provenance:
- `source_mode`
- `repo`
- `branch`
- `commit_sha`

### 2. Workspace-derived docs are candidates first

If the source contains uncommitted changes, the resulting docs must enter as:
- `candidate`
- `overlay`
- or another clearly non-canonical state

They must not replace the effective projection without explicit promotion logic.

### 3. Background jobs must prefer diffs over full scans

When a previous documented snapshot exists, background autodocs should:
- compare previous `commit_sha` to current `commit_sha`
- identify touched files/components
- rebuild only affected sections/components when possible

### 4. Missing git metadata must be visible

If the system falls back from `git_snapshot` to `workspace`, that downgrade must be visible in:
- readiness
- docs metadata
- operator-facing status

### 5. Knowledge and projection remain separate

Autodocs continue to be a projection layer.

Code-derived docs may be anchored to git, but they still do not become the source of truth over:
- task memory
- project laws
- reviewed runtime hints

## Target Data Model Additions

The following fields should be treated as first-class on code-derived knowledge records and projections:

- `source_mode`
- `repo`
- `branch`
- `commit_sha`
- `snapshot_ts`
- `dirty_workspace`
- `base_commit_sha`
- `diff_summary`
- `derived_from_files`
- `projection_state`

Recommended semantics:
- `projection_state=effective` for canonical snapshot-backed docs
- `projection_state=candidate` for workspace overlay docs

## Rebuild Semantics

### Canonical rebuild

Use when:
- a new commit is available
- an operator requests explicit refresh
- integrity requires projection recovery

Flow:
1. Load last effective snapshot metadata.
2. Resolve current snapshot metadata.
3. If `commit_sha` is unchanged, skip expensive rebuild unless forced.
4. If changed, compute diff scope.
5. Rebuild only touched sections/components where possible.
6. Store new projection with explicit snapshot provenance.
7. Promote to `effective`.

### Overlay rebuild

Use when:
- workspace is dirty
- local colocated mode is active
- the user wants draft docs for uncommitted work

Flow:
1. Start from the current effective snapshot-backed projection.
2. Analyze only dirty files or local diff.
3. Store results as `candidate` overlay.
4. Surface availability to `enrich-task` and docs views.
5. Do not silently replace effective docs.

## Retrieval Semantics

### `enrich-task`

Should prefer:
1. latest effective snapshot-backed docs
2. newer candidate overlay only when clearly marked

Context should surface:
- current snapshot commit
- whether a newer workspace candidate exists
- whether the project is running in degraded `workspace-only` mode

### Readiness / bootstrap

Should report:
- whether knowledge is anchored to an explicit snapshot
- whether docs are running in `workspace-only` degraded mode
- whether snapshot provenance is consistent across components

### Integrity / hygiene

Should eventually detect:
- docs missing snapshot metadata
- mixed projections from multiple commits without explicit candidate/effective distinction
- stale effective docs when code snapshot advanced

## Token Economy Impact

`git-first autodocs` should reduce token usage by:
- replacing full-tree rereads with diff-aware rebuild
- avoiding repeated documentation of unchanged components
- preventing background jobs from re-analyzing noisy local workspace artifacts
- allowing cheap deterministic file selection before any model call

This should be considered a primary design benefit, not a side effect.

## Open Design Decisions Closed By This Spec

### 1. Is git the only source?

No.

Decision:
- `git snapshot` is the canonical source for background code-derived autodocs
- `workspace` remains a fallback or overlay source

### 2. Should workspace changes be ignored?

No.

Decision:
- workspace changes are allowed as a candidate overlay
- they are not canonical by default

### 3. Does this replace memory-first?

No.

Decision:
- this strengthens memory-first by making code-derived projections more stable and traceable

### 4. Does this require remote GitHub integration?

No.

Decision:
- local git metadata is sufficient for the first implementation slice
- GitHub-aware enrichment can come later

## MVP Implementation Order

1. Make `docs_rebuild` snapshot-aware with explicit `git_snapshot` preference.
2. Persist `dirty_workspace` and `base_commit_sha` alongside current snapshot metadata.
3. Skip rebuild when `commit_sha` is unchanged unless forced.
4. Add diff-scoped file selection for component/doc rebuild.
5. Store uncommitted local rebuilds as `candidate` overlay instead of effective docs.
6. Surface snapshot and overlay state in docs status, readiness, and `enrich-task`.
7. Later add integrity checks for stale or mixed-snapshot doc projections.

## Immediate Next Slice

The next implementation slice should be:

`git-aware docs_rebuild baseline`

Meaning:
- detect current git snapshot for colocated projects
- compare to last documented snapshot
- no-op when unchanged
- rebuild only when changed or forced
- store provenance on resulting doc sections
