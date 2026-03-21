"""
Skill marketplace — publish, discover, and install skills across LLM agents.

Skills are stored as memories with category="skill" plus extra payload fields:
  skill_name, platform, domain_tags, content (raw SKILL.md text)

Two-level filtering:
  1. Fast (tag-based): filter by domain_tags without LLM
  2. Smart (LLM scoring): rank by relevance to a context description
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from time import perf_counter, time as _now
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.dependencies import JobQueueDep, OllamaDep, QdrantDep
from app.services.performance_tracker import get_tracker
from app.services.adaptive_state import get_adaptive_store
from app.services.text_localization import normalize_text_for_display, prepare_artifact_texts
from qdrant_client.http import models as qmodels

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/skills", tags=["skills"])

MANAGER_MODEL = "qwen3:1.7b"

# Known domains for auto-suggestion
KNOWN_DOMAINS = [
    "web", "frontend", "backend", "api", "deploy",
    "cloudflare", "netlify", "docker", "kubernetes",
    "python", "typescript", "javascript", "rust", "go",
    "cnc", "linuxcnc", "hal", "gcode", "hardware", "machining",
    "database", "sql", "qdrant", "redis", "postgres",
    "llm", "ai", "ml", "embedding", "rag",
    "git", "ci", "testing", "debugging",
    "linux", "windows", "shell", "devops",
]

ADAPTIVE_COMPONENT = "adaptive-skillization"


def _track_adaptive(
    task_type: str,
    *,
    success: bool,
    started_at: float,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Best-effort observability for adaptive skillization lifecycle."""
    try:
        get_tracker().record(
            component=ADAPTIVE_COMPONENT,
            task_type=task_type,
            success=success,
            latency_ms=round((perf_counter() - started_at) * 1000, 2),
            agent_id=agent_id,
            session_id=session_id,
            metadata=metadata,
        )
    except Exception as e:
        logger.warning("Adaptive tracker record failed for %s: %s", task_type, e)


def _tokenize_text(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9_]{4,}", value.lower())
        if len(token) >= 4
    }


def _preference_match_score(skill: dict, preferences: list[str]) -> float:
    if not preferences:
        return 0.0
    skill_text = " ".join([
        skill.get("name", ""),
        skill.get("description", ""),
        skill.get("content", ""),
        " ".join(skill.get("domain_tags", [])),
    ])
    skill_tokens = _tokenize_text(skill_text)
    if not skill_tokens:
        return 0.0

    score = 0.0
    for pref in preferences:
        pref_tokens = _tokenize_text(pref)
        if not pref_tokens:
            continue
        overlap = pref_tokens & skill_tokens
        if overlap:
            score += len(overlap) / max(len(pref_tokens), 1)
    return round(score, 3)


async def _load_user_preferences(qdrant, agent_id: Optional[str], limit: int = 12) -> list[str]:
    if not agent_id:
        return []
    must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="user_preference")),
        qmodels.FieldCondition(key="agent_id", match=qmodels.MatchValue(value=agent_id)),
    ]
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    preferences: list[str] = []
    seen: set[str] = set()
    for result in results:
        content = (result.payload.get("content") or "").strip()
        if content and content not in seen:
            seen.add(content)
            preferences.append(content)
    return preferences


# ── Schemas ────────────────────────────────────────────────────────────────────

class SkillPublish(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Skill name (slug)")
    content: str = Field(..., min_length=10, max_length=20000, description="Full SKILL.md content")
    platform: str = Field("claude", description="claude | codex | cursor | universal")
    agent_id: str = Field("shared", max_length=256)
    description: Optional[str] = Field(None, max_length=512, description="Short description (auto-extracted if omitted)")
    domain_tags: Optional[list[str]] = Field(None, description="Domain tags (auto-extracted if omitted)")
    importance_score: float = Field(0.7, ge=0.0, le=1.0)
    pinned: bool = Field(False, description="Always include in get_onboarding regardless of domain context")
    reference_url: Optional[str] = Field(None, max_length=512, description="External resource URL (makes this skill a reference pointer)")


class SkillRecord(BaseModel):
    id: str
    name: str
    description: str
    platform: str
    agent_id: str
    domain_tags: list[str]
    importance_score: float
    timestamp: str
    install_path: str  # suggested path: ~/.claude/skills/{name}/SKILL.md
    pinned: bool = False
    reference_url: Optional[str] = None
    usage_count: int = 0
    helpful_count: int = 0
    usefulness_score: float = 1.0
    suppressed: bool = False


class SkillInstallRequest(BaseModel):
    skill_id: str
    target_dir: Optional[str] = None  # override install dir


class SkillPackItem(BaseModel):
    id: str
    name: str
    description: str
    domain_tags: list[str]
    content: str  # full SKILL.md content for injection


class TaskProfileRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class TaskProfileResult(BaseModel):
    task_type: str  # bug | feature | research | coding | other
    domains: list[str]
    confidence: float


class GenerateForDomainRequest(BaseModel):
    domains: list[str] = Field(..., min_length=1, max_length=6)
    task_context: Optional[str] = Field(None, max_length=500, description="Optional task description for more specific generation")
    agent_id: str = Field("auto-generator", max_length=256)
    platform: str = Field("claude")


# ── LLM helpers ────────────────────────────────────────────────────────────────

async def _llm(prompt: str) -> str:
    """Call cloud LLM if configured, otherwise fall back to local Ollama."""
    from app.services.cloud_llm import cloud_available, cloud_complete
    if cloud_available():
        try:
            return await cloud_complete(prompt, timeout=60.0)
        except Exception as e:
            logger.warning("Cloud LLM failed in skills, falling back to local: %s", e)

    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            f"{settings.ollama_base_url}/api/generate",
            json={"model": MANAGER_MODEL, "prompt": prompt, "stream": False},
        )
        r.raise_for_status()
        text = r.json()["response"].strip()
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


async def _extract_skill_meta(name: str, content: str) -> tuple[str, list[str]]:
    """Use LLM to extract description and domain_tags from skill content."""
    prompt = f"""/no_think
Analyze this skill/tool definition and return JSON with two fields:
- "description": one sentence summary (max 100 chars)
- "domain_tags": list of 2-6 lowercase domain keywords from: {', '.join(KNOWN_DOMAINS[:30])}
  Add custom tags if none fit. Focus on WHAT domain this skill is useful for.

Skill name: {name}
Content (first 1500 chars):
{content[:1500]}

Return only valid JSON, no other text. Example:
{{"description": "Deploy apps to Cloudflare Workers", "domain_tags": ["cloudflare", "deploy", "web"]}}
"""
    raw = await _llm(prompt)
    # Extract JSON from response
    match = re.search(r'\{[^}]+\}', raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            description = str(data.get("description", name))[:256]
            tags = [str(t).lower()[:32] for t in data.get("domain_tags", [])[:8]]
            return description, tags
        except Exception:
            pass
    return name, []


async def _score_relevance(context: str, skills: list[dict]) -> list[tuple[dict, float]]:
    """Use LLM to score skill relevance to a context description."""
    if not skills:
        return []

    skill_list = "\n".join(
        f"{i+1}. [{s['name']}] {s['description']} (tags: {', '.join(s['domain_tags'])})"
        for i, s in enumerate(skills)
    )
    prompt = f"""/no_think
Context: "{context}"

Rate each skill's relevance to this context on a scale 0.0-1.0.
Return JSON array of numbers in the same order as the list.
Only return the JSON array, nothing else.

Skills:
{skill_list}

Example output: [0.9, 0.1, 0.7, 0.0]
"""
    raw = await _llm(prompt)
    match = re.search(r'\[[\d.,\s]+\]', raw)
    if match:
        try:
            scores = json.loads(match.group())
            return [(skill, float(scores[i]) if i < len(scores) else 0.0)
                    for i, skill in enumerate(skills)]
        except Exception:
            pass
    # fallback: return all with equal score
    return [(s, 0.5) for s in skills]


# ── Qdrant helpers ─────────────────────────────────────────────────────────────

def _to_skill_record(point, store_data: dict | None = None) -> dict:
    """Build a skill dict from a Qdrant point, optionally hydrated from SQLite."""
    p = point.payload
    sd = store_data or {}
    meta = sd.get("metadata", {})
    # Content: SQLite is authoritative; Qdrant payload is fallback
    content = sd.get("content") or p.get("content", "")
    name = meta.get("skill_name") or p.get("skill_name", "unknown")
    description = meta.get("description") or p.get("skill_description", p.get("content", "")[:100])
    platform = meta.get("platform") or p.get("platform", "claude")
    reference_url = meta.get("reference_url") or p.get("reference_url")
    return {
        "id": str(point.id),
        "name": name,
        "description": description,
        "platform": platform,
        "agent_id": p.get("agent_id", "shared"),
        "domain_tags": p.get("domain_tags", []),
        "importance_score": p.get("importance_score", 0.5),
        "timestamp": p.get("timestamp", ""),
        "content": content,
        "install_path": f"~/.claude/skills/{name}/SKILL.md",
        # Counters — Qdrant values used as fallback; SQLite is authoritative (see _hydrate_counters_bulk)
        "usage_count": p.get("usage_count", 0),
        "helpful_count": p.get("helpful_count", 0),
        "usefulness_score": p.get("usefulness_score", 1.0),
        "suppressed": p.get("suppressed", False),
        "pinned": p.get("pinned", False),
        "reference_url": reference_url,
    }


async def _write_skill_to_store(
    memory_id: str,
    content: str,
    skill_name: str,
    description: str,
    platform: str,
    reference_url: str | None = None,
) -> None:
    """Dual-write skill content + metadata to SQLite memory_store."""
    from app.services.memory_store import get_memory_store
    await get_memory_store().upsert(
        memory_id, "skill", content,
        {"skill_name": skill_name, "description": description,
         "platform": platform, "reference_url": reference_url},
    )


async def _hydrate_counters_bulk(skills: list[dict]) -> list[dict]:
    """Overwrite counter fields with authoritative SQLite values (if present)."""
    from app.services.skill_counters import get_skill_counters
    ids = [s["id"] for s in skills]
    meta = await get_skill_counters().get_many(ids)
    for s in skills:
        m = meta.get(s["id"])
        if m:
            s["usage_count"] = m["usage_count"]
            s["helpful_count"] = m["helpful_count"]
            s["usefulness_score"] = m["usefulness_score"]
            s["pinned"] = bool(m["pinned"])
    return skills


async def _hydrate_content_bulk(skills: list[dict]) -> list[dict]:
    """Overwrite content/metadata fields with authoritative SQLite values (if present)."""
    from app.services.memory_store import get_memory_store
    ids = [s["id"] for s in skills]
    store_data = await get_memory_store().get_many(ids)
    for s in skills:
        sd = store_data.get(s["id"])
        if sd:
            meta = sd.get("metadata", {})
            s["content"] = sd.get("content") or s["content"]
            if meta.get("skill_name"):
                s["name"] = meta["skill_name"]
                s["install_path"] = f"~/.claude/skills/{meta['skill_name']}/SKILL.md"
            if meta.get("description"):
                s["description"] = meta["description"]
            if meta.get("platform"):
                s["platform"] = meta["platform"]
            if meta.get("reference_url"):
                s["reference_url"] = meta["reference_url"]
    return skills


async def _scroll_skills(
    qdrant,
    domain_filter: Optional[list[str]] = None,
    limit: int = 50,
    include_suppressed: bool = False,
) -> list[dict]:
    must: list = [qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill"))]
    if domain_filter:
        must.append(qmodels.FieldCondition(
            key="domain_tags",
            match=qmodels.MatchAny(any=domain_filter),
        ))
    must_not: list = []
    if not include_suppressed:
        must_not.append(qmodels.FieldCondition(key="suppressed", match=qmodels.MatchValue(value=True)))
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must, must_not=must_not or None),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    skills = [_to_skill_record(r) for r in results]
    skills = await _hydrate_content_bulk(skills)
    return await _hydrate_counters_bulk(skills)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/domains")
async def list_domains():
    """List known domain tags for filtering."""
    return {"domains": KNOWN_DOMAINS}


async def _bg_retag_skill(memory_id: str, content: str, current_desc: str, current_tags: list,
                          qdrant, ollama) -> None:
    """Background task: enrich skill description/tags via LLM after publish returns."""
    try:
        meta = await _infer_skill_all(content)
        new_desc = current_desc or meta.get("description") or ""
        new_tags = current_tags or meta.get("domain_tags") or []
        if not new_desc and not new_tags:
            return
        patch: dict = {}
        if not current_desc and new_desc:
            patch["skill_description"] = new_desc
        if not current_tags and new_tags:
            patch["domain_tags"] = new_tags
            patch["tags"] = new_tags
        if patch:
            await qdrant._client.set_payload(
                collection_name=qdrant._collection,
                payload=patch,
                points=[memory_id],
            )
            # Re-embed with enriched text
            skill_name = content.split("\n")[0].lstrip("# ").strip()
            new_vector = await ollama.embed(f"{skill_name} {new_desc} {' '.join(new_tags)}")
            await qdrant._client.update_vectors(
                collection_name=qdrant._collection,
                points=[{"id": memory_id, "vector": new_vector}],
            )
            # Patch SQLite metadata with enriched fields
            from app.services.memory_store import get_memory_store
            meta_patch: dict = {}
            if new_desc:
                meta_patch["description"] = new_desc
            if skill_name:
                meta_patch["skill_name"] = skill_name
            if meta_patch:
                await get_memory_store().patch_metadata(memory_id, meta_patch)
            logger.info("bg_retag: enriched skill %s desc=%s tags=%s", memory_id, bool(new_desc), new_tags)
    except Exception as e:
        logger.warning("bg_retag failed for %s: %s", memory_id, e)


@router.post("/publish", response_model=SkillRecord)
async def publish_skill(body: SkillPublish, qdrant: QdrantDep, ollama: OllamaDep,
                        background_tasks: BackgroundTasks):
    """Publish a skill to the shared marketplace with auto-extracted domain tags."""
    from app.services.learning_store import make_context_signature

    t0 = perf_counter()
    description = body.description
    domain_tags = body.domain_tags

    # Auto-extract via LLM if not provided (fast path: parse SKILL.md structure)
    if not description or not domain_tags:
        try:
            llm_desc, llm_tags = await _extract_skill_meta(body.name, body.content)
            description = description or llm_desc
            domain_tags = domain_tags or llm_tags
        except Exception as e:
            logger.warning("LLM meta extraction failed: %s", e)
            description = description or body.name
            domain_tags = domain_tags or []

    from app.models.memory import MemoryCreate
    from app.models.enums import MemoryType

    mem = MemoryCreate(
        content=body.content[:10000],
        agent_id=body.agent_id,
        memory_type=MemoryType.context,
        category="skill",
        importance_score=body.importance_score,
        source=f"skill-publish:{body.name}",
        tags=[body.name, body.platform] + domain_tags,
        session_id=None,
    )

    # Store extra fields in payload via raw upsert
    vector = await ollama.embed(f"{body.name} {description} {' '.join(domain_tags)}")
    memory_id = await qdrant.insert(mem, vector)

    # Patch payload with skill-specific fields
    extra_payload: dict = {
        "skill_name": body.name,
        "skill_description": description,
        "platform": body.platform,
        "domain_tags": domain_tags,
        "pinned": body.pinned,
    }
    if body.reference_url:
        extra_payload["reference_url"] = body.reference_url
    await qdrant._client.set_payload(
        collection_name=qdrant._collection,
        payload=extra_payload,
        points=[str(memory_id)],
    )
    # Dual-write content + metadata to SQLite
    await _write_skill_to_store(
        str(memory_id), body.content, body.name, description,
        body.platform, body.reference_url,
    )

    # If description or tags still missing — schedule background LLM retag (non-blocking)
    if not description or not domain_tags:
        background_tasks.add_task(
            _bg_retag_skill, str(memory_id), body.content,
            description, domain_tags, qdrant, ollama,
        )

    from app.services.event_emitter import emit
    duration_s = max(0.0, perf_counter() - t0)
    ctx_sig_tool = make_context_signature(
        project="supermemory",
        task_type="tool",
        phase="call",
        category="skill_publish",
        transport="api",
    )
    ctx_sig_evt = make_context_signature(
        project="supermemory",
        task_type="skills",
        phase="publish",
        category="skill",
        transport="api",
    )
    background_tasks.add_task(
        emit,
        "tool_call",
        agent_id=body.agent_id or "",
        project="supermemory",
        transport="api",
        context_signature=ctx_sig_tool,
        payload={"tool_name": "skill_publish", "duration_s": duration_s},
    )
    background_tasks.add_task(
        emit,
        "tool_result",
        agent_id=body.agent_id or "",
        project="supermemory",
        transport="api",
        context_signature=ctx_sig_tool,
        payload={"tool_name": "skill_publish", "success": True, "empty": False},
    )
    background_tasks.add_task(emit, "skill_published",
        agent_id=body.agent_id or "",
        project="supermemory",
        transport="api",
        context_signature=ctx_sig_evt,
        payload={"skill_name": body.name, "platform": body.platform,
                 "domain_tags": domain_tags})

    return SkillRecord(
        id=str(memory_id),
        name=body.name,
        description=description,
        platform=body.platform,
        agent_id=body.agent_id,
        domain_tags=domain_tags,
        importance_score=body.importance_score,
        timestamp="",
        install_path=f"~/.claude/skills/{body.name}/SKILL.md",
        pinned=body.pinned,
        reference_url=body.reference_url,
    )


@router.get("/search", response_model=list[SkillRecord])
async def search_skills(
    qdrant: QdrantDep,
    context: Optional[str] = Query(None, description="Current task/project context for LLM relevance scoring"),
    domains: Optional[str] = Query(None, description="Comma-separated domain tags to filter by"),
    platform: Optional[str] = Query(None, description="claude | codex | cursor | universal"),
    limit: int = Query(20, ge=1, le=100),
    min_relevance: float = Query(0.3, description="Min relevance score when context is provided"),
):
    """Search skills with optional LLM-based context filtering."""
    domain_filter = [d.strip() for d in domains.split(",")] if domains else None
    skills = await _scroll_skills(qdrant, domain_filter=domain_filter, limit=limit * 2)

    # Filter by platform
    if platform:
        skills = [s for s in skills if s["platform"] in (platform, "universal")]

    if not context or not skills:
        return [SkillRecord(**{k: v for k, v in s.items() if k != "content"}) for s in skills[:limit]]

    # LLM scoring
    try:
        scored = await _score_relevance(context, skills)
        scored.sort(key=lambda x: x[1], reverse=True)
        skills = [s for s, score in scored if score >= min_relevance][:limit]
    except Exception as e:
        logger.warning("LLM relevance scoring failed: %s", e)

    return [SkillRecord(**{k: v for k, v in s.items() if k != "content"}) for s in skills]


@router.get("/pinned", response_model=list[SkillRecord])
async def get_pinned_skills(qdrant: QdrantDep):
    """Return all pinned skills (always-visible references, e.g. emergency contacts)."""
    from app.services.skill_counters import get_skill_counters
    store = get_skill_counters()

    # Primary: query SQLite for pinned IDs, fetch from Qdrant by ID
    pinned_ids = await store.get_pinned_ids()
    skills: list[dict] = []
    if pinned_ids:
        try:
            points = await qdrant._client.retrieve(
                collection_name=qdrant._collection,
                ids=pinned_ids,
                with_payload=True,
                with_vectors=False,
            )
            skills = [_to_skill_record(p) for p in points]
        except Exception:
            pass

    # Fallback: also check Qdrant pinned flag (for skills pinned before migration)
    must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
        qmodels.FieldCondition(key="pinned", match=qmodels.MatchValue(value=True)),
    ]
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=50,
        with_payload=True,
        with_vectors=False,
    )
    seen = {s["id"] for s in skills}
    for r in results:
        if str(r.id) not in seen:
            skills.append(_to_skill_record(r))

    skills = await _hydrate_counters_bulk(skills)
    return [SkillRecord(**{k: v for k, v in s.items() if k != "content"}) for s in skills]


@router.patch("/{skill_id}/pin", response_model=SkillRecord)
async def pin_skill(skill_id: str, qdrant: QdrantDep, pinned: bool = True):
    """Pin or unpin a skill. Pinned skills are always included in get_onboarding."""
    from qdrant_client.http import models as qm
    try:
        results, _ = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
            ]),
            limit=1000,
            with_payload=True,
            with_vectors=False,
        )
        point = next((r for r in results if str(r.id) == skill_id), None)
        if not point:
            raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")
        # SQLite is authoritative for pinned state
        from app.services.skill_counters import get_skill_counters
        await get_skill_counters().set_pinned(skill_id, pinned)
        # Keep Qdrant in sync so legacy filter queries still work
        await qdrant._client.set_payload(
            collection_name=qdrant._collection,
            payload={"pinned": pinned},
            points=[skill_id],
        )
        skill = _to_skill_record(point)
        skill["pinned"] = pinned
        return SkillRecord(**{k: v for k, v in skill.items() if k != "content"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


_REGENERATE_SKILL_PROMPT = """/no_think
Generate a complete SKILL.md for an AI coding assistant skill.

Skill name: {name}
Description: {description}
Domain tags: {tags}

The SKILL.md must follow this exact structure:
# {title}

One sentence describing what this skill does.

## When to use

- Bullet list of 2-4 trigger conditions (when should an agent invoke this skill?)

## Instructions

Step-by-step instructions for executing this skill.
Be precise and actionable. Reference specific tools, APIs, or commands where relevant.

## Examples

Show 1-2 concrete examples of inputs and expected outputs.

Start directly with "# ". Do not add any preamble. Be concise — aim for ~400-600 words.
"""


async def _regenerate_single_skill(skill_id: str, name: str, description: str, tags: list[str], qdrant, ollama) -> bool:
    """Generate SKILL.md content for a skill that has no content. Returns True on success."""
    from app.services.cloud_llm import cloud_available, cloud_complete
    title = name.replace("-", " ").title()
    prompt = _REGENERATE_SKILL_PROMPT.format(
        name=name,
        title=title,
        description=description or name,
        tags=", ".join(tags) if tags else "general",
    )
    try:
        if cloud_available():
            content = await cloud_complete(prompt, max_tokens=1024, temperature=0.3)
        else:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    f"{settings.ollama_base_url}/api/generate",
                    json={"model": MANAGER_MODEL, "prompt": prompt, "stream": False},
                )
                r.raise_for_status()
                content = r.json()["response"].strip()
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

        if not content or len(content) < 50:
            return False

        # Update Qdrant payload
        await qdrant._client.set_payload(
            collection_name=qdrant._collection,
            payload={"content": content},
            points=[skill_id],
        )
        # Re-embed with content
        embed_text = f"{name} {description} {' '.join(tags)} {content[:300]}"
        new_vector = await ollama.embed(embed_text)
        await qdrant._client.update_vectors(
            collection_name=qdrant._collection,
            points=[{"id": skill_id, "vector": new_vector}],
        )
        # Update content in SQLite
        from app.services.memory_store import get_memory_store
        store = get_memory_store()
        existing = await store.get(skill_id)
        if existing:
            await store.upsert(skill_id, "skill", content, existing.get("metadata", {}))
        else:
            await _write_skill_to_store(skill_id, content, name, description, "claude")
        return True
    except Exception as e:
        logger.warning("regenerate_single_skill failed for %s: %s", skill_id, e)
        return False


async def _regenerate_content_handler(payload: dict) -> dict:
    """Job queue handler: regenerate SKILL.md content for all empty skills."""
    from app.dependencies import get_qdrant, get_ollama
    qdrant = get_qdrant()
    ollama = get_ollama()

    min_content_len = payload.get("min_content_len", 100)
    dry_run = payload.get("dry_run", False)

    # Scroll all skills
    must = [qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill"))]
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=1000,
        with_payload=True,
        with_vectors=False,
    )

    empty_skills = []
    for r in results:
        p = r.payload
        content = (p.get("content") or "").strip()
        if len(content) < min_content_len:
            empty_skills.append({
                "id": str(r.id),
                "name": p.get("skill_name", "unknown"),
                "description": p.get("skill_description", ""),
                "tags": p.get("domain_tags", []),
            })

    logger.info("regenerate_content: found %d skills with short/empty content (dry_run=%s)", len(empty_skills), dry_run)

    if dry_run:
        return {"dry_run": True, "would_regenerate": len(empty_skills), "total": len(results)}

    success = 0
    failed = 0
    for skill in empty_skills:
        ok = await _regenerate_single_skill(
            skill["id"], skill["name"], skill["description"], skill["tags"], qdrant, ollama,
        )
        if ok:
            success += 1
        else:
            failed += 1
        # small delay to avoid hammering GLM
        import asyncio
        await asyncio.sleep(0.3)

    logger.info("regenerate_content: done — success=%d failed=%d", success, failed)
    return {
        "total_skills": len(results),
        "candidates": len(empty_skills),
        "regenerated": success,
        "failed": failed,
    }


@router.post("/regenerate-content")
async def regenerate_skill_content(
    qdrant: QdrantDep,
    ollama: OllamaDep,
    dry_run: bool = Query(False, description="If true, only count skills to regenerate — don't call LLM"),
    min_content_len: int = Query(100, description="Skills with content shorter than this are regenerated"),
):
    """
    Regenerate SKILL.md content for skills that have empty or stub-only content.
    Uses GLM (or local fallback) to generate full SKILL.md from name + description + domain_tags.
    Runs as a background job. Poll GET /tasks/{job_id} for progress.
    """
    from app.services.job_queue import get_job_queue
    job_id = await get_job_queue().submit("regenerate_skill_content", {
        "dry_run": dry_run,
        "min_content_len": min_content_len,
    })
    return {"job_id": job_id, "status": "queued", "dry_run": dry_run}


_GENERATE_DOMAIN_SKILL_PROMPT = """/no_think
You are a senior engineer writing a concise best-practices guide for an AI coding assistant.

Generate a SKILL.md for the domain(s): {domains}
{context_line}

The file must follow this exact structure (markdown):

# Best Practices: {domains_title}

One sentence: what this skill covers.

## When to use

- Trigger condition 1
- Trigger condition 2
- (2-4 bullets max)

## Key practices

Short numbered list of the most important rules/patterns for this domain.
Be concrete and actionable. Max 8 items.

## Common pitfalls

- Pitfall 1 and how to avoid it
- Pitfall 2 and how to avoid it
(2-4 bullets)

## Quick reference

Most useful commands, API calls, or code snippets for this domain (if applicable).

Start directly with "# Best Practices:". Be concise — the whole file should fit in ~600 words.
"""


@router.post("/generate-for-domain")
async def generate_skill_for_domain(body: GenerateForDomainRequest, qdrant: QdrantDep, ollama: OllamaDep):
    """
    Generate a best-practices skill for given domains via LLM and auto-publish it.
    Used as fallback when skill pack returns empty results.
    """
    # Deduplicate domains
    domains = list(dict.fromkeys(d.strip().lower() for d in body.domains if d.strip()))[:6]
    if not domains:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="At least one domain required")

    # Check if we already have a generated skill for these domains (avoid duplicates)
    existing = await _scroll_skills(qdrant, domain_filter=domains, limit=3)
    auto_existing = [s for s in existing if "auto-generated" in s.get("domain_tags", [])]
    if auto_existing:
        # Return existing auto-generated skill content immediately
        s = auto_existing[0]
        return {
            "generated": False,
            "reused": True,
            "skill_id": s["id"],
            "skill_name": s["name"],
            "content": s.get("content", ""),
            "domains": domains,
        }

    domains_title = " + ".join(d.capitalize() for d in domains)
    context_line = f"Task context: {body.task_context}" if body.task_context else ""

    prompt = _GENERATE_DOMAIN_SKILL_PROMPT.format(
        domains=", ".join(domains),
        domains_title=domains_title,
        context_line=context_line,
    )

    try:
        skill_content = await _llm(prompt)
    except Exception as e:
        logger.warning("LLM generation failed for domains %s: %s", domains, e)
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=f"LLM unavailable: {e}")

    # Build skill name from domains
    skill_name = "best-practices-" + "-".join(domains[:3])

    # Publish to marketplace with low importance (auto-generated, unreviewed)
    from app.models.memory import MemoryCreate
    from app.models.enums import MemoryType

    all_tags = domains + ["auto-generated", "best-practices"]
    description = f"Auto-generated best practices for: {', '.join(domains)}"

    mem = MemoryCreate(
        content=skill_content[:10000],
        agent_id=body.agent_id,
        memory_type=MemoryType.context,
        category="skill",
        importance_score=0.4,
        source=f"skill-generate:{skill_name}",
        tags=[skill_name, body.platform] + all_tags,
        session_id=None,
    )
    vector = await ollama.embed(f"{skill_name} {description} {' '.join(domains)}")
    memory_id = await qdrant.insert(mem, vector)

    await qdrant._client.set_payload(
        collection_name=qdrant._collection,
        payload={
            "skill_name": skill_name,
            "skill_description": description,
            "platform": body.platform,
            "domain_tags": all_tags,
            "content": skill_content,
            # Step 8: auto-generated skills require human review before becoming active
            "review_status": "pending_review",
            "suppressed": True,
            "auto_generated": True,
        },
        points=[str(memory_id)],
    )
    # Dual-write to SQLite
    await _write_skill_to_store(str(memory_id), skill_content, skill_name, description, body.platform)

    logger.info("Auto-generated skill '%s' for domains %s (id=%s, pending review)", skill_name, domains, memory_id)

    return {
        "generated": True,
        "reused": False,
        "skill_id": str(memory_id),
        "skill_name": skill_name,
        "content": skill_content,
        "domains": domains,
    }


@router.post("/profile", response_model=TaskProfileResult)
async def profile_task(body: TaskProfileRequest) -> TaskProfileResult:
    """Fast keyword-based task profiling — returns domain tags for skill pack selection."""
    started_at = perf_counter()
    text_lower = body.text.lower()

    # Domain keyword map (no LLM, deterministic, fast)
    domain_hints: dict[str, list[str]] = {
        "memory": ["memory", "remember", "recall", "супрапамять", "забудь", "сохрани в памят"],
        "skill": ["skill", "скилл", "умение", "инструкц"],
        "api": ["api", "endpoint", "rest", "http", "request", "curl", "fetch"],
        "frontend": ["ui", "component", "react", "vue", "css", "html", "frontend", "layout"],
        "backend": ["backend", "server", "fastapi", "django", "flask", "express"],
        "database": ["database", "sql", "qdrant", "redis", "postgres", "mongo", "query"],
        "llm": ["llm", "ai", "ml", "model", "embedding", "rag", "prompt", "claude", "gpt"],
        "deploy": ["deploy", "docker", "kubernetes", "ci", "cd", "pipeline", "release"],
        "git": ["git", "commit", "branch", "merge", "pr", "pull request"],
        "testing": ["test", "pytest", "unittest", "coverage", "mock"],
        "debugging": ["debug", "error", "bug", "fix", "issue", "traceback", "exception"],
        "python": ["python", "pip", "venv", "pydantic", "async", "asyncio"],
        "typescript": ["typescript", "ts", "tsx", "node", "npm", "yarn"],
        "linux": ["linux", "bash", "shell", "chmod", "systemd", "cron"],
        "cnc": ["cnc", "gcode", "linuxcnc", "hal", "machining", "spindle"],
    }

    task_type_hints: dict[str, list[str]] = {
        "bug": ["bug", "fix", "error", "broken", "crash", "fail", "не работает", "сломал"],
        "feature": ["add", "implement", "create", "новый", "добавь", "сделай", "реализуй"],
        "research": ["what", "how", "why", "explain", "что такое", "как", "почему", "объясни"],
        "coding": ["code", "function", "class", "write", "напиши", "код", "функци"],
    }

    domain_hints["memory"].extend([
        "\u0441\u0443\u043f\u0440\u0430\u043f\u0430\u043c\u044f\u0442\u044c",
        "\u0437\u0430\u0431\u0443\u0434\u044c",
        "\u0441\u043e\u0445\u0440\u0430\u043d\u0438 \u0432 \u043f\u0430\u043c\u044f\u0442",
    ])
    domain_hints["skill"].extend([
        "\u0441\u043a\u0438\u043b\u043b",
        "\u0443\u043c\u0435\u043d\u0438\u0435",
        "\u0438\u043d\u0441\u0442\u0440\u0443\u043a\u0446",
    ])
    task_type_hints = {
        "bug": [
            "bug", "fix", "error", "broken", "crash", "fail",
            "\u043d\u0435 \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442",
            "\u0441\u043b\u043e\u043c\u0430\u043b",
            "\u043f\u043e\u0447\u0438\u043d\u0438",
        ],
        "feature": [
            "add", "implement", "create",
            "\u043d\u043e\u0432\u044b\u0439",
            "\u0434\u043e\u0431\u0430\u0432\u044c",
            "\u0441\u0434\u0435\u043b\u0430\u0439",
            "\u0440\u0435\u0430\u043b\u0438\u0437\u0443\u0439",
            "\u0444\u0438\u0447\u0443",
            "\u0444\u0438\u0447\u0430",
        ],
        "research": [
            "what", "how", "why", "explain",
            "\u0447\u0442\u043e \u0442\u0430\u043a\u043e\u0435",
            "\u043a\u0430\u043a",
            "\u043f\u043e\u0447\u0435\u043c\u0443",
            "\u043e\u0431\u044a\u044f\u0441\u043d\u0438",
        ],
        "coding": [
            "code", "function", "class", "write",
            "\u043d\u0430\u043f\u0438\u0448\u0438",
            "\u043a\u043e\u0434",
            "\u0444\u0443\u043d\u043a\u0446\u0438",
        ],
    }

    matched_domains: list[str] = []
    for domain, keywords in domain_hints.items():
        if any(kw in text_lower for kw in keywords):
            matched_domains.append(domain)

    task_type = "other"
    for ttype, keywords in task_type_hints.items():
        if any(kw in text_lower for kw in keywords):
            task_type = ttype
            break

    confidence = min(1.0, len(matched_domains) * 0.3 + 0.2) if matched_domains else 0.1

    result = TaskProfileResult(
        task_type=task_type,
        domains=matched_domains[:6],
        confidence=round(confidence, 2),
    )
    _track_adaptive(
        "task_profile",
        success=True,
        started_at=started_at,
        metadata={
            "domains_count": len(result.domains),
            "task_type": result.task_type,
            "confidence": result.confidence,
        },
    )
    return result


class InferDomainRequest(BaseModel):
    agent_id: str = Field(..., description="Agent identifier to look up history for")
    session_hints: list[str] = Field(default_factory=list, description="Tool names or tags from current session")


class InferDomainResult(BaseModel):
    domain: str
    confidence: float
    all_domains: list[str]
    signals: list[str]  # what evidence led to this inference


@router.post("/infer-domain", response_model=InferDomainResult)
async def infer_agent_domain(body: InferDomainRequest, qdrant: QdrantDep) -> InferDomainResult:
    """
    Infer the agent's working domain from its memory history and session hints.

    Called by get_onboarding when task_description is absent — the 'lost child' case.
    Answers: "what domain is this agent working in?" based on accumulated evidence.
    """
    # 1. Query recent memories for this agent (last 30 days)
    must = [qmodels.FieldCondition(key="agent_id", match=qmodels.MatchValue(value=body.agent_id))]
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=100,
        with_payload=True,
        with_vectors=False,
    )

    # 2. Count domain tag frequency from memory tags
    domain_counts: dict[str, int] = {}
    known_domain_set = set(KNOWN_DOMAINS)
    signals: list[str] = []

    for point in results:
        tags = point.payload.get("tags", [])
        for tag in tags:
            tag_lower = tag.lower().strip()
            if tag_lower in known_domain_set:
                domain_counts[tag_lower] = domain_counts.get(tag_lower, 0) + 1

    # 3. Also score session hints (tools used, query keywords)
    for hint in body.session_hints:
        hint_lower = hint.lower()
        for domain in KNOWN_DOMAINS:
            if domain in hint_lower:
                domain_counts[domain] = domain_counts.get(domain, 0) + 2  # session hints weighted higher

    if not domain_counts:
        return InferDomainResult(
            domain="general",
            confidence=0.1,
            all_domains=[],
            signals=["no history found — new agent"],
        )

    # 4. Sort by frequency
    sorted_domains = sorted(domain_counts.items(), key=lambda x: -x[1])
    top_domain, top_count = sorted_domains[0]
    total_signals = sum(v for _, v in sorted_domains)

    confidence = round(min(0.95, top_count / max(total_signals, 1) * 2), 2)
    all_domains = [d for d, _ in sorted_domains[:6]]

    for d, cnt in sorted_domains[:3]:
        signals.append(f"{d}: {cnt} occurrences in history")

    return InferDomainResult(
        domain=top_domain,
        confidence=confidence,
        all_domains=all_domains,
        signals=signals,
    )


@router.get("/pack", response_model=list[SkillPackItem])
async def get_skill_pack(
    qdrant: QdrantDep,
    task_tags: str = Query(..., description="Comma-separated domain tags"),
    agent_id: Optional[str] = Query(None, description="Optional agent scope for preference-aware ranking"),
    limit: int = Query(5, ge=1, le=10),
) -> list[SkillPackItem]:
    """Return minimal skill pack for given task tags (fast, no LLM)."""
    started_at = perf_counter()
    tags = [t.strip() for t in task_tags.split(",") if t.strip()]
    if not tags:
        _track_adaptive(
            "skill_pack_fast",
            success=True,
            started_at=started_at,
            metadata={"requested_tags": 0, "returned_skills": 0, "limit": limit},
        )
        return []

    skills = await _scroll_skills(qdrant, domain_filter=tags, limit=limit * 3)
    preferences = await _load_user_preferences(qdrant, agent_id)

    # Prefer skills that already proved useful; importance_score stays a tie-breaker.
    skills.sort(
        key=lambda s: (
            _preference_match_score(s, preferences),
            s.get("usefulness_score", 1.0),
            s.get("importance_score", 0.5),
        ),
        reverse=True,
    )

    items = [
        SkillPackItem(
            id=s["id"],
            name=s["name"],
            description=s["description"],
            domain_tags=s["domain_tags"],
            content=s.get("content", ""),
        )
        for s in skills[:limit]
        if s.get("content")  # only skills with actual content
    ]
    _track_adaptive(
        "skill_pack_fast",
        success=True,
        started_at=started_at,
        metadata={
            "requested_tags": len(tags),
            "returned_skills": len(items),
            "limit": limit,
            "preferences_used": len(preferences),
        },
    )
    return items


# ── Pack schemas ───────────────────────────────────────────────────────────────

class PackCreateRequest(BaseModel):
    domains: list[str] = Field(..., min_length=1, max_length=8)
    task_type: str = Field("other")
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    agent_id: str = Field("default", max_length=256)
    limit: int = Field(5, ge=1, le=10)


class PackResponse(BaseModel):
    pack_id: str
    phase: str          # "immediate" | "enriched"
    status: str         # "initial" | "enriched" | "empty"
    skills: list[SkillPackItem]
    domains: list[str]
    confidence: float
    enrichment_pending: bool
    created_at: str


class SkillOutcomeRequest(BaseModel):
    pack_id: str = Field(..., description="Pack ID from pack/create")
    skills_helpful: list[str] = Field(default_factory=list, description="Skill IDs that helped")
    skills_unused: list[str] = Field(default_factory=list, description="Skill IDs selected but not used")
    missing_domains: list[str] = Field(default_factory=list, description="Domains where guidance was lacking")
    success: bool = Field(True)
    agent_id: str = Field("default", max_length=256)


# ── Pack helpers ────────────────────────────────────────────────────────────────

async def _store_pack(qdrant, ollama, pack_id: str, agent_id: str,
                      domains: list[str], task_type: str,
                      skill_ids: list[str], phase: str, confidence: float,
                      enrichment_pending: bool) -> None:
    """Persist pack record to Qdrant for tracing and outcome linking."""
    from app.models.memory import MemoryCreate
    from app.models.enums import MemoryType

    content = f"skill_pack:{pack_id} phase={phase} domains={','.join(domains)} task_type={task_type}"
    vector = await ollama.embed(content)
    mem = MemoryCreate(
        content=content,
        agent_id=agent_id,
        memory_type=MemoryType.context,
        category="skill_pack",
        importance_score=0.3,
        source=f"skill-pack:{pack_id}",
        tags=["skill_pack", f"phase:{phase}"] + domains,
        session_id=None,
    )
    memory_id = await qdrant.insert(mem, vector)
    await qdrant._client.set_payload(
        collection_name=qdrant._collection,
        payload={
            "pack_id": pack_id,
            "pack_phase": phase,
            "pack_status": "initial",
            "pack_domains": domains,
            "pack_task_type": task_type,
            "pack_skill_ids": skill_ids,
            "pack_confidence": confidence,
            "pack_enrichment_pending": enrichment_pending,
        },
        points=[str(memory_id)],
    )


async def _do_enrich(pack_id: str, domains: list[str], agent_id: str,
                     task_context: str, qdrant, ollama) -> None:
    """Background task: generate missing skills for domains, update pack status."""
    try:
        # Check which domains already have skills
        existing = await _scroll_skills(qdrant, domain_filter=domains, limit=10)
        covered = set()
        for s in existing:
            for d in s.get("domain_tags", []):
                covered.add(d)
        gap_domains = [d for d in domains if d not in covered]

        if gap_domains:
            prompt = _GENERATE_DOMAIN_SKILL_PROMPT.format(
                domains=", ".join(gap_domains),
                domains_title=" + ".join(d.capitalize() for d in gap_domains),
                context_line=f"Task context: {task_context}" if task_context else "",
            )
            skill_content = await _llm(prompt)
            skill_name = "best-practices-" + "-".join(gap_domains[:3])
            from app.models.memory import MemoryCreate
            from app.models.enums import MemoryType
            all_tags = gap_domains + ["auto-generated", "best-practices"]
            mem = MemoryCreate(
                content=skill_content[:10000],
                agent_id=agent_id,
                memory_type=MemoryType.context,
                category="skill",
                importance_score=0.4,
                source=f"skill-generate:{skill_name}",
                tags=[skill_name, "claude"] + all_tags,
                session_id=None,
            )
            vector = await ollama.embed(f"{skill_name} {' '.join(gap_domains)}")
            memory_id = await qdrant.insert(mem, vector)
            # Fix 2712d6ac: Phase 2 enriched skills are immediately active for the current task
            # (review_status="task_enrichment" — visible in review queue but not suppressed).
            # Contrast with /generate-for-domain which uses "pending_review" + suppressed=True.
            enrich_desc = f"Auto-generated best practices for: {', '.join(gap_domains)}"
            await qdrant._client.set_payload(
                collection_name=qdrant._collection,
                payload={
                    "skill_name": skill_name,
                    "skill_description": enrich_desc,
                    "platform": "claude",
                    "domain_tags": all_tags,
                    "content": skill_content,
                    "review_status": "task_enrichment",
                    "suppressed": False,
                    "auto_generated": True,
                    "enriched_for_pack": pack_id,
                },
                points=[str(memory_id)],
            )
            # Dual-write to SQLite
            await _write_skill_to_store(str(memory_id), skill_content, skill_name, enrich_desc, "claude")
            logger.info("Enrichment: generated skill '%s' for pack %s (active, task_enrichment)", skill_name, pack_id)

        # Mark pack as enriched in Qdrant
        results, _ = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=__import__("qdrant_client").http.models.Filter(
                must=[__import__("qdrant_client").http.models.FieldCondition(
                    key="pack_id",
                    match=__import__("qdrant_client").http.models.MatchValue(value=pack_id)
                )]
            ),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        if results:
            await qdrant._client.set_payload(
                collection_name=qdrant._collection,
                payload={"pack_status": "enriched", "pack_enrichment_pending": False},
                points=[str(results[0].id)],
            )
    except Exception as e:
        logger.warning("Pack enrichment failed for %s: %s", pack_id, e)


# ── Pack endpoints ──────────────────────────────────────────────────────────────

@router.post("/pack/create", response_model=PackResponse)
async def create_skill_pack(
    body: PackCreateRequest,
    background_tasks: BackgroundTasks,
    qdrant: QdrantDep,
    ollama: OllamaDep,
) -> PackResponse:
    """
    Phase 1: create a traceable skill pack with pack_id.
    Returns immediately with available skills.
    If pack is empty or weak, schedules async enrichment (Phase 2).
    """
    started_at = perf_counter()
    pack_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    preferences = await _load_user_preferences(qdrant, body.agent_id)

    # Fetch skills by domain tags (fast, no LLM)
    skills_raw = await _scroll_skills(qdrant, domain_filter=body.domains, limit=body.limit * 3)
    skills_raw.sort(
        key=lambda s: (
            _preference_match_score(s, preferences),
            s.get("usefulness_score", 1.0),
            s.get("importance_score", 0.5),
        ),
        reverse=True,
    )
    skills_raw = [s for s in skills_raw if s.get("content")][:body.limit]

    skill_ids = [s["id"] for s in skills_raw]
    enrichment_pending = len(skills_raw) < 2 or body.confidence < 0.4

    # Store pack trace (non-blocking, best-effort)
    background_tasks.add_task(
        _store_pack, qdrant, ollama, pack_id, body.agent_id,
        body.domains, body.task_type, skill_ids,
        "immediate", body.confidence, enrichment_pending,
    )

    # Schedule Phase 2 enrichment if needed
    if enrichment_pending:
        background_tasks.add_task(
            _do_enrich, pack_id, body.domains, body.agent_id,
            f"task_type={body.task_type}", qdrant, ollama,
        )

    response = PackResponse(
        pack_id=pack_id,
        phase="immediate",
        status="initial" if skills_raw else "empty",
        skills=[SkillPackItem(
            id=s["id"], name=s["name"], description=s["description"],
            domain_tags=s["domain_tags"], content=s.get("content", ""),
        ) for s in skills_raw],
        domains=body.domains,
        confidence=body.confidence,
        enrichment_pending=enrichment_pending,
        created_at=now,
    )
    _track_adaptive(
        "skill_pack_create",
        success=True,
        started_at=started_at,
        agent_id=body.agent_id,
        metadata={
            "domains_count": len(body.domains),
            "returned_skills": len(response.skills),
            "enrichment_pending": response.enrichment_pending,
            "task_type": body.task_type,
            "preferences_used": len(preferences),
        },
    )
    return response


@router.post("/pack/{pack_id}/enrich")
async def enrich_pack(
    pack_id: str,
    background_tasks: BackgroundTasks,
    qdrant: QdrantDep,
    ollama: OllamaDep,
    domains: str = Query(..., description="Comma-separated domains"),
    agent_id: str = Query("default"),
):
    """Trigger async enrichment for an existing pack. Returns immediately."""
    started_at = perf_counter()
    domain_list = [d.strip() for d in domains.split(",") if d.strip()]
    background_tasks.add_task(
        _do_enrich, pack_id, domain_list, agent_id, "", qdrant, ollama,
    )
    _track_adaptive(
        "skill_pack_enrich_request",
        success=True,
        started_at=started_at,
        agent_id=agent_id,
        metadata={"pack_id": pack_id, "domains_count": len(domain_list)},
    )
    return {"status": "enrichment_started", "pack_id": pack_id}


# ── Outcome endpoint ────────────────────────────────────────────────────────────

_EVOLVE_EVERY_N = 10  # auto-trigger evolver every N outcomes

@router.post("/outcome")
async def record_skill_outcome(
    body: SkillOutcomeRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
    queue: JobQueueDep,
) -> dict:
    """
    Record post-task skill usefulness (SkillUsefulnessReport v1).
    Updates helpful_count / usage_count on referenced skills.
    """
    from app.models.memory import MemoryCreate
    from app.models.enums import MemoryType

    started_at = perf_counter()
    report_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Persist outcome record
    all_skill_ids = list(set(body.skills_helpful + body.skills_unused))
    content = (
        f"skill_outcome pack_id={body.pack_id} "
        f"helpful={len(body.skills_helpful)} unused={len(body.skills_unused)} "
        f"missing_domains={','.join(body.missing_domains)} success={body.success}"
    )
    vector = await ollama.embed(content)
    mem = MemoryCreate(
        content=content,
        agent_id=body.agent_id,
        memory_type=MemoryType.experience,
        category="skill_outcome",
        importance_score=0.6 if body.success else 0.8,
        source=f"skill-outcome:{report_id}",
        tags=(
            ["skill_outcome", f"pack:{body.pack_id}", f"success:{body.success}"]
            + [f"helpful:{sid}" for sid in body.skills_helpful]
            + [f"unused:{sid}" for sid in body.skills_unused]
            + body.missing_domains
        ),
        session_id=None,
    )
    await qdrant.insert(mem, vector)

    # Increment counters atomically in SQLite (no Qdrant round-trips needed)
    from app.services.skill_counters import get_skill_counters
    counters = get_skill_counters()

    for skill_id in body.skills_helpful:
        try:
            await counters.increment_helpful(skill_id)
        except Exception as e:
            logger.warning("Failed to update skill %s counts: %s", skill_id, e)

    for skill_id in body.skills_unused:
        try:
            await counters.increment_usage(skill_id)
        except Exception as e:
            logger.warning("Failed to update skill %s usage: %s", skill_id, e)

    logger.info(
        "Outcome recorded: pack=%s helpful=%d unused=%d missing=%s success=%s",
        body.pack_id, len(body.skills_helpful), len(body.skills_unused),
        body.missing_domains, body.success,
    )

    response = {
        "recorded": True,
        "report_id": report_id,
        "pack_id": body.pack_id,
        "created_at": now,
        "stats": {
            "helpful": len(body.skills_helpful),
            "unused": len(body.skills_unused),
            "missing_domains": len(body.missing_domains),
            "success": body.success,
        },
    }
    _track_adaptive(
        "skill_outcome_report",
        success=body.success,
        started_at=started_at,
        agent_id=body.agent_id,
        metadata={
            "helpful": len(body.skills_helpful),
            "unused": len(body.skills_unused),
            "missing_domains": len(body.missing_domains),
        },
    )

    # Auto-trigger evolver every N outcomes
    try:
        outcome_count, _ = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill_outcome"))
            ]),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        total = getattr(outcome_count, "__len__", lambda: 0)()
        # Use scroll count via count API
        count_result = await qdrant._client.count(
            collection_name=qdrant._collection,
            count_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill_outcome"))
            ]),
            exact=False,
        )
        total = count_result.count
        if total > 0 and total % _EVOLVE_EVERY_N == 0:
            await queue.submit("evolve_skills", {"triggered_by": "auto", "outcome_count": total})
            logger.info("Auto-triggered evolve_skills at outcome_count=%d", total)
    except Exception as e:
        logger.debug("Auto-evolve check failed: %s", e)

    return response


# ── Domain gaps & analytics ─────────────────────────────────────────────────────

_OUTCOME_SYSTEM_TAGS = {"skill_outcome"}
_OUTCOME_TAG_PREFIXES = ("pack:", "success:", "helpful:", "unused:", "session:")


def _is_domain_tag(tag: str) -> bool:
    """Return True if this tag is a missing-domain name (not a system tag)."""
    if tag in _OUTCOME_SYSTEM_TAGS:
        return False
    return not any(tag.startswith(p) for p in _OUTCOME_TAG_PREFIXES)


@router.get("/gaps")
async def get_domain_gaps(
    qdrant: QdrantDep,
    agent_id: str = Query("default", max_length=256),
    min_count: int = Query(2, ge=1, le=100, description="Minimum occurrences to appear in results"),
):
    """Aggregate missing domains from skill outcome records.

    Returns domains that repeatedly appear as gaps so the system (or operators)
    can prioritise generating new skills for those areas.
    suggested=True when count >= min_count (default 2).
    """
    from qdrant_client.http import models as qmodels

    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="agent_id", match=qmodels.MatchValue(value=agent_id)),
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill_outcome")),
            ]
        ),
        limit=500,
        with_payload=True,
        with_vectors=False,
    )

    domain_counts: dict[str, int] = {}
    total_outcomes = len(results)

    for point in results:
        for tag in point.payload.get("tags", []):
            if _is_domain_tag(tag):
                domain_counts[tag] = domain_counts.get(tag, 0) + 1

    gaps = sorted(
        [
            {"domain": domain, "count": count, "suggested": count >= min_count}
            for domain, count in domain_counts.items()
        ],
        key=lambda g: g["count"],
        reverse=True,
    )

    return {
        "gaps": gaps,
        "total_outcomes": total_outcomes,
        "agent_id": agent_id,
        "min_count": min_count,
    }


@router.get("/analytics")
async def get_pack_analytics(
    qdrant: QdrantDep,
    agent_id: Optional[str] = Query(None, max_length=256, description="Filter by agent (omit for all agents"),
):
    """Aggregate skill outcome records into pack quality metrics.

    Returns success rate, top helpful skills, top unused skills, and top missing domains.
    Useful for understanding how well the adaptive skillization layer is performing.
    """
    from qdrant_client.http import models as qmodels
    from collections import Counter

    must_filters = [qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill_outcome"))]
    if agent_id:
        must_filters.append(qmodels.FieldCondition(key="agent_id", match=qmodels.MatchValue(value=agent_id)))

    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must_filters),
        limit=500,
        with_payload=True,
        with_vectors=False,
    )

    total = len(results)
    success_count = 0
    helpful: Counter = Counter()
    unused: Counter = Counter()
    missing: Counter = Counter()

    for point in results:
        tags = point.payload.get("tags", [])
        for tag in tags:
            if tag == "success:True":
                success_count += 1
            elif tag.startswith("helpful:"):
                helpful[tag[len("helpful:"):]] += 1
            elif tag.startswith("unused:"):
                unused[tag[len("unused:"):]] += 1
            elif _is_domain_tag(tag):
                missing[tag] += 1

    return {
        "agent_id": agent_id or "all",
        "total_outcomes": total,
        "success_count": success_count,
        "success_rate": round(success_count / total, 3) if total else None,
        "top_helpful_skills": [
            {"skill_id": sid, "count": cnt} for sid, cnt in helpful.most_common(10)
        ],
        "top_unused_skills": [
            {"skill_id": sid, "count": cnt} for sid, cnt in unused.most_common(10)
        ],
        "top_missing_domains": [
            {"domain": d, "count": cnt} for d, cnt in missing.most_common(10)
        ],
    }


# ── Dialogue Analyzer ──────────────────────────────────────────────────────────

class DialogueSignal(BaseModel):
    new_terminology: list[str] = Field(default_factory=list)
    missing_skill: list[str] = Field(default_factory=list)
    domain_drift: list[str] = Field(default_factory=list)
    user_preference: list[str] = Field(default_factory=list)
    successful_pattern: list[str] = Field(default_factory=list)


class AdaptationSuggestion(BaseModel):
    type: str        # skill_gap | domain_drift | new_terminology | successful_pattern
    message: str     # user-facing text
    confidence: float
    action: str      # generate_skill | new_dialog | add_normalization | crystallize
    domain: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)


def _derive_suggestions(signal: DialogueSignal) -> list[AdaptationSuggestion]:
    """Suggest mode: derive actionable suggestions from detected signals."""
    suggestions: list[AdaptationSuggestion] = []
    for domain in signal.missing_skill:
        suggestions.append(AdaptationSuggestion(
            type="skill_gap",
            message=(
                f"No skill guidance found for '{domain}'. "
                f"A skill gap improvement has been logged. "
                f"Consider running POST /skills/generate-for-domain to create one."
            ),
            confidence=0.8,
            action="generate_skill",
            domain=domain,
            evidence=[f"missing_skill: {domain}"],
        ))
    for drift in signal.domain_drift:
        suggestions.append(AdaptationSuggestion(
            type="domain_drift",
            message=(
                f"Topic shift detected: {drift}. "
                f"Starting a new conversation may improve focus and skill pack relevance."
            ),
            confidence=0.65,
            action="new_dialog",
            evidence=[f"domain_drift: {drift}"],
        ))
    for term in signal.new_terminology:
        suggestions.append(AdaptationSuggestion(
            type="new_terminology",
            message=(
                f"New term '{term}' detected in conversation. "
                f"Consider adding it to the normalization glossary for consistent handling."
            ),
            confidence=0.7,
            action="add_normalization",
            evidence=[f"new_terminology: {term}"],
        ))
    for pattern in signal.successful_pattern:
        suggestions.append(AdaptationSuggestion(
            type="successful_pattern",
            message=(
                f"Successful pattern detected: '{pattern}'. "
                f"Consider crystallizing it as a reusable skill via POST /crystallizer/crystallize."
            ),
            confidence=0.6,
            action="crystallize",
            evidence=[f"successful_pattern: {pattern}"],
        ))
    for pref in signal.user_preference:
        suggestions.append(AdaptationSuggestion(
            type="user_preference",
            message=(
                f"User preference observed: '{pref}'. "
                f"This can inform future skill pack curation and normalization rules."
            ),
            confidence=0.55,
            action="note_preference",
            evidence=[f"user_preference: {pref}"],
        ))
    return suggestions


class DialogueAnalyzeRequest(BaseModel):
    transcript: str = Field(..., min_length=20, max_length=8000)
    pack_id: Optional[str] = Field(None, description="Associated pack ID for outcome linking")
    agent_id: str = Field("default", max_length=256)
    session_id: Optional[str] = Field(None)


_DIALOGUE_ANALYSIS_PROMPT = """/no_think
Analyze this conversation transcript and extract knowledge signals.
Return JSON with exactly these fields:
- "new_terminology": list of new technical terms/jargon the user introduced (strings, max 5)
- "missing_skill": list of domains where the assistant lacked clear guidance (lowercase domain keywords, max 4)
- "domain_drift": list of unexpected domain shifts as "from->to" strings (max 3)
- "user_preference": list of user preferences or working style hints observed (max 3)
- "successful_pattern": list of approaches that clearly worked well (max 3)

Keep each item under 60 chars. Return empty lists for fields with nothing to report.
Return only valid JSON, no other text.

Transcript:
{transcript}
"""


def _parse_dialogue_signal(raw: str) -> DialogueSignal:
    signal = DialogueSignal()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        return signal
    try:
        data = json.loads(match.group())
        return DialogueSignal(
            new_terminology=[str(x)[:60] for x in data.get("new_terminology", [])[:5]],
            missing_skill=[str(x)[:60] for x in data.get("missing_skill", [])[:4]],
            domain_drift=[str(x)[:60] for x in data.get("domain_drift", [])[:3]],
            user_preference=[str(x)[:60] for x in data.get("user_preference", [])[:3]],
            successful_pattern=[str(x)[:60] for x in data.get("successful_pattern", [])[:3]],
        )
    except Exception as e:
        logger.warning("Failed to parse dialogue signal JSON: %s | raw=%s", e, raw[:200])
        return signal


def _dialogue_excerpt(transcript: str, max_chars: int = 320) -> str:
    compact = re.sub(r"\s+", " ", normalize_text_for_display(transcript or "")).strip()
    if len(compact) <= max_chars:
        return compact
    return "…" + compact[-(max_chars - 1):]


async def _create_dialogue_candidates(
    *,
    signal: DialogueSignal,
    suggestions: list[AdaptationSuggestion],
    transcript: str,
    agent_id: str,
    session_id: Optional[str],
    source_path: str = "",
    file_hash: str = "",
    transport: str = "api",
) -> dict[str, int]:
    from app.services.learning_store import get_learning_store, make_context_signature

    store = get_learning_store()
    excerpt = _dialogue_excerpt(transcript)
    counters = {"created": 0, "updated": 0}
    source_marker = (file_hash or (session_id or "") or "dialogue")[:24]

    for suggestion in suggestions[:6]:
        if suggestion.type not in {"skill_gap", "successful_pattern", "new_terminology"}:
            continue

        if suggestion.type == "skill_gap":
            subject = suggestion.domain or "unknown-domain"
            observation = (
                f"Dialogue analysis detected a skill gap for '{subject}'. "
                f"Excerpt: {excerpt}"
            )
            why_it_matters = (
                "Surfacing this as pending review lets the user approve a targeted improvement "
                "without manually restating the problem."
            )
        elif suggestion.type == "successful_pattern":
            subject = suggestion.evidence[0].split(": ", 1)[-1] if suggestion.evidence else "pattern"
            observation = (
                f"Dialogue analysis found a reusable successful pattern: '{subject}'. "
                f"Excerpt: {excerpt}"
            )
            why_it_matters = (
                "Successful approaches should be queued for reuse before they are forgotten at the end of a session."
            )
        else:
            subject = suggestion.evidence[0].split(": ", 1)[-1] if suggestion.evidence else "term"
            observation = (
                f"Dialogue analysis found new terminology requiring normalization: '{subject}'. "
                f"Excerpt: {excerpt}"
            )
            why_it_matters = (
                "Capturing terminology as reviewable guidance reduces future ambiguity and repeated clarification."
            )

        cleaned_fields, enriched_meta = await prepare_artifact_texts(
            content=suggestion.message,
            observation=observation,
            why_it_matters=why_it_matters,
            meta={
                "signal_type": suggestion.type,
                "dialogue_excerpt": excerpt,
                "dialogue_source_path": source_path,
                "dialogue_file_hash": file_hash,
                "dialogue_session_id": session_id,
                "evidence": [normalize_text_for_display(item) for item in (suggestion.evidence or [])],
                "analysis_mode": "dialogue-auto-review",
            },
        )
        base_sig = make_context_signature(
            project="supermemory",
            task_type="dialogue_analysis",
            phase="pending_review",
            category=f"dialogue_{suggestion.type}",
            transport=transport,
            agent=agent_id or None,
        )
        context_signature = f"{base_sig};signal={suggestion.type};source={source_marker}"
        artifact_id, created = await store.upsert_candidate(
            agent_id=agent_id,
            action_type="suggest_create_improvement",
            artifact_type="meta_guidance",
            content=cleaned_fields["content"],
            context_signature=context_signature,
            observation=cleaned_fields["observation"],
            why_it_matters=cleaned_fields["why_it_matters"],
            risk_level="low",
            confidence=max(0.55, min(0.9, float(suggestion.confidence))),
            domain=suggestion.domain or "",
            tags=["dialogue-analysis", suggestion.type, transport] + ([suggestion.domain] if suggestion.domain else []),
            meta=enriched_meta,
        )
        counters["created" if created else "updated"] += 1

        try:
            await store.write_event(
                event_type="artifact_suggested",
                agent_id=agent_id,
                project="supermemory",
                transport=transport,
                episode_id=session_id or "",
                context_signature=context_signature,
                payload={
                    "artifact_id": str(artifact_id),
                    "action_type": "suggest_create_improvement",
                    "signal_type": suggestion.type,
                    "source_path": source_path,
                    "file_hash": file_hash,
                },
            )
        except Exception:
            pass

    return counters


async def analyze_dialogue_transcript(
    *,
    transcript: str,
    agent_id: str,
    qdrant,
    ollama,
    pack_id: Optional[str] = None,
    session_id: Optional[str] = None,
    source_path: str = "",
    file_hash: str = "",
    transport: str = "api",
) -> dict:
    from app.models.memory import MemoryCreate
    from app.models.enums import MemoryType
    from app.services.learning_store import get_learning_store, make_context_signature

    started_at = perf_counter()

    try:
        raw = await _llm(_DIALOGUE_ANALYSIS_PROMPT.format(transcript=transcript[-4000:]))
    except Exception as e:
        logger.warning("Dialogue analysis LLM failed: %s", e)
        _track_adaptive(
            "dialogue_analyze",
            success=False,
            started_at=started_at,
            agent_id=agent_id,
            session_id=session_id,
            metadata={"reason": "llm_error", "transport": transport},
        )
        return {"recorded": False, "error": str(e), "signals": DialogueSignal().model_dump()}

    signal = _parse_dialogue_signal(raw)

    has_signals = any([
        signal.new_terminology, signal.missing_skill, signal.domain_drift,
        signal.user_preference, signal.successful_pattern,
    ])
    excerpt = _dialogue_excerpt(transcript)
    if not has_signals:
        _track_adaptive(
            "dialogue_analyze",
            success=True,
            started_at=started_at,
            agent_id=agent_id,
            session_id=session_id,
            metadata={"signals_detected": 0, "suggestions": 0, "transport": transport},
        )
        return {"recorded": False, "signals": signal.model_dump(), "reason": "no signals detected"}

    try:
        store = get_learning_store()
        excerpt_ctx = make_context_signature(
            project="supermemory",
            task_type="dialogue",
            phase="excerpt",
            category="dialogue_excerpt",
            transport=transport,
            agent=agent_id or None,
        )
        signal_ctx = make_context_signature(
            project="supermemory",
            task_type="dialogue",
            phase="signals",
            category="dialogue_signal",
            transport=transport,
            agent=agent_id or None,
        )
        await store.write_event(
            event_type="dialogue_excerpt",
            agent_id=agent_id,
            project="supermemory",
            transport=transport,
            episode_id=session_id or "",
            context_signature=f"{excerpt_ctx};source={(file_hash or session_id or 'dialogue')[:24]}",
            payload={
                "excerpt": excerpt,
                "source_path": source_path,
                "file_hash": file_hash,
                "transport": transport,
            },
        )
        await store.write_event(
            event_type="dialogue_signal",
            agent_id=agent_id,
            project="supermemory",
            transport=transport,
            episode_id=session_id or "",
            context_signature=f"{signal_ctx};source={(file_hash or session_id or 'dialogue')[:24]}",
            payload={
                "missing_skill": signal.missing_skill,
                "successful_pattern": signal.successful_pattern,
                "new_terminology": signal.new_terminology,
                "user_preference": signal.user_preference,
                "domain_drift": signal.domain_drift,
                "excerpt": excerpt,
                "source_path": source_path,
                "file_hash": file_hash,
                "transport": transport,
            },
        )
    except Exception as e:
        logger.warning("Failed to persist dialogue learning events: %s", e)

    content_parts = []
    if signal.new_terminology:
        content_parts.append(f"new_terms={','.join(signal.new_terminology)}")
    if signal.missing_skill:
        content_parts.append(f"missing_skill={','.join(signal.missing_skill)}")
    if signal.domain_drift:
        content_parts.append(f"domain_drift={','.join(signal.domain_drift)}")
    if signal.user_preference:
        content_parts.append(f"user_preference={','.join(signal.user_preference)}")
    if signal.successful_pattern:
        content_parts.append(f"successful_pattern={','.join(signal.successful_pattern)}")

    content = "dialogue_signal " + " | ".join(content_parts)
    if pack_id:
        content += f" | pack_id={pack_id}"

    tags = (
        ["dialogue_signal", transport]
        + signal.missing_skill
        + signal.new_terminology
        + ([f"pack:{pack_id}"] if pack_id else [])
        + ([f"session:{session_id}"] if session_id else [])
        + ([file_hash] if file_hash else [])
    )

    vector = await ollama.embed(content)
    mem = MemoryCreate(
        content=content,
        agent_id=agent_id,
        memory_type=MemoryType.experience,
        category="dialogue_signal",
        importance_score=0.5,
        source=source_path or "dialogue-analyzer",
        tags=tags,
        session_id=session_id,
    )
    mem_id = await qdrant.insert(mem, vector)

    await qdrant._client.set_payload(
        collection_name=qdrant._collection,
        payload={
            "signal_json": signal.model_dump(),
            "source_path": source_path,
            "file_hash": file_hash,
            "transport": transport,
        },
        points=[str(mem_id)],
    )

    for pref in signal.user_preference[:3]:
        try:
            pref_text = pref.strip()
            if not pref_text:
                continue
            pref_vector = await ollama.embed(f"user preference {pref_text}")
            pref_mem = MemoryCreate(
                content=pref_text,
                agent_id=agent_id,
                memory_type=MemoryType.preference,
                category="user_preference",
                importance_score=0.55,
                source="dialogue-analyzer:user-preference",
                tags=["user_preference", transport] + ([f"session:{session_id}"] if session_id else []),
                session_id=session_id,
            )
            pref_id = await qdrant.insert(pref_mem, pref_vector)
            await qdrant._client.set_payload(
                collection_name=qdrant._collection,
                payload={"preference_text": pref_text},
                points=[str(pref_id)],
            )
        except Exception as e:
            logger.warning("Failed to persist user preference '%s': %s", pref, e)

    if pack_id and signal.missing_skill:
        try:
            outcome_content = (
                f"skill_outcome pack_id={pack_id} "
                f"helpful=0 unused=0 "
                f"missing_domains={','.join(signal.missing_skill)} success=True"
            )
            outcome_vector = await ollama.embed(outcome_content)
            outcome_mem = MemoryCreate(
                content=outcome_content,
                agent_id=agent_id,
                memory_type=MemoryType.experience,
                category="skill_outcome",
                importance_score=0.5,
                source=f"dialogue-auto-outcome:{pack_id}",
                tags=(
                    ["skill_outcome", f"pack:{pack_id}", transport]
                    + signal.missing_skill
                    + ([f"session:{session_id}"] if session_id else [])
                ),
                session_id=session_id,
            )
            await qdrant.insert(outcome_mem, outcome_vector)
            logger.info("Auto-recorded outcome for pack=%s missing=%s", pack_id, signal.missing_skill)
        except Exception as e:
            logger.warning("Failed to auto-record outcome from dialogue signal: %s", e)

    if signal.missing_skill:
        from app.services.improvements_store import get_improvements_store
        for domain in signal.missing_skill[:3]:
            try:
                _, created = await get_improvements_store().upsert_by_title(
                    title=f"Skill gap detected: {domain}",
                    description=(
                        f"Dialogue Analyzer detected missing guidance for domain '{domain}'. "
                        f"The conversation contained tasks in this domain but the skill pack "
                        f"provided no adequate coverage.\n\n"
                        f"Recommendation: publish or generate a skill for domain '{domain}'."
                    ),
                    project="supermemory",
                    agent_id=agent_id,
                    importance_score=0.65,
                    tags=["skill-gap", "auto-detected", domain],
                )
                if created:
                    logger.info("Auto-created improvement for skill gap: domain=%s", domain)
            except Exception as e:
                logger.warning("Failed to create improvement for domain %s: %s", domain, e)

    suggestions = _derive_suggestions(signal)
    candidate_stats = await _create_dialogue_candidates(
        signal=signal,
        suggestions=suggestions,
        transcript=transcript,
        agent_id=agent_id,
        session_id=session_id,
        source_path=source_path,
        file_hash=file_hash,
        transport=transport,
    )

    logger.info(
        "Dialogue signal stored: id=%s missing=%s terms=%s candidates=%s",
        mem_id, signal.missing_skill, signal.new_terminology, candidate_stats,
    )

    response = {
        "recorded": True,
        "memory_id": str(mem_id),
        "signals": signal.model_dump(),
        "suggestions": [s.model_dump() for s in suggestions],
        "analysis_mode": "suggest",
        "candidate_stats": candidate_stats,
    }
    _track_adaptive(
        "dialogue_analyze",
        success=True,
        started_at=started_at,
        agent_id=agent_id,
        session_id=session_id,
        metadata={
            "signals_detected": sum(len(v) for v in response["signals"].values() if isinstance(v, list)),
            "suggestions": len(response["suggestions"]),
            "pack_linked": bool(pack_id),
            "transport": transport,
            "candidates_created": candidate_stats["created"],
            "candidates_updated": candidate_stats["updated"],
        },
    )
    return response


@router.post("/dialogue/analyze")
async def analyze_dialogue(
    body: DialogueAnalyzeRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
) -> dict:
    """
    Dialogue Analyzer (observe mode) — extract DialogueSignals from conversation transcript.
    Stores signals as memories; auto-records outcome gaps if pack_id is provided.
    """
    return await analyze_dialogue_transcript(
        transcript=body.transcript,
        agent_id=body.agent_id,
        qdrant=qdrant,
        ollama=ollama,
        pack_id=body.pack_id,
        session_id=body.session_id,
        transport="api",
    )


# ── Pack Trace (Step 9) ─────────────────────────────────────────────────────────

@router.get("/pack/{pack_id}")
async def get_pack_trace(pack_id: str, qdrant: QdrantDep) -> dict:
    """
    Retrieve a stored pack record by pack_id, including Phase 2 enriched skills.
    Pack records are written asynchronously — may not be available immediately after pack/create.
    Fix 2712d6ac: returns enriched_skills added during Phase 2 enrichment.
    """
    _NIL_UUID = "00000000-0000-0000-0000-000000000000"
    if pack_id == _NIL_UUID:
        raise HTTPException(status_code=404, detail="No active pack")

    started_at = perf_counter()
    must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill_pack")),
        qmodels.FieldCondition(key="pack_id", match=qmodels.MatchValue(value=pack_id)),
    ]
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        _track_adaptive(
            "skill_pack_trace_not_found",
            success=True,
            started_at=started_at,
            metadata={"pack_id": pack_id, "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail="Pack not found (may still be pending storage)")
    p = results[0].payload

    # Fetch skills generated during Phase 2 enrichment for this pack
    enriched_must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
        qmodels.FieldCondition(key="enriched_for_pack", match=qmodels.MatchValue(value=pack_id)),
    ]
    enriched_results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=enriched_must),
        limit=10,
        with_payload=True,
        with_vectors=False,
    )
    enriched_skills = [
        SkillPackItem(
            id=str(r.id),
            name=r.payload.get("skill_name", "unknown"),
            description=r.payload.get("skill_description", ""),
            domain_tags=r.payload.get("domain_tags", []),
            content=r.payload.get("content", ""),
        )
        for r in enriched_results
        if r.payload.get("content")
    ]

    status = p.get("pack_status", "unknown")
    if enriched_skills and status == "enriched":
        phase = "enriched"
    else:
        phase = p.get("pack_phase", "immediate")

    response = {
        "pack_id": pack_id,
        "phase": phase,
        "status": status,
        "domains": p.get("pack_domains", []),
        "task_type": p.get("pack_task_type", "other"),
        "skill_ids": p.get("pack_skill_ids", []),
        "confidence": p.get("pack_confidence", 0.0),
        "enrichment_pending": p.get("pack_enrichment_pending", False),
        "enriched_skills": [s.model_dump() for s in enriched_skills],
        "added_count": len(enriched_skills),
    }
    _track_adaptive(
        "skill_pack_trace",
        success=True,
        started_at=started_at,
        metadata={
            "pack_id": pack_id,
            "status": status,
            "added_count": len(enriched_skills),
        },
    )
    return response


# ── Adaptation Suggestions (Step 9) ─────────────────────────────────────────────

@router.get("/adaptation-suggestions")
async def get_adaptation_suggestions(
    qdrant: QdrantDep,
    session_id: Optional[str] = Query(None, description="Filter by session ID"),
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """
    Retrieve adaptation suggestions derived from stored dialogue signals.
    Returns suggestions grouped by type: skill_gap, domain_drift, new_terminology, successful_pattern.
    """
    started_at = perf_counter()
    must: list = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="dialogue_signal")),
    ]
    if agent_id:
        must.append(qmodels.FieldCondition(key="agent_id", match=qmodels.MatchValue(value=agent_id)))

    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )

    # Filter by session_id tag if provided
    if session_id:
        session_tag = f"session:{session_id}"
        results = [r for r in results if session_tag in (r.payload.get("tags") or [])]

    all_suggestions: list[dict] = []
    for r in results:
        # Fix 31550476: prefer structured payload over fragile string parsing
        signal_data = r.payload.get("signal_json")
        if signal_data:
            try:
                signal = DialogueSignal(**signal_data)
            except Exception:
                signal = DialogueSignal()
        else:
            # Legacy fallback: parse from delimited content string
            content = r.payload.get("content", "")
            if content.startswith("dialogue_signal "):
                content = content[len("dialogue_signal "):]
            signal = DialogueSignal()
            for part in content.split(" | "):
                part = part.strip()
                if part.startswith("missing_skill="):
                    vals = part[len("missing_skill="):].split(",")
                    signal.missing_skill = [v.strip() for v in vals if v.strip()]
                elif part.startswith("new_terms="):
                    vals = part[len("new_terms="):].split(",")
                    signal.new_terminology = [v.strip() for v in vals if v.strip()]
                elif part.startswith("domain_drift="):
                    vals = part[len("domain_drift="):].split(",")
                    signal.domain_drift = [v.strip() for v in vals if v.strip()]
                elif part.startswith("successful_pattern="):
                    vals = part[len("successful_pattern="):].split(",")
                    signal.successful_pattern = [v.strip() for v in vals if v.strip()]
        suggestions = _derive_suggestions(signal)
        all_suggestions.extend(s.model_dump() for s in suggestions)

    # P3: pull workflow_guidance records into the unified loop
    from app.services.learning_store import get_learning_store, make_context_signature
    _ls = get_learning_store()
    wf_rows = await _ls.list_artifacts(
        agent_id=agent_id, artifact_type="workflow_guidance", limit=limit
    )
    for r in wf_rows:
        wf_type = r.get("workflow_type", "")
        template = _WORKFLOW_GUIDANCE_MAP.get(wf_type, {})
        if not template:
            continue
        all_suggestions.append({
            "type": f"workflow_{wf_type}",
            "action": template.get("action", r.get("workflow_action", "")),
            "message": template.get("message", r.get("content", "")),
            "confidence": r.get("confidence", template.get("confidence", 0.7)),
            "evidence": [r.get("workflow_context", "")],
            "domain": None,
            "source": "workflow_guidance",
        })

    # P4: ledger_mirror — promoted_pattern artifacts matching current context
    _ctx_sig = make_context_signature(
        project=agent_id or "unknown",  # best-effort context from available params
        agent=agent_id,
    )
    mirror_rows = await _ls.ledger_mirror(_ctx_sig, limit=5)
    for r in mirror_rows:
        all_suggestions.append({
            "type": r.get("action_type") or "promoted_pattern",
            "action": r.get("action_type") or "",
            "message": r.get("content") or r.get("observation") or "",
            "confidence": r.get("confidence", 0.8),
            "evidence": [r.get("context_signature", "")],
            "domain": r.get("domain") or None,
            "source": "ledger_mirror",
        })

    # Group by type
    grouped: dict[str, list] = {}
    for s in all_suggestions:
        grouped.setdefault(s["type"], []).append(s)

    # Deduplicate by (type, domain/evidence) across all sources
    deduped: list[dict] = []
    seen: set[str] = set()
    for s in all_suggestions:
        key = f"{s['type']}:{s.get('domain') or (s['evidence'][0] if s['evidence'] else '')}"
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    # Sort: higher confidence first
    deduped.sort(key=lambda s: s.get("confidence", 0), reverse=True)

    response = {
        "suggestions": deduped,
        "by_type": {k: len(v) for k, v in grouped.items()},
        "total": len(deduped),
        "sources": len(results) + len(wf_rows) + len(mirror_rows),
    }
    _track_adaptive(
        "adaptation_suggestions",
        success=True,
        started_at=started_at,
        agent_id=agent_id,
        session_id=session_id,
        metadata={
            "total": response["total"],
            "sources": response["sources"],
            "types": sorted(response["by_type"].keys()),
        },
    )
    return response


# ── Review Queue (Step 8) ───────────────────────────────────────────────────────

@router.get("/observability-summary")
async def get_observability_summary(
    qdrant: QdrantDep,
    since_hours: Optional[float] = Query(None, gt=0),
    agent_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
) -> dict:
    """Adaptive skillization metrics with per-scope filtering and latency percentiles."""
    tracker = get_tracker()
    rows = tracker.stats(
        component=ADAPTIVE_COMPONENT,
        since_hours=since_hours,
        agent_id=agent_id,
        session_id=session_id,
    )
    pcts = tracker.percentiles(
        component=ADAPTIVE_COMPONENT,
        since_hours=since_hours,
        agent_id=agent_id,
        session_id=session_id,
    )

    total_events = sum(r["total"] for r in rows)
    total_success = sum(r["success"] for r in rows)

    task_types = {}
    for r in rows:
        tt = r["task_type"]
        entry = dict(r)
        if tt in pcts:
            entry["latency_percentiles"] = pcts[tt]
        task_types[tt] = entry

    # Enrichment usefulness: ratio of task_enrichment skills with usefulness_score
    enrichment_useful: Optional[float] = None
    try:
        must = [
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
            qmodels.FieldCondition(key="review_status", match=qmodels.MatchValue(value="task_enrichment")),
        ]
        enrich_results, _ = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=qmodels.Filter(must=must),
            limit=200,
            with_payload=True,
            with_vectors=False,
        )
        if enrich_results:
            scores = [
                r.payload.get("usefulness_score", 0.0)
                for r in enrich_results
                if r.payload.get("usefulness_score") is not None
            ]
            enrichment_useful = round(sum(scores) / len(scores), 3) if scores else None
    except Exception:
        pass

    # P6: workflow feedback summary
    wf_feedback_rows = tracker.stats(
        component=ADAPTIVE_COMPONENT, task_type="workflow_feedback",
        since_hours=since_hours, agent_id=agent_id,
    )
    wf_feedback = wf_feedback_rows[0] if wf_feedback_rows else None

    # P6: behavior stats
    behavior_rows = tracker.stats(
        component=ADAPTIVE_COMPONENT, task_type="behavior_record",
        since_hours=since_hours, agent_id=agent_id,
    )
    behavior_total = behavior_rows[0]["total"] if behavior_rows else 0

    automatable_count = 0
    if agent_id:
        try:
            patterns = get_adaptive_store().list_patterns(agent_id)
            for entry in patterns:
                conf = _behavior_confidence(entry["accepts"], entry["rejects"])
                rc = _behavior_recent_confidence(entry["recent"])
                if (
                    entry["action_type"] not in _HIGH_RISK_ACTIONS
                    and entry["accepts"] >= _BEHAVIOR_SUGGEST_MIN_ACCEPTS
                    and conf >= _BEHAVIOR_SUGGEST_CONFIDENCE
                    and rc >= _BEHAVIOR_SUGGEST_CONFIDENCE
                ):
                    automatable_count += 1
        except Exception:
            pass

    scope = {}
    if agent_id:
        scope["agent_id"] = agent_id
    if session_id:
        scope["session_id"] = session_id
    if since_hours:
        scope["since_hours"] = since_hours

    return {
        "component": ADAPTIVE_COMPONENT,
        "scope": scope or None,
        "total_events": total_events,
        "success_rate": round(total_success / total_events, 3) if total_events else None,
        "task_types": task_types,
        "enrichment_usefulness_avg": enrichment_useful,
        "workflow_feedback": {
            "total_ratings": wf_feedback["total"] if wf_feedback else 0,
            "useful_rate": wf_feedback["success_rate"] if wf_feedback else None,
        },
        "behavior": {
            "total_records": behavior_total,
            "automatable_patterns": automatable_count,
        },
    }


class ReviewQueueItem(BaseModel):
    id: str
    name: str
    description: str
    domain_tags: list[str]
    content: str
    review_status: str
    auto_generated: bool
    importance_score: float
    created_at: str


@router.get("/review-queue", response_model=list[ReviewQueueItem])
async def get_review_queue(
    qdrant: QdrantDep,
    limit: int = Query(50, ge=1, le=200),
) -> list[ReviewQueueItem]:
    """List auto-generated skills awaiting human review (pending_review status)."""
    must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
        qmodels.FieldCondition(key="review_status", match=qmodels.MatchValue(value="pending_review")),
    ]
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    items = []
    for r in results:
        p = r.payload
        items.append(ReviewQueueItem(
            id=str(r.id),
            name=p.get("skill_name", "unknown"),
            description=p.get("skill_description", ""),
            domain_tags=p.get("domain_tags", []),
            content=p.get("content", ""),
            review_status=p.get("review_status", "pending_review"),
            auto_generated=p.get("auto_generated", True),
            importance_score=p.get("importance_score", 0.4),
            created_at=p.get("timestamp", ""),
        ))
    return items


@router.post("/review/{skill_id}/approve")
async def approve_skill(skill_id: str, qdrant: QdrantDep) -> dict:
    """
    Approve an auto-generated skill — makes it active and visible in packs.
    Sets review_status=approved, suppressed=False.
    """
    try:
        uid = uuid.UUID(skill_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid skill ID")

    results = await qdrant._client.retrieve(
        collection_name=qdrant._collection,
        ids=[str(uid)],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise HTTPException(status_code=404, detail="Skill not found")
    if results[0].payload.get("category") != "skill":
        raise HTTPException(status_code=404, detail="Not a skill record")
    if results[0].payload.get("review_status") not in ("pending_review", "rejected"):
        raise HTTPException(status_code=409, detail="Skill is not in a reviewable state")

    await qdrant._client.set_payload(
        collection_name=qdrant._collection,
        payload={"review_status": "approved", "suppressed": False},
        points=[str(uid)],
    )
    logger.info("Skill %s approved — now active", skill_id)
    return {"id": skill_id, "review_status": "approved", "active": True}


@router.post("/review/{skill_id}/reject")
async def reject_skill(skill_id: str, qdrant: QdrantDep, reason: Optional[str] = Query(None, max_length=512)) -> dict:
    """
    Reject an auto-generated skill — keeps it suppressed.
    Sets review_status=rejected. Safe: skill is never deleted.
    """
    try:
        uid = uuid.UUID(skill_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid skill ID")

    results = await qdrant._client.retrieve(
        collection_name=qdrant._collection,
        ids=[str(uid)],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise HTTPException(status_code=404, detail="Skill not found")
    if results[0].payload.get("category") != "skill":
        raise HTTPException(status_code=404, detail="Not a skill record")

    payload: dict = {"review_status": "rejected", "suppressed": True}
    if reason:
        payload["reject_reason"] = reason
    await qdrant._client.set_payload(
        collection_name=qdrant._collection,
        payload=payload,
        points=[str(uid)],
    )
    logger.info("Skill %s rejected: %s", skill_id, reason or "no reason given")
    return {"id": skill_id, "review_status": "rejected", "active": False}


# ── Workflow Guidance (Observe/Suggest mode) ────────────────────────────────

_WORKFLOW_THROTTLE_SECS = 1800  # 30 min cooldown per (agent_id, signal_type)

_WORKFLOW_GUIDANCE_MAP: dict[str, dict] = {
    "context_overload": {
        "message": "Context is getting long — consider starting a new dialog to maintain response quality.",
        "action": "new_dialog",
        "confidence": 0.80,
    },
    "task_switch_detected": {
        "message": "Topic switched significantly — a new dialog will give the agent a fresh context.",
        "action": "new_dialog",
        "confidence": 0.75,
    },
    "new_dialog_recommended": {
        "message": "Starting a new dialog is recommended for this topic.",
        "action": "new_dialog",
        "confidence": 0.85,
    },
    "manual_action_required": {
        "message": "This step requires manual action — the agent cannot complete it autonomously.",
        "action": "manual_step",
        "confidence": 0.90,
    },
    "permission_blocker": {
        "message": "Blocked by permissions — grant access or run with elevated privileges.",
        "action": "grant_permission",
        "confidence": 0.90,
    },
    "workflow_optimization_opportunity": {
        "message": "Workflow optimization available — consider the suggested approach.",
        "action": "optimize_workflow",
        "confidence": 0.65,
    },
}


class WorkflowEvent(BaseModel):
    type: str
    context: str = ""


class WorkflowAnalyzeRequest(BaseModel):
    agent_id: str
    session_id: Optional[str] = None
    events: list[WorkflowEvent]


class WorkflowGuidanceItem(BaseModel):
    type: str
    message: str
    action: str
    confidence: float
    context: str
    throttled: bool = False


@router.post("/workflow/analyze")
async def analyze_workflow(
    body: WorkflowAnalyzeRequest,
) -> dict:
    """Detect workflow anomalies and emit actionable guidance (Observe/Suggest mode)."""
    from app.services.learning_store import get_learning_store
    now = _now()
    guidance: list[WorkflowGuidanceItem] = []

    for event in body.events:
        template = _WORKFLOW_GUIDANCE_MAP.get(event.type)
        if not template:
            continue

        last_emitted = get_adaptive_store().get_last_emitted(body.agent_id, event.type)
        if now - last_emitted < _WORKFLOW_THROTTLE_SECS:
            guidance.append(WorkflowGuidanceItem(
                type=event.type,
                message=template["message"],
                action=template["action"],
                confidence=template["confidence"],
                context=event.context,
                throttled=True,
            ))
            continue

        get_adaptive_store().set_last_emitted(body.agent_id, event.type, now)

        mem_content = (
            f"workflow_guidance {event.type}: {template['message']}"
            + (f" | context: {event.context}" if event.context else "")
        )
        await get_learning_store().insert_artifact(
            agent_id=body.agent_id,
            artifact_type="workflow_guidance",
            workflow_type=event.type,
            workflow_action=template["action"],
            workflow_context=event.context,
            content=mem_content,
            confidence=template["confidence"],
            tags=["workflow", event.type],
        )

        get_tracker().record(
            ADAPTIVE_COMPONENT, "workflow_guidance",
            success=True, agent_id=body.agent_id, session_id=body.session_id,
        )

        guidance.append(WorkflowGuidanceItem(
            type=event.type,
            message=template["message"],
            action=template["action"],
            confidence=template["confidence"],
            context=event.context,
            throttled=False,
        ))

    return {
        "agent_id": body.agent_id,
        "guidance": [g.model_dump() for g in guidance],
        "total": len(guidance),
        "throttled": sum(1 for g in guidance if g.throttled),
        "emitted": sum(1 for g in guidance if not g.throttled),
    }


@router.get("/workflow-guidance")
async def get_workflow_guidance(
    agent_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Retrieve stored workflow guidance suggestions."""
    from app.services.learning_store import get_learning_store
    rows = await get_learning_store().list_artifacts(
        agent_id=agent_id, artifact_type="workflow_guidance", limit=limit
    )
    items = [
        {
            "id": r["id"],
            "type": r["workflow_type"],
            "action": r["workflow_action"],
            "context": r["workflow_context"],
            "message": r["content"],
            "confidence": r["confidence"],
            "agent_id": r["agent_id"],
            "useful_votes": r["useful_votes"],
            "not_useful_votes": r["not_useful_votes"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    by_type: dict[str, int] = {}
    for item in items:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
    return {"guidance": items, "total": len(items), "by_type": by_type}


@router.post("/workflow/guidance/{guidance_id}/rate")
async def rate_workflow_guidance(
    guidance_id: str,
    useful: bool = Query(..., description="True if guidance was useful"),
) -> dict:
    """Rate the usefulness of a workflow guidance item (post-task feedback)."""
    from app.services.learning_store import get_learning_store
    try:
        uid = uuid.UUID(guidance_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid guidance ID")

    updated = await get_learning_store().rate_artifact(uid, useful)
    if updated is None:
        raise HTTPException(status_code=404, detail="Guidance not found")

    get_tracker().record(
        ADAPTIVE_COMPONENT, "workflow_feedback",
        success=useful,
        agent_id=updated.get("agent_id"),
        metadata={
            "workflow_type": updated.get("workflow_type", "unknown"),
            "useful": useful,
            "confidence": updated["confidence"],
        },
    )

    return {
        "id": guidance_id,
        "useful": useful,
        "useful_votes": updated["useful_votes"],
        "not_useful_votes": updated["not_useful_votes"],
        "updated_confidence": updated["confidence"],
    }


# ── Behavioral Adaptation (Observe/Suggest mode) ────────────────────────────

from app.services.behavior_adaptation import (
    HIGH_RISK_ACTIONS,
    RECENT_WINDOW as _BEHAVIOR_RECENT_WINDOW,
    SUGGEST_CONFIDENCE as _BEHAVIOR_SUGGEST_CONFIDENCE,
    SUGGEST_MIN_ACCEPTS as _BEHAVIOR_SUGGEST_MIN_ACCEPTS,
    behavior_confidence as _behavior_confidence,
    behavior_recent_confidence as _behavior_recent_confidence,
    record_behavior_event,
    should_suggest_automation,
)


class BehaviorRecordRequest(BaseModel):
    agent_id: str
    action_type: str
    accepted: bool
    context: str = ""
    context_signature: str = ""  # P2: scope key to prevent overgeneralization


class BehaviorPattern(BaseModel):
    action_type: str
    context_signature: str
    accepts: int
    rejects: int
    confidence: float
    recent_confidence: float
    suggest_automation: bool
    decaying: bool
    high_risk: bool


@router.post("/behavior/record")
async def record_behavior(body: BehaviorRecordRequest) -> dict:
    """Record a user accept/reject decision for a low-risk action."""
    eval_ = record_behavior_event(
        agent_id=body.agent_id,
        action_type=body.action_type,
        accepted=body.accepted,
        context_signature=body.context_signature,
        recent_window=_BEHAVIOR_RECENT_WINDOW,
    )

    get_tracker().record(
        ADAPTIVE_COMPONENT, "behavior_record",
        success=True, agent_id=body.agent_id,
    )

    return {
        "action_type": body.action_type,
        "context_signature": body.context_signature,
        "accepted": body.accepted,
        "confidence": eval_.confidence,
        "recent_confidence": eval_.recent_confidence,
        "suggest_automation": eval_.suggest_automation,
        "high_risk": eval_.high_risk,
    }


@router.get("/behavior/patterns")
async def get_behavior_patterns(
    agent_id: str = Query(...),
    suggest_only: bool = Query(False),
    context_signature: Optional[str] = Query(None),
) -> dict:
    """Get behavioral patterns with confidence scores for an agent."""
    rows = get_adaptive_store().list_patterns(agent_id)
    if context_signature is not None:
        rows = [r for r in rows if r["context_sig"] == context_signature]

    patterns = []
    for entry in rows:
        conf = _behavior_confidence(entry["accepts"], entry["rejects"])
        recent_conf = _behavior_recent_confidence(entry["recent"])
        high_risk = entry["action_type"] in HIGH_RISK_ACTIONS
        suggest = should_suggest_automation(
            action_type=entry["action_type"],
            accepts=entry["accepts"],
            rejects=entry["rejects"],
            recent=entry["recent"],
        )
        decaying = conf >= _BEHAVIOR_SUGGEST_CONFIDENCE and recent_conf < conf - 0.15

        p = BehaviorPattern(
            action_type=entry["action_type"],
            context_signature=entry["context_sig"],
            accepts=entry["accepts"],
            rejects=entry["rejects"],
            confidence=conf,
            recent_confidence=recent_conf,
            suggest_automation=suggest,
            decaying=decaying,
            high_risk=high_risk,
        )
        if suggest_only and not suggest:
            continue
        patterns.append(p.model_dump())

    return {
        "agent_id": agent_id,
        "patterns": patterns,
        "total": len(patterns),
        "automatable": sum(1 for p in patterns if p["suggest_automation"]),
    }


@router.post("/behavior/patterns/{action_type}/reset")
async def reset_behavior_pattern(
    action_type: str,
    agent_id: str = Query(...),
    context_signature: str = Query(""),
) -> dict:
    """Reset a behavioral pattern (e.g. after user opts out of automation)."""
    deleted = get_adaptive_store().delete_pattern(agent_id, action_type, context_signature)
    if not deleted:
        raise HTTPException(status_code=404, detail="Pattern not found")
    # P4: track reset as negative feedback signal
    get_tracker().record(
        ADAPTIVE_COMPONENT, "behavior_reset",
        success=True, agent_id=agent_id,
        metadata={"action_type": action_type, "context_sig": context_signature},
    )
    return {"agent_id": agent_id, "action_type": action_type, "reset": True}


# ── Adaptive Artifacts Governance (P5) ───────────────────────────────────────
#
# Lifecycle: runtime_hint → persistent_rule → promoted_pattern
#   runtime_hint    — emitted per-session, not persisted long-term
#   persistent_rule — promoted from hint (high useful_votes), survives restarts
#   promoted_pattern— crystallized into a skill-like artifact, globally visible

_ARTIFACT_SCOPE_ORDER = ["runtime_hint", "persistent_rule", "promoted_pattern"]


@router.post("/adaptive-artifacts/promote/{artifact_id}")
async def promote_adaptive_artifact(
    artifact_id: str,
    agent_id: str = Query(...),
) -> dict:
    """Promote a workflow_guidance artifact to persistent_rule or promoted_pattern."""
    from app.services.learning_store import get_learning_store
    try:
        uid = uuid.UUID(artifact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid artifact ID")

    existing = await get_learning_store().get_artifact(uid)
    if existing is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    current_scope = existing.get("artifact_scope", "runtime_hint")
    updated = await get_learning_store().promote_artifact(uid, agent_id)
    if updated is None:
        raise HTTPException(status_code=400, detail=f"Already at max scope: {current_scope}")

    get_tracker().record(
        ADAPTIVE_COMPONENT, "artifact_promote",
        success=True, agent_id=agent_id,
        metadata={"from": current_scope, "to": updated["artifact_scope"]},
    )
    return {"id": artifact_id, "previous_scope": current_scope, "scope": updated["artifact_scope"]}


@router.get("/adaptive-artifacts")
async def list_adaptive_artifacts(
    agent_id: Optional[str] = Query(None),
    scope: Optional[str] = Query(None, description="runtime_hint | persistent_rule | promoted_pattern"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """List adaptive artifacts with optional scope/agent filter."""
    from app.services.learning_store import get_learning_store
    rows = await get_learning_store().list_artifacts(
        agent_id=agent_id, artifact_type="workflow_guidance", scope=scope, limit=limit
    )
    items = [
        {
            "id": r["id"],
            "type": r["workflow_type"],
            "scope": r["artifact_scope"],
            "confidence": r["confidence"],
            "useful_votes": r["useful_votes"],
            "not_useful_votes": r["not_useful_votes"],
            "agent_id": r["agent_id"],
            "promoted_by": r.get("promoted_by"),
        }
        for r in rows
    ]
    by_scope: dict[str, int] = {}
    for item in items:
        by_scope[item["scope"]] = by_scope.get(item["scope"], 0) + 1
    return {"artifacts": items, "total": len(items), "by_scope": by_scope}


# ── Encoding / Terminal Artifact Detection (Observe/Suggest mode) ────────────

# Mojibake: cp1251 bytes decoded as latin-1 produce these sequences
_MOJIBAKE_PATTERNS = [
    (re.compile(r"[\xc0-\xff]{3,}"), "encoding_artifact"),           # high-byte runs
    (re.compile(r"[√÷≤≥±×°²³]{2,}"), "encoding_artifact"),          # math-symbol clusters
    (re.compile(r"(?:&#\d{4,5};){2,}"), "encoding_artifact"),        # numeric HTML entities
    (re.compile(r"[╗╔╝╚═║]{3,}"), "terminal_rendering_issue"),       # box-drawing runs
    (re.compile(r"<\?xml[^>]*encoding=['\"]windows-1251['\"]"), "shell_artifact"),
    (re.compile(r"cp1251|windows-1251|cp866", re.IGNORECASE), "shell_artifact"),
    (re.compile(r"\bchcp\s+\d{3,4}\b", re.IGNORECASE), "shell_artifact"),  # chcp command
    (re.compile(r"UnicodeDecodeError|UnicodeEncodeError|charmap.*codec"), "encoding_artifact"),
]

_ENCODING_SUGGESTIONS: dict[str, dict] = {
    "encoding_artifact": {
        "action": "fix_encoding",
        "message": "Possible encoding corruption detected — ensure terminal uses UTF-8.",
        "confidence": 0.75,
    },
    "terminal_rendering_issue": {
        "action": "check_terminal",
        "message": "Terminal rendering issue detected — check font and console code page.",
        "confidence": 0.70,
    },
    "shell_artifact": {
        "action": "set_utf8",
        "message": "Shell encoding artifact detected — run `chcp 65001` or set PYTHONIOENCODING=utf-8.",
        "confidence": 0.80,
    },
}


class EncodingAnalyzeRequest(BaseModel):
    text: str
    agent_id: str = "default"
    session_id: Optional[str] = None


class EncodingArtifact(BaseModel):
    signal_type: str   # encoding_artifact | terminal_rendering_issue | shell_artifact
    action: str
    message: str
    confidence: float
    evidence: str      # matched substring


@router.post("/encoding/analyze")
async def analyze_encoding(body: EncodingAnalyzeRequest) -> dict:
    """Detect encoding/terminal artifacts in text (Observe/Suggest mode, rule-based)."""
    found: dict[str, EncodingArtifact] = {}

    for pattern, signal_type in _MOJIBAKE_PATTERNS:
        match = pattern.search(body.text)
        if match and signal_type not in found:
            template = _ENCODING_SUGGESTIONS[signal_type]
            found[signal_type] = EncodingArtifact(
                signal_type=signal_type,
                action=template["action"],
                message=template["message"],
                confidence=template["confidence"],
                evidence=match.group(0)[:80],
            )

    artifacts = [a.model_dump() for a in found.values()]
    return {
        "agent_id": body.agent_id,
        "artifacts": artifacts,
        "total": len(artifacts),
        "clean": len(artifacts) == 0,
    }


@router.get("/{skill_id}/content")
async def get_skill_content(skill_id: str, qdrant: QdrantDep):
    """Get raw SKILL.md content for a skill (for installation)."""
    from uuid import UUID
    try:
        uid = UUID(skill_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid skill ID")

    results = await qdrant._client.retrieve(
        collection_name=qdrant._collection,
        ids=[str(uid)],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise HTTPException(status_code=404, detail="Skill not found")

    p = results[0].payload
    if p.get("category") != "skill":
        raise HTTPException(status_code=404, detail="Not a skill record")

    return {
        "id": skill_id,
        "name": p.get("skill_name", "unknown"),
        "content": p.get("content", ""),
        "install_path": f"~/.claude/skills/{p.get('skill_name', 'unknown')}/SKILL.md",
    }


# ── Self-healing: retag broken skills via local LLM ────────────────────────────

async def _infer_skill_all(content: str) -> dict:
    """Ask local LLM to infer name, description, and domain_tags from raw skill content."""
    heading_match = re.search(r"^#\s+(.+)", content, re.MULTILINE)
    name_hint = heading_match.group(1).strip() if heading_match else "unknown"

    prompt = f"""/no_think
You are analyzing a skill definition file. Extract structured metadata.

Return ONLY valid JSON with these fields:
- "name": short slug (lowercase, hyphens, no spaces, max 30 chars). Infer from heading or content.
- "description": one sentence summary of what this skill does (max 120 chars)
- "domain_tags": list of 2-6 lowercase domain keywords (e.g. "git", "testing", "deploy", "api", "memory")

Skill content (first 1500 chars):
{content[:1500]}

JSON only, no explanation:
{{"name": "{name_hint.lower().replace(' ', '-')[:30]}", "description": "...", "domain_tags": [...]}}"""

    raw = await _llm(prompt)
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        return {"name": name_hint.lower().replace(" ", "-")[:30], "description": name_hint, "domain_tags": []}
    try:
        data = json.loads(match.group())
        return {
            "name": re.sub(r"[^a-z0-9-]", "", str(data.get("name", name_hint)).lower().replace(" ", "-"))[:30] or "unknown",
            "description": str(data.get("description", name_hint))[:120],
            "domain_tags": [str(t).lower()[:32] for t in data.get("domain_tags", [])[:8]],
        }
    except Exception:
        return {"name": name_hint.lower().replace(" ", "-")[:30], "description": name_hint, "domain_tags": []}


async def _retag_handler(payload: dict) -> dict:
    """Background job handler for skills_retag."""
    from app.dependencies import get_qdrant, get_ollama
    qdrant = get_qdrant()
    ollama = get_ollama()
    limit = payload.get("limit", 20)
    return await _run_retag(qdrant, ollama, limit)


async def _run_retag(qdrant, ollama, limit: int) -> dict:
    """Core retag logic shared between sync endpoint and background handler."""
    # Scroll all skills including suppressed ones
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
        ]),
        limit=limit * 3,
        with_payload=True,
        with_vectors=False,
    )

    broken = [
        r for r in results
        if r.payload.get("skill_name", "unknown") == "unknown"
        or not r.payload.get("domain_tags")
    ][:limit]

    fixed = 0
    skipped = 0
    details = []

    for point in broken:
        p = point.payload
        content = p.get("skill_description") or p.get("content") or ""
        if not content or len(content.strip()) < 20:
            skipped += 1
            continue
        try:
            meta = await _infer_skill_all(content)
        except Exception as e:
            logger.warning("retag LLM failed for %s: %s", point.id, e)
            skipped += 1
            continue

        embed_text = f"{meta['name']} {meta['description']} {' '.join(meta['domain_tags'])}"
        try:
            vector = await ollama.embed(embed_text)
        except Exception:
            vector = None

        payload_update = {
            "skill_name": meta["name"],
            "skill_description": meta["description"],
            "domain_tags": meta["domain_tags"],
            "content": content,
        }
        await qdrant._client.set_payload(
            collection_name=qdrant._collection,
            payload=payload_update,
            points=[str(point.id)],
        )
        if vector:
            from qdrant_client.http import models as qm
            await qdrant._client.update_vectors(
                collection_name=qdrant._collection,
                points=[qm.PointVectors(id=str(point.id), vector=vector)],
            )
        # Dual-write to SQLite
        await _write_skill_to_store(str(point.id), content, meta["name"], meta["description"], p.get("platform", "claude"))
        details.append({"id": str(point.id), "name": meta["name"], "domains": meta["domain_tags"]})
        fixed += 1
        logger.info("retag: %s -> name=%s domains=%s", point.id, meta["name"], meta["domain_tags"])

    return {"fixed": fixed, "skipped": skipped, "total_broken": len(broken), "details": details}


@router.post("/retag")
async def retag_skills(qdrant: QdrantDep, ollama: OllamaDep, queue: JobQueueDep,
                       limit: int = Query(20, ge=1, le=100),
                       background: bool = False) -> dict:
    """
    Self-healing: find skills with name='unknown' or empty domain_tags,
    infer proper metadata via local LLM, and update Qdrant payload.
    Use `?background=true` to submit as a background job.
    """
    if background:
        job_id = await queue.submit("skills_retag", {"limit": limit})
        return {"job_id": job_id, "status": "queued", "poll": f"/api/v1/tasks/{job_id}"}
    # Scroll all skills including suppressed ones
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
        ]),
        limit=limit * 3,
        with_payload=True,
        with_vectors=False,
    )

    broken = [
        r for r in results
        if r.payload.get("skill_name", "unknown") == "unknown"
        or not r.payload.get("domain_tags")
    ][:limit]

    fixed = 0
    skipped = 0
    details = []

    for point in broken:
        p = point.payload
        # Use skill_description as content source (that's where SKILL.md text lands)
        content = p.get("skill_description") or p.get("content") or ""
        if not content or len(content.strip()) < 20:
            skipped += 1
            continue

        try:
            meta = await _infer_skill_all(content)
        except Exception as e:
            logger.warning("retag LLM failed for %s: %s", point.id, e)
            skipped += 1
            continue

        # Re-embed with enriched text for better retrieval
        embed_text = f"{meta['name']} {meta['description']} {' '.join(meta['domain_tags'])}"
        try:
            vector = await ollama.embed(embed_text)
        except Exception:
            vector = None

        payload_update = {
            "skill_name": meta["name"],
            "skill_description": meta["description"],
            "domain_tags": meta["domain_tags"],
            "content": content,  # preserve full content in correct field
        }

        await qdrant._client.set_payload(
            collection_name=qdrant._collection,
            payload=payload_update,
            points=[str(point.id)],
        )

        # Update vector if we got a new embedding
        if vector:
            from qdrant_client.http import models as qm
            await qdrant._client.update_vectors(
                collection_name=qdrant._collection,
                points=[qm.PointVectors(id=str(point.id), vector=vector)],
            )

        # Dual-write to SQLite
        await _write_skill_to_store(str(point.id), content, meta["name"], meta["description"], p.get("platform", "claude"))
        details.append({"id": str(point.id), "name": meta["name"], "domains": meta["domain_tags"]})
        fixed += 1
        logger.info("retag: %s -> name=%s domains=%s", point.id, meta["name"], meta["domain_tags"])

    return {"fixed": fixed, "skipped": skipped, "total_broken": len(broken), "details": details}
