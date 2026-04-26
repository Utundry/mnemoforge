from app.routers import models as models_router


async def test_coordination_message_lifecycle(client):
    sent = await client.post(
        "/api/v1/models/coordination/messages",
        json={
            "project": "alpha",
            "from_agent": "backend-agent",
            "to_agent": "frontend-agent",
            "message_type": "request_action",
            "content": "Please verify whether the dashboard still depends on legacy docs cache.",
            "requested_action": "inspect dashboard data source",
            "priority": "high",
        },
    )
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["status"] == "new"
    assert body["thread_id"]
    assert body["requested_action"] == "inspect dashboard data source"

    pickup = await client.post(
        "/api/v1/models/coordination/pickup",
        json={"agent_id": "frontend-agent", "project": "alpha", "limit": 10},
    )
    assert pickup.status_code == 200, pickup.text
    pickup_data = pickup.json()
    assert pickup_data["total"] == 1
    picked = pickup_data["items"][0]
    assert picked["memory_id"] == body["memory_id"]
    assert picked["status"] == "acknowledged"

    in_progress = await client.post(
        f"/api/v1/models/coordination/messages/{body['memory_id']}/status",
        json={
            "status": "in_progress",
            "acted_by": "frontend-agent",
            "action_source": "coordination_test",
            "reason": "Started inspection.",
        },
    )
    assert in_progress.status_code == 200, in_progress.text
    assert in_progress.json()["status"] == "in_progress"

    reply = await client.post(
        "/api/v1/models/coordination/messages",
        json={
            "project": "alpha",
            "from_agent": "frontend-agent",
            "to_agent": "backend-agent",
            "message_type": "response",
            "content": "The dashboard still reads the effective docs projection.",
            "thread_id": body["thread_id"],
            "response_to_message_id": body["memory_id"],
        },
    )
    assert reply.status_code == 200, reply.text
    reply_body = reply.json()
    assert reply_body["thread_id"] == body["thread_id"]
    assert reply_body["response_to_message_id"] == body["memory_id"]

    outbox = await client.get(
        "/api/v1/models/coordination/messages",
        params={"agent_id": "frontend-agent", "project": "alpha", "mailbox": "outbox"},
    )
    assert outbox.status_code == 200, outbox.text
    outbox_data = outbox.json()
    assert outbox_data["total"] >= 1

    thread = await client.get(
        "/api/v1/models/coordination/messages",
        params={"project": "alpha", "mailbox": "thread", "thread_id": body["thread_id"], "limit": 10},
    )
    assert thread.status_code == 200, thread.text
    thread_data = thread.json()
    assert thread_data["total"] == 2
    assert {item["message_type"] for item in thread_data["items"]} == {"request_action", "response"}


async def test_coordination_alias_routes_match_models_routes(client):
    sent = await client.post(
        "/api/v1/coordination/messages",
        json={
            "project": "alpha",
            "from_agent": "codex",
            "to_agent": "debian",
            "message_type": "request_action",
            "content": "Inspect toolchange flow.",
        },
    )
    assert sent.status_code == 200, sent.text
    body = sent.json()

    pickup = await client.post(
        "/api/v1/coordination/pickup",
        json={"agent_id": "debian", "project": "alpha", "limit": 10},
    )
    assert pickup.status_code == 200, pickup.text
    pickup_data = pickup.json()
    assert pickup_data["total"] == 1
    assert pickup_data["items"][0]["memory_id"] == body["memory_id"]

    inbox = await client.get(
        "/api/v1/coordination/messages",
        params={"agent_id": "debian", "project": "alpha", "mailbox": "inbox"},
    )
    assert inbox.status_code == 200, inbox.text
    inbox_data = inbox.json()
    assert inbox_data["total"] == 1
    assert inbox_data["items"][0]["memory_id"] == body["memory_id"]

    closed = await client.post(
        f"/api/v1/coordination/messages/{body['memory_id']}/status",
        json={
            "status": "closed",
            "acted_by": "debian",
            "action_source": "coordination_alias_test",
            "reason": "Handled via alias route.",
        },
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
