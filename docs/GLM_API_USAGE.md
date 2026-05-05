# GLM/Zhipu AI API Integration

Это руководство объясняет, как использовать API от z.ai (GLM/Zhipu AI) в проекте MnemoForge.

## Содержание

- [Получение API ключа](#получение-api-ключа)
- [Настройка](#настройка)
- [Использование в коде](#использование-в-коде)
- [Доступные модели](#доступные-модели)
- [Примеры](#примеры)
- [Тестирование](#тестирование)
- [Устранение проблем](#устранение-проблем)

## Получение API ключа

1. Перейдите на официальный сайт: https://open.bigmodel.cn/
2. Зарегистрируйтесь или войдите в аккаунт
3. Создайте новый API ключ в разделе API Keys
4. Сохраните ключ — он понадобится для настройки

## Настройка

### Шаг 1: Отредактируйте файл `.env`

Добавьте следующие переменные в ваш `.env` файл:

```bash
# Cloud LLM - GLM (Zhipu AI / z.ai)
GLM_API_KEY=ваш_ключ_здесь
GLM_MODEL=glm-4.5-air
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
```

### Шаг 2: Проверьте конфигурацию

```python
from app.services.cloud_llm import cloud_available

if cloud_available():
    print("✅ GLM API настроен и готов к работе!")
else:
    print("❌ GLM API не настроен. Проверьте GLM_API_KEY в .env")
```

## Использование в коде

### Базовое использование

```python
from app.services.cloud_llm import cloud_complete, cloud_available

async def example_usage():
    if not cloud_available():
        # Fallback на локальный Ollama
        return "Используем локальную модель"
    
    result = await cloud_complete(
        prompt="Ваш запрос здесь",
        system="Вы полезный AI-ассистент",
        max_tokens=2048,
        temperature=0.3
    )
    return result
```

### Параметры функции `cloud_complete`

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|--------------|----------|
| `prompt` | str | **обязательно** | Основной запрос к модели |
| `system` | str | "You are a helpful assistant." | Системный промпт |
| `max_tokens` | int | 2048 | Максимальное количество токенов в ответе |
| `temperature` | float | 0.3 | Креативность (0.0 - 1.0) |
| `timeout` | float | 60.0 | Тайм-аут запроса в секундах |

## Доступные модели

GLM предлагает несколько моделей с разными характеристиками:

| Модель | Описание | Скорость | Стоимость | Лучше подходит для |
|--------|----------|----------|-----------|---------------------|
| `glm-4.5-air` | Быстрая и экономичная | ⚡⚡⚡ | 💰 | Общие задачи, чат-боты |
| `glm-4.5-flash` | Самая быстрая | ⚡⚡⚡⚡ | 💰💰 | Быстрые ответы |
| `glm-4-plus` | Максимальная мощность | ⚡⚡ | 💰💰💰 | Сложные задачи, анализ |

**Рекомендация:** Используйте `glm-4.5-air` по умолчанию — отличный баланс скорости и качества.

## Примеры

### Пример 1: Генерация навыка

```python
from app.services.cloud_llm import cloud_complete

async def create_skill_description():
    skill_prompt = """
    Создай описание навыка "анализ кода":
    1. Назначение
    2. Основные шаги
    3. Рекомендации
    """
    
    description = await cloud_complete(
        prompt=skill_prompt,
        system="Ты эксперт по созданию AI-навыков.",
        max_tokens=500,
        temperature=0.5
    )
    
    return description
```

### Пример 2: Краткое содержание

```python
async def summarize_text(text: str) -> str:
    summary = await cloud_complete(
        prompt=f"Сделай краткое содержание этого текста:\n\n{text}",
        system="Ты профессиональный редактор.",
        max_tokens=300,
        temperature=0.3
    )
    return summary
```

### Пример 3: Кодогенерация

```python
async def generate_code(task: str) -> str:
    code = await cloud_complete(
        prompt=f"Напиши код на Python для: {task}",
        system="Ты опытный программист на Python. Пиши только код, без комментариев.",
        max_tokens=1000,
        temperature=0.1  # Низкая температура для более детерминированного кода
    )
    return code
```

### Пример 4: Многоязычный перевод

```python
async def translate_text(text: str, target_lang: str) -> str:
    translation = await cloud_complete(
        prompt=f"Переведи этот текст на {target_lang}:\n{text}",
        system="Ты профессиональный переводчик.",
        max_tokens=500,
        temperature=0.2
    )
    return translation
```

## Тестирование

Проект включает тестовый скрипт для проверки подключения:

```bash
python scripts/test_glm_api.py
```

Этот скрипт проверяет:
1. Доступность API
2. Базовый запрос
3. Генерацию навыка
4. Различные параметры (температура)

## Устранение проблем

### Проблема: "No cloud LLM configured"

**Причина:** Не задан `GLM_API_KEY` в `.env` файле.

**Решение:**
```bash
# Добавьте в .env
GLM_API_KEY=ваш_ключ
```

### Проблема: Timeout или медленные ответы

**Причины:**
1. Медленное интернет-соединение
2. Высокая нагрузка на API
3. Большой `max_tokens`

**Решения:**
- Уменьшите `max_tokens` (например, до 1024)
- Увеличьте `timeout` (например, до 120.0)
- Проверьте интернет-соединение

### Проблема: Некачественные ответы

**Причины:**
1. Неоптимальный `temperature`
2. Плохой системный промпт
3. Не подходящая модель

**Решения:**
- Для точных ответов используйте `temperature=0.1-0.3`
- Для креативных задач используйте `temperature=0.7-1.0`
- Улучшите системный промпт с примерами
- Попробуйте другую модель (например, `glm-4-plus`)

### Проблема: API ключ не работает

**Причины:**
1. Неправильный ключ
2. Ключ был отозван
3. Превышен лимит запросов

**Решения:**
- Проверьте, что ключ скопирован полностью
- Создайте новый ключ на https://open.bigmodel.cn/
- Проверьте баланс и лимиты на сайте

## Интеграция с существующими модулями

GLM API автоматически используется в следующих модулях (если настроен):

1. **`app/services/ai_dir_parser.py`** — парсинг директорий
2. **`app/services/skill_crystallizer.py`** — кристаллизация навыков
3. **`app/services/task_router.py`** — маршрутизация задач
4. **`app/services/normalization_service.py`** — нормализация данных

Эти модули используют fallback-механизм: если GLM недоступен, они переключаются на локальный Ollama.

## Дополнительные ресурсы

- [Официальная документация GLM](https://open.bigmodel.cn/dev/api)
- [Список доступных моделей](https://open.bigmodel.cn/dev/api#models)
- [Цены и лимиты](https://open.bigmodel.cn/pricing)

## Поддержка

Если у вас возникли проблемы:
1. Проверьте логи в `server.log`
2. Запустите тестовый скрипт: `python scripts/test_glm_api.py`
3. Проверьте настройки в `.env`
4. Обратитесь к официальной документации GLM
