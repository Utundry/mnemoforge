import time
import os
from fastapi import APIRouter

from app.config import settings
from app.dependencies import OllamaDep, QdrantDep
from app.services.data_hygiene_service import get_data_hygiene_store
from app.services.data_integrity_service import get_data_integrity_store
from app.services.lmstudio_service import LMStudioService
from app.services.server_build_info import public_server_build_info
from app.services.storage_trust_service import build_storage_trust_report

router = APIRouter(tags=["health"])

_STARTED_AT = int(time.time())


def _parse_model_list(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


async def _lmstudio_status() -> dict:
    service = LMStudioService()
    try:
        models = await service.list_models()
        reachable = bool(models) or await service.health()
        configured = os.getenv("LMSTUDIO_MODEL", settings.lmstudio_model).strip() or settings.lmstudio_model
        selected = await service.resolve_model(configured) if reachable else configured
        return {
            "reachable": reachable,
            "url": settings.lmstudio_base_url,
            "model": configured,
            "selected_model": selected,
            "models": models,
        }
    finally:
        await service.close()


def _llm_status(lmstudio_status: dict | None = None) -> dict:
    from app.services.cloud_llm import configured_cloud_model_profiles, cloud_available, cloud_provider

    cloud_profiles = configured_cloud_model_profiles()
    cloud_models = list(cloud_profiles.keys())
    cloud_model_details = [
        {
            "model": model_id,
            "provider": profile.provider,
            "api_style": profile.api_style,
            "base_url": profile.base_url,
        }
        for model_id, profile in cloud_profiles.items()
    ]
    default_provider = cloud_provider() if cloud_available() else "cloud-llm"
    local_provider = os.getenv("LOCAL_LLM_PROVIDER", settings.local_llm_provider).strip().lower() or "auto"
    local_order = _parse_model_list(os.getenv("LOCAL_LLM_FALLBACK_ORDER", settings.local_llm_fallback_order))
    return {
        "local_model": os.getenv("LOCAL_GENERATE_MODEL", settings.learning_mirror_model or "qwen3:1.7b").strip()
        or "qwen3:1.7b",
        "local_provider": local_provider,
        "local_fallback_order": local_order,
        "lmstudio": lmstudio_status
        or {
            "reachable": False,
            "url": settings.lmstudio_base_url,
            "model": os.getenv("LMSTUDIO_MODEL", settings.lmstudio_model).strip() or settings.lmstudio_model,
            "selected_model": "",
            "models": [],
        },
        "cloud_available": cloud_available(),
        "default_cloud_provider": default_provider,
        "configured_cloud_models": cloud_models,
        "configured_cloud_model_details": cloud_model_details,
        "gateway": {
            "local_fallback_enabled": os.getenv("LLM_GATEWAY_ENABLE_LOCAL_FALLBACK", "1").strip().lower()
            not in {"0", "false", "no", "off"},
            "economy_cloud_llms": _parse_model_list(os.getenv("ECONOMY_CLOUD_LLMS", "")),
            "balanced_cloud_llms": _parse_model_list(os.getenv("BALANCED_CLOUD_LLMS", "")),
            "reasoning_cloud_llms": _parse_model_list(os.getenv("REASONING_CLOUD_LLMS", "")),
            "profile_count": len(cloud_models),
        },
    }


def _provider_matrix_status(*, ollama_ok: bool, lmstudio_status: dict, llm: dict) -> dict:
    cloud_ok = bool(llm.get("cloud_available"))
    lmstudio_ok = bool((lmstudio_status or {}).get("reachable"))
    local_provider = os.getenv("LOCAL_LLM_PROVIDER", settings.local_llm_provider).strip().lower() or "auto"
    local_order = _parse_model_list(os.getenv("LOCAL_LLM_FALLBACK_ORDER", settings.local_llm_fallback_order))
    providers = {
        "ollama": {
            "enabled": local_provider in {"", "auto", "ollama"} or "ollama" in local_order,
            "reachable": bool(ollama_ok),
            "kind": "local",
            "url": settings.ollama_base_url,
            "model": os.getenv("LOCAL_GENERATE_MODEL", settings.learning_mirror_model or "qwen3:1.7b").strip()
            or "qwen3:1.7b",
        },
        "lmstudio": {
            "enabled": local_provider in {"auto", "lmstudio"} or "lmstudio" in local_order,
            "reachable": lmstudio_ok,
            "kind": "local_openai_compatible",
            "url": settings.lmstudio_base_url,
            "selected_model": (lmstudio_status or {}).get("selected_model", ""),
        },
        "cloud": {
            "enabled": cloud_ok,
            "reachable": cloud_ok,
            "kind": "cloud_openai_compatible",
            "models": llm.get("configured_cloud_models") or [],
            "model_details": llm.get("configured_cloud_model_details") or [],
            "default_provider": llm.get("default_cloud_provider"),
        },
    }
    usable = [
        name
        for name, provider in providers.items()
        if bool(provider.get("enabled")) and bool(provider.get("reachable"))
    ]
    available_llms = []
    for name, provider in providers.items():
        if name not in usable:
            continue
        if name == "cloud":
            for detail in provider.get("model_details") or []:
                available_llms.append(
                    {
                        "id": detail.get("model"),
                        "provider": detail.get("provider") or "cloud",
                        "kind": provider.get("kind"),
                        "scope": "cloud",
                    }
                )
            if not provider.get("model_details"):
                available_llms.append(
                    {
                        "id": provider.get("default_provider") or "cloud-llm",
                        "provider": "cloud",
                        "kind": provider.get("kind"),
                        "scope": "cloud",
                    }
                )
            continue
        available_llms.append(
            {
                "id": provider.get("selected_model") or provider.get("model") or name,
                "provider": name,
                "kind": provider.get("kind"),
                "scope": "local",
            }
        )
    return {
        "healthy": bool(usable),
        "usable_providers": usable,
        "available_llms": available_llms,
        "providers": providers,
        "health_rule": "healthy when at least one enabled LLM provider is usable",
    }

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
    lmstudio = await _lmstudio_status()
    integrity = get_data_integrity_store().overview()
    data_hygiene = get_data_hygiene_store().overview()
    storage_trust = build_storage_trust_report()
    llm = _llm_status(lmstudio)
    llm_providers = _provider_matrix_status(ollama_ok=ollama_ok, lmstudio_status=lmstudio, llm=llm)
    status = "ok" if (qdrant_ok and llm_providers["healthy"] and integrity["status"] == "ok") else "degraded"
    return {
        "status": status,
        "started_at": _STARTED_AT,
        "build": public_server_build_info(),
        "qdrant": {"reachable": qdrant_ok},
        "ollama": {"reachable": ollama_ok},
        "lmstudio": lmstudio,
        "llm": llm,
        "llm_providers": llm_providers,
        "integrity": integrity,
        "data_hygiene": data_hygiene,
        "storage_trust": storage_trust,
    }


@router.get("/stats")
async def stats(qdrant: QdrantDep):
    return await qdrant.collection_stats()


@router.get("/system/info")
async def system_info(qdrant: QdrantDep, ollama: OllamaDep):
    """
    Full system overview: status, components, live counters, models.
    Use this to understand what SloplessCode can do and what's currently running.
    """
    qdrant_ok = await qdrant.health()
    ollama_ok = await ollama.health()
    lmstudio = await _lmstudio_status()
    integrity = get_data_integrity_store().overview()
    data_hygiene = get_data_hygiene_store().overview()
    storage_trust = build_storage_trust_report()
    llm = _llm_status(lmstudio)
    llm_providers = _provider_matrix_status(ollama_ok=ollama_ok, lmstudio_status=lmstudio, llm=llm)

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
        "status": "ok" if (qdrant_ok and llm_providers["healthy"] and integrity["status"] == "ok") else "degraded",
        "started_at": _STARTED_AT,
        "uptime_seconds": int(time.time()) - _STARTED_AT,
        "infrastructure": {
            "qdrant": {"reachable": qdrant_ok, "url": f"{settings.qdrant_host}:{settings.qdrant_port}"},
            "ollama": {"reachable": ollama_ok, "url": settings.ollama_base_url, "models": ollama_models},
            "lmstudio": lmstudio,
            "embedding_model": settings.ollama_embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
            "llm": llm,
            "llm_providers": llm_providers,
        },
        "counters": {
            "memories": memory_count,
            "skills": skill_count,
            "layout_terms": layout_terms_count,
        },
        "integrity": integrity,
        "data_hygiene": data_hygiene,
        "storage_trust": storage_trust,
        "components": _COMPONENTS,
        "api_prefix": settings.api_prefix,
    }
