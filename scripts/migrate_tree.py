import asyncio
import sys
import logging
import os

# Добавляем корень проекта в пути импорта
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.config import settings
from app.services.ollama_service import OllamaService
from app.services.cloud_llm import cloud_complete, cloud_available
from app.models.knowledge_tree_repo import KnowledgeTreeRepo
from app.services.knowledge_tree import KnowledgeTree

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TreeMigration")

async def run_migration():
    client = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    ollama = OllamaService()
    repo = KnowledgeTreeRepo()

    logger.info("Fetching unclassified memories from Qdrant...")
    # Ищем все записи, где topic_path еще пустой (от старой версии)
    records, _ = await client.scroll(
        collection_name=settings.qdrant_collection_name,
        scroll_filter=qmodels.Filter(must=[qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key="topic_path"))]),
        limit=10000,
        with_payload=True,
        with_vectors=False
    )

    if not records:
        logger.info("All memories are already classified!")
        return

    logger.info(f"Found {len(records)} memories to classify. Starting migration...")
    
    # Снижаем параллелизм для облака, чтобы не перегружать API (лимиты тарифов)
    max_concurrent = 5 if cloud_available() else 4
    sem = asyncio.Semaphore(max_concurrent)
    migrated_count = 0

    async def process_record(record):
        nonlocal migrated_count
        content = record.payload.get("content", "")
        if not content:
            return
            
        prompt = f"Classify this memory into a hierarchical category (max 3 levels, e.g., 'project/architecture/database'). Just output the path. No other words.\nContent: {content[:500]}"
        
        category = "general"
        async with sem:
            # Механизм повторных попыток на случай Rate Limit или сбоя сети
            for attempt in range(3):
                try:
                    if cloud_available():
                        await asyncio.sleep(0.3) # Смягчаем пиковую нагрузку на API
                        category = await cloud_complete(prompt, system="You are a strict taxonomy classifier. Output only the path.")
                    else:
                        category = await ollama.generate(prompt, model=os.getenv("SLM_MODEL", "qwen3:1.7b"))
                    break  # Успех, выходим из цикла retry
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"Ошибка LLM (попытка {attempt+1}): {e}. Ждем перед повтором...")
                        await asyncio.sleep(2 ** attempt) # 1с, 2с...
                    else:
                        logger.error(f"Не удалось классифицировать: {e}")
                        category = "general"
            
            category = category.strip().lower()
            if not category or len(category) > 50:
                category = "general"
                
            # 1. Сохраняем в Qdrant
            await client.set_payload(collection_name=settings.qdrant_collection_name, payload={"topic_path": category}, points=[record.id])
            
            # 2. Выращиваем ветку в SQLite
            node_path = category.split("/")
            with repo._lock:
                for i in range(len(node_path)):
                    sub_path = "/".join(node_path[:i+1])
                    repo._conn.execute("INSERT OR IGNORE INTO tree_nodes (path, parent_path, level) VALUES (?, ?, ?)", 
                                       (sub_path, "/".join(node_path[:i]) if i > 0 else None, i+1))
                repo._conn.commit()
                
            migrated_count += 1
            if migrated_count % 50 == 0:
                logger.info(f"Migrated {migrated_count}/{len(records)} memories...")

    tasks = [process_record(r) for r in records]
    await asyncio.gather(*tasks)

    logger.info("Migration complete!")
    await client.close()

if __name__ == "__main__":
    asyncio.run(run_migration())
