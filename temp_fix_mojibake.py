"""
Script to fix mojibake in agent_memories collection.

This script:
1. Scans all memories for mojibake
2. Repairs the content using repair_mojibake()
3. Updates the records in Qdrant
"""
import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from app.services.text_localization import looks_like_mojibake, normalize_text_for_display, repair_mojibake
import os

async def fix_mojibake():
    client = AsyncQdrantClient(url=os.getenv('QDRANT_URL', 'http://localhost:6333'))
    collection = "agent_memories"

    print("Scanning agent_memories for mojibake...")
    mojibake_records = []
    offset = None
    limit = 100

    # First pass: find all records with mojibake
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

                if looks_like_mojibake(content):
                    repaired = repair_mojibake(content)
                    if repaired != content:
                        mojibake_records.append({
                            'id': str(r.id),
                            'original_content': content,
                            'repaired_content': repaired,
                            'payload': payload
                        })

            offset = next_page_offset
            if offset is None:
                break

        except Exception as e:
            print(f'Error during scan: {e}')
            break

    print(f'\nFound {len(mojibake_records)} records with mojibake that can be repaired')

    if not mojibake_records:
        print("No records to fix.")
        return

    # Second pass: update the records
    print("\nUpdating records...")
    updated_count = 0
    failed_count = 0

    for record in mojibake_records:
        try:
            # Update the payload with repaired content
            payload = record['payload'].copy()
            payload['content'] = record['repaired_content']

            await client.set_payload(
                collection_name=collection,
                payload=payload,
                points=[record['id']]
            )

            updated_count += 1
            if updated_count % 10 == 0:
                print(f"Updated {updated_count}/{len(mojibake_records)} records...")

        except Exception as e:
            print(f"Error updating record {record['id']}: {e}")
            failed_count += 1

    print(f"\nDone! Updated {updated_count} records successfully.")
    if failed_count > 0:
        print(f"Failed to update {failed_count} records.")

    # Verify the fix
    print("\nVerifying fix...")
    verification_failed = 0
    for record in mojibake_records[:5]:  # Check first 5
        try:
            result = await client.retrieve(
                collection_name=collection,
                ids=[record['id']],
                with_payload=True,
                with_vectors=False,
            )
            if result:
                current_content = str(result[0].payload.get("content", ""))
                if looks_like_mojibake(current_content):
                    print(f"Verification failed for {record['id']}: still has mojibake")
                    verification_failed += 1
                else:
                    print(f"Verification passed for {record['id']}")
        except Exception as e:
            print(f"Error verifying record {record['id']}: {e}")

    if verification_failed == 0:
        print("All verified records are clean!")
    else:
        print(f"{verification_failed} records still have mojibake")

if __name__ == "__main__":
    asyncio.run(fix_mojibake())
