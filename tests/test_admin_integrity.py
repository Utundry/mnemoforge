import pytest
from qdrant_client.http import models as qmodels

from app.config import settings
from app.dependencies import get_qdrant
from app.services.data_integrity_service import build_auto_discovery_guard
from app.services.data_integrity_service import build_auto_remediation_guard
from app.services.data_integrity_service import GENERIC_MEMORY_FILTER_SLICE_ID
from app.services.data_integrity_service import CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID
from app.services.data_integrity_service import DOC_SECTION_STATUS_FILTER_SLICE_ID
from app.services.data_integrity_service import get_data_integrity_store
from app.services.data_integrity_service import HANDOFF_STATUS_FILTER_SLICE_ID
from app.services.data_integrity_service import maybe_auto_discover_slice
from app.services.data_integrity_service import SKILL_DOMAIN_TAGS_FILTER_SLICE_ID
from app.services.data_integrity_service import TASK_MEMOIR_TAG_FILTER_SLICE_ID
from app.services.job_queue import get_job_queue
from app.services.memory_store import get_memory_store


@pytest.mark.asyncio
async def test_admin_status_includes_integrity(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )

    resp = await client.get("/api/v1/admin/status")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["integrity"]["status"] == "degraded"
    assert "qdrant.skill_domain_tags_filter" in data["integrity"]["degraded_slices"]


def test_auto_remediation_guard_allows_when_no_recent_remediation():
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )

    guard = build_auto_remediation_guard(SKILL_DOMAIN_TAGS_FILTER_SLICE_ID, cooldown_seconds=3600.0)
    assert guard["allowed"] is True
    assert guard["reason"] == "no_recent_remediation"


def test_auto_remediation_guard_allows_when_slice_is_healthy_but_has_active_findings():
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="healthy",
        source="test",
    )
    store.upsert_finding(
        finding_id="healthy-slice-finding",
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        category="skill",
        record_id="skill-healthy-attention",
        suspicion_type="missing_domain_tags",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag"},
    )

    guard = build_auto_remediation_guard(SKILL_DOMAIN_TAGS_FILTER_SLICE_ID, cooldown_seconds=3600.0)
    assert guard["allowed"] is True
    assert guard["reason"] == "no_recent_remediation"
    assert guard["slice_status"] == "healthy"
    assert guard["active_findings"] >= 1


def test_auto_discovery_guard_allows_when_detector_exists_and_no_active_findings():
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=GENERIC_MEMORY_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )

    guard = build_auto_discovery_guard(GENERIC_MEMORY_FILTER_SLICE_ID, cooldown_seconds=3600.0)
    assert guard["allowed"] is True
    assert guard["reason"] == "no_recent_auto_discovery"


def test_auto_discovery_guard_blocks_when_active_findings_exist():
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=GENERIC_MEMORY_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )
    store.upsert_finding(
        finding_id="generic-discovery-block",
        slice_id=GENERIC_MEMORY_FILTER_SLICE_ID,
        category="memory",
        record_id="memory-block",
        suspicion_type="missing_source",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "qdrant_reindex_from_sqlite"},
    )

    guard = build_auto_discovery_guard(GENERIC_MEMORY_FILTER_SLICE_ID, cooldown_seconds=3600.0)
    assert guard["allowed"] is False
    assert guard["reason"] == "active_findings_exist"
    assert guard["active_findings"] >= 1


def test_auto_remediation_guard_blocks_when_remediation_is_active():
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )
    store.queue_remediation(
        remediation_id="active-remediation",
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        action_type="qdrant_reindex_from_sqlite",
        requested_by="test",
        job_id="job-active",
        details={"description": "Reindex"},
    )

    guard = build_auto_remediation_guard(SKILL_DOMAIN_TAGS_FILTER_SLICE_ID, cooldown_seconds=3600.0)
    assert guard["allowed"] is False
    assert guard["reason"] == "active_remediation_exists"
    assert guard["active_action_type"] == "qdrant_reindex_from_sqlite"


def test_auto_remediation_guard_respects_cooldown_after_recent_completion():
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )
    store.queue_remediation(
        remediation_id="done-remediation",
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        action_type="qdrant_reindex_from_sqlite",
        requested_by="test",
        job_id="job-done",
        details={"description": "Reindex"},
    )
    store.sync_remediation_status(remediation_id="done-remediation", status="done")

    blocked = build_auto_remediation_guard(SKILL_DOMAIN_TAGS_FILTER_SLICE_ID, cooldown_seconds=3600.0)
    assert blocked["allowed"] is False
    assert blocked["reason"] == "cooldown_active"

    allowed = build_auto_remediation_guard(SKILL_DOMAIN_TAGS_FILTER_SLICE_ID, cooldown_seconds=0.0)
    assert allowed["allowed"] is True
    assert allowed["reason"] == "cooldown_elapsed"


@pytest.mark.asyncio
async def test_maybe_auto_discover_slice_records_discovery_metadata(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=GENERIC_MEMORY_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="generic memory filter failed",
    )
    await get_memory_store().upsert(
        "memory-auto-discovery-1",
        "memory",
        "Generic memory content that is missing core metadata.",
        {
            "memory_type": "fact",
            "category": "general",
            "source": "",
            "timestamp": "",
        },
    )

    result = await maybe_auto_discover_slice(
        GENERIC_MEMORY_FILTER_SLICE_ID,
        limit=20,
        cooldown_seconds=3600.0,
    )
    assert result["performed"] is True
    assert result["discovered"] >= 1

    slice_info = get_data_integrity_store().get_slice(GENERIC_MEMORY_FILTER_SLICE_ID)
    details = (slice_info or {}).get("details", {})
    assert float(details.get("auto_discovery_checked_at") or 0.0) > 0
    assert int(details.get("auto_discovery_discovered") or 0) >= 1
    assert str(details.get("auto_discovery_last_error") or "") == ""

    guard = build_auto_discovery_guard(GENERIC_MEMORY_FILTER_SLICE_ID, cooldown_seconds=3600.0)
    assert guard["allowed"] is False
    assert guard["reason"] in {"active_findings_exist", "cooldown_active"}


@pytest.mark.asyncio
async def test_admin_integrity_endpoint_returns_slice_overview(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )

    resp = await client.get("/api/v1/admin/integrity")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["degraded_count"] == 1
    assert SKILL_DOMAIN_TAGS_FILTER_SLICE_ID in data["recommended_remediations"]


@pytest.mark.asyncio
async def test_admin_integrity_endpoint_includes_actionable_slice_with_findings_even_when_healthy(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="healthy",
        source="test",
    )
    store.upsert_finding(
        finding_id="healthy-actionable-finding",
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        category="skill",
        record_id="skill-actionable",
        suspicion_type="missing_domain_tags",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag"},
    )

    resp = await client.get("/api/v1/admin/integrity")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert SKILL_DOMAIN_TAGS_FILTER_SLICE_ID in data["actionable_slices"]
    assert SKILL_DOMAIN_TAGS_FILTER_SLICE_ID in data["recommended_remediations"]


@pytest.mark.asyncio
async def test_admin_integrity_recommendations_include_generic_memory_reindex(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=GENERIC_MEMORY_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="generic memory filter failed",
    )

    resp = await client.get("/api/v1/admin/integrity")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    recommendations = data["recommended_remediations"][GENERIC_MEMORY_FILTER_SLICE_ID]
    assert recommendations[0]["action_type"] == "qdrant_reindex_from_sqlite"
    assert recommendations[0]["payload"]["targets"] == ["memory"]


@pytest.mark.asyncio
async def test_integrity_recommendations_escalate_after_done_remediation_that_did_not_clear_slice(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="still broken after retag",
    )
    store.queue_remediation(
        remediation_id="rem-done",
        slice_id="qdrant.skill_domain_tags_filter",
        action_type="skills_retag",
        requested_by="test",
        job_id="job-done",
        details={"description": "Retag skills"},
    )
    store.sync_remediation_status(remediation_id="rem-done", status="done")
    store.patch_remediation_details(
        remediation_id="rem-done",
        patch={"closure_summary": {"repaired_findings": 0}},
    )

    recommendations = store.recommended_remediations("qdrant.skill_domain_tags_filter")

    assert recommendations[0]["action_type"] == "manual_forensic_audit"
    assert recommendations[0]["escalated_after"] == "skills_retag"
    assert recommendations[1]["action_type"] == "qdrant_reindex_from_sqlite"
    assert recommendations[2]["action_type"] == "skills_retag"


@pytest.mark.asyncio
async def test_admin_integrity_recommendations_include_handoff_status_repair(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="handoff status filter failed",
    )

    resp = await client.get("/api/v1/admin/integrity")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    recommendations = data["recommended_remediations"][HANDOFF_STATUS_FILTER_SLICE_ID]
    assert recommendations[0]["action_type"] == "qdrant_reindex_from_sqlite"
    assert recommendations[0]["payload"]["targets"] == ["handoff"]
    assert recommendations[1]["action_type"] == "handoff_repair_status"


@pytest.mark.asyncio
async def test_admin_integrity_recommendations_include_doc_section_reindex(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=DOC_SECTION_STATUS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="doc_section status filter failed",
    )

    resp = await client.get("/api/v1/admin/integrity")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    recommendations = data["recommended_remediations"][DOC_SECTION_STATUS_FILTER_SLICE_ID]
    assert recommendations[0]["action_type"] == "qdrant_reindex_from_sqlite"
    assert recommendations[0]["payload"]["targets"] == ["doc_section"]


@pytest.mark.asyncio
async def test_admin_can_queue_integrity_remediation(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )

    resp = await client.post("/api/v1/admin/integrity/remediate/qdrant.skill_domain_tags_filter")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == "qdrant.skill_domain_tags_filter"
    assert data["action_type"] == "qdrant_reindex_from_sqlite"
    assert data["status"] == "queued"
    assert data["job_id"]

    job = get_job_queue().get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "qdrant_reindex_from_sqlite"
    assert job["payload"]["targets"] == ["skill"]

    overview = get_data_integrity_store().overview()
    assert len(overview["active_remediations"]) == 1


@pytest.mark.asyncio
async def test_admin_integrity_remediation_re_registers_skills_retag_handler_if_missing(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )
    store.upsert_finding(
        finding_id="skill-retag-reregister",
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        category="skill",
        record_id="skill-retag-reregister",
        suspicion_type="missing_domain_tags",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag", "reason": "domain_tags empty"},
    )

    queue = get_job_queue()
    queue._handlers.pop("skills_retag", None)

    resp = await client.post(f"/api/v1/admin/integrity/remediate/{SKILL_DOMAIN_TAGS_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action_type"] == "skills_retag"
    assert "skills_retag" in queue._handlers

    job = queue.get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "skills_retag"


@pytest.mark.asyncio
async def test_admin_integrity_remediation_re_registers_qdrant_reindex_handler_if_missing(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )

    queue = get_job_queue()
    queue._handlers.pop("qdrant_reindex_from_sqlite", None)

    resp = await client.post(f"/api/v1/admin/integrity/remediate/{SKILL_DOMAIN_TAGS_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action_type"] == "qdrant_reindex_from_sqlite"
    assert "qdrant_reindex_from_sqlite" in queue._handlers

    job = queue.get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "qdrant_reindex_from_sqlite"
    assert job["payload"]["targets"] == ["skill"]


@pytest.mark.asyncio
async def test_admin_manual_remediation_discovers_findings_before_selecting_action(client):
    await get_memory_store().upsert(
        "skill-manual-discovery",
        "skill",
        "# Broken Skill\n\nBare content.",
        {
            "skill_name": "unknown",
            "description": "",
            "platform": "claude",
            "domain_tags": [],
        },
    )
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="skill filter failed",
    )

    resp = await client.post(f"/api/v1/admin/integrity/remediate/{SKILL_DOMAIN_TAGS_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action_type"] == "skills_retag"
    assert data["details"]["source"] == "recommended_by_findings"
    assert "skill-manual-discovery" in data["details"]["payload"]["record_ids"]

    job = get_job_queue().get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "skills_retag"
    assert "skill-manual-discovery" in job["payload"]["record_ids"]


@pytest.mark.asyncio
async def test_admin_recommended_remediation_prefers_action_with_active_findings(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="handoff target issue",
    )
    store.upsert_finding(
        finding_id="handoff-recommended-target",
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        category="handoff",
        record_id="00000000-0000-0000-0000-00000000d066",
        suspicion_type="missing_handoff_target",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "handoff_repair_target", "reason": "to:<agent> missing"},
    )

    resp = await client.post(f"/api/v1/admin/integrity/remediate/{HANDOFF_STATUS_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == HANDOFF_STATUS_FILTER_SLICE_ID
    assert data["action_type"] == "handoff_repair_target"
    assert data["details"]["source"] == "recommended_by_findings"
    assert "00000000-0000-0000-0000-00000000d066" in data["details"]["payload"]["record_ids"]

    job = get_job_queue().get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "handoff_repair_target"
    assert "00000000-0000-0000-0000-00000000d066" in job["payload"]["record_ids"]


@pytest.mark.asyncio
async def test_admin_recommended_remediation_returns_400_when_only_manual_action_exists(client):
    store = get_data_integrity_store()
    slice_id = "qdrant.custom_manual_only"
    store.upsert_slice(
        slice_id=slice_id,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="manual follow-up required",
    )
    store.queue_remediation(
        remediation_id="manual-only-rem",
        slice_id=slice_id,
        action_type="custom_repair",
        requested_by="test",
        job_id="manual-only-job",
        details={"description": "Previous custom repair"},
    )
    store.sync_remediation_status(remediation_id="manual-only-rem", status="done")
    store.patch_remediation_details(
        remediation_id="manual-only-rem",
        patch={"closure_summary": {"repaired_findings": 0}},
    )

    resp = await client.post(f"/api/v1/admin/integrity/remediate/{slice_id}")
    assert resp.status_code == 400, resp.text
    data = resp.json()
    assert "No background remediation is available" in data["detail"]


@pytest.mark.asyncio
async def test_admin_lists_integrity_remediations(client):
    store = get_data_integrity_store()
    queued = store.queue_remediation(
        remediation_id="rem-1",
        slice_id="qdrant.skill_domain_tags_filter",
        action_type="skills_retag",
        requested_by="test",
        job_id="job-1",
        details={"description": "Retag skills"},
    )

    resp = await client.get("/api/v1/admin/integrity/remediations")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["remediation_id"] == queued["remediation_id"]


@pytest.mark.asyncio
async def test_admin_can_upsert_and_list_integrity_rules(client):
    create_resp = await client.post(
        "/api/v1/admin/integrity/rules",
        json={
            "slice_id": "qdrant.skill_domain_tags_filter",
            "description": "Treat domain_tags filter failures as schema-drift candidates before repeating automated repair.",
            "guidance": {"action_hint": "Inspect suspect skill payload shapes before rerunning automated retag."},
            "priority": 10,
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    created = create_resp.json()
    assert created["slice_id"] == "qdrant.skill_domain_tags_filter"
    assert created["priority"] == 10

    list_resp = await client.get("/api/v1/admin/integrity/rules?slice_id=qdrant.skill_domain_tags_filter")
    assert list_resp.status_code == 200, list_resp.text
    data = list_resp.json()
    assert data["total"] == 1
    assert data["items"][0]["description"].startswith("Treat domain_tags filter failures")


@pytest.mark.asyncio
async def test_integrity_audit_checks_all_registered_slices(client):
    resp = await client.post("/api/v1/admin/integrity/audit")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    checked = {item["slice_id"] for item in data["checks"]}
    assert GENERIC_MEMORY_FILTER_SLICE_ID in checked
    assert "qdrant.skill_domain_tags_filter" in checked
    assert HANDOFF_STATUS_FILTER_SLICE_ID in checked
    assert CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID in checked
    assert DOC_SECTION_STATUS_FILTER_SLICE_ID in checked
    assert TASK_MEMOIR_TAG_FILTER_SLICE_ID in checked


@pytest.mark.asyncio
async def test_integrity_audit_marks_skill_slice_degraded_when_qdrant_scroll_panics(client, monkeypatch):
    qdrant = get_qdrant()
    real_scroll = qdrant._client.scroll

    async def fail_skill_scroll(*args, **kwargs):
        scroll_filter = kwargs.get("scroll_filter")
        must_conditions = list(getattr(scroll_filter, "must", []) or [])
        for condition in must_conditions:
            if getattr(condition, "key", "") == "domain_tags":
                raise RuntimeError("simulated qdrant panic for skill filter")
        return await real_scroll(*args, **kwargs)

    monkeypatch.setattr(qdrant._client, "scroll", fail_skill_scroll)

    resp = await client.post("/api/v1/admin/integrity/audit")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    checks = {item["slice_id"]: item for item in data["checks"]}
    assert checks[SKILL_DOMAIN_TAGS_FILTER_SLICE_ID]["status"] == "degraded"
    assert GENERIC_MEMORY_FILTER_SLICE_ID in checks
    assert HANDOFF_STATUS_FILTER_SLICE_ID in checks
    assert CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID in checks
    assert DOC_SECTION_STATUS_FILTER_SLICE_ID in checks
    assert TASK_MEMOIR_TAG_FILTER_SLICE_ID in checks

    overview = get_data_integrity_store().overview()
    assert SKILL_DOMAIN_TAGS_FILTER_SLICE_ID in overview["degraded_slices"]


@pytest.mark.asyncio
async def test_admin_discover_integrity_findings_for_skill_slice(client):
    await get_memory_store().upsert(
        "skill-1",
        "skill",
        "# Broken Skill\n\nBare content.",
        {
            "skill_name": "unknown",
            "description": "",
            "platform": "claude",
            "domain_tags": [],
        },
    )

    resp = await client.post("/api/v1/admin/integrity/discover/qdrant.skill_domain_tags_filter")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == "qdrant.skill_domain_tags_filter"
    assert data["discovered"] >= 2

    findings_resp = await client.get("/api/v1/admin/integrity/findings?slice_id=qdrant.skill_domain_tags_filter")
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()["items"]
    suspicion_types = {item["suspicion_type"] for item in findings}
    assert "missing_domain_tags" in suspicion_types
    assert "unknown_skill_name" in suspicion_types


@pytest.mark.asyncio
async def test_admin_discover_integrity_findings_for_generic_memory_slice(client):
    await get_memory_store().upsert(
        "memory-generic-1",
        "memory",
        "Generic memory content that is missing core metadata.",
        {
            "memory_type": "fact",
            "category": "general",
            "source": "",
            "timestamp": "",
        },
    )

    resp = await client.post(f"/api/v1/admin/integrity/discover/{GENERIC_MEMORY_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == GENERIC_MEMORY_FILTER_SLICE_ID
    assert data["discovered"] >= 2

    findings_resp = await client.get(f"/api/v1/admin/integrity/findings?slice_id={GENERIC_MEMORY_FILTER_SLICE_ID}")
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()["items"]
    suspicion_types = {item["suspicion_type"] for item in findings}
    assert "missing_agent_id" in suspicion_types
    assert "missing_source" in suspicion_types
    by_type = {item["suspicion_type"]: item for item in findings}
    assert by_type["missing_source"]["details"]["suggested_repair"] == "qdrant_reindex_from_sqlite"


@pytest.mark.asyncio
async def test_admin_discover_integrity_findings_for_code_component_slice(client):
    await get_memory_store().upsert(
        "code-component-1",
        "code_component",
        "def broken():\n    pass\n",
        {
            "category": "code_component",
            "agent_id": "code-search",
            "memory_type": "context",
            "code_path": "",
            "code_language": "",
        },
    )

    resp = await client.post(f"/api/v1/admin/integrity/discover/{CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID
    assert data["discovered"] >= 2

    findings_resp = await client.get(f"/api/v1/admin/integrity/findings?slice_id={CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID}")
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()["items"]
    suspicion_types = {item["suspicion_type"] for item in findings}
    assert "missing_code_path" in suspicion_types
    assert "missing_code_language" in suspicion_types


@pytest.mark.asyncio
async def test_admin_discover_integrity_findings_for_doc_section_slice(client):
    await get_memory_store().upsert(
        "doc-section-1",
        "doc_section",
        "Broken doc section projection.",
        {
            "category": "doc_section",
            "agent_id": "system",
            "memory_type": "procedural",
            "status": "draft",
            "project": "",
        },
    )

    resp = await client.post(f"/api/v1/admin/integrity/discover/{DOC_SECTION_STATUS_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == DOC_SECTION_STATUS_FILTER_SLICE_ID
    assert data["discovered"] >= 2

    findings_resp = await client.get(f"/api/v1/admin/integrity/findings?slice_id={DOC_SECTION_STATUS_FILTER_SLICE_ID}")
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()["items"]
    suspicion_types = {item["suspicion_type"] for item in findings}
    assert "invalid_doc_section_status" in suspicion_types
    assert "missing_doc_section_project" in suspicion_types


@pytest.mark.asyncio
async def test_admin_discover_integrity_findings_for_task_memoir_slice(client):
    await get_memory_store().upsert(
        "task-memoir-1",
        "task_memoir",
        "# Memoir\n\nBroken metadata.",
        {
            "category": "task_memoir",
            "agent_id": "codex",
            "memory_type": "experience",
            "tags": ["project:supermemory"],
            "meta": {},
        },
    )

    resp = await client.post(f"/api/v1/admin/integrity/discover/{TASK_MEMOIR_TAG_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == TASK_MEMOIR_TAG_FILTER_SLICE_ID
    assert data["discovered"] >= 2

    findings_resp = await client.get(f"/api/v1/admin/integrity/findings?slice_id={TASK_MEMOIR_TAG_FILTER_SLICE_ID}")
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()["items"]
    suspicion_types = {item["suspicion_type"] for item in findings}
    assert "missing_memoir_tag" in suspicion_types
    assert "missing_task_id" in suspicion_types


@pytest.mark.asyncio
async def test_admin_discover_integrity_findings_for_handoff_slice(client):
    qdrant = get_qdrant()
    handoff_id = "00000000-0000-0000-0000-00000000ab12"
    await qdrant._client.upsert(
        collection_name=settings.qdrant_collection_name,
        points=[
            qmodels.PointStruct(
                id=handoff_id,
                vector=[0.1] * settings.embedding_dimensions,
                payload={
                    "category": "handoff",
                    "tags": ["from:codex"],
                },
            )
        ],
    )

    resp = await client.post(f"/api/v1/admin/integrity/discover/{HANDOFF_STATUS_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == HANDOFF_STATUS_FILTER_SLICE_ID
    assert data["discovered"] >= 2

    findings_resp = await client.get(f"/api/v1/admin/integrity/findings?slice_id={HANDOFF_STATUS_FILTER_SLICE_ID}")
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()["items"]
    suspicion_types = {item["suspicion_type"] for item in findings}
    assert "missing_handoff_status" in suspicion_types
    assert "missing_handoff_target" in suspicion_types
    by_type = {item["suspicion_type"]: item for item in findings}
    assert by_type["missing_handoff_target"]["details"]["suggested_repair"] == "handoff_repair_target"


@pytest.mark.asyncio
async def test_admin_discover_handoff_findings_falls_back_to_sqlite_when_qdrant_scroll_fails(client, monkeypatch):
    await get_memory_store().upsert(
        "00000000-0000-0000-0000-00000000ab34",
        "memory",
        "task: fallback handoff packet",
        {
            "category": "handoff",
            "tags": ["from:codex"],
        },
    )

    qdrant = get_qdrant()

    async def fail_scroll(*args, **kwargs):
        raise RuntimeError("simulated qdrant panic during handoff discovery")

    monkeypatch.setattr(qdrant._client, "scroll", fail_scroll)

    resp = await client.post(f"/api/v1/admin/integrity/discover/{HANDOFF_STATUS_FILTER_SLICE_ID}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == HANDOFF_STATUS_FILTER_SLICE_ID
    assert data["discovered"] >= 2

    findings_resp = await client.get(f"/api/v1/admin/integrity/findings?slice_id={HANDOFF_STATUS_FILTER_SLICE_ID}")
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()["items"]
    suspicion_types = {item["suspicion_type"] for item in findings}
    assert "missing_handoff_status" in suspicion_types
    assert "missing_handoff_target" in suspicion_types

    overview = get_data_integrity_store().overview()
    assert HANDOFF_STATUS_FILTER_SLICE_ID in overview["degraded_slices"]


@pytest.mark.asyncio
async def test_admin_discover_integrity_findings_rejects_unknown_slice(client):
    resp = await client.post("/api/v1/admin/integrity/discover/qdrant.unknown_slice")
    assert resp.status_code == 400, resp.text
    data = resp.json()
    assert "No discovery detector is registered" in data["detail"]


@pytest.mark.asyncio
async def test_admin_integrity_forensics_summarizes_findings_and_recent_remediations(client):
    await get_memory_store().upsert(
        "skill-1",
        "skill",
        "# Broken Skill\n\nBare content.",
        {
            "skill_name": "unknown",
            "description": "",
            "platform": "claude",
            "domain_tags": [],
        },
    )
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )
    store.queue_remediation(
        remediation_id="rem-forensics",
        slice_id="qdrant.skill_domain_tags_filter",
        action_type="skills_retag",
        requested_by="test",
        job_id="job-forensics",
        details={"description": "Retag skills"},
    )
    store.sync_remediation_status(remediation_id="rem-forensics", status="done")
    store.upsert_rule(
        slice_id="qdrant.skill_domain_tags_filter",
        description="Preserve current suspect records for manual schema audit before repeating automated repair.",
        guidance={"action_hint": "Inspect suspect skill payload shapes before rerunning automated retag."},
        priority=5,
    )

    discover_resp = await client.post("/api/v1/admin/integrity/discover/qdrant.skill_domain_tags_filter")
    assert discover_resp.status_code == 200, discover_resp.text

    resp = await client.get("/api/v1/admin/integrity/forensics/qdrant.skill_domain_tags_filter")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == "qdrant.skill_domain_tags_filter"
    assert data["summary"]["total_findings"] >= 2
    assert "missing_domain_tags" in data["summary"]["suspicion_types"]
    assert data["recent_remediations"][0]["action_type"] == "skills_retag"
    assert data["rules"][0]["description"].startswith("Preserve current suspect records")
    assert "Inspect suspect skill payload shapes before rerunning automated retag." in data["next_actions"]
    assert data["next_actions"]


@pytest.mark.asyncio
async def test_admin_integrity_repair_plan_groups_active_findings_by_suggested_repair(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )
    store.upsert_rule(
        slice_id="qdrant.skill_domain_tags_filter",
        description="Inspect suspect records before rerunning automated repair.",
        guidance={"action_hint": "Inspect suspect skill payload shapes before rerunning automated retag."},
        priority=5,
    )
    store.upsert_finding(
        finding_id="finding-a",
        slice_id="qdrant.skill_domain_tags_filter",
        category="skill",
        record_id="skill-a",
        suspicion_type="missing_domain_tags",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag", "reason": "domain_tags empty"},
    )
    store.upsert_finding(
        finding_id="finding-b",
        slice_id="qdrant.skill_domain_tags_filter",
        category="skill",
        record_id="skill-b",
        suspicion_type="unknown_skill_name",
        confidence=0.7,
        source="test",
        details={"suggested_repair": "manual_review", "reason": "name must be checked manually"},
    )
    store.set_finding_status(finding_id="finding-b", status="quarantine_candidate")

    resp = await client.get("/api/v1/admin/integrity/repair-plan/qdrant.skill_domain_tags_filter")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == "qdrant.skill_domain_tags_filter"
    assert data["summary"]["active_findings"] >= 2
    assert "review_forensics" in data["summary"]["recommended_sequence"]
    assert "apply_active_rules" in data["summary"]["recommended_sequence"]
    action_types = {item["action_type"] for item in data["actions"]}
    assert "skills_retag" in action_types
    assert "manual_review" in action_types
    retag_action = next(item for item in data["actions"] if item["action_type"] == "skills_retag")
    assert retag_action["finding_count"] >= 1
    assert "missing_domain_tags" in retag_action["suspicion_types"]
    assert "Inspect suspect skill payload shapes before rerunning automated retag." in data["forensics"]["next_actions"]


@pytest.mark.asyncio
async def test_admin_integrity_repair_batch_preview_reports_supported_executor(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )
    store.upsert_finding(
        finding_id="finding-preview",
        slice_id="qdrant.skill_domain_tags_filter",
        category="skill",
        record_id="skill-preview",
        suspicion_type="missing_domain_tags",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag", "reason": "domain_tags empty"},
    )

    resp = await client.get("/api/v1/admin/integrity/repair-batch/qdrant.skill_domain_tags_filter?action_type=skills_retag")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["supported"] is True
    assert data["action_type"] == "skills_retag"
    assert data["finding_count"] >= 1
    assert "skill-preview" in data["record_ids"]
    assert data["executor"]["job_type"] == "skills_retag"


@pytest.mark.asyncio
async def test_admin_integrity_repair_batch_preview_supports_generic_memory_reindex(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=GENERIC_MEMORY_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="generic memory filter failed",
    )
    store.upsert_finding(
        finding_id="generic-memory-preview",
        slice_id=GENERIC_MEMORY_FILTER_SLICE_ID,
        category="memory",
        record_id="memory-preview",
        suspicion_type="missing_source",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "qdrant_reindex_from_sqlite", "reason": "source missing"},
    )

    resp = await client.get(
        f"/api/v1/admin/integrity/repair-batch/{GENERIC_MEMORY_FILTER_SLICE_ID}?action_type=qdrant_reindex_from_sqlite"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["supported"] is True
    assert data["action_type"] == "qdrant_reindex_from_sqlite"
    assert data["finding_count"] >= 1
    assert "memory-preview" in data["record_ids"]
    assert data["executor"]["job_type"] == "qdrant_reindex_from_sqlite"


@pytest.mark.asyncio
async def test_admin_integrity_repair_batch_preview_supports_handoff_status_repair(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="handoff status filter failed",
    )
    store.upsert_finding(
        finding_id="handoff-finding-preview",
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        category="handoff",
        record_id="00000000-0000-0000-0000-00000000ac11",
        suspicion_type="missing_handoff_status",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "handoff_repair_status", "reason": "status missing"},
    )

    resp = await client.get(
        f"/api/v1/admin/integrity/repair-batch/{HANDOFF_STATUS_FILTER_SLICE_ID}?action_type=handoff_repair_status"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["supported"] is True
    assert data["action_type"] == "handoff_repair_status"
    assert data["finding_count"] >= 1
    assert "00000000-0000-0000-0000-00000000ac11" in data["record_ids"]
    assert data["executor"]["job_type"] == "handoff_repair_status"


@pytest.mark.asyncio
async def test_admin_integrity_repair_batch_preview_supports_handoff_target_repair(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="handoff target filter failed",
    )
    store.upsert_finding(
        finding_id="handoff-target-preview",
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        category="handoff",
        record_id="00000000-0000-0000-0000-00000000af33",
        suspicion_type="missing_handoff_target",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "handoff_repair_target", "reason": "to:<agent> missing"},
    )

    resp = await client.get(
        f"/api/v1/admin/integrity/repair-batch/{HANDOFF_STATUS_FILTER_SLICE_ID}?action_type=handoff_repair_target"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["supported"] is True
    assert data["action_type"] == "handoff_repair_target"
    assert data["finding_count"] >= 1
    assert "00000000-0000-0000-0000-00000000af33" in data["record_ids"]
    assert data["executor"]["job_type"] == "handoff_repair_target"


@pytest.mark.asyncio
async def test_admin_can_queue_targeted_integrity_repair_batch(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )
    store.upsert_finding(
        finding_id="finding-queue",
        slice_id="qdrant.skill_domain_tags_filter",
        category="skill",
        record_id="skill-queue",
        suspicion_type="missing_domain_tags",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag", "reason": "domain_tags empty"},
    )

    resp = await client.post(
        "/api/v1/admin/integrity/repair-batch/qdrant.skill_domain_tags_filter?action_type=skills_retag&requested_by=tester&limit=10"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == "qdrant.skill_domain_tags_filter"
    assert data["action_type"] == "skills_retag"
    assert data["status"] == "queued"
    assert data["job_id"]
    assert data["details"]["source"] == "targeted_repair_batch"
    assert "skill-queue" in data["details"]["payload"]["record_ids"]

    job = get_job_queue().get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "skills_retag"
    assert "skill-queue" in job["payload"]["record_ids"]


@pytest.mark.asyncio
async def test_admin_targeted_integrity_repair_batch_rejects_unsupported_action(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )

    resp = await client.post(
        "/api/v1/admin/integrity/repair-batch/qdrant.skill_domain_tags_filter?action_type=unknown_action&requested_by=tester&limit=10"
    )
    assert resp.status_code == 400, resp.text
    data = resp.json()
    assert "No active findings currently map to this repair action." in data["detail"]


@pytest.mark.asyncio
async def test_admin_can_queue_targeted_handoff_status_repair_batch(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="handoff status filter failed",
    )
    store.upsert_finding(
        finding_id="handoff-finding-queue",
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        category="handoff",
        record_id="00000000-0000-0000-0000-00000000ad22",
        suspicion_type="missing_handoff_status",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "handoff_repair_status", "reason": "status missing"},
    )

    resp = await client.post(
        f"/api/v1/admin/integrity/repair-batch/{HANDOFF_STATUS_FILTER_SLICE_ID}?action_type=handoff_repair_status&requested_by=tester&limit=10"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == HANDOFF_STATUS_FILTER_SLICE_ID
    assert data["action_type"] == "handoff_repair_status"
    assert data["status"] == "queued"
    assert data["job_id"]
    assert "00000000-0000-0000-0000-00000000ad22" in data["details"]["payload"]["record_ids"]

    job = get_job_queue().get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "handoff_repair_status"
    assert "00000000-0000-0000-0000-00000000ad22" in job["payload"]["record_ids"]


@pytest.mark.asyncio
async def test_admin_can_queue_targeted_handoff_target_repair_batch(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="handoff target filter failed",
    )
    store.upsert_finding(
        finding_id="handoff-target-queue",
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        category="handoff",
        record_id="00000000-0000-0000-0000-00000000b044",
        suspicion_type="missing_handoff_target",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "handoff_repair_target", "reason": "to:<agent> missing"},
    )

    resp = await client.post(
        f"/api/v1/admin/integrity/repair-batch/{HANDOFF_STATUS_FILTER_SLICE_ID}?action_type=handoff_repair_target&requested_by=tester&limit=10"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slice_id"] == HANDOFF_STATUS_FILTER_SLICE_ID
    assert data["action_type"] == "handoff_repair_target"
    assert data["status"] == "queued"
    assert data["job_id"]
    assert "00000000-0000-0000-0000-00000000b044" in data["details"]["payload"]["record_ids"]

    job = get_job_queue().get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "handoff_repair_target"
    assert "00000000-0000-0000-0000-00000000b044" in job["payload"]["record_ids"]


@pytest.mark.asyncio
async def test_admin_integrity_remediation_outcome_reports_targeted_delta(client):
    await get_memory_store().upsert(
        "skill-outcome",
        "skill",
        "# Healthy Skill\n\nUseful content with enough detail.",
        {
            "skill_name": "healthy-skill",
            "description": "Healthy description",
            "platform": "claude",
            "domain_tags": ["python"],
        },
    )
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )
    store.upsert_finding(
        finding_id="finding-outcome",
        slice_id="qdrant.skill_domain_tags_filter",
        category="skill",
        record_id="skill-outcome",
        suspicion_type="unknown_skill_name",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag"},
    )

    queue = get_job_queue()
    job_id = await queue.submit("skills_retag", {"limit": 1, "record_ids": ["skill-outcome"]})
    queue._set_done(job_id, {"details": [{"id": "skill-outcome", "name": "healthy-skill", "domains": ["python"]}]})
    store.queue_remediation(
        remediation_id="rem-outcome",
        slice_id="qdrant.skill_domain_tags_filter",
        action_type="skills_retag",
        requested_by="tester",
        job_id=job_id,
        details={
            "description": "Targeted retag",
            "payload": {"limit": 1, "record_ids": ["skill-outcome"]},
            "source": "targeted_repair_batch",
        },
    )
    store.sync_remediations_from_jobs(queue.list_jobs(limit=50))

    reconcile_resp = await client.post("/api/v1/admin/integrity/reconcile")
    assert reconcile_resp.status_code == 200, reconcile_resp.text

    resp = await client.get("/api/v1/admin/integrity/remediations/rem-outcome/outcome")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["remediation_id"] == "rem-outcome"
    assert data["summary"]["attempted_record_count"] == 1
    assert data["summary"]["fixed_record_count"] == 1
    assert data["summary"]["unresolved_record_count"] == 0
    assert data["summary"]["repaired_findings"] == 1
    assert data["targeted_record_ids"] == ["skill-outcome"]
    assert data["fixed_ids"] == ["skill-outcome"]
    assert data["unresolved_record_ids"] == []


@pytest.mark.asyncio
async def test_admin_integrity_remediation_outcome_supports_qdrant_reindex(client):
    await get_memory_store().upsert(
        "skill-reindex-outcome",
        "skill",
        "# Healthy Skill\n\nUseful content with enough detail.",
        {
            "skill_name": "healthy-skill",
            "description": "Healthy description",
            "platform": "claude",
            "domain_tags": ["python"],
        },
    )
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="skill filter failed",
    )
    store.upsert_finding(
        finding_id="skill-reindex-finding",
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        category="skill",
        record_id="skill-reindex-outcome",
        suspicion_type="missing_domain_tags",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag"},
    )

    queue = get_job_queue()
    job_id = await queue.submit(
        "qdrant_reindex_from_sqlite",
        {"limit": 1, "targets": ["skill"], "record_ids": ["skill-reindex-outcome"]},
    )
    queue._set_done(job_id, {"upserted_ids": ["skill-reindex-outcome"], "by_target": {"skill": {"upserted_ids": ["skill-reindex-outcome"]}}})
    store.queue_remediation(
        remediation_id="rem-skill-reindex-outcome",
        slice_id=SKILL_DOMAIN_TAGS_FILTER_SLICE_ID,
        action_type="qdrant_reindex_from_sqlite",
        requested_by="tester",
        job_id=job_id,
        details={
            "description": "Reindex skill slice from SQLite",
            "payload": {"limit": 1, "targets": ["skill"], "record_ids": ["skill-reindex-outcome"]},
            "source": "recommended_by_findings",
        },
    )
    store.sync_remediations_from_jobs(queue.list_jobs(limit=50))

    reconcile_resp = await client.post("/api/v1/admin/integrity/reconcile")
    assert reconcile_resp.status_code == 200, reconcile_resp.text

    resp = await client.get("/api/v1/admin/integrity/remediations/rem-skill-reindex-outcome/outcome")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["summary"]["attempted_record_count"] == 1
    assert data["summary"]["fixed_record_count"] == 1
    assert data["summary"]["unresolved_record_count"] == 0
    assert data["summary"]["repaired_findings"] == 1
    assert data["fixed_ids"] == ["skill-reindex-outcome"]


@pytest.mark.asyncio
async def test_admin_integrity_remediation_outcome_supports_handoff_target_repair(client):
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="handoff target filter failed",
    )
    store.upsert_finding(
        finding_id="handoff-target-outcome",
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        category="handoff",
        record_id="00000000-0000-0000-0000-00000000c055",
        suspicion_type="missing_handoff_target",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "handoff_repair_target"},
    )

    queue = get_job_queue()
    job_id = await queue.submit(
        "handoff_repair_target",
        {"limit": 1, "record_ids": ["00000000-0000-0000-0000-00000000c055"]},
    )
    queue._set_done(job_id, {"fixed_ids": ["00000000-0000-0000-0000-00000000c055"]})
    store.queue_remediation(
        remediation_id="rem-handoff-target-outcome",
        slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
        action_type="handoff_repair_target",
        requested_by="tester",
        job_id=job_id,
        details={
            "description": "Targeted handoff target repair",
            "payload": {"limit": 1, "record_ids": ["00000000-0000-0000-0000-00000000c055"]},
            "source": "targeted_repair_batch",
        },
    )
    store.sync_remediations_from_jobs(queue.list_jobs(limit=50))

    reconcile_resp = await client.post("/api/v1/admin/integrity/reconcile")
    assert reconcile_resp.status_code == 200, reconcile_resp.text

    resp = await client.get("/api/v1/admin/integrity/remediations/rem-handoff-target-outcome/outcome")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["summary"]["attempted_record_count"] == 1
    assert data["summary"]["fixed_record_count"] == 1
    assert data["summary"]["repaired_findings"] == 1


@pytest.mark.asyncio
async def test_admin_can_mark_finding_as_quarantine_candidate(client):
    store = get_data_integrity_store()
    finding = store.upsert_finding(
        finding_id="finding-1",
        slice_id="qdrant.skill_domain_tags_filter",
        category="skill",
        record_id="skill-1",
        suspicion_type="missing_domain_tags",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag"},
    )

    resp = await client.post("/api/v1/admin/integrity/findings/finding-1/status?status=quarantine_candidate")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["finding_id"] == finding["finding_id"]
    assert data["status"] == "quarantine_candidate"


@pytest.mark.asyncio
async def test_admin_reconcile_marks_resolved_finding_as_repaired(client):
    await get_memory_store().upsert(
        "skill-1",
        "skill",
        "# Healthy Skill\n\nUseful content with enough detail.",
        {
            "skill_name": "healthy-skill",
            "description": "Healthy description",
            "platform": "claude",
            "domain_tags": ["python"],
        },
    )
    store = get_data_integrity_store()
    store.upsert_finding(
        finding_id="finding-healthy",
        slice_id="qdrant.skill_domain_tags_filter",
        category="skill",
        record_id="skill-1",
        suspicion_type="unknown_skill_name",
        confidence=0.8,
        source="test",
        details={"suggested_repair": "skills_retag"},
    )

    queue = get_job_queue()
    job_id = await queue.submit("skills_retag", {"limit": 1})
    queue._set_done(job_id, {"details": [{"id": "skill-1", "name": "healthy-skill", "domains": ["python"]}]})
    store.queue_remediation(
        remediation_id="rem-healthy",
        slice_id="qdrant.skill_domain_tags_filter",
        action_type="skills_retag",
        requested_by="test",
        job_id=job_id,
        details={"description": "Retag skills"},
    )
    store.sync_remediations_from_jobs(queue.list_jobs(limit=50))

    resp = await client.post("/api/v1/admin/integrity/reconcile")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reconciled"] >= 1

    finding = store.list_findings(record_id="skill-1", limit=10)[0]
    assert finding["status"] == "repaired"
