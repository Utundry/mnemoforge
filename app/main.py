import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from qdrant_client import AsyncQdrantClient

from app.config import settings
from app.core.logging import setup_logging
from app.dependencies import set_ollama_service, set_qdrant_client
from app.routers import admin, auto_memory, batch, code_search, context_pages, crystallizer, dashboard, docs, entities, governance, health, improvements, ingest, knowledge_tree_api, laws, layout_fixer, learning, log_filter, memories, mcp_sse, models, normalization, openai_compat, outcomes, project, project_tasks, registry, router_api, setup, skills, task_execution_context, tasks, tracker, tree, unified_artifacts, watcher
from app.services.data_hygiene_service import (
    apply_approved_delete,
    apply_reviewed_delete,
    close_data_hygiene_store,
    get_data_hygiene_store,
    promote_auto_test_cleanup_candidates,
    queue_approved_delete_remediation,
    queue_reviewed_delete_remediation,
    reconcile_completed_remediations as reconcile_hygiene_completed_remediations,
    run_data_hygiene_audit,
)
from app.services.data_integrity_service import (
    build_auto_discovery_guard,
    build_auto_remediation_guard,
    close_data_integrity_store,
    get_data_integrity_store,
    maybe_auto_discover_slice,
    queue_recommended_remediation,
    reconcile_completed_remediations,
    run_integrity_audit,
)
from app.services.ollama_service import OllamaService
from app.services.lmstudio_service import LMStudioService
from app.services.qdrant_service import QdrantService
from app.services.runtime_owner_guard import acquire_runtime_ownership

logger = logging.getLogger(__name__)


def _format_interval(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60.0
    if minutes < 60:
        if abs(minutes - round(minutes)) < 0.05:
            return f"{round(minutes):.0f}min"
        return f"{minutes:.1f}min"
    hours = minutes / 60.0
    if abs(hours - round(hours)) < 0.05:
        return f"{round(hours):.0f}h"
    return f"{hours:.1f}h"


def _env_interval_seconds(
    *,
    minutes_env: str | None = None,
    hours_env: str | None = None,
    default_minutes: float | None = None,
    default_hours: float | None = None,
) -> float:
    if minutes_env:
        raw_minutes = os.getenv(minutes_env)
        if raw_minutes not in {None, ""}:
            return max(1.0, float(raw_minutes) * 60.0)
    if hours_env:
        raw_hours = os.getenv(hours_env)
        if raw_hours not in {None, ""}:
            return max(1.0, float(raw_hours) * 3600.0)
    if default_minutes is not None:
        return max(1.0, default_minutes * 60.0)
    if default_hours is not None:
        return max(1.0, default_hours * 3600.0)
    raise ValueError("Either env vars or defaults must be provided")


async def _warmup_ollama_embeddings(ollama_svc: OllamaService) -> int:
    attempts = max(1, int(os.getenv("OLLAMA_WARMUP_ATTEMPTS", "3")))
    delay_seconds = max(0.1, float(os.getenv("OLLAMA_WARMUP_RETRY_SECONDS", "1.5")))
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            warmup_vector = await ollama_svc.embed("warmup")
            return len(warmup_vector)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            logger.warning(
                "Ollama warmup attempt %d/%d failed; retrying in %.1fs: %s",
                attempt,
                attempts,
                delay_seconds,
                exc,
            )
            await asyncio.sleep(delay_seconds)

    assert last_error is not None
    raise last_error


async def _warmup_lmstudio_embeddings() -> int:
    attempts = max(1, int(os.getenv("LMSTUDIO_WARMUP_ATTEMPTS", "2")))
    delay_seconds = max(0.1, float(os.getenv("LMSTUDIO_WARMUP_RETRY_SECONDS", "1.5")))
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        service = LMStudioService(timeout=30.0)
        try:
            warmup_vector = await service.embed("warmup", timeout=20.0)
            if not warmup_vector:
                raise RuntimeError("LM Studio embedding endpoint returned an empty vector")
            return len(warmup_vector)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            logger.warning(
                "LM Studio warmup attempt %d/%d failed; retrying in %.1fs: %s",
                attempt,
                attempts,
                delay_seconds,
                exc,
            )
            await asyncio.sleep(delay_seconds)
        finally:
            await service.close()

    assert last_error is not None
    raise last_error


def _env_enabled(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _local_llm_provider_order() -> tuple[str, list[str]]:
    local_provider = os.getenv("LOCAL_LLM_PROVIDER", settings.local_llm_provider).strip().lower() or "auto"
    fallback_order = [
        item.strip().lower()
        for item in os.getenv("LOCAL_LLM_FALLBACK_ORDER", settings.local_llm_fallback_order).split(",")
        if item.strip()
    ]
    return local_provider, fallback_order


def _ollama_warmup_enabled() -> bool:
    if not _env_enabled("OLLAMA_WARMUP_ENABLED", "1"):
        return False
    local_provider, fallback_order = _local_llm_provider_order()
    return local_provider in {"", "auto", "ollama"} or "ollama" in fallback_order


def _lmstudio_warmup_enabled() -> bool:
    if not _env_enabled("LMSTUDIO_WARMUP_ENABLED", "1"):
        return False
    local_provider, fallback_order = _local_llm_provider_order()
    return local_provider in {"auto", "lmstudio"} or "lmstudio" in fallback_order


async def _warmup_local_embedding_services(ollama_svc: OllamaService) -> None:
    if _ollama_warmup_enabled():
        try:
            actual_dim = await _warmup_ollama_embeddings(ollama_svc)
            if actual_dim != settings.embedding_dimensions:
                logger.warning(
                    "Ollama embedding dimension mismatch: model '%s' produces %d-dim vectors, "
                    "but EMBEDDING_DIMENSIONS=%d; embedding gateway will try the next provider.",
                    settings.ollama_embedding_model,
                    actual_dim,
                    settings.embedding_dimensions,
                )
            else:
                logger.info("Ollama embed model warmed up (dim=%d)", actual_dim)
        except Exception as e:
            logger.warning("Ollama warmup failed after retries (will retry via embedding gateway): %s", e)
    else:
        logger.info("Ollama warmup skipped: Ollama is not enabled in the local LLM provider order")

    if _lmstudio_warmup_enabled():
        try:
            actual_dim = await _warmup_lmstudio_embeddings()
            if actual_dim != settings.embedding_dimensions:
                logger.warning(
                    "LM Studio embedding dimension mismatch: selected embedding model produces %d-dim vectors, "
                    "but EMBEDDING_DIMENSIONS=%d; embedding gateway will try the next provider.",
                    actual_dim,
                    settings.embedding_dimensions,
                )
            else:
                logger.info("LM Studio embedding model warmed up (dim=%d)", actual_dim)
        except Exception as e:
            logger.warning("LM Studio warmup failed after retries (will retry via embedding gateway): %s", e)
    else:
        logger.info("LM Studio warmup skipped: LM Studio is not enabled in the local LLM provider order")


async def _ensure_qdrant_ready(qdrant_svc: QdrantService) -> None:
    """
    Wait for Qdrant to answer before finishing startup.

    Docker Compose only guarantees container start order, not readiness. A short
    retry window avoids failing the whole app when Qdrant is still initializing.
    """
    if settings.qdrant_in_memory:
        await qdrant_svc.ensure_collection()
        return

    attempts = max(1, int(os.getenv("QDRANT_STARTUP_RETRIES", "30")))
    delay_seconds = max(0.1, float(os.getenv("QDRANT_STARTUP_RETRY_SECONDS", "2.0")))
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            await qdrant_svc.ensure_collection()
            if attempt > 1:
                logger.info("Qdrant became ready after %d attempt(s)", attempt)
            return
        except Exception as exc:
            from app.core.exceptions import QdrantServiceError

            if isinstance(exc, QdrantServiceError):
                raise

            last_error = exc
            if attempt >= attempts:
                break
            logger.warning(
                "Qdrant startup attempt %d/%d failed; retrying in %.1fs: %s",
                attempt,
                attempts,
                delay_seconds,
                exc,
            )
            await asyncio.sleep(delay_seconds)

    assert last_error is not None
    raise RuntimeError(
        f"Qdrant did not become ready after {attempts} attempt(s); "
        f"check QDRANT_HOST={settings.qdrant_host!r} and QDRANT_PORT={settings.qdrant_port}"
    ) from last_error


async def _enqueue_startup_docs_refresh(
    queue,
    project_id: str,
    *,
    force: bool = False,
    min_age_seconds: float = 0.0,
) -> tuple[str | None, bool]:
    """Queue startup living-docs refresh without blocking readiness."""
    if not force and min_age_seconds > 0:
        import time as _time_docs_refresh
        from app.services.docs_cache_store import get_docs_cache_store
        from app.services.docs_service import load_docs_cache

        last_refresh_at = 0.0
        store_row = get_docs_cache_store().get(project_id)
        if store_row is not None:
            last_refresh_at = float(store_row.get("updated_at") or 0.0)
        elif (cached := load_docs_cache(project_id)) is not None:
            last_refresh_at = cached.generated_at.timestamp()
        if last_refresh_at > 0:
            age_seconds = max(0.0, _time_docs_refresh.time() - last_refresh_at)
            if age_seconds < min_age_seconds:
                return None, False

    recent_jobs = queue.list_jobs(job_type="docs_rebuild", limit=100)
    for job in recent_jobs:
        payload = job.get("payload") or {}
        if payload.get("project") != project_id:
            continue
        if job.get("status") in {"queued", "running"}:
            return str(job.get("id") or ""), False
    job_id = await queue.submit(
        "docs_rebuild",
        {
            "project": project_id,
            "force": force,
            "_queue_lane": "slow",
        },
    )
    return job_id, True


def _run_project_tree_exact_dedupe(*, limit_groups: int) -> dict:
    from app.services.improvements_store import get_improvements_store
    from app.services.project_tree_store import get_tree_store

    return get_tree_store().dedupe_exact_nodes(
        limit_groups=max(1, limit_groups),
        relink_node_reference=get_improvements_store().replace_node_id,
    )


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


async def repair_handoff_status_payload(payload: dict) -> dict:
    from qdrant_client.http import models as _qm
    from app.dependencies import get_qdrant
    from app.services.memory_store import get_memory_store

    qdrant = get_qdrant()
    memory_store = get_memory_store()
    allowed_statuses = {"pending", "picked_up", "active", "paused", "closed", "archived"}
    limit = max(1, int(payload.get("limit", 100)))
    record_ids = [str(item) for item in (payload.get("record_ids") or []) if item]
    qdrant_fixed_ids: list[str] = []
    sqlite_fixed_ids: list[str] = []
    skipped_record_ids: list[str] = []

    if record_ids:
        points = await qdrant._client.retrieve(
            collection_name=settings.qdrant_collection_name,
            ids=record_ids,
            with_payload=True,
            with_vectors=False,
        )
        store_rows = list((await memory_store.get_many(record_ids)).values())
    else:
        points, _ = await qdrant._client.scroll(
            collection_name=settings.qdrant_collection_name,
            scroll_filter=_qm.Filter(
                must=[
                    _qm.FieldCondition(key="category", match=_qm.MatchValue(value="handoff")),
                ]
            ),
            limit=max(limit, 100),
            with_payload=True,
            with_vectors=False,
        )
        store_rows = await memory_store.list_rows(category="memory", limit=max(limit, 100), offset=0)

    for point in points[:limit]:
        record_id = str(point.id)
        payload_obj = point.payload or {}
        if payload_obj.get("category") != "handoff":
            skipped_record_ids.append(record_id)
            continue
        current_status = str(payload_obj.get("status") or "").strip()
        if current_status in allowed_statuses:
            continue
        await qdrant._client.set_payload(
            collection_name=settings.qdrant_collection_name,
            payload={"status": "pending"},
            points=[record_id],
        )
        qdrant_fixed_ids.append(record_id)

    for row in store_rows[:limit]:
        record_id = str(row.get("memory_id") or "")
        metadata = dict(row.get("metadata") or {})
        if metadata.get("category") != "handoff":
            skipped_record_ids.append(record_id)
            continue
        current_status = str(metadata.get("status") or "").strip()
        if current_status in allowed_statuses:
            continue
        await memory_store.patch_metadata(record_id, {"status": "pending"})
        sqlite_fixed_ids.append(record_id)

    fixed_ids = sorted(set(qdrant_fixed_ids) | set(sqlite_fixed_ids))
    return {
        "requested_record_ids": record_ids,
        "fixed_ids": fixed_ids,
        "qdrant_fixed_ids": qdrant_fixed_ids,
        "sqlite_fixed_ids": sqlite_fixed_ids,
        "updated": len(fixed_ids),
        "skipped_record_ids": sorted(set(skipped_record_ids)),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    loop, previous_handler = _install_asyncio_exception_filter()
    logger.info("Starting memory server")
    runtime_owner = acquire_runtime_ownership(
        runtime_kind=settings.runtime_kind,
        enabled=settings.runtime_owner_guard and not settings.qdrant_in_memory,
        allow_takeover=settings.runtime_owner_allow_takeover,
        stale_seconds=settings.runtime_owner_stale_seconds,
    )
    from app.routers.admin import register_task, start_task as _admin_start_task

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
    await _ensure_qdrant_ready(qdrant_svc)

    # Init Ollama service object; warmup is local-only and optional.
    ollama_svc = OllamaService()
    set_ollama_service(ollama_svc)

    # Wire watcher service — without this watcher silently does nothing
    from app.services.watcher_service import set_services as watcher_set_services
    watcher_set_services(qdrant_svc, ollama_svc)
    await _warmup_local_embedding_services(ollama_svc)

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
    from app.services.knowledge_tree import verify_tree_classification_handler
    from app.services.qdrant_rebuild_service import register_qdrant_reindex_job_handler
    job_queue = get_job_queue()
    job_queue.register("project_ingest", _ingest_handler)
    job_queue.register("project_refresh", _refresh_handler)
    job_queue.register("skills_retag", _retag_handler)
    job_queue.register("evolve_skills", _evolve_handler)
    job_queue.register("regenerate_skill_content", _regenerate_content_handler)
    job_queue.register("verify_tree_classification", verify_tree_classification_handler)
    register_qdrant_reindex_job_handler(job_queue)

    async def _task_memoir_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant, get_ollama
        from app.services.memoir_service import generate_and_store_memoir
        from app.config import settings
        task_id = payload["task_id"]
        project = payload.get("project", "mnemoforge")
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
        from app.dependencies import get_qdrant, get_ollama
        from app.services.docs_service import invalidate_docs_cache, rebuild_docs, sync_docs_projection_memory
        from app.config import settings
        project_id = payload.get("project", "mnemoforge")
        force = payload.get("force", False)
        changed_component_ids = [str(item).strip() for item in (payload.get("changed_component_ids") or []) if str(item).strip()]
        changed_files = [str(item).strip() for item in (payload.get("changed_files") or []) if str(item).strip()]
        if force:
            invalidate_docs_cache(project_id)
        qdrant = get_qdrant()
        status = await rebuild_docs(
            project_id,
            qdrant._client,
            settings.qdrant_collection_name,
            force=force,
            changed_component_ids=changed_component_ids,
            changed_files=changed_files,
        )
        synced_doc_sections = await sync_docs_projection_memory(project_id, qdrant, get_ollama())
        return {
            "project": project_id,
            "generated_at": status.generated_at.isoformat(),
            "sections": list(status.sections.keys()),
            "last_rebuild_mode": status.last_rebuild_mode,
            "synced_doc_sections": synced_doc_sections,
        }

    job_queue.register("docs_rebuild", _docs_rebuild_handler)

    async def _docs_sync_memory_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant, get_ollama
        from app.services.docs_service import sync_docs_projection_memory

        project_id = payload.get("project", "mnemoforge")
        synced = await sync_docs_projection_memory(project_id, get_qdrant(), get_ollama())
        return {"project": project_id, "synced_doc_sections": synced}

    job_queue.register("docs_sync_memory", _docs_sync_memory_handler)

    async def _handoff_packet_llm_handler(payload: dict) -> dict:
        from app.dependencies import get_llm_gateway
        from app.services.handoff_packet_executor import execute_handoff_packet

        return await execute_handoff_packet(payload, get_llm_gateway())

    job_queue.register("handoff_packet_llm", _handoff_packet_llm_handler)

    async def _memory_scribe_compact_handler(payload: dict) -> dict:
        from app.dependencies import get_llm_gateway
        from app.services.memory_scribe_service import compact_memory_scribe

        return await compact_memory_scribe(payload, get_llm_gateway())

    job_queue.register("memory_scribe_compact", _memory_scribe_compact_handler)

    async def _draft_task_checkpoint_handler(payload: dict) -> dict:
        from app.dependencies import get_llm_gateway
        from app.services.memory_scribe_service import draft_task_checkpoint

        return await draft_task_checkpoint(payload, get_llm_gateway())

    job_queue.register("draft_task_checkpoint", _draft_task_checkpoint_handler)

    async def _integrity_audit_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant

        return await run_integrity_audit(get_qdrant())

    job_queue.register("data_integrity_audit", _integrity_audit_handler)

    async def _handoff_repair_status_handler(payload: dict) -> dict:
        return await repair_handoff_status_payload(payload)

    job_queue.register("handoff_repair_status", _handoff_repair_status_handler)

    async def _handoff_repair_target_handler(payload: dict) -> dict:
        from qdrant_client.http import models as _qm
        from app.dependencies import get_qdrant

        def _extract_target_agent(payload_obj: dict) -> str:
            tags = [str(tag).strip() for tag in (payload_obj.get("tags") or [])]
            for tag in tags:
                if tag.startswith("to:"):
                    value = tag[3:].strip()
                    if value:
                        return value
            meta = payload_obj.get("meta")
            if isinstance(meta, dict):
                value = str(meta.get("to_agent") or "").strip()
                if value:
                    return value
            content = str(payload_obj.get("content") or "")
            for line in content.splitlines():
                normalized = line.strip()
                lower = normalized.lower()
                if lower.startswith("to_agent:"):
                    value = normalized.split(":", 1)[1].strip()
                    if value:
                        return value
                if lower.startswith("to:"):
                    value = normalized.split(":", 1)[1].strip()
                    if value:
                        return value
            return ""

        qdrant = get_qdrant()
        limit = max(1, int(payload.get("limit", 100)))
        record_ids = [str(item) for item in (payload.get("record_ids") or []) if item]
        fixed_ids: list[str] = []
        unresolved_record_ids: list[str] = []
        skipped_record_ids: list[str] = []

        if record_ids:
            points = await qdrant._client.retrieve(
                collection_name=settings.qdrant_collection_name,
                ids=record_ids,
                with_payload=True,
                with_vectors=False,
            )
        else:
            points, _ = await qdrant._client.scroll(
                collection_name=settings.qdrant_collection_name,
                scroll_filter=_qm.Filter(
                    must=[
                        _qm.FieldCondition(key="category", match=_qm.MatchValue(value="handoff")),
                    ]
                ),
                limit=max(limit, 100),
                with_payload=True,
                with_vectors=False,
            )

        for point in points[:limit]:
            record_id = str(point.id)
            payload_obj = point.payload or {}
            if payload_obj.get("category") != "handoff":
                skipped_record_ids.append(record_id)
                continue
            tags = [str(tag).strip() for tag in (payload_obj.get("tags") or []) if str(tag).strip()]
            if any(tag.startswith("to:") and tag[3:].strip() for tag in tags):
                continue
            target_agent = _extract_target_agent(payload_obj)
            if not target_agent:
                unresolved_record_ids.append(record_id)
                continue
            tags.append(f"to:{target_agent}")
            deduped_tags = list(dict.fromkeys(tags))
            await qdrant._client.set_payload(
                collection_name=settings.qdrant_collection_name,
                payload={"tags": deduped_tags},
                points=[record_id],
            )
            fixed_ids.append(record_id)

        return {
            "requested_record_ids": record_ids,
            "fixed_ids": fixed_ids,
            "unresolved_record_ids": unresolved_record_ids,
            "updated": len(fixed_ids),
            "skipped_record_ids": skipped_record_ids,
        }

    job_queue.register("handoff_repair_target", _handoff_repair_target_handler)

    async def _data_hygiene_audit_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant

        return await run_data_hygiene_audit(
            get_qdrant(),
            memory_limit=int(payload.get("memory_limit", 1000)),
            event_limit=int(payload.get("event_limit", 1000)),
        )

    job_queue.register("data_hygiene_audit", _data_hygiene_audit_handler)

    # Seed MCP tool lifecycle records so new tools start in testing automatically.
    try:
        from app.services.mcp_tool_contracts import list_shared_tool_names
        from app.services.mcp_tool_registry import bootstrap_tool_lifecycle

        lifecycle_seed = bootstrap_tool_lifecycle(list_shared_tool_names())
        if lifecycle_seed.get("created") or lifecycle_seed.get("auto_testing"):
            logger.info(
                "MCP tool lifecycle bootstrap: created=%d auto_testing=%d total=%d",
                lifecycle_seed.get("created", 0),
                lifecycle_seed.get("auto_testing", 0),
                lifecycle_seed.get("total", 0),
            )
    except Exception as _mcp_lifecycle_seed_err:
        logger.warning("MCP tool lifecycle bootstrap failed (non-fatal): %s", _mcp_lifecycle_seed_err)

    import os as _os_mcp_lifecycle
    _MCP_TOOL_LIFECYCLE_REVIEW_INTERVAL_HOURS = float(
        _os_mcp_lifecycle.getenv("MCP_TOOL_LIFECYCLE_REVIEW_INTERVAL_HOURS", "12")
    )
    _MCP_TOOL_LIFECYCLE_MIN_AGE_DAYS = float(_os_mcp_lifecycle.getenv("MCP_TOOL_LIFECYCLE_MIN_AGE_DAYS", "7"))
    _MCP_TOOL_LIFECYCLE_MAX_AGE_DAYS = float(_os_mcp_lifecycle.getenv("MCP_TOOL_LIFECYCLE_MAX_AGE_DAYS", "21"))
    _MCP_TOOL_LIFECYCLE_MIN_FEEDBACK = int(_os_mcp_lifecycle.getenv("MCP_TOOL_LIFECYCLE_MIN_FEEDBACK", "3"))

    async def _mcp_tool_lifecycle_loop() -> None:
        from app.services.mcp_tool_contracts import list_shared_tool_definitions
        from app.services.mcp_tool_registry import bootstrap_tool_lifecycle, review_due_tool_lifecycles

        while True:
            try:
                tool_defs = list_shared_tool_definitions()
                bootstrap_tool_lifecycle([item.get("name") for item in tool_defs if item.get("name")])
                result = await review_due_tool_lifecycles(
                    tool_catalog=tool_defs,
                    ollama=ollama_svc,
                    min_age_days=_MCP_TOOL_LIFECYCLE_MIN_AGE_DAYS,
                    max_age_days=_MCP_TOOL_LIFECYCLE_MAX_AGE_DAYS,
                    min_feedback=_MCP_TOOL_LIFECYCLE_MIN_FEEDBACK,
                )
                if result.get("reviewed") or result.get("kept_testing") or result.get("llm_used"):
                    logger.info(
                        "MCP tool lifecycle review: reviewed=%d promoted=%d deprecated=%d kept=%d llm=%d",
                        result.get("reviewed", 0),
                        result.get("promoted", 0),
                        result.get("deprecated", 0),
                        result.get("kept_testing", 0),
                        result.get("llm_used", 0),
                    )
            except Exception as _mcp_lifecycle_err:
                logger.warning("MCP tool lifecycle review loop error (non-fatal): %s", _mcp_lifecycle_err)
            await asyncio.sleep(_MCP_TOOL_LIFECYCLE_REVIEW_INTERVAL_HOURS * 3600)

    _mcp_tool_lifecycle_entry = register_task("mcp_tool_lifecycle_review", _mcp_tool_lifecycle_loop)
    _mcp_tool_lifecycle_task = _admin_start_task(_mcp_tool_lifecycle_entry)
    logger.info(
        "MCP tool lifecycle review task started (interval=%s min_age=%sd max_age=%sd min_feedback=%d)",
        _format_interval(_MCP_TOOL_LIFECYCLE_REVIEW_INTERVAL_HOURS * 3600.0),
        int(_MCP_TOOL_LIFECYCLE_MIN_AGE_DAYS),
        int(_MCP_TOOL_LIFECYCLE_MAX_AGE_DAYS),
        _MCP_TOOL_LIFECYCLE_MIN_FEEDBACK,
    )

    async def _data_hygiene_apply_exclusion_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant

        qdrant = get_qdrant()
        finding_ids = list(payload.get("finding_ids") or [])
        records = list(payload.get("records") or [])
        updated = 0
        skipped = 0
        for record in records:
            if record.get("store_name") == "qdrant_memories":
                record_id = str(record.get("record_locator") or "")
                if not record_id:
                    skipped += 1
                    continue
                results = await qdrant._client.retrieve(
                    collection_name=settings.qdrant_collection_name,
                    ids=[record_id],
                    with_payload=True,
                    with_vectors=False,
                )
                if not results:
                    skipped += 1
                    continue
                current = results[0].payload or {}
                tags = list(current.get("tags") or [])
                if "hygiene:excluded-from-learning" not in tags:
                    tags.append("hygiene:excluded-from-learning")
                meta = dict(current.get("meta") or {})
                meta["dataset_boundary"] = {
                    "exclude_from_learning": True,
                    "action": "exclude-from-learning",
                    "applied_by": "data_hygiene",
                }
                await qdrant._client.set_payload(
                    collection_name=settings.qdrant_collection_name,
                    payload={"tags": tags, "meta": meta},
                    points=[record_id],
                )
                updated += 1
            else:
                # learning_events are excluded by classifier at read-time; no data mutation needed
                updated += 1
        return {"finding_ids": finding_ids, "updated": updated, "skipped": skipped}

    job_queue.register("data_hygiene_apply_exclusion", _data_hygiene_apply_exclusion_handler)

    async def _data_hygiene_mark_archive_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant

        qdrant = get_qdrant()
        finding_ids = list(payload.get("finding_ids") or [])
        records = list(payload.get("records") or [])
        updated = 0
        skipped = 0
        for record in records:
            if record.get("store_name") != "qdrant_memories":
                updated += 1
                continue
            record_id = str(record.get("record_locator") or "")
            if not record_id:
                skipped += 1
                continue
            results = await qdrant._client.retrieve(
                collection_name=settings.qdrant_collection_name,
                ids=[record_id],
                with_payload=True,
                with_vectors=False,
            )
            if not results:
                skipped += 1
                continue
            current = results[0].payload or {}
            tags = list(current.get("tags") or [])
            if "hygiene:archived" not in tags:
                tags.append("hygiene:archived")
            meta = dict(current.get("meta") or {})
            meta["dataset_boundary"] = {
                "archived": True,
                "action": "archive",
                "applied_by": "data_hygiene",
            }
            await qdrant._client.set_payload(
                collection_name=settings.qdrant_collection_name,
                payload={"tags": tags, "meta": meta},
                points=[record_id],
            )
            updated += 1
        return {"finding_ids": finding_ids, "updated": updated, "skipped": skipped}

    job_queue.register("data_hygiene_mark_archive", _data_hygiene_mark_archive_handler)
    async def _data_hygiene_reviewed_delete_handler(payload: dict) -> dict:
        return await apply_reviewed_delete(payload)

    job_queue.register("data_hygiene_reviewed_delete", _data_hygiene_reviewed_delete_handler)
    async def _data_hygiene_approved_delete_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant

        return await apply_approved_delete(payload, get_qdrant())

    job_queue.register("data_hygiene_approved_delete", _data_hygiene_approved_delete_handler)

    async def _rebuild_project_tasks_handler(payload: dict) -> dict:
        from app.dependencies import get_qdrant, get_ollama
        from app.services.project_tasks_rebuilder import rebuild_project_tasks

        project_id = payload.get("project")
        limit = int(payload.get("limit", 0) or 0)
        changes_limit = int(payload.get("changes_limit", 0) or 0)
        return await rebuild_project_tasks(
            qdrant_client=get_qdrant()._client,
            ollama=get_ollama(),
            project=project_id,
            limit=limit,
            changes_limit=changes_limit,
        )

    job_queue.register("rebuild_project_tasks", _rebuild_project_tasks_handler)

    async def _task_capture_refresh_handler(payload: dict) -> dict:
        from app.dependencies import get_ollama, get_qdrant
        from app.services.task_capture_service import build_task_capture_completion

        project = str(payload.get("project") or "").strip()
        task_id = str(payload.get("task_id") or "").strip()
        trigger = str(payload.get("trigger") or "").strip() or "unknown"
        if not project or not task_id:
            raise ValueError("task_capture_refresh requires project and task_id")
        result = await build_task_capture_completion(
            get_qdrant(),
            get_ollama(),
            project=project,
            task_id=task_id,
            persist=True,
            use_local_generation=bool(payload.get("use_local_generation", True)),
        )
        return {
            "project": project,
            "task_id": task_id,
            "trigger": trigger,
            "persisted_count": result.persisted_count,
            "reused_count": result.reused_count,
            "missing_before": result.missing_before,
            "missing_after": result.missing_after,
            "local_generation_used": result.local_generation_used,
        }

    job_queue.register("task_capture_refresh", _task_capture_refresh_handler)
    await job_queue.start()
    logger.info("Job queue started")

    _INTEGRITY_AUDIT_INTERVAL_SECONDS = _env_interval_seconds(
        minutes_env="INTEGRITY_AUDIT_INTERVAL_MINUTES",
        hours_env="INTEGRITY_AUDIT_INTERVAL_HOURS",
        default_minutes=15,
    )
    _DATA_HYGIENE_AUDIT_SECONDS = _env_interval_seconds(
        minutes_env="DATA_HYGIENE_AUDIT_MINUTES",
        default_minutes=2,
    )
    _DATA_HYGIENE_MEMORY_LIMIT = max(25, int(os.getenv("DATA_HYGIENE_MEMORY_LIMIT", "100")))
    _DATA_HYGIENE_EVENT_LIMIT = max(25, int(os.getenv("DATA_HYGIENE_EVENT_LIMIT", "100")))
    _INTEGRITY_REMEDIATION_SYNC_SECONDS = _env_interval_seconds(
        minutes_env="INTEGRITY_REMEDIATION_SYNC_MINUTES",
        default_minutes=1,
    )
    _PACKET_BACKGROUND_SYNC_SECONDS = _env_interval_seconds(
        minutes_env="PACKET_BACKGROUND_SYNC_MINUTES",
        default_minutes=1,
    )

    try:
        audit_result = await run_integrity_audit(qdrant_svc)
        logger.info(
            "Integrity audit startup probe: status=%s degraded_count=%d",
            audit_result.get("status", "unknown"),
            audit_result.get("degraded_count", 0),
        )
    except Exception as _integrity_startup_err:
        logger.warning("Integrity audit startup probe failed (non-fatal): %s", _integrity_startup_err)

    try:
        _hygiene_store = get_data_hygiene_store()
        _hygiene_scan_state = _hygiene_store.get_scan_state()
        hygiene_result = await run_data_hygiene_audit(
            qdrant_svc,
            memory_limit=_DATA_HYGIENE_MEMORY_LIMIT,
            event_limit=_DATA_HYGIENE_EVENT_LIMIT,
            qdrant_offset=_hygiene_scan_state.get("qdrant_offset"),
            event_before_ts=_hygiene_scan_state.get("event_before_ts"),
            event_before_id=_hygiene_scan_state.get("event_before_id"),
        )
        _hygiene_store.set_scan_state(
            {
                "qdrant_offset": hygiene_result.get("next_qdrant_offset"),
                "event_before_ts": hygiene_result.get("next_event_before_ts"),
                "event_before_id": hygiene_result.get("next_event_before_id"),
                "updated_by": "startup_probe",
                "updated_at": hygiene_result.get("latest_audit", {}).get("finished_at"),
            }
        )
        logger.info(
            "Data hygiene startup probe: status=%s findings=%d memory_limit=%d event_limit=%d",
            hygiene_result.get("status", "unknown"),
            hygiene_result.get("findings_count", 0),
            _DATA_HYGIENE_MEMORY_LIMIT,
            _DATA_HYGIENE_EVENT_LIMIT,
        )
    except Exception as _hygiene_startup_err:
        logger.warning("Data hygiene startup probe failed (non-fatal): %s", _hygiene_startup_err)

    # Migrate skill counters from Qdrant payload → SQLite (idempotent, skips already-migrated)
    from app.services.skill_counters import get_skill_counters
    try:
        _sc = get_skill_counters()
        _existing_skill_meta = await _sc.count()
        if _existing_skill_meta == 0:
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
        else:
            logger.info("Skill counters migration skipped: SQLite already has %d rows", _existing_skill_meta)
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
                project=_p.get("project", "mnemoforge"),
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
        for _cat in ("skill", "code_component"):
            _existing_cat_count = await _ms.count(_cat)
            if _existing_cat_count > 0:
                logger.info("memory_store migration skipped for %s: SQLite already has %d rows", _cat, _existing_cat_count)
                continue

            from qdrant_client.http import models as _qm4
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

    # Seed docs_cache SQLite store from legacy file cache (idempotent)
    try:
        from app.models.docs import DocsStatus as _DocsStatus
        from app.services.docs_cache_store import get_docs_cache_store as _get_docs_cache_store
        from app.services.docs_service import _CACHE_DIR as _DOCS_CACHE_DIR

        _docs_cache_store = _get_docs_cache_store()
        _docs_cache_seeded = 0
        if _DOCS_CACHE_DIR.exists():
            for _cache_file in _DOCS_CACHE_DIR.glob("*.json"):
                try:
                    _status = _DocsStatus.model_validate_json(_cache_file.read_text(encoding="utf-8"))
                except Exception as _docs_cache_parse_err:
                    logger.warning("Docs cache seed skipped %s: %s", _cache_file.name, _docs_cache_parse_err)
                    continue
                if _docs_cache_store.get(_status.project):
                    continue
                _docs_cache_store.upsert(_status.project, _status.model_dump_json())
                _docs_cache_seeded += 1
        if _docs_cache_seeded:
            logger.info("docs_cache migration: seeded %d project caches from legacy files", _docs_cache_seeded)
    except Exception as _e:
        logger.warning("docs_cache migration failed (non-fatal): %s", _e)

    # Backfill legacy doc_section payloads into SQLite-backed refs (idempotent)
    try:
        from app.services.doc_section_service import backfill_legacy_doc_sections_to_store

        _doc_section_backfill = await backfill_legacy_doc_sections_to_store(
            client,
            settings.qdrant_collection_name,
            limit=5000,
            rewrite_qdrant_refs=True,
            dry_run=False,
        )
        if _doc_section_backfill.get("copied_to_sqlite") or _doc_section_backfill.get("rewritten_qdrant_refs"):
            logger.info(
                "doc_section migration: copied=%d rewritten=%d already_ref=%d failed=%d",
                int(_doc_section_backfill.get("copied_to_sqlite") or 0),
                int(_doc_section_backfill.get("rewritten_qdrant_refs") or 0),
                int(_doc_section_backfill.get("already_ref_payload") or 0),
                int(_doc_section_backfill.get("failed") or 0),
            )
    except Exception as _e:
        logger.warning("doc_section migration failed (non-fatal): %s", _e)

    # Start model mirror background loop
    from app.services.model_mirror import get_model_mirror, MODEL_MIRROR_INTERVAL_HOURS

    async def _model_mirror_loop() -> None:
        mirror = get_model_mirror()
        from app.services.learning_store import get_learning_store as _get_ls
        while True:
            import time as _time_model_mirror
            mirror.next_run_at = _time_model_mirror.time() + MODEL_MIRROR_INTERVAL_HOURS * 3600
            await asyncio.sleep(MODEL_MIRROR_INTERVAL_HOURS * 3600)
            mirror.next_run_at = None
            try:
                result = await mirror.run(ollama_svc, _get_ls())
                logger.info(
                    "Model mirror: created=%d updated=%d events=%d errors=%d",
                    result.candidates_created,
                    result.candidates_updated,
                    result.events_analyzed,
                    len(result.errors),
                )
            except Exception as _model_mirror_err:
                logger.warning("Model mirror loop error (non-fatal): %s", _model_mirror_err)

    _model_mirror_entry = register_task("model_mirror", _model_mirror_loop)
    _model_mirror_task = _admin_start_task(_model_mirror_entry)
    logger.info(
        "Model mirror background task started (interval=%s)",
        _format_interval(MODEL_MIRROR_INTERVAL_HOURS * 3600.0),
    )

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
    logger.info(
        "Importance decay background task started (interval=%s)",
        _format_interval(_DECAY_INTERVAL_HOURS * 3600.0),
    )

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
    logger.info(
        "Crystallization background task started (interval=%s)",
        _format_interval(_CRYSTAL_INTERVAL_HOURS * 3600.0),
    )

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
                    project=settings.self_project_id,
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
                            project=settings.self_project_id,
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
    logger.info(
        "Scout background task started (interval=%s)",
        _format_interval(_SCOUT_INTERVAL_HOURS * 3600.0),
    )

    # Start knowledge tree pruning background loop
    import os as _os_tree
    _TREE_PRUNING_INTERVAL_HOURS = int(_os_tree.getenv("TREE_PRUNING_INTERVAL_HOURS", "24"))

    async def _tree_pruning_loop():
        from app.services.knowledge_tree import tree_pruning_task
        await tree_pruning_task(interval_hours=_TREE_PRUNING_INTERVAL_HOURS)

    _tree_pruning_entry = register_task("tree_pruning", _tree_pruning_loop)
    _tree_pruning_task = _admin_start_task(_tree_pruning_entry)
    logger.info("Knowledge tree pruning background task started (interval=%dh)", _TREE_PRUNING_INTERVAL_HOURS)

    import os as _os_integrity

    async def _integrity_audit_loop() -> None:
        from app.dependencies import get_qdrant as _get_qdrant_integrity

        while True:
            try:
                result = await run_integrity_audit(_get_qdrant_integrity())
                logger.info(
                    "Integrity audit loop: status=%s degraded_count=%d",
                    result.get("status", "unknown"),
                    result.get("degraded_count", 0),
                )
            except Exception as _integrity_err:
                logger.warning("Integrity audit loop error (non-fatal): %s", _integrity_err)
            await asyncio.sleep(_INTEGRITY_AUDIT_INTERVAL_SECONDS)

    _integrity_entry = register_task("data_integrity_audit", _integrity_audit_loop)
    _integrity_task = _admin_start_task(_integrity_entry)
    logger.info(
        "Data integrity audit background task started (interval=%s)",
        _format_interval(_INTEGRITY_AUDIT_INTERVAL_SECONDS),
    )

    _DATA_HYGIENE_AUTO_TEST_CLEANUP = _os_integrity.getenv("DATA_HYGIENE_AUTO_TEST_CLEANUP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    _DATA_HYGIENE_AUTO_TEST_CLEANUP_LIMIT = int(_os_integrity.getenv("DATA_HYGIENE_AUTO_TEST_CLEANUP_LIMIT", "200"))

    async def _data_hygiene_loop() -> None:
        store = get_data_hygiene_store()
        from app.services.job_queue import get_job_queue as _get_job_queue_hygiene

        while True:
            try:
                scan_state = store.get_scan_state()
                result = await run_data_hygiene_audit(
                    qdrant_svc,
                    memory_limit=_DATA_HYGIENE_MEMORY_LIMIT,
                    event_limit=_DATA_HYGIENE_EVENT_LIMIT,
                    qdrant_offset=scan_state.get("qdrant_offset"),
                    event_before_ts=scan_state.get("event_before_ts"),
                    event_before_id=scan_state.get("event_before_id"),
                )
                store.set_scan_state(
                    {
                        "qdrant_offset": result.get("next_qdrant_offset"),
                        "event_before_ts": result.get("next_event_before_ts"),
                        "event_before_id": result.get("next_event_before_id"),
                        "updated_by": "background_loop",
                        "updated_at": result.get("latest_audit", {}).get("finished_at"),
                    }
                )
                logger.info(
                    "Data hygiene audit loop: status=%s findings=%d memory_limit=%d event_limit=%d",
                    result.get("status", "unknown"),
                    result.get("findings_count", 0),
                    _DATA_HYGIENE_MEMORY_LIMIT,
                    _DATA_HYGIENE_EVENT_LIMIT,
                )
                changed = store.sync_remediations_from_jobs(_get_job_queue_hygiene().list_jobs(limit=500))
                if changed:
                    logger.info("Data hygiene remediation sync updated %d remediation record(s)", changed)
                reconciled = await reconcile_hygiene_completed_remediations(queue=_get_job_queue_hygiene())
                if reconciled.get("reconciled"):
                    logger.info(
                        "Data hygiene closure: remediations=%d resolved=%d archived=%d",
                        reconciled.get("reconciled", 0),
                        reconciled.get("resolved_findings", 0),
                        reconciled.get("archived_findings", 0),
                    )
                if _DATA_HYGIENE_AUTO_TEST_CLEANUP:
                    latest_audit = result.get("latest_audit") or {}
                    latest_details = latest_audit.get("details") or {}
                    qdrant_scan_error = str(latest_details.get("qdrant_scan_error") or "").strip()
                    promoted = promote_auto_test_cleanup_candidates(
                        limit=max(1, _DATA_HYGIENE_AUTO_TEST_CLEANUP_LIMIT),
                        include_qdrant=not bool(qdrant_scan_error),
                        include_learning_events=True,
                    )
                    if promoted.get("updated"):
                        logger.info(
                            "Data hygiene auto test cleanup promoted: updated=%d reviewed_ready=%d approved_ready=%d skipped=%d",
                            promoted.get("updated", 0),
                            promoted.get("ready_for_reviewed_delete", 0),
                            promoted.get("ready_for_approved_delete", 0),
                            promoted.get("skipped", 0),
                        )
                    if int(promoted.get("ready_for_reviewed_delete") or 0) > 0:
                        try:
                            item = await queue_reviewed_delete_remediation(
                                requested_by="auto_test_cleanup",
                                queue=_get_job_queue_hygiene(),
                                limit=min(
                                    max(1, _DATA_HYGIENE_AUTO_TEST_CLEANUP_LIMIT),
                                    int(promoted.get("ready_for_reviewed_delete") or 0),
                                ),
                            )
                            logger.info(
                                "Data hygiene auto reviewed-delete queued: remediation=%s job=%s",
                                item.get("remediation_id"),
                                item.get("job_id"),
                            )
                        except ValueError:
                            pass
                        except Exception as _hygiene_cleanup_reviewed_err:
                            logger.warning("Data hygiene auto reviewed-delete queue failed: %s", _hygiene_cleanup_reviewed_err)
                    if qdrant_scan_error:
                        logger.warning(
                            "Data hygiene auto approved-delete skipped for qdrant_memories due to qdrant_scan_error: %s",
                            qdrant_scan_error,
                        )
                    elif int(promoted.get("ready_for_approved_delete") or 0) > 0:
                        try:
                            item = await queue_approved_delete_remediation(
                                requested_by="auto_test_cleanup",
                                queue=_get_job_queue_hygiene(),
                                store_name="qdrant_memories",
                                limit=min(
                                    max(1, _DATA_HYGIENE_AUTO_TEST_CLEANUP_LIMIT),
                                    int(promoted.get("ready_for_approved_delete") or 0),
                                ),
                            )
                            logger.info(
                                "Data hygiene auto approved-delete queued: remediation=%s job=%s",
                                item.get("remediation_id"),
                                item.get("job_id"),
                            )
                        except ValueError:
                            pass
                        except Exception as _hygiene_cleanup_approved_err:
                            logger.warning("Data hygiene auto approved-delete queue failed: %s", _hygiene_cleanup_approved_err)
            except Exception as _hygiene_err:
                logger.warning("Data hygiene loop error (non-fatal): %s", _hygiene_err)
            await asyncio.sleep(_DATA_HYGIENE_AUDIT_SECONDS)

    _data_hygiene_entry = register_task("data_hygiene_audit", _data_hygiene_loop)
    _data_hygiene_task = _admin_start_task(_data_hygiene_entry)
    logger.info(
        "Data hygiene audit background task started (interval=%s auto_test_cleanup=%s cleanup_limit=%d memory_limit=%d event_limit=%d)",
        _format_interval(_DATA_HYGIENE_AUDIT_SECONDS),
        _DATA_HYGIENE_AUTO_TEST_CLEANUP,
        _DATA_HYGIENE_AUTO_TEST_CLEANUP_LIMIT,
        _DATA_HYGIENE_MEMORY_LIMIT,
        _DATA_HYGIENE_EVENT_LIMIT,
    )

    _INTEGRITY_AUTO_REMEDIATE = _os_integrity.getenv("INTEGRITY_AUTO_REMEDIATE", "").lower() in {"1", "true", "yes", "on"}
    _INTEGRITY_AUTO_DISCOVERY_LIMIT = int(_os_integrity.getenv("INTEGRITY_AUTO_DISCOVERY_LIMIT", "50"))
    _INTEGRITY_AUTO_DISCOVERY_COOLDOWN_MINUTES = float(
        _os_integrity.getenv("INTEGRITY_AUTO_DISCOVERY_COOLDOWN_MINUTES", "60")
    )
    _INTEGRITY_AUTO_REMEDIATE_COOLDOWN_MINUTES = float(
        _os_integrity.getenv("INTEGRITY_AUTO_REMEDIATE_COOLDOWN_MINUTES", "60")
    )

    async def _integrity_remediation_loop() -> None:
        from app.services.job_queue import get_job_queue as _get_job_queue_integrity

        store = get_data_integrity_store()
        while True:
            try:
                jobs = _get_job_queue_integrity().list_jobs(limit=500)
                changed = store.sync_remediations_from_jobs(jobs)
                if changed:
                    logger.info("Integrity remediation sync updated %d remediation record(s)", changed)
                reconciled = await reconcile_completed_remediations(queue=_get_job_queue_integrity())
                if reconciled.get("reconciled"):
                    logger.info(
                        "Integrity remediation closure: remediations=%d repaired_findings=%d",
                        reconciled.get("reconciled", 0),
                        reconciled.get("repaired_findings", 0),
                    )
                if _INTEGRITY_AUTO_REMEDIATE:
                    overview = store.overview()
                    for slice_id in overview.get("actionable_slices", []):
                        discovery_guard = build_auto_discovery_guard(
                            slice_id,
                            cooldown_seconds=max(0.0, _INTEGRITY_AUTO_DISCOVERY_COOLDOWN_MINUTES) * 60.0,
                        )
                        if discovery_guard.get("allowed"):
                            try:
                                discovery = await maybe_auto_discover_slice(
                                    slice_id,
                                    limit=max(1, _INTEGRITY_AUTO_DISCOVERY_LIMIT),
                                    cooldown_seconds=max(0.0, _INTEGRITY_AUTO_DISCOVERY_COOLDOWN_MINUTES) * 60.0,
                                )
                                if discovery.get("performed"):
                                    logger.info(
                                        "Integrity auto-discovery completed: slice=%s discovered=%d",
                                        slice_id,
                                        discovery.get("discovered", 0),
                                    )
                            except Exception as _integrity_discovery_err:
                                logger.warning(
                                    "Integrity auto-discovery failed for %s: %s",
                                    slice_id,
                                    _integrity_discovery_err,
                                )
                        guard = build_auto_remediation_guard(
                            slice_id,
                            cooldown_seconds=max(0.0, _INTEGRITY_AUTO_REMEDIATE_COOLDOWN_MINUTES) * 60.0,
                        )
                        if not guard.get("allowed"):
                            logger.debug(
                                "Integrity auto-remediation skipped: slice=%s reason=%s",
                                slice_id,
                                guard.get("reason", "unknown"),
                            )
                            continue
                        try:
                            queued = await queue_recommended_remediation(
                                slice_id=slice_id,
                                requested_by="auto_integrity",
                                queue=_get_job_queue_integrity(),
                            )
                            logger.info(
                                "Integrity auto-remediation queued: slice=%s remediation=%s job=%s",
                                slice_id,
                                queued.get("remediation_id"),
                                queued.get("job_id"),
                            )
                        except ValueError:
                            continue
                        except Exception as _integrity_queue_err:
                            logger.warning("Integrity auto-remediation queue failed for %s: %s", slice_id, _integrity_queue_err)
            except Exception as _integrity_sync_err:
                logger.warning("Integrity remediation sync loop error (non-fatal): %s", _integrity_sync_err)
            await asyncio.sleep(_INTEGRITY_REMEDIATION_SYNC_SECONDS)

    _integrity_remediation_entry = register_task("data_integrity_remediation", _integrity_remediation_loop)
    _integrity_remediation_task = _admin_start_task(_integrity_remediation_entry)
    logger.info(
        "Data integrity remediation sync task started (interval=%s auto=%s discovery_limit=%d discovery_cooldown=%.0fmin remediation_cooldown=%.0fmin)",
        _format_interval(_INTEGRITY_REMEDIATION_SYNC_SECONDS),
        _INTEGRITY_AUTO_REMEDIATE,
        _INTEGRITY_AUTO_DISCOVERY_LIMIT,
        _INTEGRITY_AUTO_DISCOVERY_COOLDOWN_MINUTES,
        _INTEGRITY_AUTO_REMEDIATE_COOLDOWN_MINUTES,
    )

    async def _packet_background_sync_loop() -> None:
        while True:
            try:
                reconciled = await models.reconcile_background_task_packets(
                    qdrant=qdrant_svc,
                    limit=200,
                    acted_by="background_sync",
                    reason="background_sync",
                )
                if reconciled.get("updated"):
                    logger.info(
                        "Packet background sync: updated=%d closed=%d paused=%d scanned=%d statuses=%s",
                        reconciled.get("updated", 0),
                        reconciled.get("closed", 0),
                        reconciled.get("paused", 0),
                        reconciled.get("scanned", 0),
                        reconciled.get("by_background_job_status", {}),
                    )
            except Exception as _packet_sync_err:
                logger.warning("Packet background sync loop error (non-fatal): %s", _packet_sync_err)
            await asyncio.sleep(_PACKET_BACKGROUND_SYNC_SECONDS)

    _packet_background_entry = register_task("packet_background_sync", _packet_background_sync_loop)
    _packet_background_task = _admin_start_task(_packet_background_entry)
    logger.info(
        "Packet background sync task started (interval=%s)",
        _format_interval(_PACKET_BACKGROUND_SYNC_SECONDS),
    )

    import os as _os_tree_dedupe
    _PROJECT_TREE_DEDUPE_SECONDS = _env_interval_seconds(
        minutes_env="PROJECT_TREE_DEDUPE_MINUTES",
        hours_env="PROJECT_TREE_DEDUPE_HOURS",
        default_minutes=10,
    )
    _PROJECT_TREE_DEDUPE_GROUP_LIMIT = max(
        1,
        int(_os_tree_dedupe.getenv("PROJECT_TREE_DEDUPE_GROUP_LIMIT", "25")),
    )

    try:
        _tree_dedupe_startup = _run_project_tree_exact_dedupe(
            limit_groups=_PROJECT_TREE_DEDUPE_GROUP_LIMIT,
        )
        if _tree_dedupe_startup.get("deleted_nodes"):
            logger.info(
                "Project tree startup dedupe: groups=%d deleted=%d relinked_children=%d relinked_journals=%d",
                _tree_dedupe_startup.get("merged_groups", 0),
                _tree_dedupe_startup.get("deleted_nodes", 0),
                _tree_dedupe_startup.get("relinked_children", 0),
                _tree_dedupe_startup.get("relinked_journals", 0),
            )
    except Exception as _tree_dedupe_startup_err:
        logger.warning("Project tree startup dedupe failed (non-fatal): %s", _tree_dedupe_startup_err)

    async def _project_tree_dedupe_loop() -> None:
        while True:
            try:
                result = _run_project_tree_exact_dedupe(
                    limit_groups=_PROJECT_TREE_DEDUPE_GROUP_LIMIT,
                )
                if result.get("deleted_nodes"):
                    logger.info(
                        "Project tree dedupe: groups=%d deleted=%d relinked_children=%d relinked_journals=%d",
                        result.get("merged_groups", 0),
                        result.get("deleted_nodes", 0),
                        result.get("relinked_children", 0),
                        result.get("relinked_journals", 0),
                    )
            except Exception as _tree_dedupe_err:
                logger.warning("Project tree dedupe loop error (non-fatal): %s", _tree_dedupe_err)
            await asyncio.sleep(_PROJECT_TREE_DEDUPE_SECONDS)

    _project_tree_dedupe_entry = register_task("project_tree_dedupe", _project_tree_dedupe_loop)
    _project_tree_dedupe_task = _admin_start_task(_project_tree_dedupe_entry)
    logger.info(
        "Project tree dedupe task started (interval=%s group_limit=%d)",
        _format_interval(_PROJECT_TREE_DEDUPE_SECONDS),
        _PROJECT_TREE_DEDUPE_GROUP_LIMIT,
    )

    # Cleanup orphaned docs cache files (projects with no data in Qdrant)
    from app.services.docs_service import cleanup_orphaned_caches
    await cleanup_orphaned_caches(client, settings.qdrant_collection_name)

    # Bootstrap self-project laws from docs/PROJECT_LAW.md when no active laws exist yet.
    try:
        import os as _os_laws
        _auto_bootstrap_laws = _os_laws.getenv("AUTO_BOOTSTRAP_SELF_PROJECT_LAWS", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        if _auto_bootstrap_laws:
            from app.services.law_import_service import ensure_project_laws_from_markdown_if_missing

            bootstrap = await ensure_project_laws_from_markdown_if_missing(
                qdrant=qdrant_svc,
                ollama=ollama_svc,
                project=settings.self_project_id,
                path="docs/PROJECT_LAW.md",
                agent_id="system",
                confirmed_by="system",
                confirmation_source="startup_bootstrap",
                reason="Bootstrap self-project laws from repository markdown.",
                extra_tags=["startup_bootstrap"],
            )
            if bootstrap is not None:
                logger.info(
                    "Self-project law bootstrap applied: project=%s parsed=%d created=%d staged=%d",
                    settings.self_project_id,
                    bootstrap.parsed,
                    bootstrap.created,
                    bootstrap.staged_candidate_revision,
                )
    except Exception as _law_bootstrap_err:
        logger.warning("Self-project law bootstrap failed (non-fatal): %s", _law_bootstrap_err)

    # Keep living docs from drifting indefinitely when no explicit rebuild trigger fired.
    try:
        import os as _os_docs
        _auto_rebuild_docs = _os_docs.getenv("AUTO_REBUILD_SELF_PROJECT_DOCS", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        if _auto_rebuild_docs:
            _startup_docs_refresh_min_age_seconds = _env_interval_seconds(
                minutes_env="AUTO_REBUILD_SELF_PROJECT_DOCS_MIN_AGE_MINUTES",
                hours_env="AUTO_REBUILD_SELF_PROJECT_DOCS_MIN_AGE_HOURS",
                default_minutes=60,
            )
            docs_job_id, queued = await _enqueue_startup_docs_refresh(
                job_queue,
                settings.self_project_id,
                force=False,
                min_age_seconds=_startup_docs_refresh_min_age_seconds,
            )
            if queued:
                logger.info(
                    "Startup living docs refresh queued: project=%s job_id=%s lane=slow",
                    settings.self_project_id,
                    docs_job_id,
                )
            elif docs_job_id is None:
                logger.info(
                    "Startup living docs refresh skipped: project=%s min_age=%s",
                    settings.self_project_id,
                    _format_interval(_startup_docs_refresh_min_age_seconds),
                )
            else:
                logger.info(
                    "Startup living docs refresh already pending: project=%s job_id=%s",
                    settings.self_project_id,
                    docs_job_id,
                )
    except Exception as _docs_startup_err:
        logger.warning("Startup living docs refresh failed (non-fatal): %s", _docs_startup_err)

    logger.info(
        "Memory server ready — Qdrant=%s:%s, Ollama=%s, model=%s",
        settings.qdrant_host,
        settings.qdrant_port,
        settings.ollama_base_url,
        settings.ollama_embedding_model,
    )

    yield

    try:
        _model_mirror_task.cancel()
        _decay_task.cancel()
        _crystal_task.cancel()
        _scout_task.cancel()
        _tree_pruning_task.cancel()
        _integrity_task.cancel()
        _data_hygiene_task.cancel()
        _integrity_remediation_task.cancel()
        _packet_background_task.cancel()
        _project_tree_dedupe_task.cancel()
        try:
            await _model_mirror_task
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
        try:
            await _tree_pruning_task
        except asyncio.CancelledError:
            pass
        try:
            await _integrity_task
        except asyncio.CancelledError:
            pass
        try:
            await _data_hygiene_task
        except asyncio.CancelledError:
            pass
        try:
            await _integrity_remediation_task
        except asyncio.CancelledError:
            pass
        try:
            await _packet_background_task
        except asyncio.CancelledError:
            pass
        try:
            await _project_tree_dedupe_task
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
        from app.services.project_tasks_store import close_project_tasks_store
        close_project_tasks_store()
        from app.services.learning_store import close_learning_store
        await close_learning_store()
        from app.services.memory_store import close_memory_store
        close_memory_store()
        from app.services.context_page_store import close_context_page_store
        close_context_page_store()
        close_data_integrity_store()
        close_data_hygiene_store()
        logger.info("Memory server stopped")
    finally:
        if runtime_owner is not None:
            runtime_owner.close()
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
                if not provided:
                    provided = request.query_params.get("api_key", "")
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
        title="MnemoForge Server",
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
    app.include_router(knowledge_tree_api.router, prefix=prefix)

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
    _try_include(app, project_tasks.router, "project_tasks", prefix, disabled)
    _try_include(app, unified_artifacts.router, "unified_artifacts", prefix, disabled)
    _try_include(app, context_pages.router, "context_pages", prefix, disabled)
    _try_include(app, laws.router, "laws", prefix, disabled)
    _try_include(app, task_execution_context.router, "task_execution_context", prefix, disabled)
    _try_include(app, tasks.router, "tasks", prefix, disabled)
    _try_include(app, layout_fixer.router, "layout_fixer", prefix, disabled)
    _try_include(app, log_filter.router, "log_filter", prefix, disabled)
    _try_include(app, watcher.router, "watcher", prefix, disabled)
    _try_include(app, docs.router, "docs", prefix, disabled)
    _try_include(app, tree.router, "tree", prefix, disabled)

    # ── Infrastructure modules ───────────────────────────────────────────────────
    _try_include(app, models.router, "models", prefix, disabled)
    _try_include(app, models.coordination_router, "models", prefix, disabled)
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
