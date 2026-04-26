# Remote Snapshot Helper Spec

Status: proposed  
Project: `supermemory`  
Priority: high

Related specs:
- `docs/GIT_FIRST_AUTODOCS_SPEC.md`
- `docs/EXTERNAL_PROJECT_ROADMAP.md`
- `docs/PROJECT_KNOWLEDGE_MODEL.md`

## Problem

For remote clients, the server should not guess the state of a project workspace that it does not control.

If background autodocs depend on implicit server-side file access, the system becomes:
- less reproducible
- harder to secure
- more expensive in tokens
- more likely to store too much raw code instead of the knowledge actually needed

## Goal

Define a `remote snapshot helper` contract for external or remote projects.

The design should:
- let a client provide an explicit project snapshot to the server
- keep the server memory-first rather than code-mirror-first
- support `git-first autodocs`
- make remote project handling cheap, traceable, and secure by default

## Architectural Thesis

For remote projects, the default boundary should be:

1. `client/helper owns source inspection`
- reads local git and workspace state
- computes diffs and file selection
- packages explicit snapshot metadata

2. `server owns knowledge projection`
- stores component knowledge, docs, task memory, laws, hints, and indexes
- decides whether to rebuild or skip
- treats raw source as optional input, not the primary stored artifact

This keeps `supermemory` aligned with its main role:

`store project knowledge and task memory, not a full duplicate repository unless explicitly requested`

## Main Decision

Default remote mode:
- do **not** store the full project checkout on the server

Default server storage:
- store `knowledge + snapshot provenance`

Optional advanced mode:
- store a selective source cache
- or a full mirror only when explicitly enabled

## Roles

### Remote snapshot helper

This is a lightweight local executor on the client machine.

It may be:
- a CLI tool
- an MCP tool
- an IDE-integrated command
- a small local agent

It is not required to be a large standalone application.

### Supermemory server

The server should receive explicit snapshot input and turn it into:
- component knowledge
- docs projections
- retrieval context
- task-facing memory artifacts

## Required Client Output

The helper should be able to produce the following payload classes.

### 1. Snapshot metadata

Required:
- `project_id`
- `source_mode`
- `repo`
- `branch`
- `commit_sha`

Recommended:
- `base_commit_sha`
- `dirty_workspace`
- `snapshot_ts`
- `diff_summary`

### 2. File change scope

Required for efficient rebuild:
- `changed_files`
- `deleted_files`
- `renamed_files`

Optional:
- `component_candidates`
- `touched_paths_by_component`

### 3. Selective source payload

Optional by default.

Used only when the server actually needs content for analysis or projection.

Recommended shape:
- `path`
- `content`
- `content_hash`
- `language`
- `status` (`added|modified|deleted|renamed`)

### 4. Helper-side extracted summaries

Optional later optimization.

The helper may pre-extract:
- component boundaries
- symbol lists
- imports
- endpoint candidates
- file purpose hints

This can reduce server token use further.

## Server Storage Contract

### Default storage

The server should store:

#### A. Snapshot provenance
- `project_id`
- `source_mode`
- `repo`
- `branch`
- `commit_sha`
- `base_commit_sha`
- `dirty_workspace`
- `snapshot_ts`
- `diff_summary`

#### B. Derived project knowledge
- component records
- effective docs
- candidate docs overlays
- task memory
- memoirs
- runtime hints
- improvements
- laws

#### C. Retrieval infrastructure
- embeddings
- Qdrant filter payload
- rebuild metadata

### Optional storage

The server may store:
- selective source cache for changed files only
- content excerpts used to build projections
- lightweight code fingerprints

### Not stored by default

The server should not, by default, store:
- full repository checkout
- full `.git` history
- all source files for all commits
- raw local workspace mirror

## Storage Tiers

### Tier 1. `knowledge_only`

Default recommended mode.

Server stores:
- snapshot metadata
- derived knowledge
- retrieval indexes

Does not store:
- source file contents except transiently during processing

Best for:
- privacy
- low storage cost
- memory-first architecture

### Tier 2. `selective_source_cache`

Recommended optional mode.

Server additionally stores:
- changed files only
- or source excerpts needed for rebuild/debug/audit

Best for:
- diff-aware rebuilds without re-requesting every file
- moderate reproducibility

### Tier 3. `full_mirror`

Explicit opt-in mode only.

Server stores:
- full source snapshot for a commit

Best for:
- air-gapped deployments
- offline rebuild guarantees
- forensic or regulated workflows

Tradeoff:
- highest storage and hygiene burden

## Security And Privacy Rules

### 1. Minimum source principle

Send only the minimum source material needed for the requested operation.

### 2. Snapshot provenance must survive even if source text is discarded

The server should be able to explain:
- what snapshot a knowledge record came from
- even if raw file content is no longer stored

### 3. Raw source retention must be configurable

If selective cache or mirror mode exists, retention must be explicit and separately governed.

### 4. Remote projects must not require hidden filesystem access

The server should not depend on directly reading client files over implicit mounts or assumptions.

## Background Autodocs Flow

Recommended remote flow:

1. Helper resolves local git state.
2. Helper sends snapshot metadata to server.
3. Helper sends diff scope.
4. Server compares with last documented snapshot.
5. If `commit_sha` unchanged and no meaningful dirty overlay, server skips rebuild.
6. If changed, server requests or accepts selective source payload for touched files only.
7. Server rebuilds affected components/docs.
8. Server stores derived knowledge plus provenance.
9. If workspace is dirty, server stores overlay docs as `candidate`, not `effective`.

## Task Context Impact

This contract should improve task context by making code-derived context:
- more reproducible
- easier to invalidate
- cheaper to refresh

`enrich-task` should eventually be able to say:
- current docs are anchored to commit `abc123...`
- there is or is not a newer uncommitted overlay
- the project is in `knowledge_only`, `selective_source_cache`, or `full_mirror` mode

## Token Economy Impact

The remote helper exists partly to reduce expensive server-side reasoning.

It should reduce token usage by:
- selecting changed files before model calls
- preventing full-tree rereads
- allowing deterministic diff filtering
- enabling no-op rebuild when snapshot is unchanged

## Open Questions Closed By This Spec

### 1. Must the project live on the server as a repo mirror?

No.

Decision:
- default mode is `knowledge_only`

### 2. Is a local helper required for remote clients?

Practically yes for the best UX and correctness.

Decision:
- a lightweight local helper is the preferred remote architecture

### 3. Is helper output only metadata?

No.

Decision:
- metadata is required
- selective source payload is optional and sent only when needed

### 4. Does this conflict with memory-first?

No.

Decision:
- this protects memory-first by preventing the server from turning into an accidental full repo host

## MVP Implementation Order

1. Add a helper-facing snapshot payload contract.
2. Make `project ingest/refresh` accept this payload cleanly for remote mode.
3. Add `knowledge_only` as the default documented storage mode.
4. Add optional `selective_source_cache` mode later.
5. Only add `full_mirror` if a real deployment requires it.

## Immediate Next Slice

The next implementation slice should define:
- the exact helper payload schema
- the `docs_rebuild` path for snapshot-aware remote refresh
- the first server-visible storage mode field for remote projects

## Implemented Operator Contract

The current server now exposes a helper-facing contract at both the REST API and MCP layers.

REST endpoints:
- `POST /api/v1/project/remote-snapshot/plan`
- `POST /api/v1/project/remote-snapshot/sync`

MCP tools:
- `plan_remote_snapshot`
- `sync_remote_snapshot`

### Helper Workflow

Recommended helper flow:

1. Collect git and workspace state locally.
2. Call `plan_remote_snapshot` to validate the payload and see whether selective file content is likely required.
3. If the plan indicates `requires_selective_source_payload=true`, prepare `files[]` only for touched files the server actually needs.
4. Call `sync_remote_snapshot`.
5. Branch locally on `action`:
   - `skipped`
   - `needs_source_payload`
   - `refreshed`
   - `bootstrap_needed`
   - `no_changes`

### Minimum MCP / REST Payload Shape

Required top-level fields:
- `project_id`
- `snapshot`

Recommended top-level fields:
- `storage_mode`
- `changed_files`
- `deleted_files`
- `renamed_files`
- `files`
- `force`

Snapshot fields:
- `source_mode`
- `repo`
- `branch`
- `commit_sha`
- `base_commit_sha`
- `dirty_workspace`
- `snapshot_ts`
- `diff_summary`
- `pr_ref`

Selective file payload fields:
- `path`
- `status`
- `content`
- `content_hash`
- `language`
- `component_hint`

## Response Semantics

### `plan_remote_snapshot`

This call is read-only. It returns:
- normalized path/file counts
- rebuild mode
- projection target state
- whether selective source content is still required
- storage contract flags

Important response fields:
- `plan.rebuild_mode`
- `plan.projection_target_state`
- `plan.requires_selective_source_payload`
- `plan.can_skip_when_unchanged`
- `contract.stores_selective_source_cache`
- `contract.full_mirror_enabled`

### `sync_remote_snapshot`

This call runs the remote refresh workflow and returns one helper-facing `action`.

Current action meanings:
- `skipped`: incoming snapshot is effectively unchanged, so the helper does not need to send content
- `needs_source_payload`: metadata and diff scope were accepted, but the server still needs changed-file content for one or more components
- `refreshed`: the server updated project knowledge from the selective payload
- `bootstrap_needed`: no components are indexed yet, so the helper or operator must ingest/bootstrap first
- `no_changes`: refresh completed but there was nothing to update and it was not a strict unchanged skip case

## Concrete Examples

### 1. `skipped`

Use when the helper sees the same clean commit and wants a cheap no-op confirmation.

```json
{
  "project_id": "alpha",
  "storage_mode": "knowledge_only",
  "snapshot": {
    "source_mode": "git_snapshot",
    "repo": "https://github.com/example/alpha",
    "branch": "main",
    "commit_sha": "abc123def456",
    "base_commit_sha": "abc123def456",
    "dirty_workspace": false
  },
  "changed_files": ["app/context.py"]
}
```

Expected sync result shape:

```json
{
  "project_id": "alpha",
  "action": "skipped",
  "plan": {
    "plan": {
      "rebuild_mode": "skip_if_unchanged",
      "projection_target_state": "effective",
      "requires_selective_source_payload": true,
      "can_skip_when_unchanged": true
    }
  },
  "refresh": {
    "updated": [],
    "up_to_date": ["context"],
    "requires_source_payload": []
  }
}
```

Operator interpretation:
- do not upload file content
- no docs/component rebuild was needed

### 2. `needs_source_payload`

Use when commit metadata changed, but the helper has not yet sent file contents.

```json
{
  "project_id": "alpha",
  "storage_mode": "knowledge_only",
  "snapshot": {
    "source_mode": "git_snapshot",
    "repo": "https://github.com/example/alpha",
    "branch": "main",
    "commit_sha": "def789abc000",
    "base_commit_sha": "abc123def456",
    "dirty_workspace": false
  },
  "changed_files": ["app/context.py"]
}
```

Expected sync result shape:

```json
{
  "project_id": "alpha",
  "action": "needs_source_payload",
  "plan": {
    "plan": {
      "rebuild_mode": "diff_only",
      "projection_target_state": "effective",
      "requires_selective_source_payload": true
    }
  },
  "refresh": {
    "updated": [],
    "up_to_date": [],
    "requires_source_payload": ["context"]
  }
}
```

Operator interpretation:
- send `files[]` for the changed component files
- do not retry with the same metadata-only payload

### 3. `refreshed`

Use when the helper sends selective content for the touched file set.

```json
{
  "project_id": "alpha",
  "storage_mode": "selective_source_cache",
  "snapshot": {
    "source_mode": "git_snapshot",
    "repo": "https://github.com/example/alpha",
    "branch": "main",
    "commit_sha": "def789abc000",
    "base_commit_sha": "abc123def456",
    "dirty_workspace": false
  },
  "changed_files": ["app/context.py"],
  "files": [
    {
      "path": "app/context.py",
      "status": "modified",
      "content": "print('remote change')",
      "content_hash": "hash-new",
      "language": "python",
      "component_hint": "context"
    }
  ]
}
```

Expected sync result shape:

```json
{
  "project_id": "alpha",
  "action": "refreshed",
  "plan": {
    "plan": {
      "rebuild_mode": "diff_only",
      "projection_target_state": "effective",
      "requires_selective_source_payload": false
    }
  },
  "refresh": {
    "updated": ["context"],
    "up_to_date": [],
    "requires_source_payload": [],
    "used_remote_file_payload": true
  }
}
```

Operator interpretation:
- selective payload was sufficient
- project knowledge was refreshed
- the helper can report success without inspecting router code
