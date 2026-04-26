"""
Test enrich-task endpoint for mojibake - full output.
"""
import asyncio
import httpx
import json

async def test_enrich_task():
    url = "http://localhost:8000/api/v1/project/enrich-task"
    payload = {
        "project_id": "supermemory",
        "task": "Fix mojibake in project context and documentation projections for legacy Russian content",
        "context_profile": "default",
        "max_components": 3
    }

    headers = {"X-Api-Key": "supermemory-local"}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            # Print improvements
            print("=== IMPROVEMENTS ===")
            improvements = data.get("improvements", [])
            for imp in improvements:
                print(f"\nID: {imp.get('id')}")
                print(f"Title: {imp.get('title')}")
                print(f"Description: {imp.get('description')}")

            # Print runtime hints
            print("\n\n=== RUNTIME HINTS ===")
            hints = data.get("runtime_hints", [])
            for hint in hints:
                print(f"\nID: {hint.get('id')}")
                print(f"Label: {hint.get('label')}")
                print(f"Content: {hint.get('content')}")
                print(f"Observation: {hint.get('observation')}")

        except httpx.HTTPError as e:
            print(f"HTTP error: {e}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_enrich_task())
