# Диагностика и исправление CloudLLMGateway

## Текущая проблема

Ошибка в логах (`glm_mirror`):
```
RuntimeError: CloudLLMGateway: all configured models failed: 
```

## Причина

Сервис `glm_mirror` (фоновый планировщик) вызывает `get_cloud_gateway().generate()` с `mode="economy"`. 
Шлюз последовательно перебирает облачные модели, указанные в `.env`:

| Модель | Статус | API Key |
|--------|--------|---------|
| `deepseek-chat` | ✅ Включён | `sk-05a88...` |
| `glm-4.7` | ❌ Отключён в профилях (enabled=false) | `06e8491...` |
| `gemini-3-flash-preview` | ❌ Отключён в профилях (enabled=false) | `AIzaSyD...` |

Все три модели недоступны по разным причинам:
1. **deepseek-chat** — API-ключ может быть невалидным/просроченным
2. **glm-4.7** — отключён через `CLOUD_LLM_MODEL_PROFILES` (`"enabled":false`)
3. **gemini-3-flash-preview** — отключён через профили + модель `gemini-3-flash-preview` не существует (актуальная: `gemini-2.0-flash`)

Локальный fallback через Ollama (`qwen3:1.7b`) тоже не работает, если Ollama не запущена на хосте.

## План исправления

### Шаг 1: Отключить GLM Mirror (временно, пока не настроены валидные ключи)

Добавить в `.env`:
```ini
GLM_MIRROR_INTERVAL_HOURS=0
```

Это остановит фоновые попытки вызова LLM.

### Шаг 2: Проверить и обновить API-ключи


Модель `gemini-3-flash-preview` не существует. Актуальная: `gemini-2.0-flash` или `gemini-2.5-flash-preview-04-17`.
Если нужно использовать Gemini, обновить:
```ini
GEMINI_MODEL=gemini-2.0-flash
```
и включить в профилях:
```json
{"gemini-2.0-flash": {"provider":"gemini","model":"gemini-2.0-flash","enabled":true}}
```

### Шаг 3: Исправить error message в коде (опционально)

В файле `app/services/llm_gateway.py`, строка 332:
```python
raise RuntimeError(f"CloudLLMGateway: all configured models failed: {last_error}")
```
заменить на:
```python
raise RuntimeError(
    f"CloudLLMGateway: all configured models failed: {describe_cloud_error(last_error) if last_error else 'unknown'}"
)
```

Это гарантирует, что сообщение об ошибке никогда не будет пустым.

### Шаг 4: Включить GLM Mirror обратно

После настройки хотя бы одного рабочего ключа, вернуть:
```ini
GLM_MIRROR_INTERVAL_HOURS=0.1667
```

### Шаг 5: Перезапустить контейнер

```bash
docker compose -f docker-compose.yml restart memory-server-dev
```

## Приоритет

1. **Сначала Шаг 1** — остановить спам в логах (безопасно, обратно совместимо)
2. **Шаг 2** — проверить и обновить ключи
3. **Шаг 3** — улучшить error message (повышает диагностируемость)
4. **Шаги 4-5** — включить mirror и перезапустить
