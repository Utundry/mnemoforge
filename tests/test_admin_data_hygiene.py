import pytest

from app.services.data_hygiene_service import apply_approved_delete, apply_reviewed_delete, classify_memory_payload
from app.services.data_hygiene_service import (
    get_data_hygiene_store,
    is_auto_test_cleanup_candidate,
    promote_auto_test_cleanup_candidates,
    resolve_governed_synthetic_false_positives,
    run_data_hygiene_audit,
)
from app.services.job_queue import get_job_queue
from app.services.learning_store import get_learning_store
from app.services.memory_store import get_memory_store


@pytest.mark.asyncio
async def test_admin_status_includes_data_hygiene(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-1",
        store_name="learning_events",
        record_locator="1",
        dataset_class="telemetry_trace",
        recommended_action="exclude-from-learning",
        exclude_from_learning=True,
        confidence=0.9,
        reasons=["telemetry_event:tool_call"],
        details={"event_type": "tool_call"},
    )

    resp = await client.get("/api/v1/admin/status")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["data_hygiene"]["status"] == "warning"
    assert data["data_hygiene"]["active_findings"] == 1
    assert data["storage_trust"]["status"] in {"warning", "degraded"}


@pytest.mark.asyncio
async def test_admin_storage_trust_report_reflects_integrity_and_hygiene(client):
    from app.services.data_integrity_service import get_data_integrity_store

    hygiene = get_data_hygiene_store()
    integrity = get_data_integrity_store()
    hygiene.upsert_finding(
        finding_id="hygiene-trust-1",
        store_name="learning_events",
        record_locator="501",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:test"],
        details={"event_type": "memory_write"},
    )
    integrity.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )

    resp = await client.get("/api/v1/admin/storage-trust")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "degraded"
    assert "qdrant.skill_domain_tags_filter" in data["signals"]["degraded_slices"]
    assert data["signals"]["manual_review_pending"]["synthetic_test"] >= 1
    assert len(data["next_actions"]) >= 1


@pytest.mark.asyncio
async def test_admin_code_hardcoding_audit_endpoint(client, monkeypatch):
    from app.routers import admin as admin_router

    def fake_audit(*, limit_per_category: int = 100):
        return {
            "status": "warning",
            "total_findings": 2,
            "by_category": {"private_network_url": 1, "hardcoded_scope_identifier": 1},
            "findings": [
                {"category": "private_network_url", "file_path": "scripts/client_scan.py", "line_number": 46},
                {"category": "hardcoded_scope_identifier", "file_path": "app/services/data_integrity_service.py", "line_number": 79},
            ],
            "next_actions": ["Replace machine-specific endpoints with config."],
        }

    monkeypatch.setattr(admin_router, "run_code_hardcoding_audit", fake_audit)

    resp = await client.get("/api/v1/admin/code-hardcoding-audit")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "warning"
    assert data["total_findings"] == 2
    assert data["by_category"]["private_network_url"] == 1


@pytest.mark.asyncio
async def test_admin_functionality_inventory_endpoint(client, monkeypatch):
    from app.routers import admin as admin_router

    def fake_inventory():
        return {
            "status": "warning",
            "inventory_version": 1,
            "total_modules": 3,
            "by_status": {"keep": 1, "review_legacy": 1, "experimental": 1},
            "by_surface_kind": {"core": 1, "optional": 2},
            "summary": {
                "keep_count": 1,
                "modernize_count": 0,
                "review_pressure": 2,
                "release_blockers": ["watcher", "layout_fixer"],
            },
            "items": [
                {"module": "memories", "surface_kind": "core", "status": "keep", "reason": "core", "source_line": 1},
                {"module": "watcher", "surface_kind": "optional", "status": "review_legacy", "reason": "legacy", "source_line": 2},
                {"module": "layout_fixer", "surface_kind": "optional", "status": "experimental", "reason": "early", "source_line": 3},
            ],
            "next_actions": ["Review legacy surfaces."],
        }

    monkeypatch.setattr(admin_router, "build_functionality_inventory", fake_inventory)

    resp = await client.get("/api/v1/admin/functionality-inventory")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "warning"
    assert data["summary"]["review_pressure"] == 2
    assert "watcher" in data["summary"]["release_blockers"]


@pytest.mark.asyncio
async def test_admin_functionality_release_scope_endpoint(client, monkeypatch):
    from app.routers import admin as admin_router

    def fake_release_scope():
        return {
            "status": "warning",
            "inventory_version": 1,
            "default_surface": ["memories", "mcp_sse"],
            "modernize_before_alpha": ["dashboard"],
            "candidate_feature_flags": ["layout_fixer"],
            "deprecate_review": ["watcher"],
            "next_actions": ["Freeze the default surface for GitHub alpha around keep modules only."],
        }

    monkeypatch.setattr(admin_router, "build_functionality_release_scope", fake_release_scope)

    resp = await client.get("/api/v1/admin/functionality-release-scope")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "warning"
    assert "memories" in data["default_surface"]
    assert "layout_fixer" in data["candidate_feature_flags"]


@pytest.mark.asyncio
async def test_admin_functionality_review_dossier_endpoint(client, monkeypatch):
    from app.routers import admin as admin_router

    def fake_dossier(module: str):
        return {
            "module": module,
            "status": "review_legacy",
            "reason": "legacy",
            "surface_kind": "optional",
            "source_line": 123,
            "file_path": f"app/routers/{module}.py",
            "line_count": 42,
            "router_prefixes": [f"/{module}"],
            "router_tags": [module],
            "references": {"count": 3, "samples": []},
            "release_recommendation": "deprecate_review",
            "next_actions": ["Run explicit keep/deprecate decision."],
        }

    monkeypatch.setattr(admin_router, "build_functionality_review_dossier", fake_dossier)

    resp = await client.get("/api/v1/admin/functionality-review-dossier/watcher")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["module"] == "watcher"
    assert data["release_recommendation"] == "deprecate_review"


@pytest.mark.asyncio
async def test_admin_functionality_review_queue_endpoint(client, monkeypatch):
    from app.routers import admin as admin_router

    def fake_queue():
        return {
            "status": "warning",
            "total": 2,
            "items": [
                {"module": "auto_memory", "status": "review_legacy"},
                {"module": "layout_fixer", "status": "experimental"},
            ],
            "next_actions": ["Resolve review_legacy modules first."],
        }

    monkeypatch.setattr(admin_router, "build_functionality_review_queue", fake_queue)

    resp = await client.get("/api/v1/admin/functionality-review-queue")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 2
    assert data["items"][0]["module"] == "auto_memory"


@pytest.mark.asyncio
async def test_admin_functionality_alpha_config_endpoint(client, monkeypatch):
    from app.routers import admin as admin_router

    def fake_alpha_config():
        return {
            "status": "warning",
            "inventory_version": 1,
            "default_surface": ["memories", "mcp_sse"],
            "disabled_modules": ["layout_fixer", "openai_compat"],
            "disabled_modules_env": "layout_fixer,openai_compat",
            "next_actions": ["Use disabled_modules as the recommended DISABLED_MODULES baseline for public alpha."],
        }

    monkeypatch.setattr(admin_router, "build_functionality_alpha_config", fake_alpha_config)

    resp = await client.get("/api/v1/admin/functionality-alpha-config")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "layout_fixer" in data["disabled_modules"]
    assert "openai_compat" in data["disabled_modules_env"]


@pytest.mark.asyncio
async def test_admin_publish_readiness_endpoint(client, monkeypatch):
    from app.routers import admin as admin_router

    def fake_publish_readiness():
        return {
            "status": "warning",
            "publish_target": "github_alpha",
            "readiness_version": 1,
            "package_presence": {"files": {"readme": True}, "missing": []},
            "public_docs": {"items": {"quickstart": {"present": True, "file": "README.md"}}, "missing": ["status_doc"]},
            "demo_dataset": {"items": {"demo_readme": {"present": False, "file": "demo/README.md"}}, "missing": ["demo_readme"]},
            "env_example": {"present": True, "keys_present": ["API_KEY"], "values": {"API_KEY": ""}, "missing_keys": ["DISABLED_MODULES"]},
            "sanitization": {
                "gitignore_present": True,
                "critical_ignore_rules_present": [".env"],
                "missing_ignore_rules": [],
                "docs_audit": {},
                "issues": {"mojibake_docs": ["SETUP.md"], "local_path_docs": ["CLIENT_SETUP.md"], "private_network_docs": []},
            },
            "alpha_surface": {"disabled_modules": ["layout_fixer"], "disabled_modules_env": "layout_fixer"},
            "blockers": ["Missing public docs coverage: status_doc"],
            "warnings": ["Mojibake detected in public docs: SETUP.md"],
            "next_actions": ["Create a dedicated public status document or equivalent alpha-status surface."],
        }

    monkeypatch.setattr(admin_router, "build_publish_readiness", fake_publish_readiness)

    resp = await client.get("/api/v1/admin/publish-readiness")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "warning"
    assert data["publish_target"] == "github_alpha"
    assert "status_doc" in data["public_docs"]["missing"]
    assert "demo_readme" in data["demo_dataset"]["missing"]
    assert "DISABLED_MODULES" in data["env_example"]["missing_keys"]


@pytest.mark.asyncio
async def test_admin_operational_instincts_endpoints(client, monkeypatch):
    from app.routers import admin as admin_router

    def fake_list(*, layer=None, scope_ref=None, family=None, phase=None, active_only=False):
        return [{"instinct_id": "trust_first", "layer": "global_builtin", "scope_ref": "", "family": "core_bootstrap", "phase": "general", "rank": "P0", "active": True}]

    def fake_active(*, context_type: str, project_id=None, storage_trust_status=None, code_inspection_recommended=False, limit=5):
        return [{"instinct_id": "ask_memory_before_code", "rank": "P0"}]

    def fake_upsert(**kwargs):
        return {"instinct_id": kwargs["instinct_id"], "layer": kwargs["layer"], "action": kwargs["action"]}

    monkeypatch.setattr(admin_router, "list_operational_instincts", fake_list)
    monkeypatch.setattr(admin_router, "get_active_operational_instincts", fake_active)
    monkeypatch.setattr(admin_router, "upsert_operational_instinct", fake_upsert)
    monkeypatch.setattr(
        admin_router,
        "build_operational_instinct_activation_summary",
        lambda limit=200: {"recent_event_count": 1, "by_context": {"task_framing": 1}, "by_family": {"task_lifecycle": 1}, "by_phase": {"task_framing": 1}, "top_instincts": [], "events": []},
    )

    list_resp = await client.get("/api/v1/admin/operational-instincts")
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()["total"] == 1

    active_resp = await client.get("/api/v1/admin/operational-instincts/active?context_type=task_enrichment&project_id=alpha")
    assert active_resp.status_code == 200, active_resp.text
    assert active_resp.json()["items"][0]["instinct_id"] == "ask_memory_before_code"

    upsert_resp = await client.post(
        "/api/v1/admin/operational-instincts",
        json={
            "instinct_id": "trust_first",
            "layer": "instance_local",
            "family": "core_bootstrap",
            "rank": "P0",
            "scope": "global",
            "trigger": "Any task start.",
            "action": "Check trust.",
            "why_it_matters": "Trust matters.",
            "failure_if_missing": "Bad retrieval.",
            "activation_tags": ["onboarding"],
        },
    )
    assert upsert_resp.status_code == 200, upsert_resp.text
    assert upsert_resp.json()["instinct_id"] == "trust_first"

    summary_resp = await client.get("/api/v1/admin/operational-instincts/activation-summary")
    assert summary_resp.status_code == 200, summary_resp.text
    assert summary_resp.json()["by_phase"]["task_framing"] == 1

    playbook_resp = await client.get("/api/v1/admin/operational-instincts/playbook?family=task_lifecycle")
    assert playbook_resp.status_code == 200, playbook_resp.text
    assert playbook_resp.json()["family"] == "task_lifecycle"
    assert "task_framing" in playbook_resp.json()["phase_sequence"]


@pytest.mark.asyncio
async def test_admin_functionality_review_hints_endpoints(client, monkeypatch):
    from app.routers import admin as admin_router

    def fake_list(*, scope: str = "mnemoforge"):
        return [{"scope": scope, "module": "watcher", "status": "review_legacy", "reason": "legacy"}]

    def fake_upsert(*, scope: str, module: str, status: str, reason: str):
        return {"scope": scope, "module": module, "status": status, "reason": reason}

    def fake_bootstrap(*, scope: str = "mnemoforge", overwrite: bool = False):
        return {"scope": scope, "created": 10, "updated": 0, "skipped": 0, "total_seeded": 10}

    monkeypatch.setattr(admin_router, "list_functionality_review_hints", fake_list)
    monkeypatch.setattr(admin_router, "upsert_functionality_review_hint", fake_upsert)
    monkeypatch.setattr(admin_router, "bootstrap_functionality_review_hints", fake_bootstrap)

    list_resp = await client.get("/api/v1/admin/functionality-review-hints")
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()["total"] == 1

    upsert_resp = await client.post(
        "/api/v1/admin/functionality-review-hints",
        json={"module": "watcher", "status": "keep", "reason": "validated"},
    )
    assert upsert_resp.status_code == 200, upsert_resp.text
    assert upsert_resp.json()["status"] == "keep"

    bootstrap_resp = await client.post("/api/v1/admin/functionality-review-hints/bootstrap")
    assert bootstrap_resp.status_code == 200, bootstrap_resp.text
    assert bootstrap_resp.json()["created"] == 10


@pytest.mark.asyncio
async def test_data_hygiene_audit_finds_synthetic_memory_and_telemetry_event(client):
    memory_payload = {
        "content": "Synthetic benchmark fixture for smoke testing",
        "agent_id": "test-agent",
        "memory_type": "fact",
        "category": "general",
        "source": "demo-fixture",
        "tags": ["synthetic", "fixture"],
    }
    memory_resp = await client.post("/api/v1/memories", json=memory_payload)
    assert memory_resp.status_code == 201, memory_resp.text

    await get_learning_store().write_event(
        event_type="tool_call",
        agent_id="test-agent",
        project="mnemoforge",
        context_signature="project=mnemoforge;category=memory_search",
        payload={"tool_name": "memory_search"},
    )

    resp = await client.post("/api/v1/admin/data-hygiene/audit?memory_limit=50&event_limit=50")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "warning"
    assert data["findings_count"] >= 2
    assert data["classified"]["synthetic_test"] >= 1
    assert data["classified"]["telemetry_trace"] >= 1

    findings_resp = await client.get("/api/v1/admin/data-hygiene/findings")
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()["items"]
    dataset_classes = {item["dataset_class"] for item in findings}
    assert "synthetic_test" in dataset_classes
    assert "telemetry_trace" in dataset_classes


def test_classify_memory_payload_keeps_evolutionary_project_records():
    result = classify_memory_payload(
        {
            "category": "task_change",
            "source": "automation",
            "tags": ["entity:task_change", "project:mnemoforge"],
            "content": "Task status moved from planning to done with rationale.",
        }
    )
    assert result["dataset_class"] == "evolutionary_knowledge"
    assert result["recommended_action"] == "keep"
    assert result["exclude_from_learning"] is False


def test_classify_memory_payload_archives_cache_operational_records():
    result = classify_memory_payload(
        {
            "category": "docs_cache",
            "source": "docs-cache:refresh",
            "tags": ["cache", "projection"],
            "content": "Ephemeral cache snapshot.",
        }
    )
    assert result["dataset_class"] == "service_operational"
    assert result["recommended_action"] == "archive"
    assert result["exclude_from_learning"] is True


def test_classify_memory_payload_treats_improvement_source_as_canonical():
    result = classify_memory_payload(
        {
            "category": "",
            "source": "improvement_created",
            "tags": ["status:open"],
            "content": "Reported product improvement with operator context.",
        }
    )
    assert result["dataset_class"] == "canonical_knowledge"
    assert result["recommended_action"] == "keep"
    assert result["exclude_from_learning"] is False


def test_classify_memory_payload_treats_source_conversation_as_raw_dialogue():
    result = classify_memory_payload(
        {
            "category": "",
            "source": "conversation",
            "tags": [],
            "content": "User and assistant dialogue trace.",
        }
    )
    assert result["dataset_class"] == "raw_dialogue_trace"
    assert result["recommended_action"] == "exclude-from-learning"
    assert result["exclude_from_learning"] is True


def test_classify_memory_payload_flags_stale_public_tool_guidance():
    result = classify_memory_payload(
        {
            "category": "context",
            "source": "agent-note",
            "tags": ["project:mnemoforge"],
            "content": "To save facts, use the memory_store tool directly before continuing.",
        }
    )

    assert result["dataset_class"] == "stale_guidance"
    assert result["recommended_action"] == "exclude-from-learning"
    assert result["exclude_from_learning"] is True
    assert any("memory_store" in reason for reason in result["reasons"])


def test_classify_memory_payload_keeps_historical_improvement_about_old_tool():
    result = classify_memory_payload(
        {
            "category": "improvement",
            "source": "improvement_created",
            "tags": ["entity:improvement", "project:mnemoforge"],
            "content": "Old report: memory_store was missing from the simplified API.",
        }
    )

    assert result["dataset_class"] == "canonical_knowledge"
    assert result["recommended_action"] == "keep"
    assert result["exclude_from_learning"] is False


def test_classify_memory_payload_flags_unknown_mailbox_form_guidance():
    result = classify_memory_payload(
        {
            "category": "reference",
            "source": "agent-guidance",
            "tags": ["project:mnemoforge"],
            "content": "Submit form_id=obsolete_checkpoint_draft to reject the draft.",
        }
    )

    assert result["dataset_class"] == "stale_guidance"
    assert result["recommended_action"] == "exclude-from-learning"
    assert any("unknown_mailbox_form_guidance:obsolete_checkpoint_draft" in reason for reason in result["reasons"])


def test_classify_memory_payload_keeps_governed_task_with_test_tags():
    result = classify_memory_payload(
        {
            "category": "task",
            "source": "improvement",
            "tags": [
                "project:mnemoforge",
                "tests",
                "entity:task",
                "task_id:faeae7b2-a8c9-4487-8b80-df9a38b2818d",
            ],
            "content": "Task mentions tests but is governed project memory, not disposable fixture data.",
        }
    )

    assert result["dataset_class"] == "canonical_knowledge"
    assert result["recommended_action"] == "keep"
    assert result["exclude_from_learning"] is False
    assert "synthetic_marker_ignored_for_governed_record" in result["reasons"]


def test_classify_memory_payload_keeps_governed_task_change_with_test_tags():
    result = classify_memory_payload(
        {
            "category": "task_change",
            "source": "improvement_created",
            "tags": [
                "project:mnemoforge",
                "test",
                "entity:task_change",
                "task_id:faeae7b2-a8c9-4487-8b80-df9a38b2818d",
            ],
            "content": "Task change references a live test but should remain historical evolution data.",
        }
    )

    assert result["dataset_class"] == "canonical_knowledge"
    assert result["recommended_action"] == "keep"
    assert result["exclude_from_learning"] is False


@pytest.mark.asyncio
async def test_data_hygiene_audit_falls_back_to_memory_store_when_qdrant_scroll_fails(client, monkeypatch):
    from app.dependencies import get_qdrant

    memory_payload = {
        "content": "Synthetic benchmark fixture for fallback audit path",
        "agent_id": "test-agent",
        "memory_type": "fact",
        "category": "general",
        "source": "demo-fixture",
        "tags": ["synthetic", "fixture"],
    }
    memory_resp = await client.post("/api/v1/memories", json=memory_payload)
    assert memory_resp.status_code == 201, memory_resp.text
    memory_id = memory_resp.json()["id"]

    qdrant = get_qdrant()

    async def fail_scroll(*args, **kwargs):
        raise RuntimeError("simulated qdrant panic during hygiene audit")

    monkeypatch.setattr(qdrant._client, "scroll", fail_scroll)

    resp = await client.post("/api/v1/admin/data-hygiene/audit?memory_limit=20&event_limit=20")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "warning"
    assert data["findings_count"] >= 1
    latest = data["latest_audit"]
    assert latest["details"]["memory_scan_source"] == "sqlite_memory_store_fallback"
    assert "simulated qdrant panic during hygiene audit" in latest["details"]["qdrant_scan_error"]

    findings_resp = await client.get("/api/v1/admin/data-hygiene/findings?store_name=qdrant_memories")
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()["items"]
    assert any(item["record_locator"] == memory_id and item["dataset_class"] == "synthetic_test" for item in findings)


@pytest.mark.asyncio
async def test_data_hygiene_audit_splits_qdrant_retrieve_batches_before_falling_back(client, monkeypatch):
    from app.dependencies import get_qdrant

    for idx in range(3):
        memory_resp = await client.post(
            "/api/v1/memories",
            json={
                "content": f"Synthetic benchmark fixture {idx} for qdrant hydration split",
                "agent_id": "test-agent",
                "memory_type": "fact",
                "category": "general",
                "source": f"demo-fixture-{idx}",
                "tags": ["synthetic", "fixture"],
            },
        )
        assert memory_resp.status_code == 201, memory_resp.text

    qdrant = get_qdrant()
    real_retrieve = qdrant._client.retrieve

    async def flaky_retrieve(*args, **kwargs):
        ids = list(kwargs.get("ids") or [])
        if len(ids) > 1:
            raise RuntimeError("simulated oversized qdrant retrieve batch")
        return await real_retrieve(*args, **kwargs)

    monkeypatch.setattr(qdrant._client, "retrieve", flaky_retrieve)

    resp = await client.post("/api/v1/admin/data-hygiene/audit?memory_limit=20&event_limit=20")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "warning"
    latest = data["latest_audit"]
    assert latest["details"]["memory_scan_source"] == "qdrant"
    assert "qdrant_scan_error" not in latest["details"]
    assert latest["details"].get("qdrant_hydration_skipped", 0) == 0


@pytest.mark.asyncio
async def test_data_hygiene_audit_uses_sqlite_for_single_payload_retrieve_failures(client, monkeypatch):
    from app.dependencies import get_qdrant

    memory_resp = await client.post(
        "/api/v1/memories",
        json={
            "content": "Synthetic benchmark fixture for sqlite payload hydration fallback",
            "agent_id": "test-agent",
            "memory_type": "fact",
            "category": "general",
            "source": "demo-fixture-single",
            "tags": ["synthetic", "fixture"],
        },
    )
    assert memory_resp.status_code == 201, memory_resp.text
    memory_id = memory_resp.json()["id"]

    qdrant = get_qdrant()
    real_retrieve = qdrant._client.retrieve

    async def flaky_retrieve(*args, **kwargs):
        ids = list(kwargs.get("ids") or [])
        if ids == [memory_id]:
            raise RuntimeError("simulated single-id payload failure")
        return await real_retrieve(*args, **kwargs)

    monkeypatch.setattr(qdrant._client, "retrieve", flaky_retrieve)

    resp = await client.post("/api/v1/admin/data-hygiene/audit?memory_limit=20&event_limit=20")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    latest = data["latest_audit"]
    assert latest["details"]["memory_scan_source"] == "qdrant"
    assert "qdrant_scan_error" not in latest["details"]
    assert latest["details"].get("qdrant_hydration_skipped", 0) == 0
    assert latest["details"].get("qdrant_sqlite_hydration_fallbacks", 0) >= 1


@pytest.mark.asyncio
async def test_data_hygiene_audit_auto_repairs_single_payload_failures_from_sqlite(client, monkeypatch):
    from app.dependencies import get_qdrant

    memory_resp = await client.post(
        "/api/v1/memories",
        json={
            "content": "Synthetic benchmark fixture for qdrant auto-repair path",
            "agent_id": "test-agent",
            "memory_type": "fact",
            "category": "general",
            "source": "demo-fixture-auto-repair",
            "tags": ["synthetic", "fixture"],
        },
    )
    assert memory_resp.status_code == 201, memory_resp.text
    memory_id = memory_resp.json()["id"]

    qdrant = get_qdrant()
    real_retrieve = qdrant._client.retrieve
    real_upsert = qdrant._client.upsert
    repair_upserts: list[object] = []

    async def flaky_retrieve(*args, **kwargs):
        ids = list(kwargs.get("ids") or [])
        if ids == [memory_id] and kwargs.get("with_payload") and not kwargs.get("with_vectors"):
            raise RuntimeError("simulated single-id payload corruption")
        return await real_retrieve(*args, **kwargs)

    async def tracking_upsert(*args, **kwargs):
        repair_upserts.append(kwargs.get("points") or [])
        return await real_upsert(*args, **kwargs)

    monkeypatch.setattr(qdrant._client, "retrieve", flaky_retrieve)
    monkeypatch.setattr(qdrant._client, "upsert", tracking_upsert)

    resp = await client.post("/api/v1/admin/data-hygiene/audit?memory_limit=20&event_limit=20")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    latest = data["latest_audit"]
    assert latest["details"]["memory_scan_source"] == "qdrant"
    assert "qdrant_scan_error" not in latest["details"]
    assert latest["details"].get("qdrant_sqlite_hydration_fallbacks", 0) >= 1
    assert latest["details"].get("qdrant_payload_auto_repairs", 0) >= 1
    assert repair_upserts


@pytest.mark.asyncio
async def test_data_hygiene_audit_auto_repairs_empty_payloads_from_sqlite(client, monkeypatch):
    from app.dependencies import get_qdrant

    memory_resp = await client.post(
        "/api/v1/memories",
        json={
            "content": "Synthetic benchmark fixture for empty-payload auto-repair",
            "agent_id": "test-agent",
            "memory_type": "fact",
            "category": "general",
            "source": "demo-fixture-empty-payload",
            "tags": ["synthetic", "fixture"],
        },
    )
    assert memory_resp.status_code == 201, memory_resp.text
    memory_id = memory_resp.json()["id"]

    qdrant = get_qdrant()
    real_retrieve = qdrant._client.retrieve
    real_upsert = qdrant._client.upsert
    repair_upserts: list[object] = []

    async def flaky_retrieve(*args, **kwargs):
        ids = list(kwargs.get("ids") or [])
        records = await real_retrieve(*args, **kwargs)
        if ids == [memory_id] and kwargs.get("with_payload") and not kwargs.get("with_vectors") and records:
            records[0].payload = {}
        return records

    async def tracking_upsert(*args, **kwargs):
        repair_upserts.append(kwargs.get("points") or [])
        return await real_upsert(*args, **kwargs)

    monkeypatch.setattr(qdrant._client, "retrieve", flaky_retrieve)
    monkeypatch.setattr(qdrant._client, "upsert", tracking_upsert)

    resp = await client.post("/api/v1/admin/data-hygiene/audit?memory_limit=20&event_limit=20")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    latest = data["latest_audit"]
    assert latest["details"]["memory_scan_source"] == "qdrant"
    assert latest["details"].get("qdrant_payload_auto_repairs", 0) >= 1
    assert repair_upserts


def test_data_hygiene_store_persists_scan_state():
    store = get_data_hygiene_store()

    saved = store.set_scan_state(
        {
            "qdrant_offset": "cursor-123",
            "event_before_ts": 123.45,
            "event_before_id": 77,
        }
    )

    assert saved["qdrant_offset"] == "cursor-123"
    assert saved["event_before_ts"] == 123.45
    assert saved["event_before_id"] == 77
    assert store.get_scan_state()["qdrant_offset"] == "cursor-123"


@pytest.mark.asyncio
async def test_data_hygiene_audit_advances_qdrant_and_event_cursors(client):
    from app.dependencies import get_qdrant

    for idx in range(3):
        memory_resp = await client.post(
            "/api/v1/memories",
            json={
                "content": f"Synthetic cursor fixture {idx} for hygiene audit progression",
                "agent_id": "cursor-agent",
                "memory_type": "fact",
                "category": "general",
                "source": f"demo-cursor-{idx}",
                "tags": ["synthetic", "fixture", f"cursor-{idx}"],
            },
        )
        assert memory_resp.status_code == 201, memory_resp.text

    learning_store = get_learning_store()
    for idx in range(3):
        await learning_store.write_event(
            event_type="tool_call",
            agent_id="cursor-agent",
            project="cursor-project",
            context_signature=f"cursor-{idx}",
            payload={"source": f"cursor-{idx}"},
        )

    qdrant = get_qdrant()

    first = await run_data_hygiene_audit(qdrant, memory_limit=1, event_limit=1)
    second = await run_data_hygiene_audit(
        qdrant,
        memory_limit=1,
        event_limit=1,
        qdrant_offset=first.get("next_qdrant_offset"),
        event_before_ts=first.get("next_event_before_ts"),
        event_before_id=first.get("next_event_before_id"),
    )

    first_details = first["latest_audit"]["details"]
    second_details = second["latest_audit"]["details"]
    assert first_details["qdrant_scan_start_offset"] is None
    assert first.get("next_qdrant_offset") is not None
    assert second_details["qdrant_scan_start_offset"] == first.get("next_qdrant_offset")
    assert second.get("next_qdrant_offset") != first.get("next_qdrant_offset")

    assert first.get("next_event_before_ts") is not None
    assert first.get("next_event_before_id") is not None
    assert second_details["event_scan_start_before_id"] == first.get("next_event_before_id")
    assert second.get("next_event_before_id") != first.get("next_event_before_id")


@pytest.mark.asyncio
async def test_admin_can_update_data_hygiene_finding_status(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-2",
        store_name="qdrant_memories",
        record_locator="memory-1",
        dataset_class="temporary_projection",
        recommended_action="archive",
        exclude_from_learning=True,
        confidence=0.8,
        reasons=["derived_projection_record"],
        details={"category": "doc_section"},
    )

    resp = await client.post("/api/v1/admin/data-hygiene/findings/hygiene-2/status?status=resolved")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["finding_id"] == "hygiene-2"
    assert data["status"] == "resolved"
    assert data["policy"]["retention"] == "archive"


def test_data_hygiene_store_resolves_open_findings_for_reclassified_record():
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-reclass-1",
        store_name="qdrant_memories",
        record_locator="memory-reclass-1",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:tests"],
        details={"category": "task", "tags": ["entity:task", "tests"]},
    )

    updated = store.resolve_open_findings_for_record(
        store_name="qdrant_memories",
        record_locator="memory-reclass-1",
        reason="current_classification_keep:canonical_knowledge",
    )

    assert updated == 1
    finding = store.get_finding("hygiene-reclass-1")
    assert finding["status"] == "resolved"
    assert "current_classification_keep:canonical_knowledge" in finding["reasons"]
    assert finding["details"]["resolved_by"] == "current_classification_keep:canonical_knowledge"


def test_resolve_governed_synthetic_false_positives_keeps_real_test_garbage_open():
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-governed-synth-1",
        store_name="qdrant_memories",
        record_locator="memory-governed-synth-1",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:tests"],
        details={
            "category": "task",
            "source": "improvement",
            "tags": ["entity:task", "tests", "project:mnemoforge"],
        },
    )
    store.upsert_finding(
        finding_id="hygiene-real-synth-1",
        store_name="qdrant_memories",
        record_locator="memory-real-synth-1",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:fixture"],
        details={"category": "general", "source": "demo-fixture", "tags": ["fixture"]},
    )

    result = resolve_governed_synthetic_false_positives(limit=20)

    assert result["updated"] == 1
    assert result["finding_ids"] == ["hygiene-governed-synth-1"]
    governed = store.get_finding("hygiene-governed-synth-1")
    real = store.get_finding("hygiene-real-synth-1")
    assert governed["status"] == "resolved"
    assert "governed_synthetic_false_positive" in governed["reasons"]
    assert real["status"] == "open"


@pytest.mark.asyncio
async def test_admin_can_resolve_governed_synthetic_review_noise(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-governed-synth-api-1",
        store_name="qdrant_memories",
        record_locator="memory-governed-synth-api-1",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:test"],
        details={
            "category": "task_change",
            "source": "improvement_created",
            "tags": ["entity:task_change", "test", "project:mnemoforge"],
        },
    )

    resp = await client.post("/api/v1/admin/data-hygiene/review/resolve-governed-synthetic?limit=20")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["updated"] == 1
    assert data["finding_ids"] == ["hygiene-governed-synth-api-1"]
    assert store.get_finding("hygiene-governed-synth-api-1")["status"] == "resolved"


@pytest.mark.asyncio
async def test_data_hygiene_policies_and_manual_review_surface(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-synth",
        store_name="learning_events",
        record_locator="77",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.97,
        reasons=["synthetic_marker:pytest"],
        details={"event_type": "dialogue_signal"},
    )

    policies_resp = await client.get("/api/v1/admin/data-hygiene/policies")
    assert policies_resp.status_code == 200, policies_resp.text
    policies = policies_resp.json()["policies"]
    assert policies["synthetic_test"]["manual_review_required"] is True
    assert policies["telemetry_trace"]["auto_remediate"] is True

    manual_resp = await client.get("/api/v1/admin/data-hygiene/manual-review")
    assert manual_resp.status_code == 200, manual_resp.text
    data = manual_resp.json()
    assert data["total"] >= 1
    assert any(item["finding_id"] == "hygiene-synth" for item in data["items"])

    quarantine_resp = await client.post(
        "/api/v1/admin/data-hygiene/findings/hygiene-synth/status?status=quarantine_candidate"
    )
    assert quarantine_resp.status_code == 200, quarantine_resp.text
    updated = quarantine_resp.json()
    assert updated["status"] == "quarantine_candidate"
    assert updated["policy"]["manual_review_required"] is True

    report_resp = await client.get("/api/v1/admin/data-hygiene/retention-report")
    assert report_resp.status_code == 200, report_resp.text
    report = report_resp.json()
    assert report["delete_candidates"] >= 1
    assert report["manual_review_pending"] >= 1


@pytest.mark.asyncio
async def test_data_hygiene_workflow_and_bulk_review_status(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-bulk-1",
        store_name="learning_events",
        record_locator="91",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.96,
        reasons=["synthetic_marker:pytest"],
        details={"event_type": "dialogue_signal"},
    )
    store.upsert_finding(
        finding_id="hygiene-bulk-2",
        store_name="learning_events",
        record_locator="92",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.97,
        reasons=["synthetic_marker:pytest"],
        details={"event_type": "dialogue_signal"},
    )

    workflow_resp = await client.get("/api/v1/admin/data-hygiene/workflow")
    assert workflow_resp.status_code == 200, workflow_resp.text
    workflow = workflow_resp.json()
    assert workflow["manual_review_pending"]["synthetic_test"] >= 2

    bulk_resp = await client.post(
        "/api/v1/admin/data-hygiene/review/bulk-status"
        "?target_status=quarantine_candidate"
        "&current_status=open"
        "&dataset_class=synthetic_test"
        "&recommended_action=delete"
    )
    assert bulk_resp.status_code == 200, bulk_resp.text
    data = bulk_resp.json()
    assert data["updated"] >= 2
    assert "hygiene-bulk-1" in data["finding_ids"]
    assert "hygiene-bulk-2" in data["finding_ids"]
    assert data["workflow"]["quarantine_candidates"]["synthetic_test"] >= 2

    items = store.list_findings(dataset_class="synthetic_test", status="quarantine_candidate", limit=10)
    assert len(items) >= 2


@pytest.mark.asyncio
async def test_quarantine_synthetic_shortcut_moves_open_candidates(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-shortcut-1",
        store_name="learning_events",
        record_locator="101",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:test"],
        details={"event_type": "memory_write"},
    )

    resp = await client.post("/api/v1/admin/data-hygiene/review/quarantine-synthetic?limit=10")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["updated"] >= 1
    assert "hygiene-shortcut-1" in data["finding_ids"]
    assert data["workflow"]["quarantine_candidates"]["synthetic_test"] >= 1

    finding = store.list_findings(dataset_class="synthetic_test", status="quarantine_candidate", limit=10)[0]
    assert finding["finding_id"] == "hygiene-shortcut-1"


@pytest.mark.asyncio
async def test_reviewed_delete_preview_lists_quarantine_candidates(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-preview-1",
        store_name="learning_events",
        record_locator="111",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.94,
        reasons=["synthetic_marker:test"],
        details={"event_type": "memory_write"},
    )
    store.set_finding_status(finding_id="hygiene-preview-1", status="quarantine_candidate")

    resp = await client.get("/api/v1/admin/data-hygiene/reviewed-delete-preview?limit=10")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["candidate_count"] >= 1
    assert data["requires_explicit_review"] is True
    assert any(item["finding_id"] == "hygiene-preview-1" for item in data["sample"])


@pytest.mark.asyncio
async def test_data_hygiene_playbook_reflects_current_workflow(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-playbook-1",
        store_name="learning_events",
        record_locator="121",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:test"],
        details={"event_type": "memory_write"},
    )

    resp = await client.get("/api/v1/admin/data-hygiene/playbook")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["principles"]) >= 3
    assert len(data["steps"]) >= 3
    assert data["workflow"]["manual_review_pending"]["synthetic_test"] >= 1
    assert data["steps"][0]["stage"] == "review"
    assert "/api/v1/admin/data-hygiene/manual-review" in data["steps"][0]["read_endpoints"]


@pytest.mark.asyncio
async def test_admin_can_queue_data_hygiene_remediation(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-3",
        store_name="learning_events",
        record_locator="3",
        dataset_class="telemetry_trace",
        recommended_action="exclude-from-learning",
        exclude_from_learning=True,
        confidence=0.9,
        reasons=["telemetry_event:tool_call"],
        details={"event_type": "tool_call"},
    )

    resp = await client.post("/api/v1/admin/data-hygiene/remediate?recommended_action=exclude-from-learning")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["recommended_action"] == "exclude-from-learning"
    assert data["status"] == "queued"
    assert data["job_id"]

    job = get_job_queue().get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "data_hygiene_apply_exclusion"


@pytest.mark.asyncio
async def test_admin_can_queue_data_hygiene_remediation_by_dataset_class(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-stale-target",
        store_name="qdrant_memories",
        record_locator="memory-stale-target",
        dataset_class="stale_guidance",
        recommended_action="exclude-from-learning",
        exclude_from_learning=True,
        confidence=0.88,
        reasons=["stale_tool_guidance:memory_store->replacement:submit:store_memory"],
        details={"category": "agent-guidance"},
    )
    store.upsert_finding(
        finding_id="hygiene-telemetry-other",
        store_name="qdrant_memories",
        record_locator="memory-telemetry-other",
        dataset_class="telemetry_trace",
        recommended_action="exclude-from-learning",
        exclude_from_learning=True,
        confidence=0.9,
        reasons=["telemetry_event:tool_call"],
        details={"event_type": "tool_call"},
    )

    resp = await client.post(
        "/api/v1/admin/data-hygiene/remediate"
        "?recommended_action=exclude-from-learning&dataset_class=stale_guidance&store_name=qdrant_memories&limit=20"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["recommended_action"] == "exclude-from-learning"
    assert "details" not in data
    assert data["details_summary"]["dataset_class"] == "stale_guidance"
    assert data["details_summary"]["finding_count"] == 1
    assert data["details_summary"]["sample_finding_ids"] == ["hygiene-stale-target"]

    job = get_job_queue().get_job(data["job_id"])
    assert job is not None
    assert job["payload"]["dataset_class"] == "stale_guidance"
    assert job["payload"]["finding_ids"] == ["hygiene-stale-target"]


@pytest.mark.asyncio
async def test_admin_lists_data_hygiene_remediations_compact_by_default(client):
    store = get_data_hygiene_store()
    job_id = await get_job_queue().submit("data_hygiene_apply_exclusion", {"finding_ids": ["a", "b"], "records": []})
    store.queue_remediation(
        remediation_id="hyg-rem-compact",
        recommended_action="exclude-from-learning",
        store_name="qdrant_memories",
        requested_by="test",
        job_id=job_id,
        details={
            "description": "Mark records as excluded from learning.",
            "dataset_class": "stale_guidance",
            "finding_ids": ["a", "b", "c", "d", "e", "f"],
        },
    )

    resp = await client.get("/api/v1/admin/data-hygiene/remediations?limit=5")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    item = next(row for row in data["items"] if row["remediation_id"] == "hyg-rem-compact")
    assert "details" not in item
    assert item["details_summary"]["finding_count"] == 6
    assert item["details_summary"]["sample_finding_ids"] == ["a", "b", "c", "d", "e"]

    full_resp = await client.get("/api/v1/admin/data-hygiene/remediations?limit=5&detail=full")
    assert full_resp.status_code == 200, full_resp.text
    full_item = next(row for row in full_resp.json()["items"] if row["remediation_id"] == "hyg-rem-compact")
    assert full_item["details"]["finding_ids"] == ["a", "b", "c", "d", "e", "f"]


@pytest.mark.asyncio
async def test_admin_reconcile_marks_data_hygiene_findings_resolved(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-4",
        store_name="learning_events",
        record_locator="4",
        dataset_class="telemetry_trace",
        recommended_action="exclude-from-learning",
        exclude_from_learning=True,
        confidence=0.9,
        reasons=["telemetry_event:tool_call"],
        details={"event_type": "tool_call"},
    )

    queue = get_job_queue()
    job_id = await queue.submit("data_hygiene_apply_exclusion", {"finding_ids": ["hygiene-4"], "records": []})
    queue._set_done(job_id, {"finding_ids": ["hygiene-4"], "updated": 1, "skipped": 0})
    store.queue_remediation(
        remediation_id="hyg-rem-1",
        recommended_action="exclude-from-learning",
        store_name="learning_events",
        requested_by="test",
        job_id=job_id,
        details={"finding_ids": ["hygiene-4"]},
    )
    store.sync_remediations_from_jobs(queue.list_jobs(limit=50))

    resp = await client.post("/api/v1/admin/data-hygiene/reconcile")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reconciled"] >= 1
    assert data["resolved_findings"] >= 1

    finding = store.list_findings(store_name="learning_events", limit=10)[0]
    assert finding["status"] == "resolved"


@pytest.mark.asyncio
async def test_reviewed_delete_removes_quarantined_learning_events_only(client):
    store = get_data_hygiene_store()
    learning_store = get_learning_store()
    event_id = await learning_store.write_event(
        event_type="tool_call",
        agent_id="pytest-agent",
        project="mnemoforge",
        context_signature="project=mnemoforge;category=memory_search",
        payload={"tool_name": "memory_search"},
    )
    store.upsert_finding(
        finding_id="hygiene-delete-1",
        store_name="learning_events",
        record_locator=str(event_id),
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.98,
        reasons=["synthetic_marker:pytest"],
        details={"event_type": "tool_call"},
    )
    store.set_finding_status(finding_id="hygiene-delete-1", status="quarantine_candidate")

    resp = await client.post("/api/v1/admin/data-hygiene/remediate-reviewed-delete?requested_by=tester")
    assert resp.status_code == 200, resp.text
    remediation = resp.json()
    assert remediation["recommended_action"] == "delete-reviewed"

    queue = get_job_queue()
    job = queue.get_job(remediation["job_id"])
    assert job is not None
    result = await apply_reviewed_delete(job["payload"])
    queue._set_done(remediation["job_id"], result)
    store.sync_remediations_from_jobs(queue.list_jobs(limit=50))
    rec = await client.post("/api/v1/admin/data-hygiene/reconcile")
    assert rec.status_code == 200, rec.text

    events = await learning_store.list_events(limit=20)
    assert all(str(item["id"]) != str(event_id) for item in events)
    finding = store.list_findings(store_name="learning_events", limit=10)[0]
    assert finding["status"] == "resolved"


@pytest.mark.asyncio
async def test_reviewed_delete_skips_when_finding_is_not_quarantine_candidate(client):
    store = get_data_hygiene_store()
    learning_store = get_learning_store()
    event_id = await learning_store.write_event(
        event_type="tool_call",
        agent_id="pytest-agent",
        project="mnemoforge",
        context_signature="project=mnemoforge;category=memory_search",
        payload={"tool_name": "memory_search"},
    )
    store.upsert_finding(
        finding_id="hygiene-delete-skip-1",
        store_name="learning_events",
        record_locator=str(event_id),
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.98,
        reasons=["synthetic_marker:pytest"],
        details={"event_type": "tool_call"},
    )

    result = await apply_reviewed_delete(
        {
            "finding_ids": ["hygiene-delete-skip-1"],
            "records": [
                {
                    "finding_id": "hygiene-delete-skip-1",
                    "store_name": "learning_events",
                    "record_locator": str(event_id),
                    "dataset_class": "synthetic_test",
                    "details": {"event_type": "tool_call"},
                }
            ],
        }
    )
    assert result["deleted"] == 0
    assert result["skipped_manual_only"] == 1

    events = await learning_store.list_events(limit=20)
    assert any(str(item["id"]) == str(event_id) for item in events)


@pytest.mark.asyncio
async def test_approved_delete_removes_quarantined_qdrant_memories_only(client):
    from app.dependencies import get_qdrant

    memory_resp = await client.post(
        "/api/v1/memories",
        json={
            "content": "Synthetic fixture memory scheduled for approved delete",
            "agent_id": "pytest-agent",
            "memory_type": "fact",
            "category": "general",
            "source": "demo-fixture",
            "tags": ["synthetic", "fixture"],
        },
    )
    assert memory_resp.status_code == 201, memory_resp.text
    memory_id = memory_resp.json()["id"]

    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-delete-qdrant-1",
        store_name="qdrant_memories",
        record_locator=memory_id,
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.99,
        reasons=["synthetic_marker:fixture"],
        details={"category": "general"},
    )

    status_resp = await client.post(
        "/api/v1/admin/data-hygiene/findings/hygiene-delete-qdrant-1/status?status=quarantined"
    )
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["status"] == "quarantined"

    dry_run_resp = await client.get("/api/v1/admin/data-hygiene/delete-dry-run")
    assert dry_run_resp.status_code == 200, dry_run_resp.text
    dry_run = dry_run_resp.json()
    assert dry_run["candidate_count"] >= 1
    assert any(item["finding_id"] == "hygiene-delete-qdrant-1" for item in dry_run["sample"])

    memory_store = get_memory_store()
    await memory_store.upsert(
        memory_id=memory_id,
        category="general",
        content="shadow content record for approved delete coverage",
        metadata={"test": True},
    )
    assert await memory_store.exists(memory_id) is True

    remediation_resp = await client.post("/api/v1/admin/data-hygiene/remediate-approved-delete?requested_by=tester")
    assert remediation_resp.status_code == 200, remediation_resp.text
    remediation = remediation_resp.json()
    assert remediation["recommended_action"] == "delete-approved"

    queue = get_job_queue()
    job = queue.get_job(remediation["job_id"])
    assert job is not None
    result = await apply_approved_delete(job["payload"], get_qdrant())
    queue._set_done(remediation["job_id"], result)
    store.sync_remediations_from_jobs(queue.list_jobs(limit=50))

    rec = await client.post("/api/v1/admin/data-hygiene/reconcile")
    assert rec.status_code == 200, rec.text

    get_resp = await client.get(f"/api/v1/memories/{memory_id}")
    assert get_resp.status_code == 404, get_resp.text
    assert await memory_store.exists(memory_id) is False

    finding = store.list_findings(store_name="qdrant_memories", limit=10)[0]
    assert finding["status"] == "resolved"


@pytest.mark.asyncio
async def test_approved_delete_skips_when_finding_is_not_quarantined(client):
    from app.dependencies import get_qdrant

    memory_resp = await client.post(
        "/api/v1/memories",
        json={
            "content": "Synthetic fixture memory should not be deleted before quarantine",
            "agent_id": "pytest-agent",
            "memory_type": "fact",
            "category": "general",
            "source": "demo-fixture",
            "tags": ["synthetic", "fixture"],
        },
    )
    assert memory_resp.status_code == 201, memory_resp.text
    memory_id = memory_resp.json()["id"]

    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-delete-skip-qdrant-1",
        store_name="qdrant_memories",
        record_locator=memory_id,
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.99,
        reasons=["synthetic_marker:fixture"],
        details={"category": "general"},
    )
    store.set_finding_status(finding_id="hygiene-delete-skip-qdrant-1", status="quarantine_candidate")

    result = await apply_approved_delete(
        {
            "finding_ids": ["hygiene-delete-skip-qdrant-1"],
            "records": [
                {
                    "finding_id": "hygiene-delete-skip-qdrant-1",
                    "store_name": "qdrant_memories",
                    "record_locator": memory_id,
                    "dataset_class": "synthetic_test",
                    "details": {"category": "general"},
                }
            ],
        },
        get_qdrant(),
    )
    assert result["deleted"] == 0
    assert result["skipped_manual_only"] == 1

    get_resp = await client.get(f"/api/v1/memories/{memory_id}")
    assert get_resp.status_code == 200, get_resp.text


@pytest.mark.asyncio
async def test_ai_resolve_hygiene_blocks_qdrant_mutation_when_qdrant_scan_error_exists(client):
    store = get_data_hygiene_store()
    store.record_audit(
        audit_id="audit-ai-resolve-1",
        status="warning",
        started_at=10.0,
        finished_at=11.0,
        total_records=2,
        classified={"synthetic_test": 1},
        actions={"archive": 1, "exclude-from-learning": 1},
        details={
            "memory_scan_source": "sqlite_memory_store_fallback",
            "qdrant_scan_error": "simulated qdrant panic during hygiene audit",
        },
    )
    store.upsert_finding(
        finding_id="hygiene-ai-qdrant-1",
        store_name="qdrant_memories",
        record_locator="memory-qdrant-1",
        dataset_class="temporary_projection",
        recommended_action="archive",
        exclude_from_learning=True,
        confidence=0.82,
        reasons=["derived_projection_record"],
        details={"category": "doc_section"},
    )
    store.upsert_finding(
        finding_id="hygiene-ai-learning-1",
        store_name="learning_events",
        record_locator="191",
        dataset_class="raw_dialogue_trace",
        recommended_action="exclude-from-learning",
        exclude_from_learning=True,
        confidence=0.8,
        reasons=["raw_dialogue_trace"],
        details={"event_type": "dialogue_signal"},
    )

    resp = await client.post("/api/v1/admin/data-hygiene/ai-resolve?auto_apply_safe=true&requested_by=ai-tester&limit=20")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "simulated qdrant panic" in data["plan"]["qdrant_scan_error"]
    assert data["queued_count"] >= 1
    assert any(
        item["recommended_action"] == "exclude-from-learning" and item["store_name"] == "learning_events"
        for item in data["queued_remediations"]
    )
    assert any(
        item["recommended_action"] == "archive"
        and item["store_name"] == "qdrant_memories"
        and item["reason"] == "qdrant_scan_error"
        for item in data["skipped_candidates"]
    )
    assert any(
        item["recommended_action"] == "archive"
        and item["store_name"] == "qdrant_memories"
        and item["auto_apply_allowed"] is False
        for item in data["plan"]["safe_remediation_candidates"]
    )


@pytest.mark.asyncio
async def test_ai_resolve_hygiene_plan_only_does_not_queue_jobs(client):
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="hygiene-ai-plan-only-1",
        store_name="learning_events",
        record_locator="211",
        dataset_class="raw_dialogue_trace",
        recommended_action="exclude-from-learning",
        exclude_from_learning=True,
        confidence=0.81,
        reasons=["raw_dialogue_trace"],
        details={"event_type": "dialogue_signal"},
    )

    resp = await client.post("/api/v1/admin/data-hygiene/ai-resolve?auto_apply_safe=false&limit=10")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["auto_apply_safe"] is False
    assert data["queued_count"] == 0
    assert data["queued_remediations"] == []
    assert any(
        item["recommended_action"] == "exclude-from-learning" and item["store_name"] == "learning_events"
        for item in data["plan"]["safe_remediation_candidates"]
    )


def test_auto_test_cleanup_candidate_detection_prefers_explicit_markers():
    assert is_auto_test_cleanup_candidate(
        {
            "dataset_class": "synthetic_test",
            "recommended_action": "delete",
            "reasons": ["synthetic_marker:pytest"],
            "details": {"source": "demo-fixture", "tags": ["fixture"]},
        }
    ) is True
    assert is_auto_test_cleanup_candidate(
        {
            "dataset_class": "synthetic_test",
            "recommended_action": "delete",
            "reasons": ["synthetic_marker:unknown-marker"],
            "details": {"source": "production-pipeline", "tags": ["entity:task"]},
        }
    ) is False


def test_promote_auto_test_cleanup_candidates_sets_delete_ready_statuses():
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="auto-clean-learning-1",
        store_name="learning_events",
        record_locator="301",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:pytest"],
        details={"event_type": "tool_call"},
    )
    store.upsert_finding(
        finding_id="auto-clean-qdrant-1",
        store_name="qdrant_memories",
        record_locator="memory-301",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:fixture"],
        details={"source": "demo-fixture", "tags": ["fixture"]},
    )

    result = promote_auto_test_cleanup_candidates(limit=20, include_qdrant=True, include_learning_events=True)
    assert result["eligible"] >= 2
    assert result["updated"] >= 2
    assert result["ready_for_reviewed_delete"] >= 1
    assert result["ready_for_approved_delete"] >= 1

    learning_item = store.get_finding("auto-clean-learning-1")
    qdrant_item = store.get_finding("auto-clean-qdrant-1")
    assert learning_item["status"] == "quarantine_candidate"
    assert qdrant_item["status"] == "quarantined"


def test_promote_auto_test_cleanup_candidates_can_skip_qdrant_side():
    store = get_data_hygiene_store()
    store.upsert_finding(
        finding_id="auto-clean-qdrant-skip-1",
        store_name="qdrant_memories",
        record_locator="memory-302",
        dataset_class="synthetic_test",
        recommended_action="delete",
        exclude_from_learning=True,
        confidence=0.95,
        reasons=["synthetic_marker:fixture"],
        details={"source": "demo-fixture", "tags": ["fixture"]},
    )

    result = promote_auto_test_cleanup_candidates(limit=20, include_qdrant=False, include_learning_events=True)
    assert result["ready_for_approved_delete"] == 0
    assert store.get_finding("auto-clean-qdrant-skip-1")["status"] == "open"
