# Упрощенная стратегия внедрения для MCP проекта

## Контекст

В SuperMemory основными пользователями являются AI агенты через MCP tools. Это значительно упрощает roll-out стратегию:
- ✅ Агенты автоматически используют новые tools
- ✅ Нет необходимости в canary/gradual rollout
- ✅ Legacy endpoints можно удалить сразу после добавления новых tools
- ✅ Migration guide нужен только для документации

## Упрощенная стратегия

### Шаг 1: Добавить новые MCP tools

```python
# mcp/server.py

# Новые unified tools
{
    "name": "get_artifact",
    "description": "Get a unified artifact (improvement or task) by artifact_key. Format: {type}:{project}:{local_id}",
    "inputSchema": {
        "type": "object",
        "properties": {
            "artifact_key": {
                "type": "string",
                "description": "Artifact key in format: improvement:supermemory:abc or task:supermemory:def"
            }
        },
        "required": ["artifact_key"]
    }
}

{
    "name": "list_artifacts",
    "description": "List unified artifacts with optional filtering",
    "inputSchema": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "Project name (default: supermemory)"
            },
            "status": {
                "type": "string",
                "description": "Filter by status (open, done, etc.)"
            },
            "type": {
                "type": "string",
                "description": "Filter by type (improvement, task, or null for both)"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results (default: 50)"
            }
        }
    }
}
```

### Шаг 2: Обновить agent guidance

```python
# Обновить get_started сообщение
def get_started_message():
    return """
    **Action Required:** If you're starting a new session, run these commands now:

    1. List open tasks: `list_artifacts(project="supermemory", status="open", type=None)`
    2. Search project knowledge: Search memories for 'architecture', 'docs', or 'components'

    **Unified Artifact API:**
    - Use `get_artifact(artifact_key)` to get any artifact by key
    - Use `list_artifacts(...)` to list artifacts with filtering
    - Artifact key format: `{type}:{project}:{local_id}`
      - Example: `improvement:supermemory:2e8fdc03-fc0b-4f77-bbaa-99f570e8894c`
      - Example: `task:supermemory:6174ad7b-1fd9-4b6b-bb59-4f932b8cfc8c`
    """
```

### Шаг 3: Удалить legacy MCP tools

```python
# Удалить из mcp/server.py:
# - list_improvements
# - resolve_improvement
# - (прямой доступ к /improvements и /project-tasks endpoints)

# Оставить только:
# - get_artifact
# - list_artifacts
# - resolve_artifact (новый)
# - reopen_artifact (новый)
```

### Шаг 4: Удалить legacy HTTP endpoints

```python
# Удалить из app/routers/improvements.py:
# - @router.get("") - list_improvements
# - @router.get("/{improvement_id}") - get_improvement
# - @router.patch("/{improvement_id}/resolve") - resolve_improvement

# Удалить из app/routers/project_tasks.py:
# - @router.get("") - list_tasks
# - @router.get("/{task_id}") - get_task
# - @router.post("/{task_id}/reopen") - reopen_task

# Оставить только unified endpoints:
# - @router.get("/artifacts") - list_artifacts
# - @router.get("/artifacts/{artifact_key}") - get_artifact
# - @router.post("/artifacts/{artifact_key}/resolve") - resolve_artifact
# - @router.post("/artifacts/{artifact_key}/reopen") - reopen_artifact
```

## Тестирование

### 1. Unit тесты

```python
# tests/test_unified_artifact_service.py
@pytest.mark.asyncio
async def test_get_artifact_improvement():
    """Получить improvement через unified API."""
    artifact_key = "improvement:supermemory:2e8fdc03-fc0b-4f77-bbaa-99f570e8894c"
    result = await unified_artifact_service.get_artifact(artifact_key)
    assert result.type == "improvement"
    assert result.status == "open"

@pytest.mark.asyncio
async def test_get_artifact_task():
    """Получить task через unified API."""
    artifact_key = "task:supermemory:6174ad7b-1fd9-4b6b-bb59-4f932b8cfc8c"
    result = await unified_artifact_service.get_artifact(artifact_key)
    assert result.type == "task"
    assert result.status == "active"

@pytest.mark.asyncio
async def test_list_artifacts_mixed():
    """Получить смешанный список improvements и tasks."""
    results = await unified_artifact_service.list_artifacts(
        project="supermemory",
        status="open",
        type=None,
        limit=50,
    )
    assert len(results) > 0
    assert any(r.type == "improvement" for r in results)
    assert any(r.type == "task" for r in results)
```

### 2. Интеграционные тесты с MCP

```python
# tests/test_mcp_unified_artifacts.py
@pytest.mark.asyncio
async def test_mcp_get_artifact():
    """Тест MCP tool get_artifact."""
    result = execute_tool("get_artifact", {
        "artifact_key": "improvement:supermemory:2e8fdc03-fc0b-4f77-bbaa-99f570e8894c"
    })
    assert result["type"] == "improvement"
    assert "artifact_key" in result

@pytest.mark.asyncio
async def test_mcp_list_artifacts():
    """Тест MCP tool list_artifacts."""
    result = execute_tool("list_artifacts", {
        "project": "supermemory",
        "status": "open",
        "limit": 10
    })
    assert "items" in result
    assert len(result["items"]) <= 10
```

### 3. E2E тест с агентом

```python
# tests/test_agent_workflow.py
@pytest.mark.asyncio
async def test_agent_uses_unified_api():
    """Тест: агент использует unified API для работы с задачами."""

    # Симуляция агента
    agent = Agent()

    # Агент получает список открытых задач
    tasks = await agent.list_open_tasks()
    assert len(tasks) > 0

    # Агент получает конкретную задачу
    task = await agent.get_task(tasks[0]["artifact_key"])
    assert task["artifact_key"] == tasks[0]["artifact_key"]

    # Агент разрешает задачу
    resolved = await agent.resolve_task(task["artifact_key"])
    assert resolved["status"] == "done"
```

## Мониторинг (упрощенный)

### Базовые метрики

```python
# app/monitoring/unified_artifacts.py
from prometheus_client import Counter, Histogram

unified_artifacts_requests_total = Counter(
    'unified_artifacts_requests_total',
    'Total requests to unified artifacts API',
    ['tool', 'status']
)

unified_artifacts_request_duration = Histogram(
    'unified_artifacts_request_duration_seconds',
    'Request duration for unified artifacts API',
    ['tool'],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0]
)
```

### Базовые алерты

```yaml
# prometheus/alerts.yml
groups:
  - name: unified_artifacts
    rules:
      - alert: UnifiedArtifactsHighErrorRate
        expr: |
          rate(unified_artifacts_requests_total{status=~"5.."}[5m])
          /
          rate(unified_artifacts_requests_total[5m])
          > 0.01
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High error rate in unified artifacts API"
```

## Rollback (упрощенный)

```python
# Если что-то пошло не так:

# 1. Восстановить legacy MCP tools
git checkout HEAD~1 -- mcp/server.py

# 2. Восстановить legacy HTTP endpoints
git checkout HEAD~1 -- app/routers/improvements.py app/routers/project_tasks.py

# 3. Перезапустить сервис
systemctl restart supermemory-api

# 4. Верифицировать
curl -X GET /improvements  # Должен работать
```

## План внедрения (1 день)

| Время | Действие |
|-------|----------|
| 09:00-10:00 | Реализовать UnifiedArtifactService и модели |
| 10:00-11:00 | Создать unified HTTP endpoints |
| 11:00-12:00 | Добавить новые MCP tools |
| 12:00-13:00 | Обед |
| 13:00-14:00 | Написать unit тесты |
| 14:00-15:00 | Написать интеграционные тесты с MCP |
| 15:00-16:00 | Запустить все тесты |
| 16:00-16:30 | Удалить legacy MCP tools и HTTP endpoints |
| 16:30-17:00 | Деплой и верификация |

## Критерии готовности

1. ✅ Unit тесты проходят
2. ✅ Интеграционные тесты с MCP проходят
3. ✅ E2E тест с агентом проходит
4. ✅ Новые MCP tools работают
5. ✅ Legacy endpoints удалены
6. ✅ Агенты используют новый API

## Документация

### Для агентов (в get_started message)

```markdown
## Unified Artifact API

Use these tools to work with improvements and tasks:

### List artifacts
```
list_artifacts(project="supermemory", status="open", type=None, limit=50)
```

### Get artifact
```
get_artifact(artifact_key="improvement:supermemory:abc")
```

### Resolve artifact
```
resolve_artifact(artifact_key="improvement:supermemory:abc", acted_by="agent", reason="Completed")
```

### Reopen artifact
```
reopen_artifact(artifact_key="task:supermemory:def", acted_by="agent", reason="More work needed")
```

**Artifact key format:** `{type}:{project}:{local_id}`
- `improvement:supermemory:2e8fdc03-fc0b-4f77-bbaa-99f570e8894c`
- `task:supermemory:6174ad7b-1fd9-4b6b-bb59-4f932b8cfc8c`
```

### Для разработчиков (README)

```markdown
## Unified Artifacts API

The unified API provides a single interface for working with both improvements and tasks.

### Endpoints

- `GET /artifacts` - List artifacts
- `GET /artifacts/{artifact_key}` - Get artifact
- `POST /artifacts/{artifact_key}/resolve` - Resolve artifact
- `POST /artifacts/{artifact_key}/reopen` - Reopen artifact

### Migration

Legacy endpoints have been removed. Use the unified API instead:

| Legacy | Unified |
|--------|---------|
| `GET /improvements` | `GET /artifacts?type=improvement` |
| `GET /project-tasks` | `GET /artifacts?type=task` |
| `PATCH /improvements/{id}/resolve` | `POST /artifacts/improvement:{project}:{id}/resolve` |
```

## Заключение

Для MCP проекта стратегия значительно упрощается:
- ✅ Нет canary/gradual rollout
- ✅ Нет deprecation period
- ✅ Агенты автоматически используют новые tools
- ✅ Legacy endpoints можно удалить сразу
- ✅ Внедрение за 1 день

Ключевые фокусы:
1. Правильная реализация unified API
2. Полное покрытие тестами
3. Обновление agent guidance
4. Базовый мониторинг
