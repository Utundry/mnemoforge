import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from app.services.text_localization import looks_like_mojibake, normalize_text_for_display
import os

async def check():
    client = AsyncQdrantClient(url=os.getenv('QDRANT_URL', 'http://localhost:6333'))
    collection = "agent_memories"

    # Check all memories
    print("Checking agent_memories for mojibake...")
    try:
        results, _ = await client.scroll(
            collection_name=collection,
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        mojibake_count = 0
        for r in results:
            payload = r.payload or {}
            content = str(payload.get("content", ""))
            memory_type = str(payload.get("memory_type", ""))
            category = str(payload.get("category", ""))

            if looks_like_mojibake(content):
                print(f'Mojibake in memory {r.id}:')
                print(f'  Type: {memory_type}, Category: {category}')
                print(f'  Content: {content[:100]}...')
                mojibake_count += 1
        print(f'Total mojibake in agent_memories: {mojibake_count}')
    except Exception as e:
        print(f'Error: {e}')

if __name__ == "__main__":
    asyncio.run(check())
