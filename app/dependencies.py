from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends
from qdrant_client import AsyncQdrantClient

from app.config import settings
from app.services.job_queue import JobQueue, get_job_queue
from app.services.layout_memory import LayoutMemoryService
from app.services.ollama_service import OllamaService
from app.services.qdrant_service import QdrantService
from app.services.scoring_service import ScoringService
from app.services.llm_gateway import CloudLLMGateway
from app.models.knowledge_tree_repo import KnowledgeTreeRepo
from app.services.knowledge_tree import KnowledgeTree

# Singletons stored in app state — populated in lifespan
_qdrant_client: Optional[AsyncQdrantClient] = None
_ollama_service: Optional[OllamaService] = None
_scoring_service: ScoringService = ScoringService()
_knowledge_tree_repo: Optional[KnowledgeTreeRepo] = None
_llm_gateway: Optional[CloudLLMGateway] = None
_knowledge_tree: Optional[KnowledgeTree] = None


def set_qdrant_client(client: AsyncQdrantClient) -> None:
    global _qdrant_client
    _qdrant_client = client


def set_ollama_service(service: OllamaService) -> None:
    global _ollama_service
    _ollama_service = service


def get_qdrant() -> QdrantService:
    if _qdrant_client is None:
        raise RuntimeError("Qdrant client not initialised — server is still starting up")
    return QdrantService(_qdrant_client)


def get_ollama() -> OllamaService:
    if _ollama_service is None:
        raise RuntimeError("Ollama service not initialised — server is still starting up")
    return _ollama_service


def get_scorer() -> ScoringService:
    return _scoring_service


def get_layout_memory() -> LayoutMemoryService:
    if _qdrant_client is None or _ollama_service is None:
        raise RuntimeError("Services not initialised — server is still starting up")
    return LayoutMemoryService(_qdrant_client, _ollama_service)


def get_queue() -> JobQueue:
    return get_job_queue()


def get_knowledge_tree_repo() -> KnowledgeTreeRepo:
    global _knowledge_tree_repo
    if _knowledge_tree_repo is None:
        _knowledge_tree_repo = KnowledgeTreeRepo()
    return _knowledge_tree_repo


def get_llm_gateway() -> CloudLLMGateway:
    global _llm_gateway
    if _llm_gateway is None:
        _llm_gateway = CloudLLMGateway()
    return _llm_gateway


def get_knowledge_tree() -> KnowledgeTree:
    global _knowledge_tree
    if _knowledge_tree is None:
        _knowledge_tree = KnowledgeTree(
            repo=get_knowledge_tree_repo(),
            slm=get_ollama(),
            llm_gateway=get_llm_gateway(),
            job_queue=get_queue(),
            qdrant=get_qdrant(),
            scorer=get_scorer()
        )
    return _knowledge_tree

QdrantDep = Annotated[QdrantService, Depends(get_qdrant)]
OllamaDep = Annotated[OllamaService, Depends(get_ollama)]
ScorerDep = Annotated[ScoringService, Depends(get_scorer)]
LayoutMemoryDep = Annotated[LayoutMemoryService, Depends(get_layout_memory)]
JobQueueDep = Annotated[JobQueue, Depends(get_queue)]
KnowledgeTreeRepoDep = Annotated[KnowledgeTreeRepo, Depends(get_knowledge_tree_repo)]
LLMGatewayDep = Annotated[CloudLLMGateway, Depends(get_llm_gateway)]
KnowledgeTreeDep = Annotated[KnowledgeTree, Depends(get_knowledge_tree)]
