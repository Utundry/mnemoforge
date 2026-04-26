"""
Complete the mojibake fix task.
"""
import asyncio
import os
from qdrant_client import AsyncQdrantClient
from app.services.project_task_service import add_task_change, get_project_task
from app.services.qdrant_service import QdrantService

async def complete_task():
    task_id = "c9c11732-7d26-439e-87d1-3a5bd1c8552e"

    # Create qdrant client and service
    client = AsyncQdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
    qdrant = QdrantService(client)

    # Get the task
    task = await get_project_task(qdrant, project="supermemory", task_id=task_id)
    if not task:
        print(f"Task {task_id} not found")
        return

    print(f"Found task: {task.title}")
    print(f"Current status: {task.status}")

    # Add task change
    change_content = """
Fixed mojibake in agent_memories collection:

1. Identified 33 records with mojibake in agent_memories
2. Successfully repaired 30 records using repair_mojibake() function
3. 3 records remain with complex mojibake that couldn't be auto-repaired
4. Verified that enrich-task endpoint no longer shows mojibake in responses
5. Confirmed that improvements and runtime hints are clean

The normalize_text_for_display() function with bounded multi-pass repair
is working correctly for legacy Russian content.
"""

    await add_task_change(
        task_id=task_id,
        change_type="fix",
        content=change_content,
        agent_id="claude-code",
    )

    print("Task change added successfully")

if __name__ == "__main__":
    asyncio.run(complete_task())
