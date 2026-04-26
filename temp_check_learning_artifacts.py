import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from app.services.text_localization import looks_like_mojibake, normalize_text_for_display
import os

async def check():
    client = AsyncQdrantClient(url=os.getenv('QDRANT_URL', 'http://localhost:6333'))
    collection = "learning_artifacts"

    # Check all artifacts
    print("Checking learning_artifacts for mojibake...")
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
            observation = str(payload.get("observation", ""))
            why = str(payload.get("why_it_matters", ""))
            artifact_type = str(payload.get("artifact_type", ""))

            if looks_like_mojibake(content) or looks_like_mojibake(observation) or looks_like_mojibake(why):
                print(f'Mojibake in artifact {r.id}:')
                print(f'  Type: {artifact_type}')
                if looks_like_mojibake(content):
                    print(f'  Content: {content[:100]}...')
                if looks_like_mojibake(observation):
                    print(f'  Observation: {observation[:100]}...')
                if looks_like_mojibake(why):
                    print(f'  Why: {why[:100]}...')
                mojibake_count += 1
        print(f'Total mojibake in learning_artifacts: {mojibake_count}')
    except Exception as e:
        print(f'Error: {e}')

if __name__ == "__main__":
    asyncio.run(check())
