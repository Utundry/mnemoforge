import asyncio
from qdrant_client import AsyncQdrantClient
from app.services.qdrant_service import QdrantService
from app.config import settings
from app.models.memory import MemoryCreate
from app.models.enums import MemoryType

async def main():
    client = AsyncQdrantClient(":memory:")
    service = QdrantService(client)
    await service.ensure_collection()
    memory = MemoryCreate(
        content="ping",
        agent_id="tester",
        memory_type=MemoryType.context,
        category="context",
        importance_score=0.4,
        source="test",
        tags=["test"],
    )
    vector = [0.0] * settings.embedding_dimensions
    memory_id = await service.insert(memory, vector)
    print("inserted", memory_id)
    record = await service.get(memory_id)
    print("retrieved", record.id)

asyncio.run(main())
