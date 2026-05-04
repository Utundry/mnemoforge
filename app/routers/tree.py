"""
Project Knowledge Tree — REST API.

GET  /tree                          full tree (JSON or markdown)
GET  /tree/inbox                    loose ideas without parent
GET  /tree/related                  related projects by topic/tags
GET  /tree/{id}                     node + children
GET  /tree/path/{topic_path}        navigate by topic_path
POST /tree                          create node
PATCH /tree/{id}                    update node
DELETE /tree/{id}                   soft-delete
POST /tree/{id}/promote             assign idea to project
GET  /tree/{id}/journal             get journal entries for node
POST /tree/{id}/journal             add journal entry (manual or auto)
POST /tree/journal/by-path          add journal entry by topic_path + transcript
POST /tree/{id}/doc/regenerate regenerate auto-doc
GET  /tree/{id}/context        markdown context for LLM
GET  /tree/{id}/translate      translate doc
GET  /tree/workspaces/{project_id}        list workspaces
POST /tree/workspaces                     register workspace
POST /tree/workspaces/{id}/promote        promote to canonical
POST /tree/workspaces/{id}/archive        archive workspace
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.dependencies import OllamaDep, QdrantDep
from app.services.governed_artifact import apply_buffered_revision, discard_buffered_revision

router = APIRouter(prefix="/tree", tags=["tree"])

# ── Status/type constants ──────────────────────────────────────────────────────

VALID_TYPES = {"idea", "project", "area", "task", "leaf"}
VALID_STATUSES = {"inbox", "planning", "active", "in-progress", "done", "paused", "archived"}
STATUS_ICONS = {
    "inbox": "📥", "planning": "📋", "active": "🟢",
    "in-progress": "🔄", "done": "✅", "paused": "⏸", "archived": "🗄",
}
TYPE_ICONS = {
    "idea": "💡", "project": "📁", "area": "📂", "task": "📌", "leaf": "▪",
}


# ── Pydantic models ────────────────────────────────────────────────────────────

class JournalEntryCreate(BaseModel):
    content: str = Field("", description="Manual markdown content (skip for auto-generation)")
    transcript: str = Field("", description="Session transcript for auto-generation")
    session_id: str = ""
    language: str = "Russian"


class JournalByPathRequest(BaseModel):
    topic_path: str
    transcript: str
    session_id: str = ""
    language: str = "Russian"


class NodeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    type: str = Field("idea")
    parent_id: Optional[str] = None
    description: str = ""
    goal: str = ""
    status: str = "inbox"
    topic_path: str = ""
    tags: list[str] = []
    sort_order: int = 0


class NodeUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    goal: Optional[str] = None
    status: Optional[str] = None
    topic_path: Optional[str] = None
    tags: Optional[list[str]] = None
    sort_order: Optional[int] = None
    parent_id: Optional[str] = None
    doc: Optional[str] = None


class KnowledgeTreeNodeUpsert(BaseModel):
    topic_path: str = Field(..., min_length=1, max_length=512)
    title: str = Field(..., min_length=1, max_length=256)
    type: str = Field("area")
    status: str = Field("active")
    parent_topic_path: str = Field("", max_length=512)
    description: str = Field("", max_length=4000)
    goal: str = Field("", max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=64)
    doc: str = Field("", max_length=20000)
    responsibility: str = Field("", max_length=4000)
    source_of_truth: str = Field("", max_length=1000)
    runtime_entrypoints: list[str] = Field(default_factory=list, max_length=64)
    tests: list[str] = Field(default_factory=list, max_length=64)
    current_debt: list[str] = Field(default_factory=list, max_length=64)
    target_state: str = Field("", max_length=4000)
    projection_targets: list[str] = Field(default_factory=list, max_length=64)
    structured_fields: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)
    acted_by: str = Field("codex", min_length=1, max_length=256)
    source: str = Field("knowledge_tree_upsert", max_length=128)
    reason: str = Field("", max_length=1000)


class PromoteRequest(BaseModel):
    parent_id: str = Field(..., description="Target project/area node id")
    acted_by: str = Field("user", min_length=1, max_length=256)
    action_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=1000)


class WorkspaceCreate(BaseModel):
    project_id: str
    dir_path: str
    canonical: bool = False


class OrganizationActionRequest(BaseModel):
    acted_by: str = Field("user", min_length=1, max_length=256)
    action_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=1000)


class DocCandidateReviewRequest(BaseModel):
    reviewed_by: str = Field("user", min_length=1, max_length=256)
    review_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=1000)


class NodeCanonicalLinksResponse(BaseModel):
    node_id: str
    topic_path: str
    total: int
    canonicals: list[dict]


# ── Markdown rendering ─────────────────────────────────────────────────────────

def _node_to_md_line(node: dict, depth: int = 0) -> str:
    indent = "  " * depth
    status = node.get("status", "?")
    type_ = node.get("type", "?")
    icon = TYPE_ICONS.get(type_, "•") + STATUS_ICONS.get(status, "")
    title = node.get("title", "")
    tp = node.get("topic_path", "")
    path_hint = f" `{tp}`" if tp else ""
    return f"{indent}- {icon} **{title}**{path_hint} [{status}]"


def _build_md_tree(nodes: list[dict], depth: int = 0) -> list[str]:
    lines = []
    for n in nodes:
        lines.append(_node_to_md_line(n, depth))
        if n.get("doc"):
            first_line = n["doc"].strip().split("\n")[0].lstrip("#").strip()
            if first_line and first_line != n.get("title"):
                lines.append("  " * (depth + 1) + f"  _{first_line}_")
        children = n.get("children", [])
        if children:
            lines.extend(_build_md_tree(children, depth + 1))
    return lines


def _tree_to_markdown(tree: list[dict], inbox: list[dict]) -> str:
    parts = ["# Project Knowledge Tree\n"]
    if inbox:
        parts.append("## 📥 Inbox\n")
        for n in inbox:
            parts.append(f"- 💡 **{n['title']}**")
        parts.append("")
    parts.append("## 📁 Projects\n")
    parts.extend(_build_md_tree(tree))
    return "\n".join(parts)


def _node_context_md(node: dict, children: list[dict], canonicals: list[dict] | None = None) -> str:
    """Markdown context block suitable for LLM injection."""
    icon = TYPE_ICONS.get(node.get("type", ""), "")
    status_icon = STATUS_ICONS.get(node.get("status", ""), "")
    lines = [
        f"# {icon} {node.get('title', '')} [{node.get('status', '?')}]",
        f"**Path:** `{node.get('topic_path', 'unset')}` | **Type:** {node.get('type', '?')}",
        "",
    ]
    if node.get("description"):
        lines += ["## Description", node["description"], ""]
    if node.get("goal"):
        lines += ["## Goal", node["goal"], ""]
    if node.get("doc"):
        lines += ["## Documentation", node["doc"], ""]
    if children:
        lines.append("## Children")
        for c in children:
            ci = TYPE_ICONS.get(c.get("type", ""), "") + STATUS_ICONS.get(c.get("status", ""), "")
            lines.append(f"- {ci} **{c['title']}** [{c.get('status', '?')}]")
        lines.append("")
    if canonicals:
        lines.append("## Canonical Links")
        for item in canonicals:
            status = item.get("canonical_status") or ("suppressed" if item.get("suppressed") else "active")
            lines.append(
                f"- **{item.get('scope', '?')}** `{item.get('topic_path', '')}` "
                f"(supports={item.get('support_count', 0)}, status={status})"
            )
        lines.append("")
    return "\n".join(lines)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _wants_markdown(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/markdown" in accept or "text/plain" in accept


def _store():
    from app.services.project_tree_store import get_tree_store
    return get_tree_store()


def _merge_unique(*lists: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for values in lists:
        for value in values or []:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
    return result


def _structured_tree_payload(body: KnowledgeTreeNodeUpsert) -> dict[str, Any]:
    payload = {
        "responsibility": body.responsibility,
        "source_of_truth": body.source_of_truth,
        "runtime_entrypoints": body.runtime_entrypoints,
        "tests": body.tests,
        "current_debt": body.current_debt,
        "target_state": body.target_state,
        "projection_targets": body.projection_targets,
        "evidence_refs": body.evidence_refs,
        "reason": body.reason,
    }
    payload.update(body.structured_fields or {})
    return {key: value for key, value in payload.items() if value not in ("", [], {}, None)}


async def _node_canonical_links(node: dict, qdrant: QdrantDep | None, *, include_suppressed: bool = False, limit: int = 20) -> list[dict]:
    if not qdrant or not node.get("topic_path"):
        return []
    from app.services.crystallization_service import list_canonicals

    items = await list_canonicals(
        qdrant._client,
        settings.qdrant_collection_name,
        topic_prefix=node.get("topic_path", ""),
        include_suppressed=include_suppressed,
        limit=limit,
    )
    preferred_ids = list((node.get("meta_json") or {}).get("canonical_memory_ids", []))
    if preferred_ids:
        order = {cid: idx for idx, cid in enumerate(preferred_ids)}
        items.sort(key=lambda item: (order.get(item["id"], 9999), item.get("scope", ""), item.get("topic_path", "")))
    return items


# ── Endpoints: tree ────────────────────────────────────────────────────────────

@router.get("")
async def get_tree(request: Request, include_archived: bool = Query(False)):
    """Full project tree. Accept: text/markdown for LLM-friendly output."""
    s = _store()
    projects = s.get_projects()
    tree = []
    for p in projects:
        p["children"] = s.build_subtree(p["id"])
        tree.append(p)
    inbox = s.get_inbox()

    if _wants_markdown(request):
        md = _tree_to_markdown(tree, inbox)
        return PlainTextResponse(md, media_type="text/markdown")

    return {"tree": tree, "inbox": inbox, "total_projects": len(tree)}


@router.get("/inbox")
async def get_inbox():
    """Loose ideas not assigned to any project."""
    return {"ideas": _store().get_inbox()}


@router.get("/related")
async def get_related(project_id: str = Query(...)):
    """Find projects related by shared topic_path prefixes."""
    s = _store()
    projects = s.get_projects()
    target = next((p for p in projects if p.get("topic_path", "").startswith(project_id)
                   or p.get("title", "").lower() == project_id.lower()), None)
    if not target:
        return {"project_id": project_id, "related": []}

    target_tags = set(target.get("tags", []))
    target_tp = target.get("topic_path", "")

    related = []
    for p in projects:
        if p["id"] == target["id"]:
            continue
        shared_tags = target_tags & set(p.get("tags", []))
        tp = p.get("topic_path", "")
        path_overlap = bool(tp and target_tp and (
            tp.split("/")[0] == target_tp.split("/")[0]
        ))
        if shared_tags or path_overlap:
            related.append({
                "id": p["id"], "title": p["title"], "status": p["status"],
                "shared_tags": list(shared_tags), "path_overlap": path_overlap,
            })

    return {"project_id": project_id, "related": related}


@router.get("/path/{topic_path:path}")
async def get_by_path(topic_path: str, request: Request, qdrant: QdrantDep = None):
    """Get node by topic_path."""
    s = _store()
    node = s.get_by_topic_path(topic_path)
    if not node:
        raise HTTPException(404, f"No node found at path '{topic_path}'")
    children = s.get_children(node["id"])
    canonicals = await _node_canonical_links(node, qdrant)
    if _wants_markdown(request):
        return PlainTextResponse(_node_context_md(node, children, canonicals), media_type="text/markdown")
    return {**node, "children": children, "canonicals": canonicals}


@router.get("/workspaces/{project_id}")
async def list_workspaces(project_id: str):
    return {"project_id": project_id, "workspaces": _store().get_workspaces(project_id)}


@router.post("/workspaces")
async def register_workspace(body: WorkspaceCreate):
    ws_id = _store().register_workspace(
        project_id=body.project_id,
        dir_path=body.dir_path,
        canonical=body.canonical,
    )
    return {"workspace_id": ws_id, "project_id": body.project_id, "dir_path": body.dir_path}


@router.post("/workspaces/{workspace_id}/promote")
async def promote_workspace(workspace_id: str, body: OrganizationActionRequest | None = None):
    action = body or OrganizationActionRequest()
    if not _store().promote_workspace(
        workspace_id,
        acted_by=action.acted_by,
        action_source=action.action_source,
        reason=action.reason,
    ):
        raise HTTPException(404, "Workspace not found")
    workspace = _store().get_workspace(workspace_id)
    return {
        "workspace_id": workspace_id,
        "status": "canonical",
        "workspace": workspace,
        "org_last_action_type": "promote_workspace",
        "org_last_action_by": action.acted_by,
        "org_last_action_source": action.action_source,
        "org_last_action_reason": action.reason or None,
    }


@router.post("/workspaces/{workspace_id}/archive")
async def archive_workspace(workspace_id: str, body: OrganizationActionRequest | None = None):
    action = body or OrganizationActionRequest()
    if not _store().archive_workspace(
        workspace_id,
        acted_by=action.acted_by,
        action_source=action.action_source,
        reason=action.reason,
    ):
        raise HTTPException(404, "Workspace not found")
    workspace = _store().get_workspace(workspace_id)
    return {
        "workspace_id": workspace_id,
        "status": "archived",
        "workspace": workspace,
        "org_last_action_type": "archive_workspace",
        "org_last_action_by": action.acted_by,
        "org_last_action_source": action.action_source,
        "org_last_action_reason": action.reason or None,
    }


@router.get("/{node_id}/context")
async def get_context(node_id: str, request: Request, qdrant: QdrantDep = None):
    """Markdown context block for LLM injection."""
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    children = s.get_children(node_id)
    canonicals = await _node_canonical_links(node, qdrant)
    md = _node_context_md(node, children, canonicals)
    if _wants_markdown(request):
        return PlainTextResponse(md, media_type="text/markdown")
    return {"node_id": node_id, "context": md, "canonicals": canonicals}


@router.get("/{node_id}/translate")
async def translate_node(node_id: str):
    """Translate node doc to GLM_RESPONSE_LANGUAGE."""
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    doc = node.get("doc") or ""
    if not doc:
        raise HTTPException(400, "Node has no doc yet. Run /doc/regenerate first.")
    from app.services.project_tree_doc import translate_doc
    try:
        translated = await translate_doc(doc, settings.glm_response_language)
    except RuntimeError as exc:
        detail = str(exc).strip() or "Translation failed"
        status_code = 503 if "no cloud llm configured" in detail.lower() else 502
        raise HTTPException(status_code, detail) from exc
    return {
        "node_id": node_id,
        "language": settings.glm_response_language,
        "original": doc,
        "translated": translated,
    }


@router.post("/{node_id}/doc/regenerate")
async def regenerate_doc(node_id: str, background_tasks: BackgroundTasks,
                          qdrant: QdrantDep = None, ollama: OllamaDep = None):
    """Force-regenerate auto-documentation, overwriting doc and clearing lock."""
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    # Clear lock so force=True path is taken in regenerate_node_doc
    meta = node.get("meta_json") or {}
    if meta.get("doc_locked"):
        meta["doc_locked"] = False
        s.update_node(node_id, meta_json=meta)

    async def _run():
        from app.services.project_tree_doc import regenerate_node_doc
        await regenerate_node_doc(node_id, s, qdrant=qdrant, ollama=ollama, force=True)

    background_tasks.add_task(_run)
    return {"node_id": node_id, "status": "regenerating", "message": "Doc will be ready in a few seconds"}


@router.post("/{node_id}/doc/apply-candidate")
async def apply_candidate(node_id: str, body: DocCandidateReviewRequest | None = None):
    """Apply pending doc_candidate as the active doc, clear lock."""
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    if not node.get("doc_candidate", ""):
        raise HTTPException(400, "No candidate to apply")
    effective_doc, effective_generated_at, candidate_doc, candidate_generated_at = apply_buffered_revision(
        effective_value=node.get("doc", ""),
        effective_updated_at=node.get("doc_generated_at"),
        candidate_value=node.get("doc_candidate", ""),
        candidate_updated_at=node.get("doc_candidate_generated_at"),
        empty_factory=str,
    )
    meta = node.get("meta_json") or {}
    review = body or DocCandidateReviewRequest()
    meta["doc_locked"] = False
    meta["doc_last_review_action"] = "apply_candidate"
    meta["doc_last_reviewed_by"] = review.reviewed_by
    meta["doc_last_review_source"] = review.review_source
    meta["doc_last_reviewed_at"] = time.time()
    meta["doc_last_review_reason"] = review.reason or None
    s.update_node(
        node_id,
        doc=effective_doc,
        doc_candidate=candidate_doc,
        doc_generated_at=effective_generated_at,
        doc_candidate_generated_at=candidate_generated_at,
        meta_json=meta,
    )
    return {"node_id": node_id, "doc_locked": False, "applied": True}


@router.post("/{node_id}/doc/discard-candidate")
async def discard_candidate(node_id: str, body: DocCandidateReviewRequest | None = None):
    """Discard pending doc_candidate, keep manual doc as-is."""
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    if not node.get("doc_candidate", ""):
        raise HTTPException(400, "No candidate to discard")
    _, _, candidate_doc, candidate_generated_at = discard_buffered_revision(
        effective_value=node.get("doc", ""),
        effective_updated_at=node.get("doc_generated_at"),
        candidate_value=node.get("doc_candidate", ""),
        empty_factory=str,
    )
    review = body or DocCandidateReviewRequest()
    meta = node.get("meta_json") or {}
    meta["doc_last_review_action"] = "discard_candidate"
    meta["doc_last_reviewed_by"] = review.reviewed_by
    meta["doc_last_review_source"] = review.review_source
    meta["doc_last_reviewed_at"] = time.time()
    meta["doc_last_review_reason"] = review.reason or None
    s.update_node(
        node_id,
        doc_candidate=candidate_doc,
        doc_candidate_generated_at=candidate_generated_at,
        meta_json=meta,
    )
    return {"node_id": node_id, "candidate_cleared": True}


@router.post("/{node_id}/doc/unlock")
async def unlock_doc(node_id: str):
    """Remove lock without applying candidate — allow auto-regen on future updates."""
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    meta = node.get("meta_json") or {}
    meta["doc_locked"] = False
    s.update_node(node_id, meta_json=meta)
    return {"node_id": node_id, "doc_locked": False}


@router.post("/{node_id}/promote")
async def promote_node(node_id: str, body: PromoteRequest):
    """Assign an idea to a project/area (set parent_id)."""
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    meta = node.get("meta_json") or {}
    meta["org_last_action_type"] = "promote_node"
    meta["org_last_action_by"] = body.acted_by
    meta["org_last_action_source"] = body.action_source
    meta["org_last_action_at"] = time.time()
    meta["org_last_action_reason"] = body.reason or None
    new_status = "planning" if node["status"] == "inbox" else node["status"]
    s.update_node(node_id, parent_id=body.parent_id, status=new_status, meta_json=meta)
    return {
        "node_id": node_id,
        "parent_id": body.parent_id,
        "status": new_status,
        "org_last_action_type": "promote_node",
        "org_last_action_by": body.acted_by,
        "org_last_action_source": body.action_source,
        "org_last_action_reason": body.reason or None,
    }


@router.get("/{node_id}")
async def get_node(node_id: str, request: Request, qdrant: QdrantDep = None):
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    children = s.get_children(node_id)
    canonicals = await _node_canonical_links(node, qdrant)
    if _wants_markdown(request):
        return PlainTextResponse(_node_context_md(node, children, canonicals), media_type="text/markdown")
    return {**node, "children": children, "canonicals": canonicals}


@router.get("/{node_id}/canonicals", response_model=NodeCanonicalLinksResponse)
async def get_node_canonicals(
    node_id: str,
    qdrant: QdrantDep,
    include_suppressed: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
):
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    canonicals = await _node_canonical_links(node, qdrant, include_suppressed=include_suppressed, limit=limit)
    return NodeCanonicalLinksResponse(
        node_id=node_id,
        topic_path=node.get("topic_path", ""),
        total=len(canonicals),
        canonicals=canonicals,
    )


@router.get("/{node_id}/journal")
async def get_journal(node_id: str, limit: int = Query(50, le=200)):
    """Get all journal entries for a node, newest first."""
    s = _store()
    if not s.get_node(node_id):
        raise HTTPException(404, "Node not found")
    entries = s.get_journal(node_id, limit=limit)
    return {"node_id": node_id, "entries": entries, "total": len(entries)}


@router.post("/{node_id}/journal")
async def add_journal_entry(node_id: str, body: JournalEntryCreate, background_tasks: BackgroundTasks):
    """Add a journal entry. Provide content for manual entry, or transcript for auto-generation."""
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")

    if body.content:
        # Manual entry — save directly
        entry_id = s.add_journal_entry(node_id, body.content, session_id=body.session_id)
        return {"entry_id": entry_id, "node_id": node_id, "mode": "manual"}

    if not body.transcript:
        raise HTTPException(400, "Provide either content (manual) or transcript (auto-generate)")

    # Auto-generate in background, return immediately
    async def _gen():
        from app.services.node_journal import generate_journal_entry
        from app.config import settings
        prev = s.get_journal(node_id, limit=3)
        entry = await generate_journal_entry(
            node=node, transcript=body.transcript,
            prev_entries=prev, language=body.language or settings.glm_response_language,
        )
        s.add_journal_entry(node_id, entry, session_id=body.session_id)

    background_tasks.add_task(_gen)
    return {"node_id": node_id, "status": "generating", "message": "Journal entry will be ready in a few seconds"}


@router.post("/journal/by-path")
async def add_journal_by_path(body: JournalByPathRequest, background_tasks: BackgroundTasks):
    """Add a journal entry by topic_path — used by Stop hook."""
    s = _store()
    node = s.get_by_topic_path(body.topic_path)
    if not node:
        # Silently succeed — topic_path may not have a tree node yet
        return {"status": "skipped", "reason": f"no node for topic_path={body.topic_path}"}

    async def _gen():
        from app.services.node_journal import generate_journal_entry
        from app.config import settings
        prev = s.get_journal(node["id"], limit=3)
        entry = await generate_journal_entry(
            node=node, transcript=body.transcript,
            prev_entries=prev, language=body.language or settings.glm_response_language,
        )
        s.add_journal_entry(node["id"], entry, session_id=body.session_id)
        logger.info("Journal entry added via by-path for %s", body.topic_path)

    background_tasks.add_task(_gen)
    return {"node_id": node["id"], "topic_path": body.topic_path, "status": "generating"}


@router.post("/upsert-by-path")
async def upsert_node_by_path(body: KnowledgeTreeNodeUpsert):
    """Create or update a structured project-tree node by stable topic_path."""
    if body.type not in VALID_TYPES:
        raise HTTPException(400, f"Invalid type. Choose from: {', '.join(VALID_TYPES)}")
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Choose from: {', '.join(VALID_STATUSES)}")

    s = _store()
    existing = s.get_by_topic_path(body.topic_path)
    parent_id = None
    if body.parent_topic_path:
        parent = s.get_by_topic_path(body.parent_topic_path)
        if not parent:
            raise HTTPException(404, f"Parent topic_path not found: {body.parent_topic_path}")
        parent_id = parent["id"]

    structured = _structured_tree_payload(body)
    now = time.time()
    if existing:
        meta = existing.get("meta_json") or {}
        prior_structured = meta.get("structured_knowledge") or {}
        meta["structured_knowledge"] = {**prior_structured, **structured}
        meta["structured_knowledge_updated_at"] = now
        meta["structured_knowledge_updated_by"] = body.acted_by
        meta["structured_knowledge_source"] = body.source
        updates: dict[str, Any] = {
            "title": body.title,
            "type": body.type,
            "status": body.status,
            "topic_path": body.topic_path,
            "tags": _merge_unique(existing.get("tags") or [], body.tags, ["structured_knowledge"]),
            "meta_json": meta,
        }
        if body.description:
            updates["description"] = body.description
        if body.goal:
            updates["goal"] = body.goal
        if body.doc:
            updates["doc"] = body.doc
            meta["doc_locked"] = True
            updates["meta_json"] = meta
        if body.parent_topic_path:
            updates["parent_id"] = parent_id
        s.update_node(existing["id"], **updates)
        node = s.get_node(existing["id"])
        return {"created": False, "node": node, "node_id": existing["id"], "topic_path": body.topic_path}

    node_id = s.create_node(
        title=body.title,
        type=body.type,
        parent_id=parent_id,
        description=body.description,
        goal=body.goal,
        status=body.status,
        topic_path=body.topic_path,
        tags=_merge_unique(body.tags, ["structured_knowledge"]),
    )
    meta = {
        "structured_knowledge": structured,
        "structured_knowledge_updated_at": now,
        "structured_knowledge_updated_by": body.acted_by,
        "structured_knowledge_source": body.source,
    }
    updates: dict[str, Any] = {"meta_json": meta}
    if body.doc:
        meta["doc_locked"] = True
        updates["doc"] = body.doc
    s.update_node(node_id, **updates)
    node = s.get_node(node_id)
    return {"created": True, "node": node, "node_id": node_id, "topic_path": body.topic_path}


@router.post("")
async def create_node(body: NodeCreate, background_tasks: BackgroundTasks,
                       qdrant: QdrantDep = None, ollama: OllamaDep = None):
    if body.type not in VALID_TYPES:
        raise HTTPException(400, f"Invalid type. Choose from: {', '.join(VALID_TYPES)}")
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Choose from: {', '.join(VALID_STATUSES)}")

    s = _store()
    node_id = s.create_node(
        title=body.title,
        type=body.type,
        parent_id=body.parent_id,
        description=body.description,
        goal=body.goal,
        status=body.status,
        topic_path=body.topic_path,
        tags=body.tags,
        sort_order=body.sort_order,
    )

    # Auto-generate doc in background
    async def _gen_doc():
        from app.services.project_tree_doc import regenerate_node_doc
        await regenerate_node_doc(node_id, s, qdrant=qdrant, ollama=ollama)

    background_tasks.add_task(_gen_doc)
    return s.get_node(node_id)


@router.patch("/{node_id}")
async def update_node(node_id: str, body: NodeUpdate, background_tasks: BackgroundTasks,
                       qdrant: QdrantDep = None, ollama: OllamaDep = None):
    s = _store()
    if not s.get_node(node_id):
        raise HTTPException(404, "Node not found")
    if body.status and body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Choose from: {', '.join(VALID_STATUSES)}")

    updates = body.model_dump(exclude_none=True)

    # If doc is manually set — lock it so auto-regen never overwrites
    if "doc" in updates:
        node = s.get_node(node_id)
        meta = node.get("meta_json") or {}
        meta["doc_locked"] = True
        updates["meta_json"] = meta

    s.update_node(node_id, **updates)

    # node→done: auto-resolve linked improvement (fast, run first)
    if updates.get("status") == "done":
        background_tasks.add_task(_sync_node_done_to_improvement, node_id, s)

    # Regenerate doc on content changes (slow LLM call, run after sync).
    # Skip when marking done — no point regenerating docs for completed nodes.
    if "doc" not in updates and updates.get("status") != "done" and (
        "status" in updates or "description" in updates or "goal" in updates
    ):
        async def _gen_doc():
            from app.services.project_tree_doc import regenerate_node_doc
            await regenerate_node_doc(node_id, s, qdrant=qdrant, ollama=ollama)
        background_tasks.add_task(_gen_doc)

    return s.get_node(node_id)


async def _sync_node_done_to_improvement(node_id: str, s) -> None:
    try:
        node = s.get_node(node_id)
        improvement_id = (node.get("meta_json") or {}).get("improvement_id")
        if not improvement_id:
            return
        from app.services.improvements_store import get_improvements_store
        from uuid import UUID
        store = get_improvements_store()
        await store.resolve(
            UUID(improvement_id),
            acted_by="system",
            action_source="linked_tree_completion",
            reason=f"Tree node {node_id} marked done",
        )
        logger.info("Node %s done → improvement %s resolved", node_id, improvement_id)
    except Exception as e:
        logger.warning("Failed to sync node done to improvement: %s", e)


@router.delete("/{node_id}")
async def delete_node(node_id: str, body: OrganizationActionRequest | None = None):
    s = _store()
    node = s.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    action = body or OrganizationActionRequest()
    meta = node.get("meta_json") or {}
    meta["org_last_action_type"] = "archive_node"
    meta["org_last_action_by"] = action.acted_by
    meta["org_last_action_source"] = action.action_source
    meta["org_last_action_at"] = time.time()
    meta["org_last_action_reason"] = action.reason or None
    if not s.update_node(node_id, status="archived", meta_json=meta):
        raise HTTPException(404, "Node not found")
    return {
        "node_id": node_id,
        "status": "archived",
        "org_last_action_type": "archive_node",
        "org_last_action_by": action.acted_by,
        "org_last_action_source": action.action_source,
        "org_last_action_reason": action.reason or None,
    }
