# Task: SQLite Source-of-Truth for Core Memory CRUD

Status: proposed  
Project: `supermemory`  
Priority: critical

## Problem

`supermemory` currently treats Qdrant as both the semantic index and the canonical data store for too many domains:

- `memory` CRUD writes copy the full payload directly into Qdrant (content + metadata) and only keep a lightweight SQLite cache as a “nice to have.”
- Component documentation (`project_docs`), living docs cache, and several CRUD APIs still read straight from Qdrant payloads, so a corruption, compaction, or WAL replay issue in Qdrant makes those records unrecoverable.
- Repeat Qdrant integrity incidents have already orphaned slices (snapshots) with no guaranteed fallback. The current architecture violates the “DRY for data” rule: every piece of knowledge should have one source of truth, and Qdrant should hold only what’s needed for fast similarity search.

## Goal

Make SQLite the canonical store for every user-visible CRUD domain while keeping Qdrant as the semantic index/filters. Specific expectations:

1. Structured content (memories, component docs, task records, project docs, etc.) must live in SQLite tables with write-through consistency.
2. Every read path must fetch from SQLite first and treat Qdrant as the vector-only layer (payloads only used for filters, and the SQLite copy rebuilds Qdrant if needed).
3. Dual-write flows need to be transaction-friendly: SQLite insert/update/delete happens before the Qdrant upsert so the system can recover from partial failures.

## Implementation Outline

1. **Schema work:** For each domain that still lives solely in Qdrant (component docs, doc cache, handoff summaries, memo tiers), create a SQLite table keyed by Qdrant ID and containing all content/metadata.
2. **CRUD plumbing:** Update services/routers to read from SQLite (or the new helper that hydrates payloads from SQLite), only falling back to Qdrant metadata when the SQLite row is missing.
3. **Sync helpers:** Provide hydration helpers similar to `MemoryContentStore` so every Qdrant vector result is enriched from SQLite before it reaches the API layer.
4. **Recovery tooling:** Add admin endpoints or scripts to rebuild Qdrant from SQLite dumps when the vector store is damaged.

## Success Criteria

- All high-volume CRUD surfaces (memories, component docs, tasks, living docs) read content from SQLite by default.
- Qdrant insert/update operations now also persist the same payload/metadata into the new SQLite tables.
- Documented recovery/playback procedure exists for reconstructing Qdrant from SQLite dumps after an integrity failure.
- Tests cover SQLite fallback, partial write recovery, and the “DRY data” guarantee (no duplicate mutable payloads live only in Qdrant).

## Next Steps

1. Expand the `component_docs` schema (done) and keep rolling through the other domains (docs cache, handoff store, etc.).
2. Surface the DRY-for-data rule in the architecture docs so new work mirrors this path.
3. Schedule follow-on tasks for rebuilding Qdrant sweeps and observability on SQLite/Qdrant divergence.
