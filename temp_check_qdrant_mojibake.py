import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from app.services.text_localization import looks_like_mojibake, normalize_text_for_display
import os

async def check_qdrant():
    client = AsyncQdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
    collection = "memories"

    # Check memoirs
    print("Checking memoirs...")
    try:
        results, _ = await client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="task_memoir")),
                qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value="project:mnemoforge")),
            ]),
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        mojibake_count = 0
        for r in results:
            payload = r.payload or {}
            content = str(payload.get("content", ""))
            if looks_like_mojibake(content):
                print(f'Mojibake in memoir {r.id}:')
                print(f'  Content: {content[:100]}...')
                mojibake_count += 1
        print(f'Total mojibake in memoirs: {mojibake_count}')
    except Exception as e:
        print(f'Error checking memoirs: {e}')

    # Check runtime hints
    print("\nChecking runtime hints...")
    try:
        results, _ = await client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="runtime_hint")),
            ]),
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        mojibake_count = 0
        for r in results:
            payload = r.payload or {}
            content = str(payload.get("content", ""))
            observation = str(payload.get("observation", ""))
            if looks_like_mojibake(content) or looks_like_mojibake(observation):
                print(f'Mojibake in hint {r.id}:')
                print(f'  Content: {content[:100]}...')
                print(f'  Observation: {observation[:100]}...')
                mojibake_count += 1
        print(f'Total mojibake in runtime hints: {mojibake_count}')
    except Exception as e:
        print(f'Error checking runtime hints: {e}')

    # Check docs sections
    print("\nChecking docs sections...")
    try:
        results, _ = await client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="docs_section")),
            ]),
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        mojibake_count = 0
        for r in results:
            payload = r.payload or {}
            content = str(payload.get("content", ""))
            if looks_like_mojibake(content):
                print(f'Mojibake in docs section {r.id}:')
                print(f'  Content: {content[:100]}...')
                mojibake_count += 1
        print(f'Total mojibake in docs sections: {mojibake_count}')
    except Exception as e:
        print(f'Error checking docs sections: {e}')

if __name__ == "__main__":
    asyncio.run(check_qdrant())
