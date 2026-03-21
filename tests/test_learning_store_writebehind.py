import asyncio

import pytest

from app.services.learning_store import LearningStore


@pytest.mark.asyncio
async def test_learning_store_write_event_batches(tmp_path):
    store = LearningStore(db_path=tmp_path / "learning.db")
    batch_sizes: list[int] = []

    orig = store._flush_write_cmds_sync

    def wrapped(batch):
        batch_sizes.append(len(batch))
        return orig(batch)

    store._flush_write_cmds_sync = wrapped  # type: ignore[assignment]

    try:
        tasks = [
            asyncio.create_task(
                store.write_event(
                    event_type="run_tests",
                    agent_id="test",
                    project="supermemory",
                    transport="pytest",
                    context_signature="project=supermemory;task_type=test;phase=unit;category=learning;transport=pytest",
                    payload={"i": i},
                )
            )
            for i in range(500)
        ]
        ids = await asyncio.gather(*tasks)

        assert len(ids) == 500
        assert all(isinstance(i, int) for i in ids)
        assert len(set(ids)) == 500
        assert any(sz > 1 for sz in batch_sizes)
    finally:
        await store.aclose()
