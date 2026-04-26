import asyncio
from qdrant_client import AsyncQdrantClient
import os

async def check():
    client = AsyncQdrantClient(url=os.getenv('QDRANT_URL', 'http://localhost:6333'))
    collections = await client.get_collections()
    print('Available collections:')
    for c in collections.collections:
        print(f'  - {c.name}')

if __name__ == "__main__":
    asyncio.run(check())
