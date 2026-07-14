from app.services.mcp_lifecycle_receipts import build_lifecycle_receipt, public_auto_work_session_payload


def _started_session_result() -> dict:
    return {
        "status": "started",
        "project": "alpha",
        "task_id": "task-1",
        "owner_agent": "codex",
        "owner_session_id": "session-1",
        "work_handle": "wh1.public",
        "work_session": {"work_id": "work-1"},
        "lease": {
            "lease_id": "lease-1",
            "status": "active",
            "work_token": "secret-token",
            "work_token_hash": "secret-hash",
            "work_token_preview": "secret-preview",
        },
    }


def test_public_auto_work_session_payload_is_safe_and_stable() -> None:
    payload = public_auto_work_session_payload(_started_session_result())

    assert "work_token" not in payload["lease"]
    assert "work_token_hash" not in payload["lease"]
    assert "work_token_preview" not in payload["lease"]
    assert payload == {
        "auto_started": True,
        "project": "alpha",
        "task_id": "task-1",
        "work_id": "work-1",
        "work_handle": "wh1.public",
        "owner_agent": "codex",
        "owner_session_id": "session-1",
        "lease": {"lease_id": "lease-1", "status": "active"},
        "next_safe_action": "Reuse this work_handle for later checkpoint or finish operations.",
    }


def test_build_lifecycle_receipt_exposes_continuity_from_auto_work_session() -> None:
    auto_work_session = public_auto_work_session_payload(_started_session_result())
    receipt = build_lifecycle_receipt(
        route_tool="record_work_result",
        result={
            "status": "recorded",
            "target": {"task_id": "task-1"},
            "auto_work_session": auto_work_session,
            "warnings": [],
        },
        warnings=["auto-claimed"],
    )

    assert receipt["status"] == "recorded"
    assert receipt["route_tool"] == "record_work_result"
    assert receipt["task_id"] == "task-1"
    assert receipt["work_id"] == "work-1"
    assert receipt["work_handle"] == "wh1.public"
    assert receipt["lease"] == {"lease_id": "lease-1", "status": "active"}
    assert receipt["warnings"] == ["auto-claimed"]
    assert "work_handle" in receipt["next_safe_action"]


def test_build_lifecycle_receipt_exposes_start_session_continuity() -> None:
    receipt = build_lifecycle_receipt(
        route_tool="start_task_session",
        result={
            **_started_session_result(),
            "next_safe_action": "Use work_handle for later lifecycle calls.",
        },
    )

    assert receipt["status"] == "started"
    assert receipt["route_tool"] == "start_task_session"
    assert receipt["task_id"] == "task-1"
    assert receipt["work_id"] == "work-1"
    assert receipt["work_handle"] == "wh1.public"
    assert receipt["lease"] == {"lease_id": "lease-1", "status": "active"}
    assert receipt["next_safe_action"] == "Use work_handle for later lifecycle calls."


def test_build_lifecycle_receipt_exposes_finish_session_result() -> None:
    receipt = build_lifecycle_receipt(
        route_tool="finish_task_session",
        result={
            "status": "finished",
            "task_id": "task-1",
            "work_session": {"work_id": "work-1", "status": "completed"},
            "release": {
                "status": "released",
                "lease": {
                    "lease_id": "lease-1",
                    "status": "released",
                    "work_token": "secret-token",
                    "work_token_hash": "secret-hash",
                    "work_token_preview": "secret-preview",
                },
            },
            "next_safe_action": "Request state planning.",
        },
    )

    assert receipt["status"] == "finished"
    assert receipt["route_tool"] == "finish_task_session"
    assert receipt["task_id"] == "task-1"
    assert receipt["work_id"] == "work-1"
    assert receipt["next_safe_action"] == "Request state planning."
    assert "lease" not in receipt
