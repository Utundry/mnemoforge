from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.text_localization import repair_mojibake

PREFIX = "/api/v1"


def test_repair_mojibake_recovers_windows_utf8_cp1251_mix():
    original = (
        "\u0412\u043e\u0437\u043c\u043e\u0436\u043d\u044b\u0435 "
        "\u043f\u0440\u0438\u0447\u0438\u043d\u044b: "
        "\u0441\u0435\u0440\u0432\u0435\u0440 \u043d\u0435 "
        "\u0437\u0430\u043f\u0443\u0449\u0435\u043d"
    )
    mojibake = original.encode("utf-8").decode("cp1251")
    assert repair_mojibake(mojibake) == original


@pytest.mark.asyncio
async def test_artifact_translation_endpoint_matches_tree_style_contract(client, tmp_path):
    broken_russian = (
        "\u0412\u043e\u0437\u043c\u043e\u0436\u043d\u044b\u0435 "
        "\u043f\u0440\u0438\u0447\u0438\u043d\u044b: "
        "\u0441\u0435\u0440\u0432\u0435\u0440 \u043d\u0435 "
        "\u0437\u0430\u043f\u0443\u0449\u0435\u043d"
    ).encode("utf-8").decode("cp1251")
    convo = tmp_path / "session.jsonl"
    messages = [
        {"role": "user", "content": "Please inspect supermemory terminology handling."},
        {"role": "assistant", "content": broken_russian},
    ]
    convo.write_text("\n".join(json.dumps(m) for m in messages), encoding="utf-8")

    signal = json.dumps({
        "new_terminology": ["supermemory"],
        "missing_skill": [],
        "domain_drift": [],
        "user_preference": [],
        "successful_pattern": [],
    })

    async def fake_cloud_complete(prompt: str, **kwargs):
        if "Translate these artifact fields" in prompt:
            return json.dumps({
                "content": "Новый термин замечен: 'supermemory'. Стоит оформить как improvement.",
                "observation": "Анализ диалога выявил новый термин 'supermemory'. Фрагмент: Возможные причины: сервер не запущен",
                "why_it_matters": "Фиксация терминологии уменьшает неоднозначность и ускоряет review.",
            }, ensure_ascii=False)
        raise AssertionError(f"Unexpected cloud prompt: {prompt[:120]}")

    with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal)), \
         patch("app.services.cloud_llm.cloud_available", return_value=True), \
         patch("app.services.cloud_llm.cloud_complete", new=AsyncMock(side_effect=fake_cloud_complete)):
        response = await client.post(f"{PREFIX}/watcher/scan", json={
            "dirs": [str(tmp_path)],
            "agent_id": "watcher-dialogue-i18n",
            "max_files": 50,
            "dry_run": False,
        })
        assert response.status_code == 200

        artifacts = await client.get("/api/v1/learning/artifacts?scope=candidate&status=pending_review&limit=50")
        assert artifacts.status_code == 200
        pending = artifacts.json()["artifacts"]
        localized = next(item for item in pending if "dialogue-analysis" in (item.get("tags") or []))

        assert "\u0412\u043e\u0437\u043c\u043e\u0436\u043d\u044b\u0435 \u043f\u0440\u0438\u0447\u0438\u043d\u044b" in localized["observation"]
        assert "Р’РѕР·РјРѕР¶" not in localized["observation"]
        assert localized["display_content"].startswith("New term 'supermemory'")

        translated = await client.get(f"/api/v1/learning/artifacts/{localized['id']}/translate")
        assert translated.status_code == 200
        body = translated.json()
        assert body["original"].startswith("New term 'supermemory'")
        assert body["translated"].startswith("\u041d\u043e\u0432\u044b\u0439 \u0442\u0435\u0440\u043c\u0438\u043d \u0437\u0430\u043c\u0435\u0447\u0435\u043d")
        assert body["translated_observation"].startswith("\u0410\u043d\u0430\u043b\u0438\u0437 \u0434\u0438\u0430\u043b\u043e\u0433\u0430")
        assert body["translated_why_it_matters"].startswith("\u0424\u0438\u043a\u0441\u0430\u0446\u0438\u044f \u0442\u0435\u0440\u043c\u0438\u043d\u043e\u043b\u043e\u0433\u0438\u0438")
