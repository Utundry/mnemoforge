from __future__ import annotations

import pytest

from scripts.testing_guard import (
    LEGACY_TEST_TARGETS_ENV,
    LEGACY_UNSAFE_OVERRIDE_ENV,
    LIVE_TARGETS_ENV,
    TEST_TARGETS_ENV,
    UNSAFE_OVERRIDE_ENV,
    UnsafeTestTargetError,
    assert_db_backed_test_target,
    is_allowed_db_test_target,
    is_live_like_target,
)


@pytest.fixture(autouse=True)
def _configured_test_contour(monkeypatch):
    monkeypatch.setenv(
        TEST_TARGETS_ENV,
        "http://memory-server-test:8000,http://localhost:8010,http://127.0.0.1:8010",
    )
    monkeypatch.setenv(LIVE_TARGETS_ENV, "http://localhost:8000,http://127.0.0.1:8000,http://localhost:8001")


@pytest.mark.parametrize(
    "url",
    [
        "http://memory-server-test:8000",
        "http://localhost:8010",
        "http://127.0.0.1:8010",
    ],
)
def test_db_backed_guard_allows_declared_test_targets(url: str):
    assert is_allowed_db_test_target(url)
    assert_db_backed_test_target(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8001",
    ],
)
def test_db_backed_guard_rejects_live_like_targets(url: str):
    assert is_live_like_target(url)
    with pytest.raises(UnsafeTestTargetError) as exc_info:
        assert_db_backed_test_target(url, context="integration test")

    message = str(exc_info.value)
    assert "integration test refused" in message
    assert "MNEMOFORGE_ALLOW_UNSAFE_LIVE_TESTS" in message


def test_db_backed_guard_rejects_unknown_targets():
    with pytest.raises(UnsafeTestTargetError) as exc_info:
        assert_db_backed_test_target("http://example.test:8000")

    assert "outside the declared Docker test contour" in str(exc_info.value)


def test_db_backed_guard_allows_explicit_unsafe_override(monkeypatch):
    monkeypatch.setenv(UNSAFE_OVERRIDE_ENV, "1")
    assert_db_backed_test_target("http://localhost:8000")


def test_db_backed_guard_keeps_legacy_env_aliases(monkeypatch):
    monkeypatch.delenv(TEST_TARGETS_ENV)
    monkeypatch.setenv(LEGACY_TEST_TARGETS_ENV, "http://legacy-test:8000")
    assert_db_backed_test_target("http://legacy-test:8000")

    monkeypatch.setenv(LEGACY_UNSAFE_OVERRIDE_ENV, "1")
    assert_db_backed_test_target("http://localhost:8000")


def test_db_backed_guard_requires_declared_test_targets(monkeypatch):
    monkeypatch.delenv(TEST_TARGETS_ENV)
    with pytest.raises(UnsafeTestTargetError) as exc_info:
        assert_db_backed_test_target("http://localhost:8010")

    assert "no allowed DB test targets are configured" in str(exc_info.value)
