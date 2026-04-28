import json
from unittest.mock import AsyncMock

import httpx

from app import dependencies
from app.main import _should_suppress_asyncio_transport_error
from app.routers import mcp_sse
from mcp import server as mcp_stdio


class _FakeHandle:
    def __repr__(self) -> str:
        return "<Handle _ProactorBasePipeTransport._call_connection_lost(None)>"


class TestMcpDiscovery:
    async def test_oauth_discovery_root_is_benign(self, client):
        r = await client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 404
        body = r.json()
        assert body["detail"] == "OAuth not supported"

    async def test_oauth_discovery_variants_are_benign(self, client):
        for path in (
            "/.well-known/oauth-authorization-server/mcp/sse",
            "/mcp/sse/.well-known/oauth-authorization-server",
        ):
            r = await client.get(path)
            assert r.status_code == 404
            assert r.json()["detail"] == "OAuth not supported"


class TestMcpToolExecution:
    def test_sse_handoff_task_exposes_bounded_ownership_fields(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "handoff_task")
        props = tool["inputSchema"]["properties"]
        assert "owner_agent" in props
        assert "write_scope" in props
        assert props["write_scope"]["type"] == "array"

    def test_list_open_tasks_tool_is_exposed(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "list_open_tasks")
        props = tool["inputSchema"]["properties"]
        assert "project" in props
        assert "limit" in props
        assert "status" not in props

    def test_normalize_mcp_intent_tool_is_exposed(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "normalize_mcp_intent")
        props = tool["inputSchema"]["properties"]
        assert "intent" in props
        assert "project_id" in props

    def test_reopen_task_tool_is_exposed(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "reopen_task")
        props = tool["inputSchema"]["properties"]
        assert "task_id" in props
        assert "project" in props
        assert props["status"]["default"] == "active"

    def test_tool_discovery_family_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp_sse.TOOLS}
        assert {"list_tool_families", "tool_family_tools", "tool_explain", "tool_recommend", "tool_feedback", "continue_task", "draft_task_checkpoint", "record_task_checkpoint", "report_task_checkpoint"} <= names

    def test_draft_task_checkpoint_tool_is_review_only(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "draft_task_checkpoint")
        props = tool["inputSchema"]["properties"]
        assert "raw_notes" in tool["inputSchema"]["required"]
        assert props["use_llm"]["default"] is True
        assert "raw_notes" in props

    def test_stenographer_work_session_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp_sse.TOOLS}
        assert {
            "get_work_session_state",
            "start_work_session",
            "park_work_session",
            "resume_work_session",
            "end_work_session",
            "record_stenographer_span",
            "list_stenographer_spans",
        } <= names

    def test_checkpoint_draft_approval_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp_sse.TOOLS}
        assert {
            "draft_checkpoint_from_spans",
            "get_checkpoint_draft",
            "revise_checkpoint_draft",
            "approve_checkpoint_draft",
            "reject_checkpoint_draft",
        } <= names

    def test_tool_feedback_schema_exposes_evaluation_envelope_fields(self):
        tool = next(tool for tool in mcp_sse.TOOLS if tool["name"] == "tool_feedback")
        props = tool["inputSchema"]["properties"]
        assert "scope" in props
        assert "what_was_tested" in props
        assert "expected_behavior" in props
        assert "observed_behavior" in props
        assert "next_action" in props
        assert "assessment" in props
        assert "should_promote" in props
        assert "confidence" in props

    async def test_live_observer_marks_project_activity(self, monkeypatch):
        fake_post = AsyncMock(return_value={})
        fake_qdrant = type("FakeQdrant", (), {"mark_used": AsyncMock()})()

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(dependencies, "get_qdrant", lambda: fake_qdrant)

        await mcp_sse._mcp_live_observe(
            "memory_search",
            {"project": "supermemory", "agent_id": "codex"},
            "http://test",
        )

        fake_qdrant.mark_used.assert_awaited_once_with([], project="supermemory")

    async def test_live_observer_emits_user_request_when_text_snippet_exists(self, monkeypatch):
        posted: list[dict] = []
        fake_qdrant = type("FakeQdrant", (), {"mark_used": AsyncMock()})()

        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/learning/events"
            posted.append(payload)
            return {}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(dependencies, "get_qdrant", lambda: fake_qdrant)

        await mcp_sse._mcp_live_observe(
            "memory_search",
            {
                "project": "supermemory",
                "agent_id": "codex",
                "query": "Need rollback checklist for Qdrant WAL cleanup after disk pressure.",
            },
            "http://test",
        )

        event_types = [item.get("event_type") for item in posted]
        assert "tool_call" in event_types
        assert "user_request" in event_types
        user_event = next(item for item in posted if item.get("event_type") == "user_request")
        assert "rollback checklist" in user_event["payload"]["request_text"].lower()
        fake_qdrant.mark_used.assert_awaited_once_with([], project="supermemory")

    async def test_session_observe_collects_dialogue_snippets(self, monkeypatch):
        class _FakeSessionStore:
            def __init__(self):
                self.ctx = {
                    "sess-1": {
                        "agent_id": "codex",
                        "tools_called": [],
                        "queries": [],
                        "skills_accessed": [],
                        "dialogue_snippets": [],
                    }
                }

            async def get_context(self, session_id: str):
                return self.ctx.get(session_id)

            async def set_context(self, session_id: str, ctx: dict):
                self.ctx[session_id] = ctx

        fake_store = _FakeSessionStore()
        from app.services import mcp_session_store

        monkeypatch.setattr(mcp_session_store, "get_session_store", lambda: fake_store)

        await mcp_sse._session_observe(
            "sess-1",
            "decompose_task_packet",
            {
                "task_description": "Need to split this migration into bounded packets with clear ownership and done criteria.",
                "packets": [
                    {"task_description": "Packet 1 handles schema setup and migration scripts."},
                    {"task_description": "Packet 2 handles verification and rollback checklist."},
                ],
            },
        )

        ctx = fake_store.ctx["sess-1"]
        assert ctx["tools_called"][-1]["tool"] == "decompose_task_packet"
        assert len(ctx["dialogue_snippets"]) >= 2

    async def test_auto_record_session_runs_dialogue_analysis_when_snippets_exist(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)

        await mcp_sse._auto_record_session(
            {
                "api_base": "http://test/api/v1",
                "agent_id": "codex",
                "pack_id": "pack-1",
                "session_id": "sess-1",
                "connected_at": 0.0,
                "tools_called": [{"tool": "memory_search", "ts": 1.0}],
                "queries": ["qdrant rollback guide"],
                "skills_received": [],
                "dialogue_snippets": [
                    {
                        "tool": "memory_search",
                        "text": "Need rollback checklist for qdrant wal cleanup after disk pressure and segment corruption risk.",
                        "ts": 1.0,
                    }
                ],
            }
        )

        analyze_payloads = [payload for path, payload in posted if path == "/skills/dialogue/analyze"]
        assert len(analyze_payloads) == 1
        assert analyze_payloads[0]["agent_id"] == "codex"
        assert analyze_payloads[0]["session_id"] == "sess-1"
        assert "USER:" in analyze_payloads[0]["transcript"]

    async def test_memory_stats_tool_uses_memories_stats_path(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {"total": 1}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("memory_stats", {}, "http://test")

        assert seen["path"] == "/memories/stats"
        assert '"total": 1' in result

    async def test_list_open_tasks_tool_uses_unified_artifacts_path(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("list_open_tasks", {"project": "alpha", "limit": 5}, "http://test")

        assert seen["path"] == "/artifacts?project=alpha&status=open&type=task&limit=5"
        assert "No open tasks found." in result

    async def test_list_open_tasks_tool_url_encodes_datetime_filters(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        await mcp_sse._execute_tool(
            "list_open_tasks",
            {
                "project": "alpha",
                "updated_after": "2026-04-17T00:00:00+04:00",
                "updated_before": "2026-04-18T00:00:00+04:00",
            },
            "http://test",
        )

        assert "updated_after=2026-04-17T00%3A00%3A00%2B04%3A00" in seen["path"]
        assert "updated_before=2026-04-18T00%3A00%3A00%2B04%3A00" in seen["path"]

    async def test_list_artifacts_tool_url_encodes_datetime_filters(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {"items": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        await mcp_sse._execute_tool(
            "list_artifacts",
            {
                "project": "alpha",
                "status": "open",
                "type": "task",
                "updated_after": "2026-04-17T00:00:00+04:00",
                "updated_before": "2026-04-18T00:00:00+04:00",
            },
            "http://test",
        )

        assert "updated_after=2026-04-17T00%3A00%3A00%2B04%3A00" in seen["path"]
        assert "updated_before=2026-04-18T00%3A00%3A00%2B04%3A00" in seen["path"]

    async def test_normalize_mcp_intent_returns_canonical_form_for_resume_requests(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "normalize_mcp_intent",
            {"intent": "Reopen task 84c4e534-d722-4132-8660-4a56ed93f44a", "project_id": "supermemory", "top_n": 3},
            "http://test",
        )

        assert '"resolved_tool": "reopen_task"' in result
        assert '"submit_to": "reopen_task"' in result
        assert '"cache"' in result
        assert '"canonical_surface"' in result
        assert '"normalize_mcp_intent"' in result
        assert '"stage": "testing"' in result
        assert '"feedback_expected": true' in result
        assert '"follow_up": "tool_feedback"' in result
        assert '"ready_to_execute": true' in result

    async def test_list_tool_families_returns_compact_catalog(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("list_tool_families", {}, "http://test")

        assert '"tool_discovery"' in result
        assert '"project_knowledge"' in result

    async def test_tool_family_tools_returns_full_tool_schema(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "tool_family_tools",
            {"family": "tool_discovery", "depth": "full", "limit": 20},
            "http://test",
        )

        assert '"family": "tool_discovery"' in result
        assert '"tool_feedback"' in result
        assert '"inputSchema"' in result

    async def test_report_task_checkpoint_records_task_change_and_session_context(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "change-1", "task_id": "task-1", "stage": "planning", "status": "planning"}

        class _FakeSessionStore:
            def __init__(self):
                self.ctx: dict[str, dict] = {}

            async def patch_context(self, session_id: str, patch: dict):
                ctx = self.ctx.setdefault(session_id, {})
                for key, value in patch.items():
                    ctx[key] = value

        fake_store = _FakeSessionStore()
        from app.services import mcp_session_store

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(mcp_session_store, "get_session_store", lambda: fake_store)

        result = await mcp_sse._execute_tool(
            "report_task_checkpoint",
            {
                "project": "alpha",
                "task_id": "task-1",
                "stage": "planning",
                "summary": "Framed the task and confirmed the first slice.",
                "blockers": ["Waiting for schema clarification."],
                "next_step": "Record decision candidates.",
                "reason": "Initial planning checkpoint.",
                "acted_by": "codex",
                "source": "mcp",
            },
            "http://test",
            session_id="sess-1",
        )

        assert posted[0][0] == "/project/tasks/task-1/changes"
        assert "[task_checkpoint]" in posted[0][1]["content"]
        assert "task_checkpoint" in posted[0][1]["tags"]
        assert fake_store.ctx["sess-1"]["task_checkpoint_recorded"] is True
        assert fake_store.ctx["sess-1"]["current_task_checkpoint"]["stage"] == "planning"
        assert "Checkpoint recorded for task task-1" in result

    async def test_record_task_checkpoint_creates_resume_handoff_for_blocked_stage(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path == "/models/handoff":
                return {"memory_id": "handoff-1", "handoff_label": payload["handoff_label"], "task_id": payload["task_id"]}
            return {"id": "change-1", "task_id": "task-1", "stage": "blocked", "status": "paused"}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "record_task_checkpoint",
            {
                "project": "alpha",
                "task_id": "task-1",
                "stage": "blocked",
                "summary": "Implementation is paused at the checkpoint facade.",
                "blockers": ["Need API shape confirmation."],
                "decisions": ["Store all checkpoints as task_change."],
                "changed_files": ["app/routers/mcp_sse.py"],
                "verification": ["Focused MCP test will cover blocked checkpoint."],
                "remaining_risk": ["Handoff packet creation is best effort."],
                "next_step": "Resume from the MCP handler.",
                "acted_by": "codex",
                "to_agent": "codex",
            },
            "http://test",
        )

        assert posted[0][0] == "/project/tasks/task-1/changes"
        assert "Decisions: Store all checkpoints as task_change." in posted[0][1]["content"]
        assert "Changed files: app/routers/mcp_sse.py" in posted[0][1]["content"]
        assert posted[1][0] == "/models/handoff"
        assert posted[1][1]["phase"] == "blocked"
        assert posted[1][1]["write_scope"] == ["app/routers/mcp_sse.py"]
        assert "handoff_packet_created" in result
        assert "handoff-1" in result

    async def test_draft_task_checkpoint_returns_record_tool_args_without_writing(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            return {"id": "unexpected"}

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "draft_task_checkpoint",
            {
                "project": "alpha",
                "task_id": "task-1",
                "stage": "handoff",
                "status": "paused",
                "use_llm": False,
                "raw_notes": "\n".join(
                    [
                        "Summary: Drafted checkpoint args for review.",
                        "Changed files: app/services/memory_scribe_service.py",
                        "Verification: pytest tests/test_memory_scribe_service.py passed",
                        "Next step: Submit record_task_checkpoint after review.",
                    ]
                ),
            },
            "http://test",
        )

        data = json.loads(result)
        assert posted == []
        assert data["mutates_memory"] is False
        assert data["recommended_next_tool"] == "record_task_checkpoint"
        assert data["record_task_checkpoint_args"]["task_id"] == "task-1"
        assert data["record_task_checkpoint_args"]["source"] == "memory_scribe"

    async def test_stenographer_mcp_tools_enforce_active_work(self, monkeypatch):
        from pathlib import Path
        from app.services import stenographer_service as stenographer_mod

        store = stenographer_mod.StenographerStore(Path(":memory:"))
        monkeypatch.setattr(stenographer_mod, "_STORE", store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            denied = await mcp_sse._execute_tool(
                "record_stenographer_span",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "kind": "verification",
                    "content": "13 passed",
                },
                "http://test",
            )
            denied_data = json.loads(denied)
            assert denied_data["error"] == "work_session_required"
            assert denied_data["required_next_tool"] == "start_work_session"

            started = await mcp_sse._execute_tool(
                "start_work_session",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "work_id": "work-1",
                },
                "http://test",
            )
            assert json.loads(started)["status"] == "active"

            recorded = await mcp_sse._execute_tool(
                "record_stenographer_span",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "work_id": "work-1",
                    "kind": "verification",
                    "source": "pytest",
                    "content": "13 passed",
                },
                "http://test",
            )
            recorded_data = json.loads(recorded)
            assert recorded_data["work_id"] == "work-1"
            assert recorded_data["excluded_from_learning"] is True
        finally:
            store.close()

    async def test_checkpoint_draft_mcp_tools_create_preview_from_spans(self, monkeypatch):
        from pathlib import Path
        from uuid import uuid4
        from app.services import checkpoint_draft_service as draft_mod
        from app.services import stenographer_service as stenographer_mod

        root = Path("qdrant_data") / "test_mcp_checkpoint_drafts"
        root.mkdir(parents=True, exist_ok=True)
        stenographer_store = stenographer_mod.StenographerStore(root / f"stenographer-{uuid4().hex}.db")
        draft_store = draft_mod.CheckpointDraftStore(root / f"drafts-{uuid4().hex}.db")
        monkeypatch.setattr(draft_mod, "get_stenographer_store", lambda: stenographer_store)
        monkeypatch.setattr(draft_mod, "_STORE", draft_store)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        try:
            stenographer_store.start_work_session(
                project="alpha",
                task_id="task-1",
                agent_id="codex",
                session_id="sess-1",
                work_id="work-1",
            )
            for kind, content in (
                ("fact", "Implemented checkpoint draft approval by reference."),
                ("decision", "Approve by draft_id and version, not by replaying payload."),
                ("verification", "pytest tests/test_checkpoint_draft_service.py passed"),
                ("next_step", "Expose approve-by-reference through MCP."),
            ):
                stenographer_store.record_span(
                    project="alpha",
                    task_id="task-1",
                    agent_id="codex",
                    session_id="sess-1",
                    work_id="work-1",
                    kind=kind,
                    source="test",
                    content=content,
                )

            drafted = await mcp_sse._execute_tool(
                "draft_checkpoint_from_spans",
                {
                    "project": "alpha",
                    "task_id": "task-1",
                    "work_id": "work-1",
                    "agent_id": "codex",
                    "session_id": "sess-1",
                    "use_llm": False,
                },
                "http://test",
            )
            drafted_data = json.loads(drafted)
            assert drafted_data["version"] == 1
            assert drafted_data["mutates_memory"] is False
            assert drafted_data["recommended_next_tool"] == "approve_checkpoint_draft"

            preview = await mcp_sse._execute_tool(
                "get_checkpoint_draft",
                {"draft_id": drafted_data["draft_id"], "view": "preview"},
                "http://test",
            )
            preview_data = json.loads(preview)
            assert "record_task_checkpoint_args" not in preview_data
            assert preview_data["metrics"]["estimated_saved_chars"] > 0
        finally:
            stenographer_store.close()
            draft_store.close()

    async def test_continue_task_returns_latest_checkpoint_and_next_safe_action(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            if path == "/project/tasks/task-1/statement?project=alpha":
                return {
                    "task": {"task_id": "task-1", "title": "Replay checkpoint task", "status": "active", "linked_improvement_id": "imp-1"},
                    "quality": {"capture_quality": "complete", "missing_artifacts": [], "grounded_by": ["task_changes"]},
                    "capture_review": {"pending_count": 0, "promoted_count": 1},
                    "next_actions": [{"priority": "low", "action": "Proceed from task statement.", "source_kind": "ready"}],
                }
            if path == "/project/tasks/task-1/changes?project=alpha&limit=100":
                return [
                    {
                        "id": "change-1",
                        "timestamp": "2026-04-25T10:00:00Z",
                        "tags": ["task_checkpoint", "task_stage:in_progress", "task_status:active"],
                        "content": "\n".join(
                            [
                                "[task_checkpoint]",
                                "Checkpoint stage: in_progress",
                                "Checkpoint status: active",
                                "Summary: Implemented checkpoint resume lookup.",
                                "Changed files: app/routers/mcp_sse.py",
                                "Verification: focused test passed",
                                "Next step: Add API-level test coverage.",
                            ]
                        ),
                    }
                ]
            raise AssertionError(path)

        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/list"
            return {
                "handoffs": [
                    {
                        "task_id": "task-1",
                        "memory_id": "handoff-1",
                        "handoff_label": "checkpoint-task-1-blocked",
                        "status": "paused",
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "continue_task",
            {"project": "alpha", "task_id": "task-1", "agent_id": "codex"},
            "http://test",
        )

        assert '"task_id": "task-1"' in result
        assert '"latest_checkpoint"' in result
        assert '"next_safe_action": "Add API-level test coverage."' in result
        assert '"detail": "compact"' in result
        assert '"available_layers"' in result
        assert '"handoff_refs"' in result
        assert '"replay_completeness"' in result
        assert '"status": "complete"' in result

    async def test_checkpoint_replay_completeness_roundtrip_through_mcp_state(self, client, monkeypatch):
        from app.routers import models as models_router

        class FakeRegistry:
            def log_handoff(self, **kwargs):
                pass

            def rank_for_task(self, task_type: str):
                return []

        async def local_get(api_base: str, path: str):
            response = await client.get(f"/api/v1{path}")
            assert response.status_code < 400, response.text
            return response.json()

        async def local_post(api_base: str, path: str, payload: dict):
            response = await client.post(f"/api/v1{path}", json=payload)
            assert response.status_code < 400, response.text
            return response.json()

        monkeypatch.setattr(mcp_sse, "_get", local_get)
        monkeypatch.setattr(mcp_sse, "_post", local_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())
        monkeypatch.setattr(models_router, "get_model_registry", lambda: FakeRegistry())

        create = await client.post(
            "/api/v1/project/tasks",
            json={
                "project": "alpha",
                "task_id": "task-replay-1",
                "title": "Replay task from durable MCP state",
                "description": "\n".join(
                    [
                        "Build a replay completeness check for SuperMemory task continuity.",
                        "Assumption: checkpoints are task changes, not a separate store.",
                        "Constraint: a new agent must not ask the user for old session context.",
                        "Definition of done: continue_task reconstructs next action from MCP state.",
                    ]
                ),
                "agent_id": "codex",
                "status": "active",
            },
        )
        assert create.status_code == 201, create.text

        checkpoint = await mcp_sse._execute_tool(
            "record_task_checkpoint",
            {
                "project": "alpha",
                "task_id": "task-replay-1",
                "stage": "handoff",
                "status": "active",
                "summary": "Implemented checkpoint replay plumbing.",
                "decisions": ["Use a thin MCP facade over task changes and handoffs."],
                "changed_files": ["app/routers/mcp_sse.py", "tests/test_mcp_sse.py"],
                "verification": ["Replay roundtrip test exercises real task APIs."],
                "remaining_risk": ["Full-project reproduction still needs broader scenario coverage."],
                "next_step": "Add the full replay completeness scenario to the release gate.",
                "acted_by": "codex",
                "to_agent": "codex",
            },
            "http://test",
        )
        assert "Checkpoint recorded for task task-replay-1" in checkpoint
        assert "handoff_packet_created=True" in checkpoint

        replay = await mcp_sse._execute_tool(
            "continue_task",
            {"project": "alpha", "task_id": "task-replay-1", "agent_id": "codex", "detail": "full"},
            "http://test",
        )
        data = json.loads(replay)

        assert data["status"] == "ready"
        assert data["task"]["title"] == "Replay task from durable MCP state"
        assert data["latest_checkpoint"]["stage"] == "handoff"
        assert data["latest_checkpoint"]["summary"] == "Implemented checkpoint replay plumbing."
        assert data["latest_checkpoint"]["changed_files"] == ["app/routers/mcp_sse.py", "tests/test_mcp_sse.py"]
        assert data["latest_checkpoint"]["verification"] == ["Replay roundtrip test exercises real task APIs."]
        assert data["next_safe_action"] == "Add the full replay completeness scenario to the release gate."
        assert data["resume_handoffs"][0]["task_id"] == "task-replay-1"
        assert data["task_statement_quality"]["capture_quality"] in {"complete", "partial"}
        assert data["replay_completeness"] == {
            "status": "complete",
            "required_fields": [
                "task.title",
                "task.status",
                "latest_checkpoint.stage",
                "latest_checkpoint.summary",
                "latest_checkpoint.changed_files",
                "latest_checkpoint.verification",
                "next_safe_action",
            ],
            "missing_fields": [],
            "can_continue_without_user": True,
            "release_gate": "replay_completeness_v1",
        }

    async def test_continue_task_replay_completeness_flags_missing_checkpoint_fields(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            if path == "/project/tasks/task-weak/statement?project=alpha":
                return {
                    "task": {"task_id": "task-weak", "title": "Weak replay task", "status": "active"},
                    "quality": {"capture_quality": "partial", "missing_artifacts": ["verification_result"]},
                    "capture_review": {"pending_count": 0, "promoted_count": 0},
                    "next_actions": [{"priority": "high", "action": "Record a planning checkpoint.", "source_kind": "missing_context"}],
                }
            if path == "/project/tasks/task-weak/changes?project=alpha&limit=100":
                return []
            raise AssertionError(path)

        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/list"
            return {"handoffs": []}

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "continue_task",
            {"project": "alpha", "task_id": "task-weak", "agent_id": "codex"},
            "http://test",
        )
        data = json.loads(result)

        assert data["replay_completeness"]["status"] == "incomplete"
        assert data["replay_completeness"]["can_continue_without_user"] is False
        assert data["replay_completeness"]["missing_fields"] == [
            "latest_checkpoint.stage",
            "latest_checkpoint.summary",
            "latest_checkpoint.changed_files",
            "latest_checkpoint.verification",
        ]
        assert data["recommended_first_tool"] == "record_task_checkpoint"

    async def test_project_workflow_returns_task_completion_form(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "project_workflow",
            {
                "project": "alpha",
                "intent": "I finished task task-1 and ran tests",
                "task_id": "task-1",
            },
            "http://test",
        )

        assert '"workflow": "task_completion"' in result
        assert '"submit_tool": "project_workflow_submit"' in result
        assert '"artifact_key": "task:alpha:task-1"' in result
        assert '"completion_summary": ""' in result

    async def test_project_workflow_submit_records_completion_and_resolves_artifact(self, monkeypatch):
        posted: list[tuple[str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            posted.append((path, payload))
            if path.endswith("/changes"):
                return {"id": "change-1", "task_id": "task-1"}
            if path.endswith("/resolve"):
                return {"artifact_key": "task:alpha:task-1", "status": "done"}
            raise AssertionError(path)

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "project_workflow_submit",
            {
                "workflow": "task_completion",
                "acted_by": "codex",
                "form": {
                    "project": "alpha",
                    "task_id": "task-1",
                    "completion_summary": "Implemented the completion workflow.",
                    "changed_files": ["app/routers/mcp_sse.py"],
                    "tests_run": ["pytest tests/test_mcp_sse.py -k project_workflow"],
                    "test_result": "passed",
                    "verdict": "completed",
                    "residual_risks": [],
                    "should_resolve_artifact": True,
                },
            },
            "http://test",
        )

        assert posted[0][0] == "/project/tasks/task-1/changes"
        assert "Implemented the completion workflow." in posted[0][1]["content"]
        assert "Tests run: pytest tests/test_mcp_sse.py -k project_workflow" in posted[0][1]["content"]
        assert posted[1][0] == "/artifacts/task%3Aalpha%3Atask-1/resolve"
        assert posted[1][1]["acted_by"] == "codex"
        assert '"routed_to": [' in result
        assert '"resolve_artifact"' in result

    async def test_reopen_task_tool_calls_project_task_reopen_endpoint(self, monkeypatch):
        calls: list[tuple[str, str, dict]] = []

        async def fake_post(api_base: str, path: str, payload: dict):
            calls.append((api_base, path, payload))
            return {
                "id": "task-1",
                "task_id": "task-1",
                "project": "alpha",
                "status": "active",
                "changes": [],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "reopen_task",
            {"task_id": "task-1", "status": "active", "reason": "manual_resume", "acted_by": "codex"},
            "http://test",
        )

        assert calls == [("http://test", "/project/tasks/task-1/reopen", {"status": "active", "reason": "manual_resume", "acted_by": "codex", "source": "mcp"})]
        assert '"status": "active"' in result

    async def test_tool_recommend_merges_project_recommendations(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/project/enrich-task"
            return {
                "context": "## Recommended MCP Calls\n\n1. `list_open_tasks`",
                "recommended_mcp_calls": [
                    {
                        "tool": "list_open_tasks",
                        "reason": "Project-specific open work items should be inspected first.",
                        "args": {"project": "alpha", "limit": 10},
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)

        result = await mcp_sse._execute_tool(
            "tool_recommend",
            {"task": "Find the right MCP call for open work items", "project_id": "alpha", "top_n": 2},
            "http://test",
        )

        assert '"stage": "testing"' in result
        assert '"feedback_expected": true' in result
        assert '"follow_up": "tool_feedback"' in result
        assert '"tool": "list_open_tasks"' in result
        assert '"project_recommended_calls"' in result
        assert '"project_context_summary"' in result

    async def test_tool_recommend_prefers_reopen_task_for_resume_requests(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "tool_recommend",
            {"task": "Resume task 84c4e534-d722-4132-8660-4a56ed93f44a"},
            "http://test",
        )

        assert '"tool": "reopen_task"' in result
        assert '"tool": "normalize_mcp_intent"' in result
        assert '"canonical_surface"' in result
        assert '"stage": "testing"' in result
        assert '"feedback_expected": true' in result
        assert '"follow_up": "tool_feedback"' in result
        assert result.index('"tool": "reopen_task"') < result.index('"tool": "report_task_checkpoint"')

    async def test_tool_explain_marks_testing_tools_for_feedback(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "tool_explain",
            {"tool_name": "tool_feedback", "task_context": "close the loop after tool use"},
            "http://test",
        )

        assert '"stage": "testing"' in result
        assert '"feedback_expected": true' in result
        assert '"follow_up": "tool_feedback"' in result

    async def test_tool_feedback_records_learning_ledger_entry(self, monkeypatch):
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        class _FakeStore:
            def __init__(self):
                self.feedback_calls = []
                self.event_calls = []

            async def write_feedback(self, **kwargs):
                self.feedback_calls.append(kwargs)
                return 42

            async def write_event(self, **kwargs):
                self.event_calls.append(kwargs)
                return 7

        fake_store = _FakeStore()
        from app.services import learning_store

        recorded = {}

        def fake_record_tool_feedback(**kwargs):
            recorded.update(kwargs)
            return {"tool_name": kwargs.get("tool_name"), "stage": kwargs.get("tool_stage")}

        monkeypatch.setattr(learning_store, "get_learning_store", lambda: fake_store)
        monkeypatch.setattr(mcp_sse, "record_tool_feedback", fake_record_tool_feedback)

        result = await mcp_sse._execute_tool(
            "tool_feedback",
            {
                "tool_name": "tool_family_tools",
                "tool_stage": "testing",
                "valence": "positive",
                "worked": True,
                "scope": "tool discovery",
                "what_was_tested": "Family browsing for a large MCP catalog",
                "expected_behavior": "Return a compact family-level view with follow-up guidance.",
                "observed_behavior": "Returned family metadata and a clear next step.",
                "friction": "Need clearer stage markers.",
                "missing_fields": ["stage", "feedback_expected"],
                "suggestion": "Add stage to the family tool listing.",
                "next_action": "Retest with the new envelope and compare whether agents need fewer follow-up reads.",
                "task_context": "Verify tool discovery feedback flow",
                "project_id": "alpha",
                "agent_id": "codex",
                "session_id": "sess-123",
            },
            "http://test",
            session_id="sess-123",
        )

        assert '"summary": "Recorded tool feedback for tool_family_tools"' in result
        assert '"assessment": "keep_testing"' in result
        assert '"scope": "tool discovery"' in result
        assert '"what_was_tested": "Family browsing for a large MCP catalog"' in result
        assert '"expected_behavior": "Return a compact family-level view with follow-up guidance."' in result
        assert '"observed_behavior": "Returned family metadata and a clear next step."' in result
        assert '"next_action": "Retest with the new envelope and compare whether agents need fewer follow-up reads."' in result
        assert fake_store.feedback_calls
        fb = fake_store.feedback_calls[0]
        assert fb["source"] == "mcp_tool_feedback"
        assert fb["payload"]["tool_name"] == "tool_family_tools"
        assert fb["payload"]["tool_stage"] == "testing"
        assert recorded["tool_name"] == "tool_family_tools"
        assert recorded["tool_stage"] == "testing"
        assert fake_store.event_calls
        assert fake_store.event_calls[0]["event_type"] == "artifact_feedback"

    async def test_initialize_exposes_supermemory_operational_guidance(self):
        response = await mcp_sse._handle(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "initialize",
                "params": {"clientInfo": {"name": "Codex CLI"}},
            },
            "http://test",
            session_id="sess-1",
        )

        info = response["result"]["_supermemory"]
        assert info["agent_id"] == "codex-cli"
        assert "get_onboarding" in info["tip"]
        assert "pickup_coordination_messages" in info["tip"]
        assert any("/api/v1/coordination/" in line for line in info["semantic_defaults"])

    async def test_get_onboarding_includes_supermemory_basics(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            if path == "/admin/storage-trust":
                return {
                    "status": "degraded",
                    "summary": "Storage trust is degraded: at least one integrity slice is unhealthy and operator action is required.",
                }
            if path == "/skills/pinned":
                return []
            if path.startswith("/skills/gaps"):
                return {"gaps": []}
            if path.startswith("/skills/analytics"):
                return {"total_outcomes": 0}
            if path.startswith("/memories/recent"):
                return []
            raise AssertionError(f"unexpected GET path: {path}")

        async def fake_post(api_base: str, path: str, payload: dict):
            if path == "/skills/profile":
                return {"domains": ["automation"]}
            if path == "/skills/pack/create":
                return {
                    "pack_id": "pack-1",
                    "degraded": True,
                    "degraded_reason": "Qdrant skill/domain filter is degraded; skill pack may be served from fallback storage.",
                    "skills": [{"id": "skill-1", "name": "remember-context", "description": "Keep project context stable"}],
                }
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_onboarding",
            {"agent_id": "codex", "task_description": "Work with remote project coordination"},
            "http://test",
        )

        # New layered instruction format
        assert "L0: Core Policy" in result
        assert "L1: Task Context" in result
        assert "L2: Coordination Guidelines" in result
        assert "ACTIVE OPERATIONAL INSTINCTS:" in result
        assert "trust_first" in result
        assert "get_storage_trust_status" in result
        assert "STORAGE TRUST WARNING:" in result
        assert "pickup_coordination_messages" in result
        assert "coordination_is_not_truth" in result
        assert "INTEGRITY WARNING:" in result

    async def test_get_onboarding_degrades_gracefully_when_skill_pack_http500(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            if path == "/admin/storage-trust":
                return {"status": "ok", "summary": "ok"}
            if path == "/skills/pinned":
                return []
            if path.startswith("/skills/gaps"):
                return {"gaps": []}
            if path.startswith("/skills/analytics"):
                return {"total_outcomes": 0}
            if path.startswith("/memories/recent"):
                return []
            raise AssertionError(f"unexpected GET path: {path}")

        async def fake_post(api_base: str, path: str, payload: dict):
            if path == "/skills/profile":
                return {"domains": ["automation"]}
            if path == "/skills/pack/create":
                req = httpx.Request("POST", f"{api_base}{path}")
                resp = httpx.Response(
                    500,
                    request=req,
                    json={"detail": "Qdrant domain_tags filter panic"},
                )
                raise httpx.HTTPStatusError("Server error", request=req, response=resp)
            raise AssertionError(f"unexpected POST path: {path}")

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_onboarding",
            {"agent_id": "codex", "task_description": "Need orientation"},
            "http://test",
        )

        # New layered instruction format
        assert "L0: Core Policy" in result
        assert "L1: Task Context" in result
        assert "L2: Memory Operations Guidelines" in result
        assert "Skills: temporarily unavailable (HTTP 500: Qdrant domain_tags filter panic)." in result
        assert "/skills/pack/create" not in result

    async def test_list_pending_handoff_labels_formats_named_queue(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            assert path == "/models/handoff/pending_labels?agent_id=claude-code&limit=20"
            return {
                "agent_id": "claude-code",
                "found": 2,
                "labels": [
                    {
                        "handoff_label": "benchmark28",
                        "count": 1,
                        "latest_task_id": "abc12345",
                        "from_agents": ["codex"],
                    },
                    {
                        "handoff_label": "tailcutoff371",
                        "count": 2,
                        "latest_task_id": "def67890",
                        "from_agents": ["codex", "cline"],
                    },
                ],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_pending_handoff_labels",
            {"agent_id": "claude-code", "limit": 20},
            "http://test",
        )

        assert "Pending handoff labels for 'claude-code':" in result
        assert "benchmark28" in result
        assert "tailcutoff371" in result

    async def test_pickup_handoff_suggests_label_listing_when_multiple_items_arrive(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/pickup"
            return {
                "agent_id": "claude-code",
                "handoff_label": None,
                "found": 2,
                "handoffs": [
                    {
                        "task_id": "abc12345",
                        "handoff_label": "benchmark28",
                        "from_agent": "codex",
                        "memory_id": "mem-1",
                        "content": "HANDOFF CONTEXT\n...",
                    },
                    {
                        "task_id": "def67890",
                        "handoff_label": "tailcutoff371",
                        "from_agent": "codex",
                        "memory_id": "mem-2",
                        "content": "HANDOFF CONTEXT\n...",
                    },
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "pickup_handoff",
            {"agent_id": "claude-code", "limit": 5},
            "http://test",
        )

        assert "Found 2 pending handoff(s) for 'claude-code':" in result
        assert "list_pending_handoff_labels(agent_id='claude-code')" in result
        assert "handoff_label: benchmark28" in result

    async def test_pickup_handoff_renders_structured_phase_snapshot(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/pickup"
            return {
                "agent_id": "claude-code",
                "handoff_label": "benchmark28",
                "found": 1,
                "handoffs": [
                    {
                        "task_id": "abc12345",
                        "handoff_label": "benchmark28",
                        "from_agent": "codex",
                        "memory_id": "mem-1",
                        "status": "pending",
                        "project_id": "supermemory",
                        "owner_agent": "claude-code",
                        "write_scope": ["handoff", "task-packet"],
                        "phase": "task_framing",
                        "priority": "high",
                        "definition_of_done": "Produce a planning packet",
                        "expected_output_shape": "Short ranked benchmark plan",
                        "phase_objective": "Turn an incomplete request into an explicit task.",
                        "core_instinct_ids": [
                            "assume_initial_task_statement_is_incomplete",
                            "clarify_scope_assumptions_and_done",
                        ],
                        "supporting_instinct_ids": ["track_assumptions_explicitly"],
                        "project_context_summary": "coverage laws=1, components=1; highlights laws: Require review",
                        "project_context_refs": {"laws": ["law-1"], "components": ["handoff"]},
                        "content": "HANDOFF CONTEXT\n...",
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "pickup_handoff",
            {"agent_id": "claude-code", "handoff_label": "benchmark28", "limit": 5},
            "http://test",
        )

        assert "status: pending" in result
        assert "project_id: supermemory" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff, task-packet" in result
        assert "phase: task_framing" in result
        assert "priority: high" in result
        assert "definition_of_done: Produce a planning packet" in result
        assert "expected_output_shape: Short ranked benchmark plan" in result
        assert "phase_objective: Turn an incomplete request into an explicit task." in result
        assert "core_instinct_ids: assume_initial_task_statement_is_incomplete, clarify_scope_assumptions_and_done" in result
        assert "supporting_instinct_ids: track_assumptions_explicitly" in result
        assert "project_context_summary: coverage laws=1, components=1; highlights laws: Require review" in result
        assert "project_context_refs: laws=1, components=1" in result
        assert "Use expand_handoff_refs(memory_id='mem-1') to inspect referenced context." in result
        assert "laws=law-1" not in result

    async def test_expand_handoff_refs_formats_compact_resolution(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/expand_refs"
            return {
                "memory_id": "mem-1",
                "project_id": "supermemory",
                "requested_ref_types": ["laws", "components"],
                "resolved": {
                    "laws": [
                        {
                            "id": "law-1",
                            "title": "Require review",
                            "status": "active",
                            "statement": "Explicit review is required.",
                        }
                    ],
                    "components": [
                        {
                            "component_id": "handoff",
                            "name": "Handoff",
                            "summary": "Carry portable task context.",
                        }
                    ],
                },
                "unresolved": {"docs_sections": ["overview"]},
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "expand_handoff_refs",
            {"memory_id": "mem-1", "ref_types": ["laws", "components"], "limit_per_type": 2},
            "http://test",
        )

        assert "Expanded handoff refs for mem-1" in result
        assert "project_id: supermemory" in result
        assert "requested_ref_types: laws, components" in result
        assert "- law-1 [active] Require review" in result
        assert "- handoff Handoff: Carry portable task context." in result
        assert "unresolved: docs_sections=1" in result

    async def test_expand_handoff_refs_formats_task_capture_candidates(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/expand_refs"
            return {
                "memory_id": "mem-2",
                "project_id": "supermemory",
                "requested_ref_types": ["task_capture_candidates"],
                "resolved": {
                    "task_capture_candidates": [
                        {
                            "artifact_id": "capture-1",
                            "task_id": "task-1",
                            "kind": "assumption",
                            "source": "local_slm",
                            "status": "active",
                            "content": "Task framing is still incomplete until assumption capture is reviewed.",
                        }
                    ]
                },
                "unresolved": {},
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "expand_handoff_refs",
            {"memory_id": "mem-2", "ref_types": ["task_capture_candidates"], "limit_per_type": 2},
            "http://test",
        )

        assert "requested_ref_types: task_capture_candidates" in result
        assert "- capture-1 [active] assumption for task-1: Task framing is still incomplete" in result

    async def test_refresh_handoff_context_formats_refresh_result(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/refresh_context"
            return {
                "memory_id": "mem-1",
                "status": "picked_up",
                "project_id": "supermemory",
                "owner_agent": "claude-code",
                "write_scope": ["handoff"],
                "task_description": "Refresh this task packet",
                "project_context_summary": "coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1",
                "project_context_refs": {
                    "laws": ["law-1"],
                    "components": ["handoff"],
                    "improvements": ["imp-1"],
                    "tasks": ["task-1"],
                    "docs_sections": ["overview"],
                },
                "coverage": {
                    "laws": 1,
                    "components": 1,
                    "improvements": 1,
                    "runtime_hints": 0,
                    "tasks": 1,
                    "docs_sections": 1,
                },
                "code_inspection_recommended": False,
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "refresh_handoff_context",
            {"memory_id": "mem-1", "task_description": "Refresh this task packet", "max_components": 2},
            "http://test",
        )

        assert "Refreshed handoff context for mem-1" in result
        assert "status: picked_up" in result
        assert "project_id: supermemory" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff" in result
        assert "task: Refresh this task packet" in result
        assert "project_context_summary: coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result
        assert "project_context_refs: laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result
        assert "coverage: laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result

    async def test_handoff_workspace_summary_formats_compact_overview(self, monkeypatch):
        response = {
            "agent_id": "codex",
            "statuses": ["active", "paused"],
            "handoff_label": "benchmark28",
            "owner_agent": "claude-code",
            "write_scope": ["handoff", "task-packet"],
            "total": 2,
            "by_status": {"active": 1, "paused": 1},
            "by_owner_agent": {"claude-code": 1, "codex": 1},
            "by_phase": {"task_framing": 1, "pre_implementation": 1},
            "merge_back_guidance": "Merge back into the active packet after review; avoid opening a new branch unless ownership changes.",
            "recent_packets": [
                {
                    "task_id": "abc12345",
                    "handoff_label": "benchmark28",
                    "status": "active",
                    "owner_agent": "claude-code",
                    "phase": "task_framing",
                    "priority": "high",
                    "executor_used": "cheap-worker",
                    "model_used": "qwen3:1.7b",
                    "memory_id": "mem-1",
                },
                {
                    "task_id": "def67890",
                    "status": "paused",
                    "owner_agent": "codex",
                    "phase": "pre_implementation",
                    "priority": "medium",
                    "executor_used": "codex-mini",
                    "model_used": "gpt-5.1-mini",
                    "memory_id": "mem-2",
                },
            ],
        }

        async def fake_sse_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/workspace_summary"
            assert payload == {
                "agent_id": "codex",
                "statuses": ["active", "paused"],
                "handoff_label": "benchmark28",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "packet_limit": 5,
            }
            return response

        monkeypatch.setattr(mcp_sse, "_post", fake_sse_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        sse_result = await mcp_sse._execute_tool(
            "handoff_workspace_summary",
            {
                "agent_id": "codex",
                "statuses": ["active", "paused"],
                "handoff_label": "benchmark28",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "packet_limit": 5,
            },
            "http://test",
        )

        assert "Workspace handoff summary for 'codex' (active, paused):" in sse_result
        assert "total: 2" in sse_result
        assert "by_status: active=1, paused=1" in sse_result
        assert "by_owner_agent: claude-code=1, codex=1" in sse_result
        assert "by_phase: task_framing=1, pre_implementation=1" in sse_result
        assert "merge_back_guidance: Merge back into the active packet after review; avoid opening a new branch unless ownership changes." in sse_result
        assert "- task_id=abc12345 label=benchmark28 status=active owner_agent=claude-code phase=task_framing priority=high memory_id=mem-1 executor_used=cheap-worker model_used=qwen3:1.7b" in sse_result
        assert "- task_id=def67890 status=paused owner_agent=codex phase=pre_implementation priority=medium memory_id=mem-2 executor_used=codex-mini model_used=gpt-5.1-mini" in sse_result
        assert "pending_labels:" not in sse_result

    async def test_update_handoff_status_formats_packet_lifecycle_change(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/status"
            return {
                "memory_id": "mem-1",
                "status": "active",
                "acted_by": "codex",
                "reason": "resumed for implementation",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "executor_used": "cheap-worker",
                "model_used": "qwen3:1.7b",
                "result_summary": "Added the bounded packet change.",
                "verification_summary": "Focused tests passed.",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "update_handoff_status",
            {
                "memory_id": "mem-1",
                "status": "active",
                "acted_by": "codex",
                "reason": "resumed for implementation",
            },
            "http://test",
        )

        assert "Updated handoff status for mem-1" in result
        assert "status: active" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff, task-packet" in result
        assert "executor_used: cheap-worker" in result
        assert "model_used: qwen3:1.7b" in result
        assert "result_summary: Added the bounded packet change." in result
        assert "verification_summary: Focused tests passed." in result
        assert "acted_by: codex" in result
        assert "reason: resumed for implementation" in result

    async def test_list_handoffs_formats_status_aware_packet_listing(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/list"
            return {
                "agent_id": "claude-code",
                "statuses": ["active", "paused"],
                "found": 2,
                "handoffs": [
                    {
                        "task_id": "abc12345",
                        "handoff_label": "resume42",
                        "status": "paused",
                        "owner_agent": "claude-code",
                        "write_scope": ["handoff", "task-packet"],
                        "result_summary": "Prepared the bounded patch.",
                        "executor_used": "cheap-worker",
                        "model_used": "qwen3:1.7b",
                        "phase": "task_framing",
                        "priority": "high",
                        "memory_id": "mem-1",
                    },
                    {
                        "task_id": "def67890",
                        "handoff_label": "implement88",
                        "status": "active",
                        "owner_agent": "codex",
                        "write_scope": ["implementation"],
                        "phase": "pre_implementation",
                        "priority": "medium",
                        "executor_used": "codex-mini",
                        "model_used": "gpt-5.1-mini",
                        "memory_id": "mem-2",
                    },
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_handoffs",
            {"agent_id": "claude-code", "statuses": ["active", "paused"], "limit": 20},
            "http://test",
        )

        assert "Handoffs for 'claude-code' (active, paused):" in result
        assert "label=resume42 status=paused phase=task_framing priority=high memory_id=mem-1 owner_agent=claude-code write_scope=handoff, task-packet executor_used=cheap-worker model_used=qwen3:1.7b result_summary=Prepared the bounded patch." in result
        assert "label=implement88 status=active phase=pre_implementation priority=medium memory_id=mem-2 owner_agent=codex write_scope=implementation executor_used=codex-mini model_used=gpt-5.1-mini" in result

    async def test_resume_handoff_formats_reactivation_result(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/resume"
            return {
                "memory_id": "mem-1",
                "status": "active",
                "refreshed": True,
                "acted_by": "codex",
                "reason": "returning to task",
                "project_id": "supermemory",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "phase": "pre_implementation",
                "priority": "medium",
                "task_description": "Resume this task packet",
                "phase_objective": "Continue bounded implementation work.",
                "definition_of_done": "Implement the next compact slice.",
                "expected_output_shape": "Short implementation result with verification.",
                "project_context_summary": "coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1",
                "project_context_refs": {
                    "laws": ["law-1"],
                    "components": ["handoff"],
                    "improvements": ["imp-1"],
                    "tasks": ["task-1"],
                    "docs_sections": ["overview"],
                },
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "resume_handoff",
            {
                "memory_id": "mem-1",
                "refresh_context": True,
                "acted_by": "codex",
                "reason": "returning to task",
                "max_components": 2,
            },
            "http://test",
        )

        assert "Resumed handoff mem-1" in result
        assert "status: active" in result
        assert "refreshed: true" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff, task-packet" in result
        assert "acted_by: codex" in result
        assert "reason: returning to task" in result
        assert "project_id: supermemory" in result
        assert "phase: pre_implementation" in result
        assert "priority: medium" in result
        assert "task: Resume this task packet" in result
        assert "phase_objective: Continue bounded implementation work." in result
        assert "definition_of_done: Implement the next compact slice." in result
        assert "expected_output_shape: Short implementation result with verification." in result
        assert "project_context_refs: laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result

    async def test_decompose_task_packet_formats_recommended_packet_stubs(self, monkeypatch):
        response = {
            "project_id": "supermemory",
            "strategy": "split_by_write_scope",
            "recommended_packet_count": 2,
            "phase": "pre_implementation",
            "phase_objective": "Deliver a bounded implementation slice that is easy to verify and merge back.",
            "why_split": "Suggested packets are separated by bounded write scopes so they can be delegated with lower conflict risk.",
            "packets": [
                {
                    "handoff_label": "packet-models-py",
                    "owner_agent": "codex",
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "balanced",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "write_scope": ["app/routers/models.py"],
                    "definition_of_done": "Finish the bounded work for this packet, verify the result, and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary, verification summary, and any follow-up or merge-back notes.",
                },
                {
                    "handoff_label": "packet-mcp_sse-py",
                    "owner_agent": "nash",
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "definition_of_done": "Finish the bounded work for this packet, verify the result, and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary, verification summary, and any follow-up or merge-back notes.",
                },
            ],
        }

        async def fake_sse_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/decompose"
            assert payload == {
                "project_id": "supermemory",
                "task_description": "Split packet work",
                "priority": "high",
                "write_scope": ["app/routers/models.py", "app/routers/mcp_sse.py", "mcp/server.py"],
                "max_packets": 2,
            }
            return response

        monkeypatch.setattr(mcp_sse, "_post", fake_sse_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        sse_result = await mcp_sse._execute_tool(
            "decompose_task_packet",
            {
                "project_id": "supermemory",
                "task_description": "Split packet work",
                "priority": "high",
                "write_scope": ["app/routers/models.py", "app/routers/mcp_sse.py", "mcp/server.py"],
                "max_packets": 2,
            },
            "http://test",
        )

        assert "Task packet decomposition:" in sse_result
        assert "strategy: split_by_write_scope" in sse_result
        assert "recommended_packet_count: 2" in sse_result
        assert "label=packet-models-py owner_agent=codex phase=pre_implementation priority=high execution_mode=balanced suggested_execution_tier=cheap model_hint=gpt-5.1-mini write_scope=app/routers/models.py" in sse_result
        assert "label=packet-mcp_sse-py owner_agent=nash phase=pre_implementation priority=high execution_mode=economy suggested_execution_tier=cheap model_hint=gpt-5.1-mini write_scope=app/routers/mcp_sse.py, mcp/server.py" in sse_result

    async def test_create_task_packets_formats_created_packet_bundle(self, monkeypatch):
        response = {
            "project_id": "supermemory",
            "created_count": 2,
            "packets": [
                {
                    "task_id": "task-1",
                    "handoff_label": "packet-models-py",
                    "memory_id": "mem-1",
                    "to_agent": "codex",
                    "status": "pending",
                    "owner_agent": "codex",
                    "write_scope": ["app/routers/models.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "balanced",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "why_now": "Keep the models patch isolated.",
                    "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary and verification summary.",
                    "phase_objective": "Deliver a bounded implementation slice that is easy to verify and merge back.",
                    "core_instinct_ids": ["every_task_must_exist_in_memory"],
                    "supporting_instinct_ids": ["clarify_scope_assumptions_and_done"],
                    "pickup_instruction": "In codex: use pickup_handoff(agent_id='codex', handoff_label='packet-models-py')",
                },
                {
                    "task_id": "task-2",
                    "handoff_label": "packet-mcp_sse-py",
                    "memory_id": "mem-2",
                    "to_agent": "nash",
                    "status": "pending",
                    "owner_agent": "nash",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary and verification summary.",
                    "pickup_instruction": "In nash: use pickup_handoff(agent_id='nash', handoff_label='packet-mcp_sse-py')",
                },
            ],
        }

        expected_payload = {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "project_id": "supermemory",
            "task_description": "Split packet work",
            "partial_result": "Preflight ready",
            "key_facts": ["one", "two"],
            "reason": "manual",
            "from_model_id": "gpt-4o",
            "agent_id": "handoff",
            "packets": [
                {
                    "handoff_label": "packet-models-py",
                    "owner_agent": "codex",
                    "write_scope": ["app/routers/models.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "balanced",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "why_now": "Keep the models patch isolated.",
                    "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary and verification summary.",
                    "phase_objective": "Deliver a bounded implementation slice that is easy to verify and merge back.",
                    "core_instinct_ids": ["every_task_must_exist_in_memory"],
                    "supporting_instinct_ids": ["clarify_scope_assumptions_and_done"],
                    "project_context_summary": "coverage laws=1, components=1",
                    "project_context_refs": {"laws": ["law-1"], "components": ["handoff"]},
                    "project_context_snapshot": "HANDOFF CONTEXT",
                },
                {
                    "handoff_label": "packet-mcp_sse-py",
                    "owner_agent": "nash",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                    "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                    "expected_output_shape": "Short result summary and verification summary.",
                },
            ],
        }

        async def fake_sse_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/create_packets"
            assert payload == expected_payload
            return response

        monkeypatch.setattr(mcp_sse, "_post", fake_sse_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        sse_result = await mcp_sse._execute_tool(
            "create_task_packets",
            {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "project_id": "supermemory",
                "task_description": "Split packet work",
                "partial_result": "Preflight ready",
                "key_facts": ["one", "two"],
                "reason": "manual",
                "from_model_id": "gpt-4o",
                "agent_id": "handoff",
                "packets": [
                {
                    "handoff_label": "packet-models-py",
                    "owner_agent": "codex",
                    "write_scope": ["app/routers/models.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "balanced",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                        "why_now": "Keep the models patch isolated.",
                        "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                        "expected_output_shape": "Short result summary and verification summary.",
                        "phase_objective": "Deliver a bounded implementation slice that is easy to verify and merge back.",
                        "core_instinct_ids": ["every_task_must_exist_in_memory"],
                        "supporting_instinct_ids": ["clarify_scope_assumptions_and_done"],
                        "project_context_summary": "coverage laws=1, components=1",
                        "project_context_refs": {"laws": ["law-1"], "components": ["handoff"]},
                        "project_context_snapshot": "HANDOFF CONTEXT",
                    },
                {
                    "handoff_label": "packet-mcp_sse-py",
                    "owner_agent": "nash",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "gpt-5.1-mini",
                        "definition_of_done": "Finish the bounded work and return a short merge-back summary.",
                        "expected_output_shape": "Short result summary and verification summary.",
                    },
                ],
            },
            "http://test",
        )
        assert "Created 2 task packet(s)" in sse_result
        assert "handoff_label: packet-models-py" in sse_result
        assert "handoff_label: packet-mcp_sse-py" in sse_result
        assert "owner_agent: codex" in sse_result
        assert "write_scope: app/routers/models.py" in sse_result
        assert "write_scope: app/routers/mcp_sse.py, mcp/server.py" in sse_result
        assert "execution_mode: balanced" in sse_result
        assert "execution_mode: economy" in sse_result
        assert "suggested_execution_tier: cheap" in sse_result
        assert "model_hint: gpt-5.1-mini" in sse_result

    async def test_route_task_packet_execution_formats_compact_recommendation(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/route_execution"
            assert payload["memory_id"] == "mem-1"
            assert payload["packet"]["task_description"] == "Implement cheap-tier MCP parity"
            assert payload["packet"]["write_scope"] == ["app/routers/mcp_sse.py", "mcp/server.py"]
            assert payload["packet"]["execution_mode"] == "strict_economy"
            return {
                "memory_id": "mem-1",
                "packet": payload["packet"],
                "packet_profile": {"tier": "cheap", "score": 0.93, "reason": "small bounded packet"},
                "routing_basis": ["cheap-tier executor is sufficient", "write scope is narrow"],
                "eligible_executors": ["cheap-worker", "codex-mini"],
                "recommended_executor": "cheap-worker",
                "recommended_model": "gemini-2.5-flash",
                "recommendation_reason": "packet fits the cheap-tier executor and stays within the write scope",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "route_task_packet_execution",
            {
                "memory_id": "mem-1",
                "packet": {
                    "task_description": "Implement cheap-tier MCP parity",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "phase": "task_framing",
                    "execution_mode": "strict_economy",
                    "suggested_execution_tier": "cheap",
                    "model_hint": "qwen3:1.7b",
                    "executor_used": "cheap-worker",
                    "model_used": "qwen3:1.7b",
                    "definition_of_done": "Tool is available in both transports",
                    "expected_output_shape": "Compact routing recommendation",
                },
            },
            "http://test",
        )

        assert "Route execution recommendation:" in result
        assert "memory_id: mem-1" in result
        assert "packet: task_description=Implement cheap-tier MCP parity" in result
        assert "phase=task_framing" in result
        assert "execution_mode=strict_economy" in result
        assert "suggested_execution_tier=cheap" in result
        assert "model_hint=qwen3:1.7b" in result
        assert "executor_used=cheap-worker" in result
        assert "model_used=qwen3:1.7b" in result
        assert "write_scope=app/routers/mcp_sse.py, mcp/server.py" in result
        assert "definition_of_done: Tool is available in both transports" in result
        assert "expected_output_shape: Compact routing recommendation" in result
        assert "packet_profile: tier=cheap; score=0.93; reason=small bounded packet" in result
        assert "routing_basis: cheap-tier executor is sufficient, write scope is narrow" in result
        assert "eligible_executors: cheap-worker, codex-mini" in result
        assert "recommended_executor: cheap-worker" in result
        assert "recommended_model: gemini-2.5-flash" in result
        assert "recommendation_reason: packet fits the cheap-tier executor" in result

    async def test_dispatch_background_task_packet_formats_job_submission(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/dispatch_background"
            assert payload == {
                "memory_id": "mem-1",
                "acted_by": "codex",
                "reason": "queue the low-risk packet",
            }
            return {
                "memory_id": "mem-1",
                "status": "active",
                "executor_used": "local_slm_background",
                "model_used": "qwen3:1.7b",
                "background_job_type": "docs_rebuild",
                "job_id": "job-docs-1",
                "poll": "/api/v1/tasks/job-docs-1",
                "recommendation_reason": "Strict economy mode prefers local background execution first for low-risk packets that are easy to verify.",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "dispatch_background_task_packet",
            {
                "memory_id": "mem-1",
                "acted_by": "codex",
                "reason": "queue the low-risk packet",
            },
            "http://test",
        )

        assert "Background dispatch queued:" in result
        assert "memory_id: mem-1" in result
        assert "status: active" in result
        assert "executor_used: local_slm_background" in result
        assert "model_used: qwen3:1.7b" in result
        assert "background_job_type: docs_rebuild" in result
        assert "job_id: job-docs-1" in result
        assert "poll: /api/v1/tasks/job-docs-1" in result

    async def test_reconcile_background_task_packet_formats_job_state(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/reconcile_background"
            assert payload == {"memory_id": "mem-1", "acted_by": "codex"}
            return {
                "memory_id": "mem-1",
                "status": "closed",
                "job_id": "job-docs-1",
                "background_job_status": "done",
                "background_job_type": "docs_rebuild",
                "executor_used": "local_slm_background",
                "model_used": "qwen3:1.7b",
                "result_summary": "Background job docs_rebuild completed.",
                "verification_summary": "project=supermemory; sections=overview, api",
                "poll": "/api/v1/tasks/job-docs-1",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "reconcile_background_task_packet",
            {"memory_id": "mem-1", "acted_by": "codex"},
            "http://test",
        )

        assert "Background job reconciled:" in result
        assert "status: closed" in result
        assert "background_job_status: done" in result
        assert "background_job_type: docs_rebuild" in result
        assert "executor_used: local_slm_background" in result
        assert "model_used: qwen3:1.7b" in result
        assert "result_summary: Background job docs_rebuild completed." in result
        assert "verification_summary: project=supermemory; sections=overview, api" in result
        assert "poll: /api/v1/tasks/job-docs-1" in result

    async def test_list_handoffs_surfaces_background_job_state(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            assert path == "/models/handoff/list"
            return {
                "found": 1,
                "statuses": ["closed"],
                "handoffs": [
                    {
                        "task_id": "task-1",
                        "handoff_label": "resume42",
                        "status": "closed",
                        "phase": "task_framing",
                        "priority": "high",
                        "memory_id": "mem-1",
                        "background_job_status": "done",
                        "dispatched_job_id": "job-docs-1",
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_handoffs",
            {"agent_id": "codex", "statuses": ["closed"], "limit": 10},
            "http://test",
        )

        assert "background_job_status=done" in result
        assert "dispatched_job_id=job-docs-1" in result

    async def test_handoff_task_includes_phase_and_iteration_contract(self, monkeypatch):
        async def fake_post(api_base: str, path: str, payload: dict):
            if path == "/project/enrich-task":
                assert payload["project_id"] == "supermemory"
                assert payload["task"] == "Benchmark competitors"
                return {
                    "context": "## Relevant Components\n\n### Handoff\n**Purpose:** Carry portable task context.\n",
                    "components": [{"component_id": "handoff", "name": "Handoff"}],
                    "laws": [{"id": "law-1", "title": "Require review"}],
                    "improvements": [{"id": "imp-1", "title": "Tighten handoff packet"}],
                    "runtime_hints": [],
                    "memoirs": [],
                    "tasks": [{"task_id": "task-1", "title": "Benchmark flow"}],
                    "docs_sections": [{"section_key": "overview"}],
                    "operational_instincts": [],
                    "missing_sources": [],
                    "code_inspection_recommended": False,
                    "message": "",
                }
            assert path == "/models/handoff"
            assert payload["project_id"] == "supermemory"
            assert payload["phase"] == "task_framing"
            assert payload["priority"] == "high"
            assert payload["definition_of_done"] == "Produce a planning packet"
            assert payload["expected_output_shape"] == "Short ranked benchmark plan"
            assert payload["phase_objective"]
            assert "every_task_must_exist_in_memory" in payload["core_instinct_ids"]
            assert payload["project_context_summary"].startswith("coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1")
            assert payload["project_context_refs"] == {
                "laws": ["law-1"],
                "components": ["handoff"],
                "improvements": ["imp-1"],
                "tasks": ["task-1"],
                "docs_sections": ["overview"],
            }
            assert payload["owner_agent"] == "claude-code"
            assert payload["write_scope"] == ["handoff", "task-packet"]
            return {
                "memory_id": "mem-1",
                "task_id": "abc12345",
                "handoff_label": "benchmark28",
                "project_id": "supermemory",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "phase": "task_framing",
                "priority": "high",
                "phase_objective": "Turn an incomplete request into an explicit, bounded, and explainable task.",
                "core_instinct_ids": [
                    "assume_initial_task_statement_is_incomplete",
                    "clarify_scope_assumptions_and_done",
                    "every_task_must_exist_in_memory",
                ],
                "project_context_summary": "coverage laws=1, components=1, improvements=1, tasks=1, docs_sections=1; highlights laws: Require review | components: Handoff | improvements: Tighten handoff packet",
                "project_context_refs": {
                    "laws": ["law-1"],
                    "components": ["handoff"],
                    "improvements": ["imp-1"],
                    "tasks": ["task-1"],
                    "docs_sections": ["overview"],
                },
                "to_agent": "claude-code",
                "next_available": [{"model_id": "gpt-4o"}],
                "pickup_instruction": "In claude-code: use pickup_handoff(agent_id='claude-code', handoff_label='benchmark28')",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "handoff_task",
            {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "task_description": "Benchmark competitors",
                "project_id": "supermemory",
                "owner_agent": "claude-code",
                "write_scope": ["handoff", "task-packet"],
                "handoff_label": "benchmark28",
                "phase": "task_framing",
                "priority": "high",
                "definition_of_done": "Produce a planning packet",
                "expected_output_shape": "Short ranked benchmark plan",
            },
            "http://test",
        )

        assert "handoff_label: benchmark28" in result
        assert "owner_agent: claude-code" in result
        assert "write_scope: handoff, task-packet" in result
        assert "phase: task_framing" in result
        assert "priority: high" in result
        assert "phase_objective:" in result
        assert "core_instinct_ids:" in result
        assert "project_context_summary:" in result
        assert "project_context_refs: laws=1, components=1, improvements=1, tasks=1, docs_sections=1" in result

    async def test_registry_best_lists_all_ranked_components(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            return {
                "task_type": "code_review",
                "ranked": [
                    {"component": "cloud-llm", "score": 0.91},
                    {"component": "qwen3:1.7b", "score": 0.44},
                ],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "registry_best",
            {"task_type": "code_review", "top": 2},
            "http://test",
        )

        assert "1. cloud-llm" in result
        assert "2. qwen3:1.7b" in result

    async def test_list_project_laws_tool_uses_laws_endpoint(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {
                "items": [
                    {
                        "id": "law-1",
                        "title": "Require explicit approval",
                        "status": "active",
                        "scope": "project",
                        "project": "alpha",
                        "is_project_local": True,
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_project_laws",
            {"project": "alpha", "status": "active", "limit": 10},
            "http://test",
        )

        assert seen["path"] == "/laws?status=active&limit=10&include_promoted=true&project=alpha"
        assert "Require explicit approval" in result

    async def test_get_project_law_tool_formats_law(self, monkeypatch):
        async def fake_get(api_base: str, path: str):
            assert path == "/laws/law-1"
            return {
                "id": "law-1",
                "title": "Require explicit approval",
                "status": "active",
                "scope": "project",
                "project": "alpha",
                "version": "1.0",
                "statement": "Do not deploy without approval.",
                "rationale": "Deploys are high-risk.",
                "evidence": ["incident-1"],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_project_law",
            {"law_id": "law-1"},
            "http://test",
        )

        assert "Require explicit approval" in result
        assert "incident-1" in result

    async def test_sse_list_learning_candidates_uses_pending_review_endpoint(self, monkeypatch):
        seen: dict[str, str] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {
                "artifacts": [
                    {
                        "id": "cand-1",
                        "content": "No skill guidance found for 'ssl termination'.",
                        "evidence_count": 3,
                        "confidence": 0.8,
                        "risk_level": "low",
                        "meta": {"signal_type": "skill_gap"},
                    }
                ]
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "list_learning_candidates",
            {"limit": 5},
            "http://test",
        )

        assert seen["path"] == "/learning/artifacts?scope=candidate&status=pending_review&limit=5"
        assert "cand-1" in result
        assert "ssl termination" in result

    async def test_sse_approve_learning_candidate_uses_review_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "id": "cand-1",
                "status": "active",
                "artifact_scope": "runtime_hint",
                "meta": {
                    "approved_by": "user",
                    "approval_source": "dashboard_review",
                    "approval_reason": "Looks good.",
                },
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "approve_learning_candidate",
            {"artifact_id": "cand-1", "approved_by": "user", "approval_source": "dashboard_review", "reason": "Looks good."},
            "http://test",
        )

        assert seen["path"] == "/learning/candidates/cand-1/approve"
        assert seen["payload"] == {
            "approved_by": "user",
            "approval_source": "dashboard_review",
            "reason": "Looks good.",
        }
        assert "approved learning candidate cand-1" in result
        assert "approval_source=dashboard_review" in result

    async def test_sse_get_project_readiness_uses_project_readiness_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "project_id": "alpha",
                "readiness_level": "limited_pilot_ready",
                "readiness_score": 72,
                "coverage": {
                    "components": 1,
                    "laws": 0,
                    "improvements": 1,
                    "runtime_hints": 0,
                    "memoirs": 0,
                    "tasks": 1,
                    "docs_sections": 1,
                },
                "blocking_gaps": [],
                "recommended_actions": ["Optionally import or define initial project laws for stronger governance and context."],
                "strengths": ["Component knowledge is indexed."],
                "code_inspection_recommended": False,
                "summary": "Project 'alpha' is limited_pilot_ready (72/100).",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_project_readiness",
            {"project_id": "alpha"},
            "http://test",
        )

        assert seen["path"] == "/project/readiness"
        assert seen["payload"] == {"project_id": "alpha"}
        assert "Project readiness for alpha: limited_pilot_ready (72/100)" in result
        assert "Coverage: components=1" in result

    async def test_sse_get_project_bootstrap_checklist_uses_bootstrap_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "project_id": "alpha",
                "readiness_level": "bootstrap_needed",
                "bootstrap_ready": False,
                "next_step": "components_indexed",
                "summary": "Bootstrap checklist for 'alpha': bootstrap_needed. Next step: components_indexed.",
                "steps": [
                    {
                        "step_id": "components_indexed",
                        "title": "Index or refresh component knowledge",
                        "required": True,
                        "status": "pending",
                        "action": "Ingest the project or refresh indexed components.",
                        "tool_hint": "project/ingest or project/refresh",
                    }
                ],
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "get_project_bootstrap_checklist",
            {"project_id": "alpha"},
            "http://test",
        )

        assert seen["path"] == "/project/bootstrap-checklist"
        assert seen["payload"] == {"project_id": "alpha"}
        assert "Bootstrap checklist for alpha: bootstrap_needed" in result
        assert "components_indexed" in result

    async def test_sse_plan_remote_snapshot_uses_project_plan_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "project_id": "alpha",
                "storage_mode": "knowledge_only",
                "snapshot": {
                    "source_mode": "git_snapshot",
                    "repo": "https://github.com/example/alpha",
                    "branch": "main",
                    "commit_sha": "abc123",
                    "base_commit_sha": "abc123",
                    "dirty_workspace": False,
                },
                "counts": {
                    "changed_files": 1,
                    "deleted_files": 0,
                    "renamed_files": 0,
                    "files_with_content": 0,
                },
                "plan": {
                    "rebuild_mode": "skip_if_unchanged",
                    "projection_target_state": "effective",
                    "requires_selective_source_payload": True,
                    "can_skip_when_unchanged": True,
                    "touched_paths": ["app/context.py"],
                },
                "contract": {
                    "stores_selective_source_cache": False,
                    "full_mirror_enabled": False,
                },
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "plan_remote_snapshot",
            {
                "project_id": "alpha",
                "storage_mode": "knowledge_only",
                "snapshot": {"source_mode": "git_snapshot", "commit_sha": "abc123"},
                "changed_files": ["app/context.py"],
            },
            "http://test",
        )

        assert seen["path"] == "/project/remote-snapshot/plan"
        assert seen["payload"]["project_id"] == "alpha"
        assert "Remote snapshot plan for alpha: rebuild_mode=skip_if_unchanged" in result
        assert "Touched paths: app/context.py" in result

    async def test_sse_sync_remote_snapshot_uses_project_sync_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "project_id": "alpha",
                "action": "needs_source_payload",
                "plan": {
                    "plan": {
                        "rebuild_mode": "diff_only",
                        "projection_target_state": "effective",
                        "requires_selective_source_payload": True,
                    }
                },
                "refresh": {
                    "updated": [],
                    "up_to_date": [],
                    "requires_source_payload": ["context"],
                    "used_remote_file_payload": False,
                },
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "sync_remote_snapshot",
            {
                "project_id": "alpha",
                "storage_mode": "knowledge_only",
                "snapshot": {"source_mode": "git_snapshot", "commit_sha": "def456"},
                "changed_files": ["app/context.py"],
            },
            "http://test",
        )

        assert seen["path"] == "/project/remote-snapshot/sync"
        assert seen["payload"]["project_id"] == "alpha"
        assert "Remote snapshot sync for alpha: action=needs_source_payload" in result
        assert "Components needing source payload: context" in result

    async def test_sse_get_storage_trust_status_uses_admin_storage_trust(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_get(api_base: str, path: str):
            seen["path"] = path
            return {
                "status": "degraded",
                "summary": "Storage trust is degraded: at least one integrity slice is unhealthy and operator action is required.",
                "signals": {
                    "degraded_slices": ["qdrant.skill_domain_tags_filter"],
                    "active_hygiene_findings": 948,
                    "manual_review_pending": {"synthetic_test": 23},
                    "quarantine_candidates": {},
                    "delete_ready": {},
                },
                "next_actions": ["Investigate degraded integrity slices before trusting affected storage filters and retrieval paths."],
            }

        monkeypatch.setattr(mcp_sse, "_get", fake_get)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool("get_storage_trust_status", {}, "http://test")

        assert seen["path"] == "/admin/storage-trust"
        assert "Storage trust: degraded" in result
        assert "qdrant.skill_domain_tags_filter" in result

    async def test_sse_send_coordination_message_uses_models_coordination_endpoint(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_post(api_base: str, path: str, payload: dict):
            seen["path"] = path
            seen["payload"] = payload
            return {
                "memory_id": "msg-1",
                "project": "alpha",
                "thread_id": "thread-1",
                "from_agent": "codex",
                "to_agent": "claude-code",
                "message_type": "question",
                "content": "Need file ownership clarification.",
                "status": "new",
                "priority": "normal",
                "requested_action": "",
                "response_to_message_id": "",
                "source": "mcp_coordination",
                "tags": ["project:alpha"],
                "timestamp": "2026-03-22T12:00:00Z",
            }

        monkeypatch.setattr(mcp_sse, "_post", fake_post)
        monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())

        result = await mcp_sse._execute_tool(
            "send_coordination_message",
            {"project": "alpha", "from_agent": "codex", "to_agent": "claude-code", "content": "Need file ownership clarification."},
            "http://test",
        )

        assert seen["path"] == "/models/coordination/messages"
        assert seen["payload"]["project"] == "alpha"
        assert "msg-1" in result
        assert "codex" in result


class TestAsyncioNoiseFilter:
    def test_suppresses_winerror_10054_connection_lost_noise(self):
        exc = ConnectionResetError(10054, "remote host closed connection")
        exc.winerror = 10054
        context = {
            "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
            "exception": exc,
            "handle": _FakeHandle(),
        }
        assert _should_suppress_asyncio_transport_error(context) is True

    def test_keeps_other_asyncio_errors_visible(self):
        context = {
            "message": "Exception in callback some_other_handler",
            "exception": RuntimeError("boom"),
        }
        assert _should_suppress_asyncio_transport_error(context) is False
