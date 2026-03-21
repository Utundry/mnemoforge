import time
from fastapi import APIRouter

from app.config import settings
from app.dependencies import OllamaDep, QdrantDep

router = APIRouter(tags=["health"])

_STARTED_AT = int(time.time())

_COMPONENTS = [
    {
        "id": "memory",
        "name": "Vector Memory",
        "description": "Store and search memories using semantic vector embeddings (Qdrant + nomic-embed-text).",
        "endpoints": ["POST /memories", "GET /memories/search", "DELETE /memories/{id}"],
        "status": "core",
    },
    {
        "id": "skills",
        "name": "Skill Marketplace",
        "description": "Publish, discover, and install reusable skill packs. Adaptive packs are assembled per task on every prompt.",
        "endpoints": ["POST /skills/publish", "GET /skills/search", "POST /skills/pack/create", "POST /skills/retag", "POST /skills/evolve → /crystallizer/evolve"],
        "status": "optional",
    },
    {
        "id": "layout-fixer",
        "name": "Layout Fixer",
        "description": "Fix Russian↔English keyboard layout errors. Two implementations: rule+vector memory (fast) and rule+LLM+few-shot (adaptive).",
        "endpoints": ["POST /auto/fix-layout", "POST /auto/fix-layout/feedback", "POST /layout/fix"],
        "status": "optional",
    },
    {
        "id": "router",
        "name": "Task Router",
        "description": "Choose the best component (local LLM / cloud LLM / skill / reference) for a task type based on capability registry scores.",
        "endpoints": ["POST /router/decide"],
        "status": "optional",
    },
    {
        "id": "tracker",
        "name": "Performance Tracker",
        "description": "Record task outcomes (success/fail/latency) per component. Feeds the capability registry for smarter routing.",
        "endpoints": ["POST /tracker/record", "GET /tracker/stats", "GET /tracker/corrections"],
        "status": "optional",
    },
    {
        "id": "crystallizer",
        "name": "Skill Crystallizer + Evolver",
        "description": "Crystallize successful patterns into reusable skills. Evolver suppresses low-usefulness skills and fills domain gaps.",
        "endpoints": ["POST /crystallizer/crystallize", "POST /crystallizer/assess", "POST /crystallizer/evolve"],
        "status": "optional",
    },
    {
        "id": "auto-memory",
        "name": "Auto Memory Extraction",
        "description": "Extract memories from conversation transcripts via local LLM. Also provides context assembly for prompt injection.",
        "endpoints": ["POST /auto/extract", "POST /auto/context", "POST /auto/fix-layout"],
        "status": "optional",
    },
    {
        "id": "handoff",
        "name": "Agent Handoff",
        "description": "Pass tasks between agents (Claude ↔ Codex ↔ Cursor) via memory. Receiver picks up context and continues.",
        "endpoints": ["POST /models/handoff/create", "POST /models/handoff/pickup"],
        "status": "optional",
    },
    {
        "id": "improvements",
        "name": "Improvement Tracker",
        "description": "Report and resolve bugs/improvements for the project. Used by agents to share findings across sessions.",
        "endpoints": ["POST /improvements", "GET /improvements", "PATCH /improvements/{id}/resolve"],
        "status": "optional",
    },
    {
        "id": "normalization",
        "name": "Semantic Normalization",
        "description": "Learn project-specific jargon and normalize terms across agents (e.g. AS → adaptive skillization).",
        "endpoints": ["POST /normalization/terms", "GET /normalization/glossary"],
        "status": "optional",
    },
    {
        "id": "project-knowledge",
        "name": "Project Knowledge Cache",
        "description": (
            "RepRap principle: index project components so agents understand them instantly without re-reading code. "
            "Hash-based refresh detects file changes. Knowledge transfers across sessions and projects."
        ),
        "endpoints": [
            "POST /project/ingest",
            "POST /project/refresh",
            "GET /project/components",
            "GET /project/component/{id}",
            "POST /project/search",
            "POST /project/enrich-task",
        ],
        "status": "optional",
    },
    {
        "id": "mcp",
        "name": "MCP SSE Transport",
        "description": "Model Context Protocol server. Exposes all tools to Claude Code, Codex, and other MCP-compatible agents.",
        "endpoints": ["GET /mcp/sse", "POST /mcp/messages"],
        "status": "core",
    },
]


@router.get("/health")
async def health(qdrant: QdrantDep, ollama: OllamaDep):
    qdrant_ok = await qdrant.health()
    ollama_ok = await ollama.health()
    status = "ok" if (qdrant_ok and ollama_ok) else "degraded"
    return {
        "status": status,
        "started_at": _STARTED_AT,
        "qdrant": {"reachable": qdrant_ok},
        "ollama": {"reachable": ollama_ok},
    }


@router.get("/stats")
async def stats(qdrant: QdrantDep):
    return await qdrant.collection_stats()


@router.get("/system/info")
async def system_info(qdrant: QdrantDep, ollama: OllamaDep):
    """
    Full system overview: status, components, live counters, models.
    Use this to understand what supermemory can do and what's currently running.
    """
    qdrant_ok = await qdrant.health()
    ollama_ok = await ollama.health()

    # Live counters — best-effort, don't fail if unavailable
    memory_count = 0
    skill_count = 0
    layout_terms_count = 0
    try:
        col_stats = await qdrant.collection_stats()
        memory_count = col_stats.get("points_count", 0)
    except Exception:
        pass

    try:
        from qdrant_client.http import models as qm
        results, _ = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=qm.Filter(must=[
                qm.FieldCondition(key="category", match=qm.MatchValue(value="skill"))
            ]),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        skill_count_result = await qdrant._client.count(
            collection_name=qdrant._collection,
            count_filter=qm.Filter(must=[
                qm.FieldCondition(key="category", match=qm.MatchValue(value="skill"))
            ]),
        )
        skill_count = skill_count_result.count
    except Exception:
        pass

    try:
        collections = await qdrant._client.get_collections()
        names = [c.name for c in collections.collections]
        if "layout_terms" in names:
            lt = await qdrant._client.count(collection_name="layout_terms")
            layout_terms_count = lt.count
    except Exception:
        pass

    # Active Ollama models
    ollama_models: list[str] = []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{settings.ollama_base_url}/api/tags")
            ollama_models = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass

    return {
        "status": "ok" if (qdrant_ok and ollama_ok) else "degraded",
        "started_at": _STARTED_AT,
        "uptime_seconds": int(time.time()) - _STARTED_AT,
        "infrastructure": {
            "qdrant": {"reachable": qdrant_ok, "url": f"{settings.qdrant_host}:{settings.qdrant_port}"},
            "ollama": {"reachable": ollama_ok, "url": settings.ollama_base_url, "models": ollama_models},
            "embedding_model": settings.ollama_embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
        },
        "counters": {
            "memories": memory_count,
            "skills": skill_count,
            "layout_terms": layout_terms_count,
        },
        "components": _COMPONENTS,
        "api_prefix": settings.api_prefix,
    }
