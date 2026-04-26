import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from app.services.text_localization import looks_like_mojibake, normalize_text_for_display
import os

async def check():
    client = AsyncQdrantClient(url=os.getenv('QDRANT_URL', 'http://localhost:6333'))
    collection = "project_docs"

    # Check all docs
    print("Checking project_docs for mojibake...")
    try:
        results, _ = await client.scroll(
            collection_name=collection,
            limit=200,
            with_payload=True,
            with_vectors=False,
        )
        mojibake_count = 0
        for r in results:
            payload = r.payload or {}
            content = str(payload.get("content", ""))
            section_name = str(payload.get("section_name", ""))

            if looks_like_mojibake(content):
                print(f'Mojibake in doc {r.id}:')
                print(f'  Section: {section_name}')
                print(f'  Content: {content[:100]}...')
                mojibake_count += 1
        print(f'Total mojibake in project_docs: {mojibake_count}')
    except Exception as e:
        print(f'Error: {e}')

if __name__ == "__main__":
    asyncio.run(check())
