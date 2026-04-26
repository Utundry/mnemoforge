import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from app.services.text_localization import looks_like_mojibake, normalize_text_for_display, repair_mojibake
import os

async def check_and_repair():
    client = AsyncQdrantClient(url=os.getenv('QDRANT_URL', 'http://localhost:6333'))
    collection = "agent_memories"

    # Check all memories
    print("Checking ALL agent_memories for mojibake...")
    mojibake_records = []
    offset = None
    limit = 100

    while True:
        try:
            results, next_page_offset = await client.scroll(
                collection_name=collection,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )

            if not results:
                break

            for r in results:
                payload = r.payload or {}
                content = str(payload.get("content", ""))
                memory_type = str(payload.get("memory_type", ""))
                category = str(payload.get("category", ""))

                if looks_like_mojibake(content):
                    repaired = repair_mojibake(content)
                    mojibake_records.append({
                        'id': str(r.id),
                        'type': memory_type,
                        'category': category,
                        'original': content[:200],
                        'repaired': repaired[:200] if repaired != content else 'NO REPAIR',
                        'needs_update': repaired != content
                    })

            offset = next_page_offset
            if offset is None:
                break

        except Exception as e:
            print(f'Error: {e}')
            break

    print(f'\nTotal mojibake records found: {len(mojibake_records)}')
    for record in mojibake_records:
        print(f"\nID: {record['id']}")
        print(f"  Type: {record['type']}, Category: {record['category']}")
        print(f"  Original: {record['original']}")
        print(f"  Repaired: {record['repaired']}")
        print(f"  Needs update: {record['needs_update']}")

    return mojibake_records

if __name__ == "__main__":
    asyncio.run(check_and_repair())
