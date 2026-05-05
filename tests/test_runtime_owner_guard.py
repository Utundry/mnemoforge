from pathlib import Path

import pytest

from app.services.runtime_owner_guard import RuntimeOwnershipError, acquire_runtime_ownership


def test_runtime_owner_guard_blocks_second_active_owner(tmp_path: Path):
    first = acquire_runtime_ownership(
        data_dir=tmp_path,
        runtime_kind="docker",
        enabled=True,
        stale_seconds=120,
    )
    assert first is not None
    try:
        with pytest.raises(RuntimeOwnershipError):
            acquire_runtime_ownership(
                data_dir=tmp_path,
                runtime_kind="host",
                enabled=True,
                stale_seconds=120,
            )
    finally:
        first.close()


def test_runtime_owner_guard_allows_explicit_takeover(tmp_path: Path):
    first = acquire_runtime_ownership(
        data_dir=tmp_path,
        runtime_kind="docker",
        enabled=True,
        stale_seconds=120,
    )
    assert first is not None
    try:
        second = acquire_runtime_ownership(
            data_dir=tmp_path,
            runtime_kind="host",
            enabled=True,
            allow_takeover=True,
            stale_seconds=120,
        )
        assert second is not None
        second.close()
    finally:
        first.close()


def test_runtime_owner_guard_ignores_disabled_guard(tmp_path: Path):
    assert acquire_runtime_ownership(data_dir=tmp_path, enabled=False) is None
