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
                    project="mnemoforge",
                    transport="pytest",
                    context_signature="project=mnemoforge;task_type=test;phase=unit;category=learning;transport=pytest",
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


@pytest.mark.asyncio
async def test_learning_store_write_event_recovers_from_leaked_transaction(tmp_path):
    store = LearningStore(db_path=tmp_path / "learning.db")
    try:
        with store._lock:
            store._conn.execute("BEGIN")

        row_id = await store.write_event(
            event_type="session_outcome",
            agent_id="test",
            project="mnemoforge",
            transport="pytest",
            context_signature="project=mnemoforge;task_type=test;phase=unit;category=learning;transport=pytest",
            payload={"ok": True},
        )

        assert isinstance(row_id, int)
        events = await store.list_events(event_type="session_outcome", limit=5)
        assert [event["id"] for event in events] == [row_id]
    finally:
        await store.aclose()
