# Handoff: Unify Improvements and Tasks

## Goal
Unify access and status synchronization for `improvements` and `project tasks` without collapsing their storage layers into one table.

## Problem Statement
Current implementation has three issues:

1. `improvements` and `tasks` are different entity types with different IDs and storage paths.
2. Access is fragmented:
   - `list_improvements` only shows improvements
   - `memory_get` cannot resolve an improvement by its UUID
3. Status sync is incomplete:
   - resolving an improvement updates its linked task in some paths
   - reopening or reclassifying a task does not consistently update the linked improvement

## Required Outcome
Create a unified governing access layer so agents can resolve a project artifact by one ID and see linked task/improvement metadata consistently.

## Scope
- Add a canonical identifier or equivalent linkage model for improvement/task pairs.
- Add a unified retrieval facade, e.g. `get_governed_artifact(id)` or `get_project_artifact(id)`.
- Keep existing stores intact.
- Make status transitions explicit and bidirectional where appropriate.
- Improve list/report responses to show linked entity state.
- Add backfill/migration support for existing rows.

## Suggested Implementation
1. Introduce a canonical `artifact_key` or equivalent link field.
2. Add a lookup service that resolves:
   - improvements store
   - project task store
   - Qdrant memory fallback
3. Extend `memory_get` or add a neighboring endpoint so improvement/task UUIDs can be resolved through the unified facade.
4. Synchronize status changes:
   - `resolve_improvement` should update the linked task and record a `status_change`
   - `reopen_task` should update the linked improvement when policy says the pair should move together
5. Update list/report endpoints to include linked metadata:
   - `linked_task_id`, `task_status`, `task_change_count`, `sync_state`
   - `linked_improvement_id`, `improvement_status`, `improvement_last_action`
6. Add a backfill path for existing data based on current links, IDs, and topic/title matching.

## Constraints
- Do not merge the physical storage tables on the first pass.
- Do not break the existing `/api/v1/improvements/*` and `/api/v1/project/tasks/*` endpoints.
- Do not rely on title matching as the primary key.
- Do not leave sync as best-effort only without tests.

## Definition of Done
- A single ID can resolve the governing artifact through a unified facade.
- Improvement and task statuses stay consistent after resolve/reopen flows.
- List/report endpoints surface linked entity state.
- Existing data can be backfilled without data loss.
- Tests cover:
  - unified lookup
  - resolve sync
  - reopen sync
  - reporting/list output with linked metadata

## Relevant Files
- `app/services/improvements_store.py`
- `app/services/project_task_service.py`
- `app/services/project_tasks_store.py`
- `app/routers/improvements.py`
- `app/routers/project_tasks.py`
- `app/routers/memories.py`
- `app/services/project_context_service.py`
