import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from qdrant_client import AsyncQdrantClient

from app.config import settings
from app.core.logging import setup_logging
from app.dependencies import set_ollama_service, set_qdrant_client
from app.routers import admin, auto_memory, batch, code_search, crystallizer, dashboard, docs, entities, governance, health, improvements, ingest, layout_fixer, learning, log_filter, memories, mcp_sse, models, normalization, openai_compat, outcomes, project, registry, router_api, setup, skills, tasks, tracker, tree, watcher
from app.services.ollama_service import OllamaService
from app.services.qdrant_service import QdrantService

logger = logging.getLogger(__name__)


def _should_suppress_asyncio_transport_error(context: dict) -> bool:
    """Treat common Windows client disconnect noise as benign."""
    exc = context.get("exception")
    if not isinstance(exc, ConnectionResetError):
        return False

    if getattr(exc, "winerror", None) != 10054:
        return False

    message = str(context.get("message", ""))
    handle = context.get("handle")
    return "_call_connection_lost" in message or "_call_connection_lost" in repr(handle)


def _install_asyncio_exception_filter() -> tuple[asyncio.AbstractEventLoop, object]:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        if _should_suppress_asyncio_transport_error(context):
            logger.debug("Suppressed benign asyncio transport disconnect: %s", context.get("exception"))
            return
        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    return loop, previous_handler


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    loop, previous_handler = _install_asyncio_exception_filter()
    logger.info("Starting memory server")

    # Init Qdrant
    if settings.qdrant_in_memory:
        client = AsyncQdrantClient(":memory:")
    else:
        client = AsyncQdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
        )
    set_qdrant_client(client)
    qdrant_svc = QdrantService(client)
    await qdrant_svc.ensure_collection()

    # Init Ollama + warm up embedding model
    ollama_svc = OllamaService()
    set_ollama_service(ollama_svc)

    # Wire watcher service — without this watcher silently does nothing
    from app.services.watcher_service import set_services as watcher_set_services
    watcher_set_services(qdrant_svc, ollama_svc)
    try:
        warmup_vector = await ollama_svc.embed("warmup")
        actual_dim = len(warmup_vector)
        if actual_dim != settings.embedding_dimensions:
            raise RuntimeError(
                f"Embedding dimension mismatch: model '{settings.ollama_embedding_model}' "
                f"produces {actual_dim}-dim vectors, but EMBEDDING_DIMENSIONS={settings.embedding_dimensions}. "
                f"Set EMBEDDING_DIMENSIONS={actual_dim} in .env or switch models."
            )
        logger.info("Ollama embed model warmed up (dim=%d)", actual_dim)
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("Ollama warmup failed (will retry on first request): %s", e)

    # Optional auto-start for AI directory watcher so dialogue learning keeps moving
    if settings.watcher_auto_start and "watcher" not in _disabled():
        try:
            from app.routers.watcher import default_ai_dirs
            from app.services.watcher_service import watcher as watcher_service_singleton

            auto_dirs = default_ai_dirs()
            if auto_dirs:
                watched = watcher_service_singleton.start(auto_dirs, settings.watcher_agent_id)
                if watched:
                    logger.info("Watcher auto-started for %d directorie(s)", len(watched))
                else:
                    logger.info("Watcher auto-start requested but no directories were started")
            else:
                logger.info("Watcher auto-start requested but no AI directories were detected")
        except Exception as e:
            logger.warning("Watcher auto-start skipped: %s", e)

    # Init job queue — register handlers for LLM-heavy background operations
    from app.services.job_queue import get_job_queue
    from app.routers.project import _ingest_handler, _refresh_handler
    from app.routers.skills import _retag_handler, _regenerate_content_handler
    from app.routers.crystallizer import _evolve_handler
    job_queue = get_job_queue()
    job_queue.register("project_ingest", _ingest_handler)
    job_queue.register("project_refresh", _refresh_handler)
    job_queue.register("skills_retag", _retag_handler)
    job_queue.register("evolve_skills", _evolve_handler)
    job_queue.register("regenerate_skill_content", _regenerate_content_handler)

    async def _task_memoir_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant, get_ollama
        from app.services.memoir_service import generate_and_store_memoir
        from app.config import settings
        task_id = payload["task_id"]
        project = payload.get("project", "supermemory")
        qdrant = get_qdrant()
        memoir_id = await generate_and_store_memoir(
            task_id=task_id,
            qdrant_client=qdrant._client,
            collection=settings.qdrant_collection_name,
            ollama=get_ollama(),
            project=project,
        )
        return {"task_id": task_id, "memoir_id": memoir_id}

    job_queue.register("task_memoir", _task_memoir_handler)

    async def _docs_rebuild_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant
        from app.services.docs_service import invalidate_docs_cache, rebuild_docs
        from app.config import settings
        project_id = payload.get("project", "supermemory")
        force = payload.get("force", False)
        if force:
            invalidate_docs_cache(project_id)
        qdrant = get_qdrant()
        status = await rebuild_docs(project_id, qdrant._client, settings.qdrant_collection_name)
        return {"project": project_id, "generated_at": status.generated_at.isoformat(), "sections": list(status.sections.keys())}

    job_queue.register("docs_rebuild", _docs_rebuild_handler)
    await job_queue.start()
    logger.info("Job queue started")

    # Migrate skill counters from Qdrant payload → SQLite (idempotent, skips already-migrated)
    from app.services.skill_counters import get_skill_counters
    try:
        _sc = get_skill_counters()
        from qdrant_client.http import models as _qm
        _skill_results, _ = await client.scroll(
            collection_name=settings.qdrant_collection_name,
            scroll_filter=_qm.Filter(must=[
                _qm.FieldCondition(key="category", match=_qm.MatchValue(value="skill"))
            ]),
            limit=1000,
            with_payload=True,
            with_vectors=False,
        )
        migrated = 0
        for _pt in _skill_results:
            _p = _pt.payload or {}
            if _p.get("usage_count", 0) > 0 or _p.get("pinned", False):
                await _sc.upsert(
                    skill_id=str(_pt.id),
                    usage_count=_p.get("usage_count", 0),
                    helpful_count=_p.get("helpful_count", 0),
                    pinned=bool(_p.get("pinned", False)),
                )
                migrated += 1
        if migrated:
            logger.info("Skill counters migration: seeded %d skills from Qdrant -> SQLite", migrated)
    except Exception as _e:
        logger.warning("Skill counters migration failed (non-fatal): %s", _e)

    # Migrate improvements from Qdrant → SQLite (idempotent, INSERT OR IGNORE)
    from app.services.improvements_store import get_improvements_store
    try:
        _is = get_improvements_store()
        from qdrant_client.http import models as _qm2
        _imp_results, _ = await client.scroll(
            collection_name=settings.qdrant_collection_name,
            scroll_filter=_qm2.Filter(must=[
                _qm2.FieldCondition(key="category", match=_qm2.MatchValue(value="improvement"))
            ]),
            limit=2000,
            with_payload=True,
            with_vectors=False,
        )
        _imp_migrated = 0
        for _pt in _imp_results:
            _p = _pt.payload or {}
            import time as _time
            _ts_raw = _p.get("timestamp")
            try:
                from datetime import datetime as _dt
                _created_at = _dt.fromisoformat(_ts_raw).timestamp() if _ts_raw else _time.time()
            except Exception:
                _created_at = _time.time()
            _resolved_raw = _p.get("resolved_at")
            from uuid import UUID as _UUID
            await _is.insert(
                improvement_id=_UUID(str(_pt.id)),
                title=_p.get("title", _p.get("content", "")[:256]),
                description=_p.get("description", _p.get("content", "")),
                project=_p.get("project", "supermemory"),
                agent_id=_p.get("agent_id", "llm"),
                importance_score=float(_p.get("importance_score", 0.7)),
                tags=_p.get("tags") or [],
                created_at=_created_at,
            )
            # If resolved in Qdrant, update status in SQLite too
            if _p.get("status") == "resolved":
                await _is.resolve(_UUID(str(_pt.id)))
            _imp_migrated += 1
        if _imp_migrated:
            logger.info("Improvements migration: seeded %d items from Qdrant -> SQLite", _imp_migrated)
    except Exception as _e:
        logger.warning("Improvements migration failed (non-fatal): %s", _e)

    # Migrate workflow_guidance from Qdrant → learning.db (idempotent, INSERT OR IGNORE)
    from app.services.learning_store import get_learning_store
    try:
        _ls = get_learning_store()
        from qdrant_client.http import models as _qm3
        _wf_results, _ = await client.scroll(
            collection_name=settings.qdrant_collection_name,
            scroll_filter=_qm3.Filter(must=[
                _qm3.FieldCondition(key="category", match=_qm3.MatchValue(value="workflow_guidance"))
            ]),
            limit=2000,
            with_payload=True,
            with_vectors=False,
        )
        _wf_migrated = 0
        for _pt in _wf_results:
            _p = _pt.payload or {}
            import time as _time2
            _ts_raw = _p.get("timestamp")
            try:
                from datetime import datetime as _dt2
                _wf_created = _dt2.fromisoformat(_ts_raw).timestamp() if _ts_raw else _time2.time()
            except Exception:
                _wf_created = _time2.time()
            from uuid import UUID as _UUID2
            await _ls.insert_artifact(
                artifact_id=_UUID2(str(_pt.id)),
                agent_id=_p.get("agent_id", ""),
                artifact_type="workflow_guidance",
                workflow_type=_p.get("workflow_type", ""),
                workflow_action=_p.get("workflow_action", ""),
                workflow_context=_p.get("workflow_context", ""),
                content=_p.get("content", ""),
                confidence=float(_p.get("importance_score", 0.7)),
                tags=_p.get("tags") or [],
                created_at=_wf_created,
            )
            _wf_migrated += 1
        if _wf_migrated:
            logger.info("Workflow guidance migration: seeded %d artifacts from Qdrant -> SQLite", _wf_migrated)
    except Exception as _e:
        logger.warning("Workflow guidance migration failed (non-fatal): %s", _e)

    # Seed memory_store.db from Qdrant for existing skill + code_component records (idempotent)
    from app.services.memory_store import get_memory_store
    try:
        _ms = get_memory_store()
        from qdrant_client.http import models as _qm4
        for _cat in ("skill", "code_component"):
            _cat_results, _ = await client.scroll(
                collection_name=settings.qdrant_collection_name,
                scroll_filter=_qm4.Filter(must=[
                    _qm4.FieldCondition(key="category", match=_qm4.MatchValue(value=_cat))
                ]),
                limit=2000,
                with_payload=True,
                with_vectors=False,
            )
            _cat_seeded = 0
            for _pt in _cat_results:
                if await _ms.exists(str(_pt.id)):
                    continue
                _p = _pt.payload or {}
                _content = _p.get("content", "")
                if _cat == "skill":
                    _meta = {
                        "skill_name": _p.get("skill_name", ""),
                        "description": _p.get("skill_description", ""),
                        "platform": _p.get("platform", "claude"),
                        "reference_url": _p.get("reference_url"),
                    }
                else:
                    _meta = {
                        "code_path": _p.get("code_path", ""),
                        "code_symbol": _p.get("code_symbol", ""),
                        "code_chunk_type": _p.get("code_chunk_type", ""),
                        "code_language": _p.get("code_language", ""),
                        "code_imports": _p.get("code_imports", []),
                    }
                import time as _time3
                _ts_raw = _p.get("timestamp")
                try:
                    from datetime import datetime as _dt3
                    _created = _dt3.fromisoformat(_ts_raw).timestamp() if _ts_raw else _time3.time()
                except Exception:
                    _created = _time3.time()
                await _ms.upsert(str(_pt.id), _cat, _content, _meta, created_at=_created)
                _cat_seeded += 1
            if _cat_seeded:
                logger.info("memory_store migration: seeded %d %s records from Qdrant -> SQLite", _cat_seeded, _cat)
    except Exception as _e:
        logger.warning("memory_store migration failed (non-fatal): %s", _e)

    # Start GLM mirror background loop
    from app.services.glm_mirror import get_glm_mirror, GLM_MIRROR_INTERVAL_HOURS

    async def _glm_mirror_loop() -> None:
        mirror = get_glm_mirror()
        from app.services.learning_store import get_learning_store as _get_ls
        while True:
            import time as _time_glm
            mirror.next_run_at = _time_glm.time() + GLM_MIRROR_INTERVAL_HOURS * 3600
            await asyncio.sleep(GLM_MIRROR_INTERVAL_HOURS * 3600)
            mirror.next_run_at = None
            try:
                result = await mirror.run(ollama_svc, _get_ls())
                logger.info(
                    "GLM mirror: created=%d updated=%d events=%d errors=%d",
                    result.candidates_created,
                    result.candidates_updated,
                    result.events_analyzed,
                    len(result.errors),
                )
            except Exception as _glm_err:
                logger.warning("GLM mirror loop error (non-fatal): %s", _glm_err)

    from app.routers.admin import register_task, start_task as _admin_start_task
    _glm_entry = register_task("glm_mirror", _glm_mirror_loop)
    _glm_task = _admin_start_task(_glm_entry)
    logger.info("GLM mirror background task started (interval=%.0fh)", GLM_MIRROR_INTERVAL_HOURS)

    # Start importance decay background loop
    import os as _os_decay
    _DECAY_INTERVAL_HOURS = float(_os_decay.getenv("DECAY_INTERVAL_HOURS", "24"))
    _DECAY_IDLE_DAYS = float(_os_decay.getenv("DECAY_IDLE_DAYS", "7"))
    _DECAY_STEP = float(_os_decay.getenv("DECAY_STEP", "0.05"))
    _DECAY_FLOOR = float(_os_decay.getenv("DECAY_FLOOR", "0.05"))

    async def _decay_loop() -> None:
        from app.routers.governance import run_decay, DecayConfig
        from app.dependencies import get_qdrant
        while True:
            await asyncio.sleep(_DECAY_INTERVAL_HOURS * 3600)
            try:
                qdrant = get_qdrant()
                cfg = DecayConfig(
                    idle_days=_DECAY_IDLE_DAYS,
                    decay_step=_DECAY_STEP,
                    floor=_DECAY_FLOOR,
                    dry_run=False,
                )
                result = await run_decay(qdrant, cfg)
                logger.info(
                    "Decay job: affected=%d skipped(pinned=%d floor=%d recent=%d)",
                    result.affected,
                    result.skipped_pinned,
                    result.skipped_floor,
                    result.skipped_recent,
                )
            except Exception as _decay_err:
                logger.warning("Decay loop error (non-fatal): %s", _decay_err)

    _decay_entry = register_task("decay", _decay_loop)
    _decay_task = _admin_start_task(_decay_entry)
    logger.info("Importance decay background task started (interval=%.0fh)", _DECAY_INTERVAL_HOURS)

    # Start crystallization background loop
    import os as _os_crystal
    _CRYSTAL_INTERVAL_HOURS = float(_os_crystal.getenv("CRYSTAL_INTERVAL_HOURS", "168"))  # weekly

    async def _crystallization_loop() -> None:
        from app.services.crystallization_service import find_crystallization_candidates
        from app.services.learning_store import get_learning_store as _get_ls_crystal
        from app.dependencies import get_qdrant as _get_qdrant_crystal
        from app.config import settings as _settings_crystal
        while True:
            await asyncio.sleep(_CRYSTAL_INTERVAL_HOURS * 3600)
            try:
                qdrant = _get_qdrant_crystal()
                ls = _get_ls_crystal()
                from app.services.text_localization import prepare_artifact_texts
                candidates = await find_crystallization_candidates(
                    qdrant._client, _settings_crystal.qdrant_collection_name, ollama_svc
                )
                created = updated = 0
                for cand in candidates:
                    cleaned_fields, enriched_meta = await prepare_artifact_texts(
                        content=cand.statement,
                        observation=cand.observation,
                        why_it_matters=cand.why_it_matters,
                        meta={
                            "candidate_key": cand.key,
                            "topic_path": cand.topic_path,
                            "target_scope": cand.target_scope,
                            "supports": cand.supports,
                            "project_diversity": cand.project_diversity,
                            "evidence_count": cand.evidence_count,
                        },
                    )
                    _, was_created = await ls.upsert_candidate(
                        agent_id="crystallization",
                        action_type="crystallize_knowledge",
                        artifact_type="canonical",
                        content=cleaned_fields["content"],
                        trigger_dsl="",
                        context_signature=cand.topic_path,
                        observation=cleaned_fields["observation"],
                        why_it_matters=cleaned_fields["why_it_matters"],
                        confidence=cand.confidence,
                        risk_level="low",
                        meta=enriched_meta,
                    )
                    if was_created:
                        created += 1
                    else:
                        updated += 1
                logger.info(
                    "Crystallization job: candidates created=%d updated=%d",
                    created, updated,
                )
            except Exception as _cryst_err:
                logger.warning("Crystallization loop error (non-fatal): %s", _cryst_err)

    _crystal_entry = register_task("crystallization", _crystallization_loop)
    _crystal_task = _admin_start_task(_crystal_entry)
    logger.info("Crystallization background task started (interval=%.0fh)", _CRYSTAL_INTERVAL_HOURS)

    # Start scout background loop
    import os as _os_scout
    _SCOUT_INTERVAL_HOURS = float(_os_scout.getenv("SCOUT_INTERVAL_HOURS", "6"))

    async def _scout_loop():
        import asyncio as _aio
        await _aio.sleep(300)  # 5 min warmup — let server fully initialize
        while True:
            try:
                from app.services.learning_store import get_learning_store as _get_ls_scout
                from app.services.best_practice_scout import check_sufficiency, fetch_best_practices
                _ls = _get_ls_scout()
                _suf = await check_sufficiency(
                    _ls,
                    project="supermemory",
                    task="general development",
                    agent_id="cron-scout",
                )
                if not _suf.sufficient:
                    logger.info(
                        "Scout: insufficient coverage (%s), fetching best practices (domains=%s)",
                        _suf.reason, _suf.missing_domains,
                    )
                    from app.routers.learning import scout_fetch, ScoutFetchRequest
                    from fastapi import BackgroundTasks as _BT
                    await scout_fetch(
                        ScoutFetchRequest(
                            project="supermemory",
                            task="general development",
                            domains=_suf.missing_domains or [],
                            agent_id="cron-scout",
                        ),
                        _BT(),
                    )
                else:
                    logger.debug("Scout: coverage sufficient (%s)", _suf.reason)
            except Exception as _scout_err:
                logger.debug("Scout loop error (non-fatal): %s", _scout_err)
            await _aio.sleep(_SCOUT_INTERVAL_HOURS * 3600)

    _scout_entry = register_task("scout", _scout_loop)
    _scout_task = _admin_start_task(_scout_entry)
    logger.info("Scout background task started (interval=%.0fh)", _SCOUT_INTERVAL_HOURS)

    # Cleanup orphaned docs cache files (projects with no data in Qdrant)
    from app.services.docs_service import cleanup_orphaned_caches
    await cleanup_orphaned_caches(client, settings.qdrant_collection_name)

    logger.info(
        "Memory server ready — Qdrant=%s:%s, Ollama=%s, model=%s",
        settings.qdrant_host,
        settings.qdrant_port,
        settings.ollama_base_url,
        settings.ollama_embedding_model,
    )

    yield

    try:
        _glm_task.cancel()
        _decay_task.cancel()
        _crystal_task.cancel()
        _scout_task.cancel()
        try:
            await _glm_task
        except asyncio.CancelledError:
            pass
        try:
            await _decay_task
        except asyncio.CancelledError:
            pass
        try:
            await _crystal_task
        except asyncio.CancelledError:
            pass
        try:
            await _scout_task
        except asyncio.CancelledError:
            pass
        await ollama_svc.close()
        await client.close()
        from app.services.performance_tracker import _tracker
        if _tracker:
            _tracker.close()
        from app.services.model_registry import _registry as _model_registry
        if _model_registry:
            _model_registry.close()
        from app.services.job_queue import get_job_queue as _get_jq
        await _get_jq().stop()
        from app.services.rate_limit import close_rate_limiter
        await close_rate_limiter()
        from app.services.mcp_session_store import close_session_store
        await close_session_store()
        from app.services.skill_counters import close_skill_counters
        close_skill_counters()
        from app.services.improvements_store import close_improvements_store
        close_improvements_store()
        from app.services.learning_store import close_learning_store
        await close_learning_store()
        from app.services.memory_store import close_memory_store
        close_memory_store()
        logger.info("Memory server stopped")
    finally:
        loop.set_exception_handler(previous_handler)


def _disabled() -> frozenset[str]:
    """Return set of module names disabled via DISABLED_MODULES env var."""
    return frozenset(
        m.strip().lower() for m in settings.disabled_modules.split(",") if m.strip()
    )


def _try_include(app: FastAPI, router, name: str, prefix: str, disabled: frozenset[str]) -> bool:
    """Include a router, skipping silently if disabled or failing gracefully on error."""
    if name in disabled:
        logger.info("Module '%s' is disabled (DISABLED_MODULES)", name)
        return False
    try:
        app.include_router(router, prefix=prefix)
        return True
    except Exception as e:
        logger.error("Failed to load optional module '%s': %s — continuing without it", name, e)
        return False


# ── Security middleware ───────────────────────────────────────────────────────

# Paths exempt from API key check (health probes, MCP handshake)
_AUTH_EXEMPT = {
    "/api/v1/health",
    "/api/v1/docs/status.html",
    "/.well-known/",
    "/dashboard",
    "/favicon.ico",
    "/mcp/",
}

# LLM-heavy endpoints that should be rate-limited
_LLM_PATHS = {"/auto/fix-layout", "/auto/extract", "/auto/context",
              "/crystallizer/crystallize", "/skills/retag", "/project/ingest"}


def _add_security_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path

        # 1. Request size limit
        if settings.max_request_size_mb > 0:
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > settings.max_request_size_mb * 1024 * 1024:
                return JSONResponse(status_code=413, content={"detail": "Request too large"})

        # 2. API key auth (skip exempt paths and OPTIONS)
        if settings.api_key and request.method != "OPTIONS":
            exempt = any(path == e or path.startswith(e) for e in _AUTH_EXEMPT)
            if not exempt:
                provided = request.headers.get("x-api-key", "")
                if provided != settings.api_key:
                    return JSONResponse(
                        status_code=401,
                        content={"error": "unauthorized_client", "error_description": "Invalid or missing X-Api-Key"},
                    )

        # 3. Per-IP rate limit on LLM endpoints
        if settings.llm_rate_limit_per_min > 0:
            is_llm = any(path.endswith(p) for p in _LLM_PATHS)
            if is_llm:
                ip = request.client.host if request.client else "unknown"
                from app.services.rate_limit import get_rate_limiter
                allowed = await get_rate_limiter().check(ip, settings.llm_rate_limit_per_min)
                if not allowed:
                    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

        return await call_next(request)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Super Memory Server",
        description="Local semantic memory store for AI agents (Qdrant + Ollama)",
        version="1.0.0",
        lifespan=lifespan,
    )
    _add_security_middleware(app)

    # Mount static files for favicon.ico and other assets
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory="static"), name="static")

    prefix = settings.api_prefix
    disabled = _disabled()

    # ── Core modules (always loaded — required for basic functionality) ──────────
    app.include_router(health.router, prefix=prefix)
    app.include_router(batch.router, prefix=prefix)
    app.include_router(memories.router, prefix=prefix)
    app.include_router(memories.hierarchy_router, prefix=prefix)
    app.include_router(outcomes.router, prefix=prefix)
    app.include_router(ingest.router, prefix=prefix)

    # ── Optional capability modules (can be disabled via DISABLED_MODULES) ───────
    _try_include(app, improvements.router, "improvements", prefix, disabled)
    _try_include(app, skills.router, "skills", prefix, disabled)
    _try_include(app, registry.router, "registry", prefix, disabled)
    _try_include(app, tracker.router, "tracker", prefix, disabled)
    _try_include(app, router_api.router, "router_api", prefix, disabled)
    _try_include(app, crystallizer.router, "crystallizer", prefix, disabled)
    _try_include(app, auto_memory.router, "auto_memory", prefix, disabled)
    _try_include(app, normalization.router, "normalization", prefix, disabled)
    _try_include(app, governance.router, "governance", prefix, disabled)
    _try_include(app, code_search.router, "code_search", prefix, disabled)
    _try_include(app, project.router, "project", prefix, disabled)
    _try_include(app, tasks.router, "tasks", prefix, disabled)
    _try_include(app, layout_fixer.router, "layout_fixer", prefix, disabled)
    _try_include(app, log_filter.router, "log_filter", prefix, disabled)
    _try_include(app, watcher.router, "watcher", prefix, disabled)
    _try_include(app, docs.router, "docs", prefix, disabled)
    _try_include(app, tree.router, "tree", prefix, disabled)

    # ── Infrastructure modules ───────────────────────────────────────────────────
    _try_include(app, models.router, "models", prefix, disabled)
    _try_include(app, entities.router, "entities", prefix, disabled)
    _try_include(app, learning.router, "learning", prefix, disabled)
    _try_include(app, admin.router, "admin", prefix, disabled)

    # OpenAI-compatible adapter — mounted at /v1/memories for cross-tool compatibility
    _try_include(app, openai_compat.router, "openai_compat", "", disabled)

    # MCP SSE transport — zero-config client connection
    app.include_router(mcp_sse.discovery_router)
    app.include_router(mcp_sse.router)
    # Client bootstrap — auto-setup script served at /client-setup
    app.include_router(setup.router)
    # Web dashboard — served at /dashboard (outside /api/v1)
    app.include_router(dashboard.router)

    if disabled:
        logger.info("Loaded with disabled modules: %s", ", ".join(sorted(disabled)))

    return app


app = create_app()
