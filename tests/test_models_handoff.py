from app.dependencies import get_qdrant
from app.routers import models as models_router
from app.services.data_integrity_service import HANDOFF_STATUS_FILTER_SLICE_ID, get_data_integrity_store
from app.services.memory_store import get_memory_store


class _FakeRegistry:
    def __init__(self):
        self._models = {"claude-sonnet": {}, "gpt-4o": {}, "glm-4": {}}
        self.reported_limits: list[str] = []
        self.handoffs: list[dict] = []

    def log_handoff(self, **kwargs):
        self.handoffs.append(kwargs)

    def handoff_log(self, limit: int = 20, handoff_label: str | None = None):
        items = list(self.handoffs)
        if handoff_label:
            items = [item for item in items if item.get("handoff_label") == handoff_label]
        items = items[:limit]
        return [
            {
                "id": idx + 1,
                "ts": float(idx),
                "task_id": item.get("task_id", ""),
                "handoff_label": item.get("handoff_label"),
                "from_agent": item.get("from_agent"),
                "to_agent": item.get("to_agent"),
                "memory_id": item.get("memory_id"),
                "reason": item.get("reason"),
            }
            for idx, item in enumerate(items)
        ]

    def report_limit_hit(self, model_id: str):
        self.reported_limits.append(model_id)

    def rank_for_task(self, task_type: str):
        return [("claude-sonnet", 0.95), ("gpt-4o", 0.83), ("glm-4", 0.71)]


async def test_handoff_limit_hit_uses_from_model_id(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    response = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "from_model_id": "claude-sonnet",
            "task_description": "Continue investigating the MCP regression",
            "reason": "limit_hit",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["from_model_id"] == "claude-sonnet"
    assert fake_registry.reported_limits == ["claude-sonnet"]
    assert all(item["model_id"] != "claude-sonnet" for item in body["next_available"])


async def test_handoff_can_use_human_readable_label_and_pickup_by_it(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "benchmark28",
            "project_id": "supermemory",
            "owner_agent": "claude-code",
            "write_scope": ["mcp/server.py", "app/routers/mcp_sse.py"],
            "phase": "task_framing",
            "priority": "high",
            "definition_of_done": "Produce a planning packet",
            "expected_output_shape": "Short ranked benchmark plan",
            "phase_objective": "Turn an incomplete request into an explicit task",
            "core_instinct_ids": ["assume_initial_task_statement_is_incomplete", "clarify_scope_assumptions_and_done"],
            "supporting_instinct_ids": ["track_assumptions_explicitly"],
            "project_context_summary": "coverage laws=1, components=1; highlights laws: Require review",
            "project_context_refs": {"laws": ["law-1"], "components": ["handoff"]},
            "project_context_snapshot": "## Relevant Components\n\n### Handoff\n**Purpose:** Carry portable task context.",
            "task_description": "Benchmark competitors for roadmap positioning",
            "reason": "manual",
        },
    )

    assert create_resp.status_code == 200, create_resp.text
    create_body = create_resp.json()
    assert create_body["handoff_label"] == "benchmark28"
    assert create_body["project_id"] == "supermemory"
    assert create_body["owner_agent"] == "claude-code"
    assert create_body["write_scope"] == ["mcp/server.py", "app/routers/mcp_sse.py"]
    assert create_body["phase"] == "task_framing"
    assert create_body["priority"] == "high"
    assert create_body["definition_of_done"] == "Produce a planning packet"
    assert create_body["expected_output_shape"] == "Short ranked benchmark plan"
    assert create_body["phase_objective"] == "Turn an incomplete request into an explicit task"
    assert create_body["core_instinct_ids"] == ["assume_initial_task_statement_is_incomplete", "clarify_scope_assumptions_and_done"]
    assert "benchmark28" in create_body["pickup_instruction"]
    assert fake_registry.handoffs[0]["handoff_label"] == "benchmark28"

    pickup_resp = await client.post(
        "/api/v1/models/handoff/pickup",
        json={"agent_id": "claude-code", "handoff_label": "benchmark28", "limit": 5},
    )
    assert pickup_resp.status_code == 200, pickup_resp.text
    pickup_body = pickup_resp.json()
    assert pickup_body["handoff_label"] == "benchmark28"
    assert pickup_body["found"] == 1
    assert pickup_body["handoffs"][0]["handoff_label"] == "benchmark28"
    assert pickup_body["handoffs"][0]["status"] == "pending"
    assert pickup_body["handoffs"][0]["project_id"] == "supermemory"
    assert pickup_body["handoffs"][0]["owner_agent"] == "claude-code"
    assert pickup_body["handoffs"][0]["write_scope"] == ["mcp/server.py", "app/routers/mcp_sse.py"]
    assert pickup_body["handoffs"][0]["phase"] == "task_framing"
    assert pickup_body["handoffs"][0]["priority"] == "high"
    assert pickup_body["handoffs"][0]["definition_of_done"] == "Produce a planning packet"
    assert pickup_body["handoffs"][0]["expected_output_shape"] == "Short ranked benchmark plan"
    assert pickup_body["handoffs"][0]["phase_objective"] == "Turn an incomplete request into an explicit task"
    assert pickup_body["handoffs"][0]["core_instinct_ids"] == [
        "assume_initial_task_statement_is_incomplete",
        "clarify_scope_assumptions_and_done",
    ]
    assert pickup_body["handoffs"][0]["supporting_instinct_ids"] == ["track_assumptions_explicitly"]
    assert pickup_body["handoffs"][0]["project_context_summary"].startswith("coverage laws=1")
    assert pickup_body["handoffs"][0]["project_context_refs"] == {"laws": ["law-1"], "components": ["handoff"]}
    assert pickup_body["handoffs"][0]["project_context_snapshot"].startswith("## Relevant Components")
    assert pickup_body["handoffs"][0]["task_capture_candidate_count"] == 0
    assert pickup_body["handoffs"][0]["task_statement_incomplete"] is False
    assert "project_id: supermemory" in pickup_body["handoffs"][0]["content"]
    assert "owner_agent: claude-code" in pickup_body["handoffs"][0]["content"]
    assert "write_scope: mcp/server.py, app/routers/mcp_sse.py" in pickup_body["handoffs"][0]["content"]
    assert "phase: task_framing" in pickup_body["handoffs"][0]["content"]
    assert "definition_of_done: Produce a planning packet" in pickup_body["handoffs"][0]["content"]
    assert "core_instinct_ids: assume_initial_task_statement_is_incomplete, clarify_scope_assumptions_and_done" in pickup_body["handoffs"][0]["content"]


async def test_handoff_payload_keeps_full_packet_in_sqlite_and_link_in_qdrant(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "project_id": "supermemory",
            "owner_agent": "codex",
            "write_scope": ["app/routers/models.py"],
            "phase": "task_framing",
            "task_description": "Validate durable handoff metadata path",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    memory_id = create_resp.json()["memory_id"]

    qdrant = get_qdrant()
    results = await qdrant._client.retrieve(
        collection_name=qdrant._collection,
        ids=[memory_id],
        with_payload=True,
        with_vectors=False,
    )
    assert results
    payload = dict(results[0].payload or {})
    assert payload.get("category") == "handoff"
    assert str(payload.get("content") or "").startswith("handoff_ref:")
    assert payload.get("meta") == {}

    row = await get_memory_store().get(memory_id)
    assert row is not None
    assert "HANDOFF CONTEXT" in str(row.get("content") or "")
    metadata = dict(row.get("metadata") or {})
    assert metadata.get("category") == "handoff"
    assert (metadata.get("meta") or {}).get("project_id") == "supermemory"
    assert (metadata.get("meta") or {}).get("task_description") == "Validate durable handoff metadata path"


async def test_handoff_list_falls_back_to_sqlite_when_qdrant_scroll_fails(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "fallbacktest",
            "task_description": "Validate SQLite fallback for handoff list",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text

    qdrant = get_qdrant()

    async def fail_scroll(*args, **kwargs):
        raise RuntimeError("simulated scroll failure for handoff filters")

    monkeypatch.setattr(qdrant._client, "scroll", fail_scroll)

    list_resp = await client.post(
        "/api/v1/models/handoff/list",
        json={
            "agent_id": "claude-code",
            "statuses": ["pending"],
            "limit": 10,
        },
    )
    assert list_resp.status_code == 200, list_resp.text
    data = list_resp.json()
    assert data["found"] >= 1
    assert data["handoffs"][0]["to_agent"] == "claude-code"
    assert data["handoffs"][0]["status"] == "pending"

    overview = get_data_integrity_store().overview()
    assert HANDOFF_STATUS_FILTER_SLICE_ID in overview["degraded_slices"]


async def test_pending_handoff_labels_fall_back_to_sqlite_when_qdrant_scroll_fails(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "fallbacklabels",
            "task_description": "Validate pending labels fallback",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text

    qdrant = get_qdrant()

    async def fail_scroll(*args, **kwargs):
        raise RuntimeError("simulated scroll failure for pending labels")

    monkeypatch.setattr(qdrant._client, "scroll", fail_scroll)

    labels = await qdrant.list_pending_handoff_labels(to_agent="claude-code", limit=10, scan_limit=50)
    assert any(item["handoff_label"] == "fallbacklabels" for item in labels)

    overview = get_data_integrity_store().overview()
    assert HANDOFF_STATUS_FILTER_SLICE_ID in overview["degraded_slices"]


async def test_background_handoffs_fall_back_to_sqlite_when_qdrant_scroll_fails(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    class _Decision:
        task_type = "text_summarization"
        component = "qwen3:1.7b"
        score = 0.77
        tier = "local"
        reasoning = "Local tier is sufficient."
        confidence = 0.8
        cloud_fallbacks = []

    async def fake_decide_task_route(*, task: str, preferred_tier: str | None = None):
        return _Decision()

    class _FakeQueue:
        async def submit(self, job_type: str, payload: dict) -> str:
            return "job-fallback-bg-1"

    monkeypatch.setattr(models_router, "decide_task_route", fake_decide_task_route)
    monkeypatch.setattr("app.services.job_queue.get_job_queue", lambda: _FakeQueue())

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "fallbackbg",
            "task_description": "Validate background fallback",
            "execution_mode": "strict_economy",
            "background_job_type": "docs_rebuild",
            "background_payload": {"project": "supermemory", "force": False},
            "suggested_execution_tier": "local",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    memory_id = create_resp.json()["memory_id"]

    qdrant = get_qdrant()
    dispatch_resp = await client.post(
        "/api/v1/models/handoff/dispatch_background",
        json={"memory_id": memory_id, "acted_by": "codex"},
    )
    assert dispatch_resp.status_code == 200, dispatch_resp.text

    async def fail_scroll(*args, **kwargs):
        raise RuntimeError("simulated scroll failure for background handoffs")

    monkeypatch.setattr(qdrant._client, "scroll", fail_scroll)

    items = await qdrant.list_background_handoffs(limit=10, statuses=["active"])
    assert any(item["memory_id"] == memory_id for item in items)

    overview = get_data_integrity_store().overview()
    assert HANDOFF_STATUS_FILTER_SLICE_ID in overview["degraded_slices"]


async def test_handoff_label_is_normalized_to_lowercase(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    response = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "BenchMark28",
            "task_description": "Normalize label",
            "reason": "manual",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["handoff_label"] == "benchmark28"


async def test_handoff_log_can_filter_by_human_readable_label(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "benchmark28",
            "task_description": "Benchmark competitors",
            "reason": "manual",
        },
    )
    await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "tailcutoff371",
            "task_description": "Investigate tail cutoff",
            "reason": "manual",
        },
    )

    log_resp = await client.get("/api/v1/models/handoff_log?handoff_label=benchmark28&limit=10")
    assert log_resp.status_code == 200, log_resp.text
    data = log_resp.json()
    assert len(data) == 1
    assert data[0]["handoff_label"] == "benchmark28"


async def test_pending_handoff_labels_lists_human_readable_queue(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "benchmark28",
            "task_description": "Benchmark competitors",
            "reason": "manual",
        },
    )
    await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "tailcutoff371",
            "task_description": "Investigate tail cutoff",
            "reason": "manual",
        },
    )

    labels_resp = await client.get("/api/v1/models/handoff/pending_labels?agent_id=claude-code&limit=10")
    assert labels_resp.status_code == 200, labels_resp.text
    data = labels_resp.json()
    assert data["found"] >= 2
    labels = {item["handoff_label"] for item in data["labels"]}
    assert "benchmark28" in labels
    assert "tailcutoff371" in labels


async def test_expand_handoff_refs_returns_compact_resolution(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "project_id": "supermemory",
            "project_context_summary": "coverage laws=1, components=1",
            "project_context_refs": {"laws": ["law-1"], "components": ["handoff"]},
            "task_description": "Expand referenced handoff context",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    memory_id = create_resp.json()["memory_id"]

    async def fake_expand_handoff_refs_for_record(**kwargs):
        assert kwargs["ref_types"] == ["laws", "components"]
        assert kwargs["limit_per_type"] == 2
        return {
            "memory_id": memory_id,
            "project_id": "supermemory",
            "available_ref_types": ["laws", "components"],
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
            "unresolved": {},
        }

    monkeypatch.setattr(models_router, "_expand_handoff_refs_for_record", fake_expand_handoff_refs_for_record)

    expand_resp = await client.post(
        "/api/v1/models/handoff/expand_refs",
        json={
            "memory_id": memory_id,
            "ref_types": ["laws", "components"],
            "limit_per_type": 2,
        },
    )
    assert expand_resp.status_code == 200, expand_resp.text
    data = expand_resp.json()
    assert data["project_id"] == "supermemory"
    assert data["requested_ref_types"] == ["laws", "components"]
    assert data["resolved"]["laws"][0]["title"] == "Require review"
    assert data["resolved"]["components"][0]["component_id"] == "handoff"


async def test_refresh_handoff_context_rebuilds_summary_and_refs(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "project_id": "supermemory",
            "task_description": "Original handoff task",
            "project_context_summary": "old summary",
            "project_context_refs": {"laws": ["old-law"]},
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    memory_id = create_resp.json()["memory_id"]

    class _Bundle:
        laws = [{"id": "law-1", "title": "Require review"}]
        components = [{"component_id": "handoff", "name": "Handoff"}]
        improvements = [{"id": "imp-1", "title": "Tighten packet"}]
        runtime_hints = []
        tasks = [{"task_id": "task-1", "title": "Refine handoff"}]
        task_triage = {"recommended_task_id": "task-1", "items": [{"task_id": "task-1", "title": "Refine handoff"}]}
        task_capture_candidates = [{"artifact_id": "capture-1", "task_id": "task-1", "kind": "assumption"}]
        docs_sections = [{"section_key": "overview"}]
        code_inspection_recommended = False
        coverage = {"laws": 1, "components": 1, "improvements": 1, "runtime_hints": 0, "tasks": 1, "docs_sections": 1}

    async def fake_assemble_project_context(**kwargs):
        assert kwargs["project_id"] == "supermemory"
        assert kwargs["task"] == "Refresh this task packet"
        assert kwargs["context_profile"] == "handoff_compact"
        return _Bundle()

    monkeypatch.setattr(models_router, "assemble_project_context", fake_assemble_project_context)

    refresh_resp = await client.post(
        "/api/v1/models/handoff/refresh_context",
        json={
            "memory_id": memory_id,
            "task_description": "Refresh this task packet",
            "max_components": 2,
        },
    )
    assert refresh_resp.status_code == 200, refresh_resp.text
    data = refresh_resp.json()
    assert data["project_id"] == "supermemory"
    assert data["task_description"] == "Refresh this task packet"
    assert data["project_context_summary"].startswith("coverage laws=1, components=1, improvements=1, tasks=1, task_capture_candidates=1, docs_sections=1")
    assert "next_task: task-1" in data["project_context_summary"]
    assert data["project_context_refs"] == {
        "laws": ["law-1"],
        "components": ["handoff"],
        "improvements": ["imp-1"],
        "tasks": ["task-1"],
        "task_capture_candidates": ["capture-1"],
        "docs_sections": ["overview"],
    }


async def test_update_handoff_status_moves_packet_lifecycle(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "task_description": "Advance packet lifecycle",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    memory_id = create_resp.json()["memory_id"]
    assert create_resp.json()["status"] == "pending"

    status_resp = await client.post(
        "/api/v1/models/handoff/status",
        json={
            "memory_id": memory_id,
            "status": "active",
            "acted_by": "codex",
            "reason": "picked up for implementation",
            "owner_agent": "feynman",
            "write_scope": ["mcp/server.py"],
            "executor_used": "cheap_subagent",
            "model_used": "gpt-5.4-mini",
            "result_summary": "Added the bounded MCP-facing change.",
            "verification_summary": "Focused tests passed.",
        },
    )
    assert status_resp.status_code == 200, status_resp.text
    data = status_resp.json()
    assert data["memory_id"] == memory_id
    assert data["status"] == "active"
    assert data["acted_by"] == "codex"
    assert data["reason"] == "picked up for implementation"
    assert data["owner_agent"] == "feynman"
    assert data["write_scope"] == ["mcp/server.py"]
    assert data["executor_used"] == "cheap_subagent"
    assert data["model_used"] == "gpt-5.4-mini"
    assert data["result_summary"] == "Added the bounded MCP-facing change."
    assert data["verification_summary"] == "Focused tests passed."

    list_resp = await client.post(
        "/api/v1/models/handoff/list",
        json={
            "agent_id": "claude-code",
            "statuses": ["active"],
            "compact": True,
        },
    )
    assert list_resp.status_code == 200, list_resp.text
    listed = list_resp.json()["handoffs"]
    assert len(listed) == 1
    assert listed[0]["executor_used"] == "cheap_subagent"
    assert listed[0]["model_used"] == "gpt-5.4-mini"


async def test_list_handoffs_filters_by_lifecycle_status(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "resume42",
            "task_description": "Lifecycle listing",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    memory_id = create_resp.json()["memory_id"]

    status_resp = await client.post(
        "/api/v1/models/handoff/status",
        json={
            "memory_id": memory_id,
            "status": "paused",
            "acted_by": "codex",
            "reason": "switching tasks",
        },
    )
    assert status_resp.status_code == 200, status_resp.text

    list_resp = await client.post(
        "/api/v1/models/handoff/list",
        json={
            "agent_id": "claude-code",
            "statuses": ["paused"],
            "limit": 10,
        },
    )
    assert list_resp.status_code == 200, list_resp.text
    data = list_resp.json()
    assert data["statuses"] == ["paused"]
    assert data["compact"] is True
    assert data["found"] >= 1
    assert any(item["memory_id"] == memory_id and item["status"] == "paused" for item in data["handoffs"])
    row = next(item for item in data["handoffs"] if item["memory_id"] == memory_id)
    assert "content" not in row
    assert "project_context_refs" not in row
    assert "content_preview" in row
    assert row["task_capture_candidate_count"] == 0
    assert row["task_statement_incomplete"] is False
    assert row["owner_agent"] == "claude-code"
    assert row["write_scope"] == []


async def test_pickup_and_workspace_summary_surface_incomplete_task_statement_signal(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "capture42",
            "task_description": "Continue a partially framed task",
            "project_id": "supermemory",
            "project_context_summary": "coverage tasks=1, task_capture_candidates=2",
            "project_context_refs": {
                "tasks": ["task-1"],
                "task_capture_candidates": ["capture-1", "capture-2"],
            },
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text

    pickup_resp = await client.post(
        "/api/v1/models/handoff/pickup",
        json={"agent_id": "claude-code", "handoff_label": "capture42", "limit": 5},
    )
    assert pickup_resp.status_code == 200, pickup_resp.text
    pickup_body = pickup_resp.json()
    assert pickup_body["handoffs"][0]["task_capture_candidate_count"] == 2
    assert pickup_body["handoffs"][0]["task_statement_incomplete"] is True

    summary_resp = await client.post(
        "/api/v1/models/handoff/workspace_summary",
        json={"agent_id": "claude-code", "packet_limit": 5},
    )
    assert summary_resp.status_code == 200, summary_resp.text
    data = summary_resp.json()
    assert data["task_statement_incomplete_count"] >= 1
    assert data["by_task_capture_candidate_count"]["2"] >= 1
    assert any(
        packet["task_capture_candidate_count"] == 2 and packet["task_statement_incomplete"] is True
        for packet in data["recent_packets"]
    )


async def test_list_handoffs_can_filter_by_owner_and_write_scope(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    first_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "owner_agent": "feynman",
            "write_scope": ["mcp/server.py", "app/routers/mcp_sse.py"],
            "task_description": "Own MCP-facing packet work",
            "reason": "manual",
        },
    )
    second_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "owner_agent": "nash",
            "write_scope": ["app/routers/models.py"],
            "task_description": "Own backend packet work",
            "reason": "manual",
        },
    )
    assert first_resp.status_code == 200, first_resp.text
    assert second_resp.status_code == 200, second_resp.text
    first_id = first_resp.json()["memory_id"]

    list_resp = await client.post(
        "/api/v1/models/handoff/list",
        json={
            "agent_id": "claude-code",
            "owner_agent": "feynman",
            "write_scope": ["mcp/server.py"],
            "limit": 10,
        },
    )
    assert list_resp.status_code == 200, list_resp.text
    data = list_resp.json()
    assert data["found"] == 1
    row = data["handoffs"][0]
    assert row["memory_id"] == first_id
    assert row["owner_agent"] == "feynman"
    assert row["write_scope"] == ["mcp/server.py", "app/routers/mcp_sse.py"]


async def test_incomplete_task_statement_handoffs_are_prioritized_in_list_and_workspace_summary(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    complete_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "complete1",
            "task_description": "Complete framing packet",
            "project_id": "supermemory",
            "project_context_summary": "coverage tasks=1",
            "project_context_refs": {"tasks": ["task-complete"]},
            "reason": "manual",
        },
    )
    incomplete_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "incomplete1",
            "task_description": "Incomplete framing packet",
            "project_id": "supermemory",
            "project_context_summary": "coverage tasks=1, task_capture_candidates=2",
            "project_context_refs": {
                "tasks": ["task-incomplete"],
                "task_capture_candidates": ["capture-a", "capture-b"],
            },
            "reason": "manual",
        },
    )
    assert complete_resp.status_code == 200, complete_resp.text
    assert incomplete_resp.status_code == 200, incomplete_resp.text

    list_resp = await client.post(
        "/api/v1/models/handoff/list",
        json={"agent_id": "claude-code", "statuses": ["pending"], "limit": 10},
    )
    assert list_resp.status_code == 200, list_resp.text
    rows = list_resp.json()["handoffs"]
    assert rows[0]["handoff_label"] == "incomplete1"
    assert rows[0]["task_statement_incomplete"] is True
    assert rows[0]["task_capture_candidate_count"] == 2

    summary_resp = await client.post(
        "/api/v1/models/handoff/workspace_summary",
        json={"agent_id": "claude-code", "packet_limit": 5},
    )
    assert summary_resp.status_code == 200, summary_resp.text
    summary = summary_resp.json()
    assert summary["recent_packets"][0]["handoff_label"] == "incomplete1"
    assert summary["recent_packets"][0]["task_statement_incomplete"] is True


async def test_handoff_views_surface_project_task_recommendations(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    async def fake_build_task_triage(project_id: str, qdrant, *, limit: int = 5):
        assert project_id == "supermemory"
        return {
            "project_id": project_id,
            "found": 2,
            "recommended_task_id": "task-next",
            "items": [
                {
                    "task_id": "task-next",
                    "title": "Finish capture review",
                    "status": "active",
                    "task_statement_incomplete": True,
                    "task_capture_pending_count": 2,
                    "task_capture_promoted_count": 0,
                    "latest_change_type": "note",
                    "latest_change_summary": "Needs framing cleanup",
                    "triage_reasons": ["incomplete_framing:2", "active_task"],
                    "updated_at": "2026-04-10T10:00:00+00:00",
                },
                {
                    "task_id": "task-later",
                    "title": "Later task",
                    "status": "planning",
                    "task_statement_incomplete": False,
                    "task_capture_pending_count": 0,
                    "task_capture_promoted_count": 1,
                    "latest_change_type": "",
                    "latest_change_summary": "",
                    "triage_reasons": ["planning_task"],
                    "updated_at": "2026-04-09T10:00:00+00:00",
                },
            ],
        }

    monkeypatch.setattr(models_router, "build_task_triage", fake_build_task_triage)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "handoff_label": "triagehint",
            "task_description": "Continue project task navigation",
            "project_id": "supermemory",
            "project_context_summary": "coverage tasks=1, task_capture_candidates=2",
            "project_context_refs": {
                "tasks": ["task-next"],
                "task_capture_candidates": ["capture-a", "capture-b"],
            },
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text

    pickup_resp = await client.post(
        "/api/v1/models/handoff/pickup",
        json={"agent_id": "claude-code", "handoff_label": "triagehint", "limit": 5},
    )
    assert pickup_resp.status_code == 200, pickup_resp.text
    pickup_data = pickup_resp.json()
    assert pickup_data["project_task_recommendations"]["supermemory"]["recommended_task_id"] == "task-next"
    assert pickup_data["handoffs"][0]["project_recommended_task_id"] == "task-next"

    list_resp = await client.post(
        "/api/v1/models/handoff/list",
        json={"agent_id": "claude-code", "limit": 10},
    )
    assert list_resp.status_code == 200, list_resp.text
    list_data = list_resp.json()
    assert list_data["found"] >= 1
    assert list_data["project_task_recommendations"]["supermemory"]["recommended_task_id"] == "task-next"
    assert any(row["project_recommended_task_id"] == "task-next" for row in list_data["handoffs"])

    summary_resp = await client.post(
        "/api/v1/models/handoff/workspace_summary",
        json={"agent_id": "claude-code", "packet_limit": 5},
    )
    assert summary_resp.status_code == 200, summary_resp.text
    summary = summary_resp.json()
    assert summary["project_task_recommendations"]["supermemory"]["recommended_task_id"] == "task-next"
    assert any(packet["project_recommended_task_id"] == "task-next" for packet in summary["recent_packets"])
    assert any(packet["project_recommended_task_id"] == "task-next" for packet in summary["parallel_execution"]["running_packets"] + [p for wave in summary["parallel_execution"]["waves"] for p in wave["packets"]])


async def test_handoff_workspace_summary_groups_parallel_packets(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    first_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "owner_agent": "feynman",
            "write_scope": ["tests/test_mcp_stdio_handoff.py"],
            "phase": "pre_implementation",
            "task_description": "Add stdio regression",
            "reason": "manual",
        },
    )
    second_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "owner_agent": "nash",
            "write_scope": ["app/routers/models.py"],
            "phase": "task_framing",
            "task_description": "Review workspace summary API",
            "reason": "manual",
        },
    )
    assert first_resp.status_code == 200, first_resp.text
    assert second_resp.status_code == 200, second_resp.text

    await client.post(
        "/api/v1/models/handoff/status",
        json={
            "memory_id": first_resp.json()["memory_id"],
            "status": "active",
            "acted_by": "codex",
            "reason": "delegated",
            "owner_agent": "feynman",
            "write_scope": ["tests/test_mcp_stdio_handoff.py"],
            "executor_used": "cheap_subagent",
        },
    )
    await client.post(
        "/api/v1/models/handoff/status",
        json={
            "memory_id": second_resp.json()["memory_id"],
            "status": "paused",
            "acted_by": "codex",
            "reason": "waiting",
            "owner_agent": "nash",
            "write_scope": ["app/routers/models.py"],
        },
    )

    summary_resp = await client.post(
        "/api/v1/models/handoff/workspace_summary",
        json={
            "agent_id": "codex",
            "packet_limit": 5,
        },
    )
    assert summary_resp.status_code == 200, summary_resp.text
    data = summary_resp.json()
    assert data["total"] >= 2
    assert data["by_status"]["active"] >= 1
    assert data["by_status"]["paused"] >= 1
    assert data["by_owner_agent"]["feynman"] >= 1
    assert data["by_owner_agent"]["nash"] >= 1
    assert data["by_phase"]["pre_implementation"] >= 1
    assert data["by_phase"]["task_framing"] >= 1
    assert data["by_execution_mode"]["balanced"] >= 1
    assert data["by_executor_used"]["cheap_subagent"] >= 1
    assert "merge_back_guidance" in data
    assert data["merge_back_guidance"]["recommended_next_step"]
    assert len(data["merge_back_guidance"]["steps"]) >= 3
    assert "parallel_execution" in data
    assert data["parallel_execution"]["running_count"] >= 1
    assert data["parallel_execution"]["planned_packet_count"] >= 1
    assert len(data["parallel_execution"]["waves"]) >= 1
    assert data["parallel_execution"]["blocked_count"] == 0
    assert any(packet["owner_agent"] == "feynman" for packet in data["recent_packets"])
    assert any(packet["owner_agent"] == "nash" for packet in data["recent_packets"])


async def test_handoff_workspace_summary_blocks_packets_conflicting_with_active_scope(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    active_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "owner_agent": "owner-a",
            "write_scope": ["app/routers/models.py"],
            "task_description": "Active packet for models router",
            "reason": "manual",
        },
    )
    conflict_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "owner_agent": "owner-b",
            "write_scope": ["app/routers/models.py"],
            "task_description": "Conflicting packet should be blocked",
            "reason": "manual",
        },
    )
    safe_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "owner_agent": "owner-c",
            "write_scope": ["app/services/qdrant_service.py"],
            "task_description": "Independent packet should be planned",
            "reason": "manual",
        },
    )
    assert active_resp.status_code == 200, active_resp.text
    assert conflict_resp.status_code == 200, conflict_resp.text
    assert safe_resp.status_code == 200, safe_resp.text

    active_id = active_resp.json()["memory_id"]
    conflict_id = conflict_resp.json()["memory_id"]
    safe_id = safe_resp.json()["memory_id"]

    activate = await client.post(
        "/api/v1/models/handoff/status",
        json={
            "memory_id": active_id,
            "status": "active",
            "acted_by": "codex",
            "reason": "running now",
            "owner_agent": "owner-a",
            "write_scope": ["app/routers/models.py"],
        },
    )
    assert activate.status_code == 200, activate.text

    summary_resp = await client.post(
        "/api/v1/models/handoff/workspace_summary",
        json={"agent_id": "codex", "packet_limit": 10},
    )
    assert summary_resp.status_code == 200, summary_resp.text
    data = summary_resp.json()
    parallel = data["parallel_execution"]
    assert parallel["running_count"] >= 1
    assert parallel["blocked_count"] >= 1
    assert any(
        blocked["packet"]["memory_id"] == conflict_id
        and blocked["reason"] == "write_scope_conflict_with_active_packet"
        and active_id in blocked["conflicts_with"]
        for blocked in parallel["blocked_packets"]
    )
    assert any(
        packet["memory_id"] == safe_id
        for wave in parallel["waves"]
        for packet in wave["packets"]
    )


async def test_decompose_task_packet_recommends_bounded_packets(client):
    response = await client.post(
        "/api/v1/models/handoff/decompose",
        json={
            "project_id": "supermemory",
            "task_description": "Split the handoff packet work between backend and MCP surfaces",
            "handoff_label_prefix": "packet",
            "priority": "high",
            "owner_agent": "codex",
            "execution_mode": "strict_economy",
            "write_scope": [
                "app/routers/models.py",
                "app/routers/mcp_sse.py",
                "mcp/server.py",
            ],
            "max_packets": 3,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["project_id"] == "supermemory"
    assert data["strategy"] == "split_by_write_scope"
    assert data["recommended_packet_count"] == 3
    assert data["phase"] == "pre_implementation"
    assert data["execution_mode"] == "strict_economy"
    assert "pre_implementation" in data["available_phases"]
    assert len(data["packets"]) == 3
    assert all(packet["owner_agent"] == "codex" for packet in data["packets"])
    assert all(packet["execution_mode"] == "strict_economy" for packet in data["packets"])
    assert any(packet["write_scope"] == ["app/routers/models.py"] for packet in data["packets"])
    assert all(packet["suggested_execution_tier"] in {"local", "mini", "standard", "frontier"} for packet in data["packets"])
    assert all(packet["suggested_execution_tier"] in {"local", "mini", "standard"} for packet in data["packets"])
    assert all(packet["model_hint"] for packet in data["packets"])
    assert all(packet["definition_of_done"] for packet in data["packets"])
    assert all(packet["expected_output_shape"] for packet in data["packets"])
    assert len(data["split_guidance"]) >= 2


async def test_decompose_task_packet_groups_related_write_scopes_by_affinity(client):
    response = await client.post(
        "/api/v1/models/handoff/decompose",
        json={
            "task_description": "Split task packets for backend and MCP surfaces",
            "handoff_label_prefix": "packet",
            "write_scope": [
                "app/routers/models.py",
                "app/routers/mcp_sse.py",
                "mcp/server.py",
            ],
            "max_packets": 2,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["strategy"] == "grouped_write_scope"
    assert len(data["packets"]) == 2
    scopes = [packet["write_scope"] for packet in data["packets"]]
    assert ["app/routers/models.py"] in scopes
    assert ["app/routers/mcp_sse.py", "mcp/server.py"] in scopes
    tiers = {tuple(packet["write_scope"]): packet["suggested_execution_tier"] for packet in data["packets"]}
    assert tiers[("app/routers/models.py",)] in {"standard", "mini"}
    assert tiers[("app/routers/mcp_sse.py", "mcp/server.py")] == "mini"


async def test_create_task_packets_materializes_multiple_real_handoffs(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    response = await client.post(
        "/api/v1/models/handoff/create_packets",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "project_id": "supermemory",
            "task_description": "Split packet work between backend and MCP surfaces",
            "execution_mode": "strict_economy",
            "packets": [
                {
                    "handoff_label": "packet-models-py",
                    "owner_agent": "codex",
                    "write_scope": ["app/routers/models.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "suggested_execution_tier": "standard",
                    "model_hint": "Use a standard model tier for bounded implementation work that still needs moderate repository context or integration judgment.",
                    "definition_of_done": "Finish the backend packet and verify it.",
                    "expected_output_shape": "Short backend result summary.",
                },
                {
                    "handoff_label": "packet-mcp_sse-py",
                    "owner_agent": "nash",
                    "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                    "phase": "pre_implementation",
                    "priority": "high",
                    "execution_mode": "max_quality",
                    "suggested_execution_tier": "mini",
                    "model_hint": "A mini or cheap cloud model should be enough because this packet is narrow and the result is easy to verify.",
                    "definition_of_done": "Finish the MCP packet and verify it.",
                    "expected_output_shape": "Short MCP result summary.",
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["created_count"] == 2
    assert data["project_id"] == "supermemory"
    assert {item["handoff_label"] for item in data["packets"]} == {"packet-models-py", "packet-mcp_sse-py"}
    assert any(item["suggested_execution_tier"] == "mini" for item in data["packets"])
    assert any(item["owner_agent"] == "nash" for item in data["packets"])
    assert any(item["execution_mode"] == "strict_economy" for item in data["packets"])
    assert any(item["execution_mode"] == "max_quality" for item in data["packets"])
    assert fake_registry.handoffs[0]["handoff_label"] == "packet-models-py"
    assert fake_registry.handoffs[1]["handoff_label"] == "packet-mcp_sse-py"

    listed = await client.post(
        "/api/v1/models/handoff/list",
        json={"agent_id": "codex", "statuses": ["pending"], "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()["handoffs"]
    assert any(item["handoff_label"] == "packet-models-py" for item in rows)
    assert any(item["handoff_label"] == "packet-mcp_sse-py" for item in rows)
    assert any(item["suggested_execution_tier"] == "mini" for item in rows)
    assert any(item["execution_mode"] == "strict_economy" for item in rows)
    assert any(item["execution_mode"] == "max_quality" for item in rows)


async def test_route_task_packet_execution_prefers_cheap_subagent_for_bounded_code_packet(client, monkeypatch):
    class _Decision:
        task_type = "code_generation"
        component = "gemini-2.5-flash"
        score = 0.82
        tier = "cloud"
        reasoning = "Cheap cloud tier is sufficient."
        confidence = 0.9
        cloud_fallbacks = [{"model_id": "glm-4.7", "score": 0.78}]

    async def fake_decide_task_route(*, task: str, preferred_tier: str | None = None):
        assert "bounded mcp packet" in task.lower()
        assert preferred_tier == "cloud"
        return _Decision()

    monkeypatch.setattr(models_router, "decide_task_route", fake_decide_task_route)

    response = await client.post(
        "/api/v1/models/handoff/route_execution",
        json={
            "packet": {
                "task_description": "Bounded MCP packet for parity and focused tests",
                "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
                "phase": "pre_implementation",
                "execution_mode": "economy",
                "suggested_execution_tier": "mini",
                "model_hint": "Use a mini model.",
                "definition_of_done": "Finish the bounded MCP packet and verify it.",
                "expected_output_shape": "Short result summary and verification summary.",
            }
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["recommended_executor"] == "cheap_subagent"
    assert data["recommended_model"] == "gemini-2.5-flash"
    assert data["routing_basis"]["tier"] == "cloud"
    assert data["packet"]["execution_mode"] == "economy"
    assert data["packet_profile"]["execution_mode"] == "economy"
    assert data["packet_profile"]["bounded_code"] is True
    assert any(item["executor"] == "cheap_subagent" and item["supported"] for item in data["eligible_executors"])


async def test_route_task_packet_execution_allows_proposal_only_write_bearing_packet(client, monkeypatch):
    class _Decision:
        task_type = "code_generation"
        component = "glm-4.7"
        score = 0.82
        tier = "cloud"
        reasoning = "Cheap cloud tier is sufficient."
        confidence = 0.9
        cloud_fallbacks = [{"model_id": "gemini-3.1-flash", "score": 0.78}]

    async def fake_decide_task_route(*, task: str, preferred_tier: str | None = None):
        assert "patch proposal" in task.lower()
        assert preferred_tier == "cloud"
        return _Decision()

    monkeypatch.setattr(models_router, "decide_task_route", fake_decide_task_route)

    response = await client.post(
        "/api/v1/models/handoff/route_execution",
        json={
            "packet": {
                "task_description": "Prepare a bounded patch proposal for packet routing metadata",
                "write_scope": ["app/routers/models.py", "app/services/handoff_packet_executor.py"],
                "phase": "pre_implementation",
                "execution_mode": "economy",
                "suggested_execution_tier": "mini",
                "definition_of_done": "Produce a bounded implementation plan and proposed patch outline.",
                "expected_output_shape": "Structured patch proposal and verification note.",
            }
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["packet_profile"]["proposal_only"] is True
    assert data["recommended_executor"] == "cheap_subagent"
    assert data["recommended_model"] == "glm-4.7"
    assert any(item["executor"] == "cheap_subagent" and item["supported"] for item in data["eligible_executors"])


async def test_route_task_packet_execution_supports_memory_id_lookup(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    class _Decision:
        task_type = "text_summarization"
        component = "qwen3:1.7b"
        score = 0.77
        tier = "local"
        reasoning = "Local tier is sufficient."
        confidence = 0.8
        cloud_fallbacks = []

    async def fake_decide_task_route(*, task: str, preferred_tier: str | None = None):
        assert "summarize operator report" in task.lower()
        assert preferred_tier == "local"
        return _Decision()

    monkeypatch.setattr(models_router, "decide_task_route", fake_decide_task_route)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "task_description": "Summarize operator report",
            "execution_mode": "strict_economy",
            "suggested_execution_tier": "local",
            "model_hint": "Prefer local background SLM.",
            "definition_of_done": "Return a concise report summary.",
            "expected_output_shape": "Short bullet summary.",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text

    route_resp = await client.post(
        "/api/v1/models/handoff/route_execution",
        json={"memory_id": create_resp.json()["memory_id"]},
    )
    assert route_resp.status_code == 200, route_resp.text
    data = route_resp.json()
    assert data["memory_id"] == create_resp.json()["memory_id"]
    assert data["recommended_executor"] == "local_slm_background"
    assert data["recommended_model"] == "qwen3:1.7b"
    assert data["packet"]["execution_mode"] == "strict_economy"
    assert data["packet_profile"]["execution_mode"] == "strict_economy"
    assert any(item["executor"] == "local_slm_background" and item["supported"] for item in data["eligible_executors"])


async def test_dispatch_background_task_packet_submits_supported_job(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    class _Decision:
        task_type = "text_summarization"
        component = "qwen3:1.7b"
        score = 0.77
        tier = "local"
        reasoning = "Local tier is sufficient."
        confidence = 0.8
        cloud_fallbacks = []

    async def fake_decide_task_route(*, task: str, preferred_tier: str | None = None):
        assert "rebuild docs projection" in task.lower()
        assert preferred_tier == "local"
        return _Decision()

    class _FakeQueue:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def submit(self, job_type: str, payload: dict) -> str:
            self.calls.append((job_type, payload))
            return "job-docs-1"

    fake_queue = _FakeQueue()

    monkeypatch.setattr(models_router, "decide_task_route", fake_decide_task_route)
    monkeypatch.setattr("app.services.job_queue.get_job_queue", lambda: fake_queue)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "task_description": "Rebuild docs projection",
            "execution_mode": "strict_economy",
            "background_job_type": "docs_rebuild",
            "background_payload": {"project": "supermemory", "force": False},
            "suggested_execution_tier": "local",
            "model_hint": "Prefer local background execution.",
            "definition_of_done": "Queue docs rebuild and return job id.",
            "expected_output_shape": "Job id and poll URL.",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text

    dispatch_resp = await client.post(
        "/api/v1/models/handoff/dispatch_background",
        json={"memory_id": create_resp.json()["memory_id"], "acted_by": "codex"},
    )
    assert dispatch_resp.status_code == 200, dispatch_resp.text
    data = dispatch_resp.json()
    assert data["status"] == "active"
    assert data["executor_used"] == "local_slm_background"
    assert data["model_used"] == "qwen3:1.7b"
    assert data["background_job_type"] == "docs_rebuild"
    assert data["job_id"] == "job-docs-1"
    assert data["poll"] == "/api/v1/tasks/job-docs-1"
    assert fake_queue.calls == [("docs_rebuild", {"project": "supermemory", "force": False})]


async def test_dispatch_background_task_packet_submits_generic_llm_job_for_cheap_subagent(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    class _Decision:
        task_type = "code_generation"
        component = "glm-4.7"
        score = 0.83
        tier = "cloud"
        reasoning = "Cheap cloud tier is sufficient."
        confidence = 0.9
        cloud_fallbacks = [{"model_id": "gemini-3.1-flash", "score": 0.77}]

    async def fake_decide_task_route(*, task: str, preferred_tier: str | None = None):
        assert "bounded mcp packet" in task.lower()
        assert preferred_tier == "cloud"
        return _Decision()

    class _FakeQueue:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def submit(self, job_type: str, payload: dict) -> str:
            self.calls.append((job_type, payload))
            return "job-llm-1"

    fake_queue = _FakeQueue()

    monkeypatch.setattr(models_router, "decide_task_route", fake_decide_task_route)
    monkeypatch.setattr("app.services.job_queue.get_job_queue", lambda: fake_queue)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "task_description": "Bounded MCP packet for parity and focused tests",
            "execution_mode": "economy",
            "suggested_execution_tier": "mini",
            "model_hint": "Use a mini model.",
            "priority": "high",
            "why_now": "Need a bounded packet proposal before merge",
            "definition_of_done": "Finish the bounded MCP packet and verify it.",
            "expected_output_shape": "Short result summary and verification summary.",
            "phase_objective": "Turn a bounded packet into an executable proposal",
            "write_scope": ["app/routers/mcp_sse.py", "mcp/server.py"],
            "core_instinct_ids": ["clarify_scope"],
            "supporting_instinct_ids": ["track_assumptions"],
            "project_context_summary": "coverage laws=1, components=2",
            "project_context_refs": {"laws": ["law-1"], "components": ["router", "handoff"]},
            "project_context_snapshot": "## Relevant Components\n\n### Router\nRoutes bounded packets.",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text

    dispatch_resp = await client.post(
        "/api/v1/models/handoff/dispatch_background",
        json={"memory_id": create_resp.json()["memory_id"], "acted_by": "codex"},
    )
    assert dispatch_resp.status_code == 200, dispatch_resp.text
    data = dispatch_resp.json()
    assert data["status"] == "active"
    assert data["executor_used"] == "cheap_subagent"
    assert data["model_used"] == "glm-4.7"
    assert data["background_job_type"] == "handoff_packet_llm"
    assert data["job_id"] == "job-llm-1"
    assert fake_queue.calls[0][0] == "handoff_packet_llm"
    assert fake_queue.calls[0][1]["recommended_model"] == "glm-4.7"
    assert fake_queue.calls[0][1]["recommended_executor"] == "cheap_subagent"
    assert fake_queue.calls[0][1]["project_context_summary"] == "coverage laws=1, components=2"
    assert fake_queue.calls[0][1]["project_context_refs"] == {"laws": ["law-1"], "components": ["router", "handoff"]}
    assert fake_queue.calls[0][1]["project_context_snapshot"].startswith("## Relevant Components")


async def test_reconcile_background_task_packet_closes_done_job(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    class _Decision:
        task_type = "text_summarization"
        component = "qwen3:1.7b"
        score = 0.77
        tier = "local"
        reasoning = "Local tier is sufficient."
        confidence = 0.8
        cloud_fallbacks = []

    async def fake_decide_task_route(*, task: str, preferred_tier: str | None = None):
        return _Decision()

    class _FakeQueue:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.jobs: dict[str, dict] = {}

        async def submit(self, job_type: str, payload: dict) -> str:
            self.calls.append((job_type, payload))
            job_id = "job-docs-1"
            self.jobs[job_id] = {
                "id": job_id,
                "job_type": job_type,
                "status": "done",
                "result": {"project": "supermemory", "sections": ["overview", "api"]},
            }
            return job_id

        def get_job(self, job_id: str) -> dict | None:
            return self.jobs.get(job_id)

    fake_queue = _FakeQueue()

    monkeypatch.setattr(models_router, "decide_task_route", fake_decide_task_route)
    monkeypatch.setattr("app.services.job_queue.get_job_queue", lambda: fake_queue)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "task_description": "Rebuild docs projection",
            "execution_mode": "strict_economy",
            "background_job_type": "docs_rebuild",
            "background_payload": {"project": "supermemory", "force": False},
            "suggested_execution_tier": "local",
            "model_hint": "Prefer local background execution.",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    memory_id = create_resp.json()["memory_id"]

    dispatch_resp = await client.post(
        "/api/v1/models/handoff/dispatch_background",
        json={"memory_id": memory_id, "acted_by": "codex"},
    )
    assert dispatch_resp.status_code == 200, dispatch_resp.text
    assert dispatch_resp.json()["model_used"] == "qwen3:1.7b"

    reconcile_resp = await client.post(
        "/api/v1/models/handoff/reconcile_background",
        json={"memory_id": memory_id, "acted_by": "codex"},
    )
    assert reconcile_resp.status_code == 200, reconcile_resp.text
    data = reconcile_resp.json()
    assert data["status"] == "closed"
    assert data["background_job_status"] == "done"
    assert data["background_job_type"] == "docs_rebuild"
    assert data["executor_used"] == "local_slm_background"
    assert data["model_used"] == "qwen3:1.7b"
    assert data["result_summary"] == "Background job docs_rebuild completed."
    assert "project=supermemory" in data["verification_summary"]

    listed = await client.post(
        "/api/v1/models/handoff/list",
        json={"agent_id": "codex", "statuses": ["closed"], "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()["handoffs"]
    row = next(item for item in rows if item["memory_id"] == memory_id)
    assert row["background_job_status"] == "done"
    assert row["dispatched_job_id"] == "job-docs-1"


async def test_reconcile_background_task_packets_syncs_active_packets(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    class _Decision:
        task_type = "text_summarization"
        component = "qwen3:1.7b"
        score = 0.77
        tier = "local"
        reasoning = "Local tier is sufficient."
        confidence = 0.8
        cloud_fallbacks = []

    async def fake_decide_task_route(*, task: str, preferred_tier: str | None = None):
        return _Decision()

    class _FakeQueue:
        def __init__(self) -> None:
            self.jobs: dict[str, dict] = {}

        async def submit(self, job_type: str, payload: dict) -> str:
            job_id = f"job-{payload['project']}"
            self.jobs[job_id] = {
                "id": job_id,
                "job_type": job_type,
                "status": "done",
                "result": {"project": payload["project"], "sections": ["overview", "api"]},
            }
            return job_id

        def get_job(self, job_id: str) -> dict | None:
            return self.jobs.get(job_id)

    fake_queue = _FakeQueue()

    monkeypatch.setattr(models_router, "decide_task_route", fake_decide_task_route)
    monkeypatch.setattr("app.services.job_queue.get_job_queue", lambda: fake_queue)

    created_ids: list[str] = []
    for project_id in ("alpha", "beta"):
        create_resp = await client.post(
            "/api/v1/models/handoff",
            json={
                "from_agent": "codex",
                "to_agent": "codex",
                "task_description": f"Rebuild docs projection for {project_id}",
                "execution_mode": "strict_economy",
                "background_job_type": "docs_rebuild",
                "background_payload": {"project": project_id, "force": False},
                "suggested_execution_tier": "local",
                "model_hint": "Prefer local background execution.",
                "reason": "manual",
            },
        )
        assert create_resp.status_code == 200, create_resp.text
        memory_id = create_resp.json()["memory_id"]
        created_ids.append(memory_id)
        dispatch_resp = await client.post(
            "/api/v1/models/handoff/dispatch_background",
            json={"memory_id": memory_id, "acted_by": "codex"},
        )
        assert dispatch_resp.status_code == 200, dispatch_resp.text

    reconciled = await models_router.reconcile_background_task_packets(
        qdrant=get_qdrant(),
        limit=10,
        acted_by="background_sync",
        reason="background_sync",
    )
    assert reconciled["scanned"] >= 2
    assert reconciled["updated"] >= 2
    assert reconciled["closed"] >= 2
    assert reconciled["by_background_job_status"]["done"] >= 2
    assert {packet["memory_id"] for packet in reconciled["packets"]} >= set(created_ids)

    listed = await client.post(
        "/api/v1/models/handoff/list",
        json={"agent_id": "codex", "statuses": ["closed"], "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    rows = {item["memory_id"]: item for item in listed.json()["handoffs"]}
    for memory_id in created_ids:
        assert rows[memory_id]["background_job_status"] == "done"


async def test_closed_packet_can_carry_result_and_verification_summaries(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "codex",
            "owner_agent": "feynman",
            "write_scope": ["tests/test_mcp_stdio_handoff.py"],
            "task_description": "Merge back stdio regression",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    memory_id = create_resp.json()["memory_id"]

    close_resp = await client.post(
        "/api/v1/models/handoff/status",
        json={
            "memory_id": memory_id,
            "status": "closed",
            "acted_by": "codex",
            "reason": "merged back",
            "owner_agent": "feynman",
            "write_scope": ["tests/test_mcp_stdio_handoff.py"],
            "result_summary": "Added a focused stdio regression for task-packet tool discovery.",
            "verification_summary": "pytest tests/test_mcp_stdio_handoff.py -q passed.",
        },
    )
    assert close_resp.status_code == 200, close_resp.text

    list_resp = await client.post(
        "/api/v1/models/handoff/list",
        json={
            "agent_id": "codex",
            "statuses": ["closed"],
            "owner_agent": "feynman",
            "write_scope": ["tests/test_mcp_stdio_handoff.py"],
            "limit": 10,
        },
    )
    assert list_resp.status_code == 200, list_resp.text
    data = list_resp.json()
    assert data["found"] >= 1
    row = next(item for item in data["handoffs"] if item["memory_id"] == memory_id)
    assert row["result_summary"] == "Added a focused stdio regression for task-packet tool discovery."
    assert row["verification_summary"] == "pytest tests/test_mcp_stdio_handoff.py -q passed."


async def test_resume_handoff_reactivates_and_refreshes_context(client, monkeypatch):
    fake_registry = _FakeRegistry()
    monkeypatch.setattr(models_router, "get_model_registry", lambda: fake_registry)

    create_resp = await client.post(
        "/api/v1/models/handoff",
        json={
            "from_agent": "codex",
            "to_agent": "claude-code",
            "project_id": "supermemory",
            "owner_agent": "claude-code",
            "write_scope": ["app/routers/models.py"],
            "task_description": "Resume this task packet",
            "reason": "manual",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    memory_id = create_resp.json()["memory_id"]

    await client.post(
        "/api/v1/models/handoff/status",
        json={
            "memory_id": memory_id,
            "status": "paused",
            "acted_by": "codex",
            "reason": "switching tasks",
        },
    )

    class _Bundle:
        laws = [{"id": "law-1", "title": "Require review"}]
        components = [{"component_id": "handoff", "name": "Handoff"}]
        improvements = [{"id": "imp-1", "title": "Tighten packet"}]
        runtime_hints = []
        tasks = [{"task_id": "task-1", "title": "Resume flow"}]
        task_triage = {"recommended_task_id": "task-1", "items": [{"task_id": "task-1", "title": "Resume flow"}]}
        task_capture_candidates = [{"artifact_id": "capture-1", "task_id": "task-1", "kind": "assumption"}]
        docs_sections = [{"section_key": "overview"}]
        code_inspection_recommended = False
        coverage = {"laws": 1, "components": 1, "improvements": 1, "runtime_hints": 0, "tasks": 1, "docs_sections": 1}

    async def fake_assemble_project_context(**kwargs):
        assert kwargs["project_id"] == "supermemory"
        assert kwargs["task"] == "Resume this task packet"
        return _Bundle()

    monkeypatch.setattr(models_router, "assemble_project_context", fake_assemble_project_context)

    resume_resp = await client.post(
        "/api/v1/models/handoff/resume",
        json={
            "memory_id": memory_id,
            "refresh_context": True,
            "acted_by": "codex",
            "reason": "returning to task",
            "owner_agent": "codex",
            "write_scope": ["app/routers/models.py", "app/services/qdrant_service.py"],
            "max_components": 2,
        },
    )
    assert resume_resp.status_code == 200, resume_resp.text
    data = resume_resp.json()
    assert data["memory_id"] == memory_id
    assert data["status"] == "active"
    assert data["refreshed"] is True
    assert data["acted_by"] == "codex"
    assert data["reason"] == "returning to task"
    assert data["owner_agent"] == "codex"
    assert data["write_scope"] == ["app/routers/models.py", "app/services/qdrant_service.py"]
    assert data["phase"] is None
    assert data["priority"] is None
    assert data["project_context_refs"] == {
        "laws": ["law-1"],
        "components": ["handoff"],
        "improvements": ["imp-1"],
        "tasks": ["task-1"],
        "task_capture_candidates": ["capture-1"],
        "docs_sections": ["overview"],
    }
