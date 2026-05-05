# External Project Readiness Roadmap

Status: active  
Project: `mnemoforge`  
Goal: make `mnemoforge` usable on a different project in a controlled pilot mode before claiming general readiness.

Priority rule:
- always prefer the next step that reduces hidden operator knowledge for an external project
- findings discovered during the roadmap should be recorded in MnemoForge and fixed later unless they block the current slice

Current status summary:
- Phase 0 is effectively complete for the `mnemoforge` project itself
- Phase 1 is the highest-priority execution slice
- Phases 2-4 are architectural follow-through and should not preempt Phase 1 unless a blocker is found

## Phase 0: Core Memory-First Substrate

Status: mostly complete

Completed or largely completed:
- unified `project/enrich-task`
- `task` and `task_change` entities
- `decision_memoir` anchoring with quality filtering
- `runtime_hint` inclusion with governance
- governed `law` layer and live migration for `mnemoforge`
- `doc_section` sync into the shared memory layer
- docs as projection, not truth source
- coverage, missing-sources, and code-fallback signals

Exit criteria:
- agents can retrieve project context from memory before code inspection
- docs, laws, tasks, memoirs, hints, and improvements are all visible in one path

## Phase 1: External Project Bootstrap

Status: highest priority

Objective:
- make a new project usable without hidden operator knowledge

Required capabilities:
- readiness assessment for a target project
- minimal bootstrap flow
- clear next actions when project knowledge is sparse
- no assumption that the new project already has laws, docs, or memoirs

Deliverables:
- project readiness endpoint and MCP visibility
- bootstrap operator workflow
- initial external-pilot checklist
- explicit coverage and blocker reporting for a target project

Exit criteria:
- a fresh project can be assessed and brought to a minimally usable state

## Phase 2: Snapshot-Aware Project Ingestion

Status: in progress

Objective:
- avoid relying on server-side guesses about client code state

Key direction:
- prefer client-side extraction or explicit snapshot ingestion
- carry `repo`, `branch`, `commit_sha`, and diff context where available
- do not assume the server can safely inspect client files directly
- use `git` and GitHub metadata where available to anchor project knowledge to an explicit snapshot

Deliverables:
- explicit snapshot contract
- git-aware metadata flow
- client/server boundary rules
- clear distinction between local colocated mode and remote snapshot mode

Current progress:
- explicit snapshot metadata is now supported on project ingest/refresh
- readiness and bootstrap surfaces can already report whether project knowledge is tied to `repo/branch/commit`
- next step is to expand this from metadata carriage into a fuller git-aware ingestion workflow
- `docs/GIT_FIRST_AUTODOCS_SPEC.md` now fixes the target contract: `git_snapshot` is the canonical source for background code-derived autodocs, with `workspace` used only as fallback or candidate overlay
- `docs/REMOTE_SNAPSHOT_HELPER_SPEC.md` now fixes the remote boundary: external projects should default to `knowledge_only` storage on the server, with a lightweight local helper providing snapshot and diff input

Exit criteria:
- project knowledge can be traced to a code snapshot rather than an ambiguous workspace state

## Phase 3: Hot / Warm / Batch Enrichment

Status: planned

Objective:
- keep interactive retrieval fast while still allowing deeper synthesis

Execution tiers:
- hot path: deterministic and fast retrieval
- warm path: background synthesis and enrichment
- batch path: historical backfill, cleanup, migration, cross-project analysis

Model routing:
- local Ollama for cheap local work
- cheap cloud LLMs for draft synthesis
- stronger cloud models for review, conflict resolution, and promotion
- interactive hot path should avoid blocking cloud calls where possible
- slow synthesis should move into server-side background or batch processing

Exit criteria:
- `enrich-task` remains fast
- slow synthesis no longer blocks interactive work

## Phase 4: Cross-Project Isolation And Promotion

Status: planned

Objective:
- make sure external projects remain isolated while reusable knowledge can still be promoted upward

Required guarantees:
- no leakage of project-local hints, docs, or laws between projects
- promoted knowledge stays explicitly marked as promoted
- retrieval can distinguish local vs broader knowledge

Exit criteria:
- project A and project B stay cleanly isolated
- reusable knowledge can still be lifted by governance

## Phase 5: First External Pilot

Status: target milestone

Objective:
- run `mnemoforge` on one real non-self project in a test mode

Pilot requirements:
- new project bootstrap succeeds
- readiness surface gives actionable gaps
- memory-first retrieval is useful before code reading
- docs and laws are visible through MCP
- operator workflow is comprehensible
- project context is machine-facing in English while preserving original evidence for auditability

Success condition:
- another project can use `mnemoforge` in a test workflow without relying on hidden repo-specific knowledge about `mnemoforge` itself
