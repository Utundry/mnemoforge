from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from qdrant_client.http import models as qmodels

from app.models.coordination import CoordinationMessageCreate, CoordinationMessageRecord
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate, MemoryUpdate


COORDINATION_CATEGORY = "coordination_message"
COORDINATION_ENTITY_TAG = "entity:coordination_message"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _status_tag(status: str) -> str:
    return f"coordination_status:{status}"


def _thread_tag(thread_id: str) -> str:
    return f"thread:{thread_id}"


def _from_tag(agent_id: str) -> str:
    return f"from:{agent_id}"


def _to_tag(agent_id: str) -> str:
    return f"to:{agent_id}"


def _project_tag(project: str) -> str:
    return f"project:{project}"


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        item = str(raw or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _thread_id_from_record(record) -> str:
    meta = dict(record.meta or {})
    return str(meta.get("thread_id") or record.session_id or record.id)


def _record_from_memory(record) -> CoordinationMessageRecord:
    meta = dict(record.meta or {})
    return CoordinationMessageRecord(
        memory_id=str(record.id),
        project=record.project or str(meta.get("project") or ""),
        thread_id=_thread_id_from_record(record),
        from_agent=str(meta.get("from_agent") or record.agent_id or ""),
        to_agent=str(meta.get("to_agent") or ""),
        message_type=str(meta.get("message_type") or "note"),
        content=record.content,
        status=record.status or str(meta.get("status") or "new"),
        priority=str(meta.get("priority") or "normal"),
        requested_action=str(meta.get("requested_action") or ""),
        response_to_message_id=str(meta.get("response_to_message_id") or ""),
        source=record.source,
        tags=list(record.tags or []),
        timestamp=record.timestamp,
        last_status_action=meta.get("last_status_action"),
        last_status_acted_by=meta.get("last_status_acted_by"),
        last_status_action_source=meta.get("last_status_action_source"),
        last_status_action_at=datetime.fromisoformat(meta["last_status_action_at"]) if meta.get("last_status_action_at") else None,
        last_status_action_reason=meta.get("last_status_action_reason"),
    )


async def _get_record(qdrant, memory_id: str):
    return await qdrant.get(memory_id)


async def create_coordination_message(qdrant, ollama, body: CoordinationMessageCreate) -> CoordinationMessageRecord:
    thread_id = (body.thread_id or body.response_to_message_id or str(uuid4())).strip()
    if body.response_to_message_id and not body.thread_id:
        try:
            parent = await _get_record(qdrant, body.response_to_message_id)
            thread_id = _thread_id_from_record(parent)
        except Exception:
            pass
    now = _utcnow().isoformat()
    meta = {
        "entity_type": "coordination_message",
        "thread_id": thread_id,
        "from_agent": body.from_agent,
        "to_agent": body.to_agent,
        "message_type": body.message_type,
        "priority": body.priority,
        "requested_action": (body.requested_action or "").strip(),
        "response_to_message_id": (body.response_to_message_id or "").strip(),
        "created_at": now,
        "updated_at": now,
    }
    memory = MemoryCreate(
        content=body.content,
        agent_id=body.from_agent,
        memory_type=MemoryType.context,
        category=COORDINATION_CATEGORY,
        importance_score=0.7,
        source=body.source,
        tags=_unique(
            list(body.tags or [])
            + [
                COORDINATION_ENTITY_TAG,
                _project_tag(body.project),
                _from_tag(body.from_agent),
                _to_tag(body.to_agent),
                _thread_tag(thread_id),
                _status_tag("new"),
                f"message_type:{body.message_type}",
                f"priority:{body.priority}",
            ]
        ),
        project=body.project,
        session_id=thread_id,
        scope="project",
        status="new",
        meta=meta,
    )
    memory_id = await qdrant.insert(memory, await ollama.embed(memory.content))
    record = await _get_record(qdrant, str(memory_id))
    return _record_from_memory(record)


async def list_coordination_messages(
    qdrant,
    *,
    agent_id: Optional[str] = None,
    project: Optional[str] = None,
    mailbox: str = "inbox",
    thread_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
) -> list[CoordinationMessageRecord]:
    must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=COORDINATION_CATEGORY)),
    ]
    if project:
        must.append(qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)))
    if mailbox == "inbox" and agent_id:
        must.append(qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=_to_tag(agent_id))))
    elif mailbox == "outbox" and agent_id:
        must.append(qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=_from_tag(agent_id))))
    if thread_id:
        must.append(qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=_thread_tag(thread_id))))
    if status:
        must.append(qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value=status)))

    rows, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    items = [_record_from_memory(await _get_record(qdrant, str(row.id))) for row in rows]
    items.sort(key=lambda item: item.timestamp, reverse=True)
    return items[:limit]


async def pickup_coordination_messages(
    qdrant,
    *,
    agent_id: str,
    project: Optional[str] = None,
    limit: int = 10,
) -> list[CoordinationMessageRecord]:
    items = await list_coordination_messages(
        qdrant,
        agent_id=agent_id,
        project=project,
        mailbox="inbox",
        status="new",
        limit=limit,
    )
    for item in items:
        await update_coordination_message_status(
            qdrant,
            message_id=item.memory_id,
            status="acknowledged",
            acted_by=agent_id,
            action_source="coordination_pickup",
            reason="Message picked up by target agent.",
        )
    refreshed: list[CoordinationMessageRecord] = []
    for item in items:
        refreshed.append(_record_from_memory(await _get_record(qdrant, item.memory_id)))
    return refreshed


async def update_coordination_message_status(
    qdrant,
    *,
    message_id: str,
    status: str,
    acted_by: str,
    action_source: str,
    reason: str = "",
) -> CoordinationMessageRecord:
    current = await _get_record(qdrant, message_id)
    meta = dict(current.meta or {})
    tags = [
        tag for tag in (current.tags or [])
        if not str(tag).startswith("coordination_status:")
    ]
    now = _utcnow().isoformat()
    meta.update(
        {
            "last_status_action": status,
            "last_status_acted_by": acted_by,
            "last_status_action_source": action_source,
            "last_status_action_at": now,
            "last_status_action_reason": reason,
            "updated_at": now,
            "status": status,
        }
    )
    updated = await qdrant.update(
        message_id,
        MemoryUpdate(
            status=status,
            tags=_unique(tags + [_status_tag(status)]),
            meta=meta,
        ),
    )
    return _record_from_memory(updated)

