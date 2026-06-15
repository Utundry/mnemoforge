from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from qdrant_client import AsyncQdrantClient

# Force in-memory Qdrant and test collection name BEFORE any app import
from app.config import settings
# Отключить проверку API key для тестов - тесты не передают x-api-key заголовок
settings.api_key = ""
settings.qdrant_in_memory = True
settings.qdrant_collection_name = "test_memories"
settings.qdrant_learning_collection_name = "test_learning_artifacts"
# Tests use 1024-dim mock vectors (independent of production model dimension).
# MOCK_VECTOR below must match this value.
settings.embedding_dimensions = 1024
settings.glm_skip_evidence_threshold = False


@pytest.fixture(autouse=True)
def _reset_adaptive_state():
    """Use fresh in-memory adaptive store for each test (no cross-test state leak)."""
    import app.services.adaptive_state as _as
    from pathlib import Path
    # Close any existing store
    if _as._store is not None:
        try:
            _as._store.close()
        except Exception:
            pass
    # Inject fresh in-memory store
    _as._store = _as.AdaptiveStateStore(Path(":memory:"))
    yield
    if _as._store is not None:
        try:
            _as._store.close()
        except Exception:
            pass
        _as._store = None


@pytest.fixture(autouse=True)
def _reset_improvements_store():
    """Use fresh in-memory improvements store for each test — prevents test writes polluting production SQLite."""
    import app.services.improvements_store as _is
    from pathlib import Path
    if _is._store is not None:
        try:
            _is._store.close()
        except Exception:
            pass
    _is._store = _is.ImprovementsStore(Path(":memory:"))
    yield
    if _is._store is not None:
        try:
            _is._store.close()
        except Exception:
            pass
        _is._store = None


@pytest.fixture(autouse=True)
def _reset_project_tasks_store():
    """Use fresh in-memory project task store for each test to avoid stale SQLite state."""
    import app.services.project_tasks_store as _pts
    from pathlib import Path

    if _pts._STORE is not None:
        try:
            _pts._STORE.close()
        except Exception:
            pass
    _pts._STORE = _pts.ProjectTasksStore(Path(":memory:"))
    yield
    if _pts._STORE is not None:
        try:
            _pts._STORE.close()
        except Exception:
            pass
        _pts._STORE = None


@pytest.fixture(autouse=True)
def _reset_mcp_feature_gate_store():
    """Use fresh in-memory MCP feature gates for each test."""
    import app.services.mcp_feature_gates as _fg
    from pathlib import Path

    if _fg._STORE is not None:
        try:
            _fg._STORE.close()
        except Exception:
            pass
    _fg._STORE = _fg.McpFeatureGateStore(Path(":memory:"))
    yield
    if _fg._STORE is not None:
        try:
            _fg._STORE.close()
        except Exception:
            pass
        _fg._STORE = None


@pytest.fixture(autouse=True)
def _reset_autonomous_mode_store():
    """Use a fresh in-memory autonomous-mode store for each test."""
    import app.services.autonomous_mode_service as _ams
    from pathlib import Path

    if _ams._STORE is not None:
        try:
            _ams._STORE.close()
        except Exception:
            pass
    _ams._STORE = _ams.AutonomousModeStore(Path(":memory:"))
    yield
    if _ams._STORE is not None:
        try:
            _ams._STORE.close()
        except Exception:
            pass
        _ams._STORE = None


@pytest.fixture(autouse=True)
def _reset_mcp_host_compatibility_store():
    """Use fresh in-memory MCP host compatibility state for each test."""
    import app.services.mcp_host_compatibility as _mhc
    from pathlib import Path

    if _mhc._STORE is not None:
        try:
            _mhc._STORE.close()
        except Exception:
            pass
    _mhc._STORE = _mhc.McpHostCompatibilityStore(Path(":memory:"))
    yield
    if _mhc._STORE is not None:
        try:
            _mhc._STORE.close()
        except Exception:
            pass
        _mhc._STORE = None


@pytest_asyncio.fixture(autouse=True)
async def _reset_learning_store():
    """Use fresh in-memory learning store for each test — avoids leaking background writer tasks."""
    import app.services.learning_store as _ls
    from pathlib import Path

    if _ls._store is not None:
        try:
            await _ls.close_learning_store()
        except Exception:
            pass

    _ls._store = _ls.LearningStore(Path(":memory:"))
    yield

    if _ls._store is not None:
        try:
            await _ls.close_learning_store()
        except Exception:
            pass
        _ls._store = None


@pytest.fixture(autouse=True)
def _reset_memory_store():
    """Use fresh in-memory content store for each test (avoid writing qdrant_data/memory_store.db)."""
    import app.services.memory_store as _ms
    from pathlib import Path

    if _ms._store is not None:
        try:
            _ms._store.close()
        except Exception:
            pass
    _ms._store = _ms.MemoryContentStore(Path(":memory:"))
    yield
    if _ms._store is not None:
        try:
            _ms._store.close()
        except Exception:
            pass
        _ms._store = None


@pytest.fixture(autouse=True)
def _reset_docs_cache_store():
    """Use fresh in-memory docs cache store for each test."""
    import app.services.docs_cache_store as _dcs
    from pathlib import Path

    if _dcs._store is not None:
        try:
            _dcs._store.close()
        except Exception:
            pass
    _dcs._store = _dcs.DocsCacheStore(Path(":memory:"))
    yield
    if _dcs._store is not None:
        try:
            _dcs._store.close()
        except Exception:
            pass
        _dcs._store = None


@pytest.fixture(autouse=True)
def _reset_component_docs_store():
    """Use fresh in-memory component docs store for each test."""
    import app.services.component_docs_store as _cds
    from pathlib import Path

    if _cds._STORE is not None:
        try:
            _cds._STORE.close()
        except Exception:
            pass
    _cds._STORE = _cds.ComponentDocsStore(Path(":memory:"))
    yield
    if _cds._STORE is not None:
        try:
            _cds._STORE.close()
        except Exception:
            pass
        _cds._STORE = None


@pytest.fixture(autouse=True)
def _reset_skill_counters_store():
    """Use fresh in-memory skill counters store for each test (avoid writing qdrant_data/skills.db)."""
    import app.services.skill_counters as _sc
    from pathlib import Path

    if _sc._store is not None:
        try:
            _sc._store.close()
        except Exception:
            pass
    _sc._store = _sc.SkillCountersStore(Path(":memory:"))
    yield
    if _sc._store is not None:
        try:
            _sc._store.close()
        except Exception:
            pass
        _sc._store = None


@pytest.fixture(autouse=True)
def _reset_data_integrity_store():
    """Use fresh in-memory integrity store for each test."""
    import app.services.data_integrity_service as _di
    from pathlib import Path

    if _di._store is not None:
        try:
            _di._store.close()
        except Exception:
            pass
    _di._store = _di.DataIntegrityStore(Path(":memory:"))
    yield
    if _di._store is not None:
        try:
            _di._store.close()
        except Exception:
            pass
        _di._store = None


@pytest.fixture(autouse=True)
def _reset_data_hygiene_store():
    """Use fresh in-memory data hygiene store for each test."""
    import app.services.data_hygiene_service as _dh
    from pathlib import Path

    if _dh._store is not None:
        try:
            _dh._store.close()
        except Exception:
            pass
    _dh._store = _dh.DataHygieneStore(Path(":memory:"))
    yield
    if _dh._store is not None:
        try:
            _dh._store.close()
        except Exception:
            pass
        _dh._store = None


@pytest.fixture(autouse=True)
def _reset_performance_tracker():
    """Use fresh in-memory performance tracker for each test (avoid writing qdrant_data/performance.db)."""
    import app.services.performance_tracker as _pt
    from pathlib import Path

    if _pt._tracker is not None:
        try:
            _pt._tracker.close()
        except Exception:
            pass
    _pt._tracker = _pt.PerformanceTracker(Path(":memory:"))
    yield
    if _pt._tracker is not None:
        try:
            _pt._tracker.close()
        except Exception:
            pass
        _pt._tracker = None


@pytest.fixture(autouse=True)
def _reset_unified_artifact_service():
    """Reset unified artifact service to use fresh stores for each test."""
    import app.services.unified_artifact_service as _uas
    _uas._service = None
    yield
    _uas._service = None


@pytest_asyncio.fixture(autouse=True)
async def _reset_job_queue():
    """Use in-memory job queue DB for each test (avoid writing qdrant_data/jobs.db)."""
    import app.services.job_queue as _jq
    from pathlib import Path

    if _jq._queue is not None:
        try:
            await _jq._queue.stop()
        except Exception:
            pass
    _jq._queue = _jq.JobQueue(Path(":memory:"))
    yield
    if _jq._queue is not None:
        try:
            await _jq._queue.stop()
        except Exception:
            pass
        _jq._queue = None

MOCK_VECTOR = [0.1] * 1024


async def _build_client(mock_vector: list[float], batch_vectors: list[list[float]] | None = None):
    from app import dependencies
    from app.main import create_app
    from app.services.ollama_service import OllamaService
    from app.services.qdrant_service import QdrantService

    qdrant_client = AsyncQdrantClient(":memory:")
    qdrant_svc = QdrantService(qdrant_client)
    await qdrant_svc.ensure_collection()
    dependencies.set_qdrant_client(qdrant_client)

    ollama = OllamaService.__new__(OllamaService)
    ollama.base_url = "http://mocked"
    ollama.model = "nomic-embed-text"
    ollama.embed = AsyncMock(return_value=mock_vector)
    ollama.embed_batch = AsyncMock(return_value=batch_vectors if batch_vectors is not None else [mock_vector] * 100)
    ollama.generate = AsyncMock(return_value="")
    ollama.health = AsyncMock(return_value=True)
    ollama.close = AsyncMock()
    dependencies.set_ollama_service(ollama)

    app = create_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, qdrant_client, ollama


@pytest_asyncio.fixture
async def client():
    """
    Spin up the FastAPI app with an in-memory Qdrant client and a mocked Ollama.
    We initialise dependencies directly so the lifespan event is not required.
    """
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c

    await qdrant_client.close()


@pytest_asyncio.fixture
async def mismatch_client():
    c, qdrant_client, _ = await _build_client([0.1] * 768)
    async with c:
        yield c

    await qdrant_client.close()


@pytest_asyncio.fixture
async def partial_batch_client():
    batch_vectors = [MOCK_VECTOR, [0.1] * 768]
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR, batch_vectors=batch_vectors)
    async with c:
        yield c

    await qdrant_client.close()


@pytest_asyncio.fixture
async def failed_batch_client():
    batch_vectors = [[0.1] * 768, [0.1] * 768]
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR, batch_vectors=batch_vectors)
    async with c:
        yield c

    await qdrant_client.close()
