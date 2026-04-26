import asyncio
from qdrant_client import AsyncQdrantClient
import os

async def check():
    client = AsyncQdrantClient(url=os.getenv('QDRANT_URL', 'http://localhost:6333'))
    ids = [
        'abb70523-9a48-4486-80fa-9a3812ec4992',
        'c3ea57fc-2b2f-4c66-ad5a-be9116c07aaf',
        'cd8a0bf6-ddeb-4007-99b9-366a85d446e9'
    ]
    results = await client.retrieve(
        collection_name='agent_memories',
        ids=ids,
        with_payload=True,
        with_vectors=False
    )
    for r in results:
        print(f'ID: {r.id}')
        print(f'Content:\n{r.payload.get("content", "")}')
        print('---\n')

if __name__ == "__main__":
    asyncio.run(check())
