"""
Test enrich-task endpoint for mojibake in Russian content.
"""
import asyncio
import httpx
import json

async def test_enrich_task():
    url = "http://localhost:8000/api/v1/project/enrich-task"
    payload = {
        "project_id": "mnemoforge",
        "task": "Fix mojibake in project context and documentation projections for legacy Russian content",
        "context_profile": "default",
        "max_components": 3
    }

    headers = {"X-Api-Key": "mnemoforge-local"}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            # Check for mojibake in the response
            mojibake_markers = ["Р", "С", "Ð", "Ñ", "â", "Â", "Ã", "�"]
            context = data.get("context", "")

            mojibake_found = False
            for marker in mojibake_markers:
                if marker in context:
                    print(f"Found mojibake marker '{marker}' in context")
                    mojibake_found = True

            if mojibake_found:
                print("\n=== CONTEXT WITH MOJIBAKE ===")
                print(context[:2000])
                print("\n=== END ===")
            else:
                print("✓ No mojibake found in enrich-task response")

            # Check improvements
            improvements = data.get("improvements", [])
            print(f"\nImprovements count: {len(improvements)}")
            for imp in improvements:
                title = imp.get("title", "")
                desc = imp.get("description", "")
                for marker in mojibake_markers:
                    if marker in title or marker in desc:
                        print(f"Found mojibake in improvement: {title[:80]}")
                        mojibake_found = True

            # Check runtime hints
            hints = data.get("runtime_hints", [])
            print(f"\nRuntime hints count: {len(hints)}")
            for hint in hints:
                content = hint.get("content", "")
                observation = hint.get("observation", "")
                for marker in mojibake_markers:
                    if marker in content or marker in observation:
                        print(f"Found mojibake in hint: {content[:80]}")
                        mojibake_found = True

            if not mojibake_found:
                print("\n✓ All checks passed - no mojibake detected")
            else:
                print("\n✗ Mojibake detected in response")

        except httpx.HTTPError as e:
            print(f"HTTP error: {e}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_enrich_task())
