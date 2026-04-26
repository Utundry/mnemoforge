from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.knowledge_language import canonicalize_agent_fields_to_english, looks_like_english_text
from app.services.text_localization import normalize_text_for_display


def test_looks_like_english_text_detects_machine_friendly_english() -> None:
    assert looks_like_english_text("Use unified retrieval before reading code.") is True
    assert looks_like_english_text("\u0420\u045c\u0421\u0453\u0420\u0455\u0420\u00b6\u0420\u00b5\u0420\u0405 law review") is False


@pytest.mark.asyncio
async def test_canonicalize_agent_fields_translates_non_english_with_cloud(monkeypatch) -> None:
    monkeypatch.setattr("app.services.knowledge_language.cloud_available", lambda: True)
    monkeypatch.setattr(
        "app.services.knowledge_language.cloud_complete",
        AsyncMock(return_value='{"title":"Storage architecture refactor","description":"Fix encoding and keep the task layer readable."}'),
    )

    translated = await canonicalize_agent_fields_to_english(
        {
            "title": "\u0420\u2018\u0421\u0402\u0420\u0406\u0421\u2026\u0420\u0451\u0421\u201a\u0420\u00b5\u0420\u0454\u0421\u201a\u0421\u0192\u0421\u0402\u0420\u0405\u0421\u2039\u0420\u2122 \u0421\u0402\u0420\u00b5\u0421\u201e\u0420\u00b0\u0420\u0454\u0421\u201a\u0420\u0455\u0421\u0402\u0420\u0451\u0420\u0405\u0420\u0453 \u0421\u2026\u0421\u0402\u0420\u00b0\u0420\u0405\u0420\u0451\u0420\u00bb\u0420\u0455\u0421\u2030",
            "description": "\u0420\u0458\u0421\u0403\u0421\u0402\u0420\u00b0\u0420\u0432\u0402\u017d\u0421\u201a\u0421\u0402 \u0420\u0454\u0420\u0455\u0420\u0491\u0420\u0451\u0421\u0402\u0420\u0455\u0420\u0406\u0420\u0454\u0421\u0192 \u0420\u0451 \u0421\u0403\u0420\u0455\u0421\u2026\u0421\u0402\u0420\u00b0\u0420\u0405\u0420\u0451\u0421\u201a\u0421\u0152 \u0421\u2021\u0420\u0451\u0421\u201a\u0420\u00b0\u0420\u00b5\u0420\u0458\u0421\u2039\u0420\u2122 task layer.",
        }
    )
    assert translated["title"] == "Storage architecture refactor"
    assert translated["description"] == "Fix encoding and keep the task layer readable."


@pytest.mark.asyncio
async def test_canonicalize_agent_fields_keeps_original_without_cloud(monkeypatch) -> None:
    monkeypatch.setattr("app.services.knowledge_language.cloud_available", lambda: False)
    source = "\u0420\u045c\u0421\u0453\u0420\u0455\u0420\u00b6\u0420\u00b5\u0420\u0405 law review"
    translated = await canonicalize_agent_fields_to_english({"title": source})
    assert translated["title"] == normalize_text_for_display(source)


@pytest.mark.asyncio
async def test_canonicalize_agent_fields_skips_cloud_when_disabled(monkeypatch) -> None:
    cloud_mock = AsyncMock(return_value='{"title":"Law review needed"}')
    monkeypatch.setattr("app.services.knowledge_language.cloud_available", lambda: True)
    monkeypatch.setattr("app.services.knowledge_language.cloud_complete", cloud_mock)
    source = "\u0420\u045c\u0421\u0453\u0420\u0455\u0420\u00b6\u0420\u00b5\u0420\u0405 law review"

    translated = await canonicalize_agent_fields_to_english(
        {"title": source},
        allow_cloud=False,
    )

    assert translated["title"] == normalize_text_for_display(source)
    cloud_mock.assert_not_awaited()
