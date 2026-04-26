import logging
import asyncio
import re
import os
from typing import Optional

from app.services.ollama_service import OllamaService
from app.services.llm_gateway import CloudLLMGateway
from app.services.cloud_llm import cloud_available
from app.models.knowledge_tree_repo import KnowledgeTreeRepo
from app.services.job_queue import JobQueue
from app.services.qdrant_service import QdrantService
from app.services.scoring_service import ScoringService
from app.models.knowledge_tree import TreeNode

logger = logging.getLogger(__name__)

SLM_MODEL = os.getenv("SLM_MODEL", "qwen3:1.7b")

class KnowledgeTree:
    """
    Управляет иерархическим Деревом Знаний и адаптивной маршрутизацией запросов.
    """
    def __init__(
        self, 
        repo: KnowledgeTreeRepo, 
        slm: OllamaService, 
        llm_gateway: CloudLLMGateway,
        job_queue: JobQueue,
        qdrant: QdrantService,
        scorer: ScoringService
    ):
        self._repo = repo
        self._slm = slm
        self._llm = llm_gateway
        self._job_queue = job_queue
        self._qdrant = qdrant
        self._scorer = scorer

    def _extract_pattern(self, query: str) -> str:
        """
        Извлекает нормализованный корень запроса для ведения статистики (MVP-версия).
        Пример: "Как настроить jwt токены?" -> "как настроить jwt"
        """
        clean = re.sub(r'[^\w\s]', '', query.lower())
        words = [w for w in clean.split() if len(w) > 2]
        return " ".join(words[:3]) if words else "general"

    async def classify_query_adaptive(self, query: str) -> str:
        """
        Классифицирует запрос с использованием Shadow Evaluation.
        Мгновенно отвечает через SLM, если нет исторического антипаттерна.
        """
        pattern = self._extract_pattern(query)
        rule = self._repo.get_routing_rule(pattern)
        
        if rule and rule.requires_llm:
            if cloud_available():
                logger.info(f"[KnowledgeTree] Routing '{pattern}' to Cloud LLM (SLM historical fail rate is high)")
                prompt = f"Classify this query into a hierarchical category (max 3 levels, e.g., 'python/fastapi/auth'). Just output the path.\nQuery: {query}"
                res = await self._llm.generate(prompt, system="You are a strict taxonomy classifier. Output only the path.")
                return res.strip().lower()
            logger.warning(
                "[KnowledgeTree] Rule for '%s' requires cloud classification, but no cloud LLM is configured; falling back to SLM",
                pattern,
            )

        # Быстрый путь (Fast Path)
        prompt = f"Classify this query into a hierarchical category (max 3 levels, e.g., 'python/fastapi/auth'). Just output the path.\nQuery: {query}"
        slm_category = await self._slm.generate(prompt, model=SLM_MODEL)
        slm_category = slm_category.strip().lower()
        
        # Фоновая оценка (Shadow Evaluation)
        if cloud_available():
            await self._job_queue.submit("verify_tree_classification", {
                "query": query,
                "pattern": pattern,
                "slm_category": slm_category
            })
        
        return slm_category

    async def slice_tree(self, query: str, agent_id: str, limit: int = 20) -> dict:
        """
        Возвращает срез дерева (поиск с бустингом скора по глубине ветки).
        """
        # 1. Классификация
        category_path = await self.classify_query_adaptive(query)
        
        # 2. Векторизация запроса
        vector = await self._slm.embed(query)
        
        # 3. Расширенный поиск в Qdrant
        raw_results = await self._qdrant.search(
            vector=vector, agent_id=agent_id, limit=limit, overfetch_factor=3
        )
        
        # 4. Ранжирование: семантика + иерархия
        path_parts = category_path.split('/')
        hierarchy = ["/".join(path_parts[:i+1]) for i in range(len(path_parts))]
        
        scored = []
        for record, sim in raw_results:
            base_score = self._scorer.score(record, sim)
            depth_boost = 0.0
            if record.topic_path:
                try:
                    # Чем ближе совпадение к листьям, тем выше буст (до +0.3)
                    idx = hierarchy.index(record.topic_path)
                    depth_boost = ((idx + 1) / len(hierarchy)) * 0.3
                except ValueError:
                    pass
                    
            final_score = (base_score * 0.7) + depth_boost
            scored.append({
                "memory": record,
                "score": round(final_score, 3),
                "similarity": round(sim, 3),
                "tree_boost": round(depth_boost, 3)
            })
            
        scored.sort(key=lambda x: x["score"], reverse=True)
        
        # Адаптивный рост/укрепление (Фазa 4: Growth/Strengthen)
        self._strengthen_path(category_path)
        
        return {
            "query": query,
            "target_category": category_path,
            "results": scored[:limit]
        }

    def _strengthen_path(self, path: str) -> None:
        """Укрепляет существующую ветку или выращивает новую."""
        if not path:
            return
        try:
            from datetime import datetime, timezone
            node = self._repo.get_node(path)
            if node:
                node.access_count += 1
                node.strength = min(1.0, node.strength + 0.05)
                node.last_accessed = datetime.now(timezone.utc)
                self._repo.upsert_node(node)
            else:
                parts = path.split("/")
                parent = "/".join(parts[:-1]) if len(parts) > 1 else None
                self._repo.upsert_node(TreeNode(
                    path=path, parent_path=parent, level=len(parts),
                    strength=0.1, access_count=1
                ))
        except Exception as e:
            logger.warning(f"Failed to strengthen node {path}: {e}")

    def prune_dead_branches(self, min_strength: float = 0.2, max_idle_days: int = 30) -> int:
        """
        Обрезка "мертвых" веток дерева (Фаза 4).
        Удаляет пути, которые давно не использовались и имеют низкую силу (strength).
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        to_delete = []
        
        with self._repo._lock:
            rows = self._repo._conn.execute("SELECT path, last_accessed, strength FROM tree_nodes").fetchall()
            for row in rows:
                try:
                    last_acc = datetime.fromisoformat(row["last_accessed"])
                    days_idle = (now - last_acc).days
                    if days_idle > max_idle_days and row["strength"] <= min_strength:
                        to_delete.append(row["path"])
                except ValueError:
                    continue
                    
            for p in to_delete:
                self._repo._conn.execute("DELETE FROM tree_nodes WHERE path = ?", (p,))
            if to_delete:
                self._repo._conn.commit()
                
        return len(to_delete)

async def verify_tree_classification_handler(payload: dict) -> dict:
    """Фоновый воркер (JobQueue) для сверки результата SLM с облачной LLM."""
    from app.dependencies import get_knowledge_tree_repo, get_llm_gateway
    if not cloud_available():
        logger.info("[ShadowEval] Skipped knowledge-tree verification: no cloud LLM configured")
        return {"status": "skipped", "reason": "cloud_unavailable"}

    query = payload["query"]
    pattern = payload["pattern"]
    slm_category = payload["slm_category"]
    
    llm = get_llm_gateway()
    repo = get_knowledge_tree_repo()
    
    prompt = f"Classify this query into a hierarchical category (max 3 levels, e.g., 'python/fastapi/auth'). Just output the path.\nQuery: {query}"
    llm_category = await llm.generate(prompt, system="You are a strict taxonomy classifier. Output only the path.")
    llm_category = llm_category.strip().lower()
    
    # Оцениваем по "стволу" (корневой категории). Если ошиблись на самом верху - это критично.
    slm_trunk = slm_category.split('/')[0] if slm_category else ""
    llm_trunk = llm_category.split('/')[0] if llm_category else ""
    
    if slm_trunk and slm_trunk != llm_trunk:
        repo.record_routing_failure(pattern)
        logger.info(f"[ShadowEval] CRITICAL MISMATCH for '{pattern}': SLM='{slm_category}', LLM='{llm_category}'")
        return {"status": "mismatch", "slm": slm_category, "llm": llm_category}
    
    repo.record_routing_success(pattern)
    logger.info(f"[ShadowEval] SUCCESS for '{pattern}': Match on trunk '{slm_trunk}'")
    return {"status": "match", "slm": slm_category, "llm": llm_category}


async def tree_pruning_task(interval_hours: int = 24):
    """
    Фоновая задача для автоматической очистки дерева знаний.
    Запускается в основном цикле FastAPI (lifespan).
    """
    from app.dependencies import get_knowledge_tree
    
    # Откладываем первый запуск на 5 минут после старта сервера
    await asyncio.sleep(300)
    
    while True:
        try:
            logger.info("[KnowledgeTree] Запуск плановой очистки мертвых веток...")
            tree = get_knowledge_tree()
            deleted = tree.prune_dead_branches()
            logger.info(f"[KnowledgeTree] Очистка завершена: удалено {deleted} мертвых веток.")
        except Exception as e:
            logger.error(f"[KnowledgeTree] Ошибка во время плановой очистки: {e}")
            
        await asyncio.sleep(interval_hours * 3600)
