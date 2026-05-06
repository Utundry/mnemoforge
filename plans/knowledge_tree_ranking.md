# Improvement: Knowledge Tree Ranking

**Status:** Open  
**Priority:** High  
**Created:** 2025-03-17  
**Created:** 2025-03-17 (Updated: 2026-03-20)  
**Agent:** architect

## Описание

### Проблема
Текущая система semantic search в mnemoforge использует плоскую модель хранения и поиска знаний. Все воспоминания хранятся с одинаковым приоритетом, и поиск возвращает результаты, отсортированные только по семантической близости и важности.

Это приводит к проблемам:
- Отсутствие контекстной иерархии (от общего к частному)
- Смешение фундаментальных и специфических знаний
- Неэффективный поиск для сложных запросов
- Трудности в организации больших баз знаний

Помимо этого, первоначальный архитектурный план содержал антипаттерны: предложение хранить структуру графа прямо в записях `Memory` в Qdrant (что ведет к дублированию данных и высоким затратам на обновление) и жесткую привязку моделей без учета фоллбэков.

### Концептуальное решение: Дерево знаний

Внедрить иерархическую структуру организации знаний по аналогии с живым деревом:

- **Ствол (Trunk)** — фундаментальные, общие знания (уровень 1)
- **Ветви (Branches)** — доменные, специализированные знания (уровень 2)
- **Подветви (Subbranches)** — узкие специализации (уровень 3)
- **Листья (Leaves)** — конкретные факты и детали (уровень 4)

**Срез дерева** — это контекстный запрос, который возвращает знания от ствола к листьям, обеспечивая естественный переход от общего к частному.

### Техническое решение: Разделение слоёв + Теневая оценка
1. **Строгое разделение (Separation of Concerns):** Qdrant отвечает только за векторы и плоские метаданные (переиспользуем поле `topic_path`). Структура графа дерева, веса веток и статистика роутинга хранятся в отдельной легковесной базе SQLite (`knowledge_tree.db`).
2. **Multi-LLM Gateway:** Использование пула облачных моделей (Primary + Fallbacks, настраиваемых через `.env`) для High Availability (высокой доступности) и консенсуса.
3. **Adaptive Query Routing (Теневая оценка):** Быстрые локальные модели (SLM) классифицируют запросы в реальном времени. В фоне (через `JobQueue`) облачные LLM проверяют ответы SLM. При совпадении SLM повышает свой внутренний рейтинг, при критическом несовпадении система обучается отправлять похожие запросы сразу в облако.

## Архитектура

### 1. Расширение модели Memory
### 1. Слой данных

**A. Слой записей (Qdrant / Pydantic - `app/models/memory.py`)**
Изменения минимальны, мы переиспользуем существующее поле:
```python
# app/models/memory.py
class Memory(BaseModel):
    # ... существующие поля ...
    
    # Новые поля для дерева
    category_path: str = "general"  # "programming/python/web/fastapi"
    tree_depth: int = 1
    parent_category: Optional[str] = None
    child_categories: list[str] = []
    tree_strength: float = 0.5  # вес ветки
class MemoryRecord(BaseModel):
    # ...
    topic_path: Optional[str] = None  # Указатель на узел дерева: "python/fastapi/auth"
```

### 2. Класс MemoryCategory
**B. Слой графа и маршрутизации (SQLite - `knowledge_tree.db`)**
```python
class TreeNode(BaseModel):
    path: str              # PK: "python/fastapi/auth"
    parent_path: str       # "python/fastapi"
    level: int             # 3
    strength: float        # 0.0 - 1.0 (увеличивается при доступе)
    access_count: int      
    last_accessed: datetime
    is_locked: bool

class RoutingRule(BaseModel):
    pattern: str           # Извлеченный паттерн (e.g. "jwt auth")
    slm_successes: int
    slm_failures: int
    requires_llm: bool     # True, если локальная модель часто ошибается
```

### 2. Сервис CloudLLMGateway
Шлюз для отказоустойчивой работы с облачными моделями:
```python
# app/models/memory.py
class MemoryCategory:
    """
    Иерархическая категория знаний.
    Пример: programming/python/web/fastapi
    """
    
    def __init__(self, path: str):
        self.path = path
        self.depth = len(path.split("/"))
    
    @property
    def trunk(self) -> str:
        """Ствол — уровень 1"""
        return self.path.split("/")[0]
    
    @property
    def branch(self) -> str:
        """Ветка — уровень 2"""
        return self.path.split("/")[1] if self.depth >= 2 else None
    
    @property
    def subbranch(self) -> str:
        """Подветка — уровень 3"""
        return self.path.split("/")[2] if self.depth >= 3 else None
class CloudLLMGateway:
    def __init__(self):
        self.primary_model = os.getenv("PRIMARY_CLOUD_LLM", "glm-4.5")
        self.fallback_models = parse_env("FALLBACK_CLOUD_LLMS")

    async def generate(self, prompt: str, require_consensus: bool = False) -> str:
        # Логика автоматического переключения при таймаутах (Cascade)
        # Либо Majority Vote (голосование 2 из 3) для опасных операций с деревом
```

### 3. Сервис KnowledgeTree
### 3. Сервис KnowledgeTree и Shadow Evaluation

```python
# app/services/knowledge_tree.py
class KnowledgeTree:
    """
    Управляет иерархическим поиском знаний.
    """
    
    def slice_tree(
        self, 
        query: str, 
        max_depth: int = 4,
        agent_id: str
    ) -> list[Memory]:
        """
        Возвращает срез дерева: от ствола до конкретного листа.
        """
        # 1. Классифицировать запрос
        category = self._classify_query(query)
    async def _classify_query_adaptive(self, query: str) -> str:
        # 1. Спрашиваем SQLite (RoutingRule), справляется ли тут SLM?
        if self._routing_repo.requires_llm(query):
            return await self.llm_gateway.generate(query)
            
        # 2. Быстрый путь
        slm_category = await self.slm.generate(query)
        
        # 2. Построить путь среза
        path = self._build_slice_path(category, max_depth)
        
        # 3. Получить знания для каждого уровня
        results = []
        for level_path in path:
            memories = self._search_by_category(level_path, query, agent_id)
            results.extend(memories)
        
        # 4. Ранжировать по близости к листьям
        return self._rank_by_tree_depth(results, category)
    
    def _classify_query(self, query: str) -> str:
        """
        Классифицирует запрос в иерархическую категорию.
        Использует LLM для многоуровневой классификации.
        """
        prompt = f"""
        Классифицируй запрос в иерархическую категорию (макс. 4 уровня).
        Формат: уровень1/уровень2/уровень3/уровень4
        
        Примеры:
        "FastAPI endpoint error" → programming/python/web/fastapi
        "UI button design" → design/ui/components/button
        "Sales strategy Q4" → business/sales/strategy/q4
        
        Запрос: {query}
        """
        # Вызов LLM через task_router
        pass
        # 3. Теневая оценка (не блокирует ответ)
        await self.job_queue.submit("verify_tree_classification", {
            "query": query, "slm_category": slm_category
        })
        return slm_category
```

### 4. API Endpoint

```python
# app/routers/memories.py
@router.post("/memories/tree-slice")
async def tree_slice(
    query: str,
    max_depth: int = 4,
    agent_id: str,
    limit: int = 20
):
    """
    Возвращает срез дерева знаний для запроса.
    """
    tree = get_knowledge_tree()
    slice_result = tree.slice_tree(query, max_depth, agent_id)
    
    return {
        "query": query,
        "category": tree._classify_query(query),
        "slice": [
            {
                "level": i + 1,
                "category": mem.category_path,
                "content": mem.content,
                "score": mem.tree_score
            }
            for i, mem in enumerate(slice_result[:limit])
        ]
    }
```

### 5. MCP Tool

```python
# mcp/server.py
TOOLS.append({
    "name": "memory_tree_slice",
    "description": (
        "Search semantic memory using hierarchical tree structure. "
        "Returns knowledge slice from trunk (general) to leaves (specific). "
        "Provides context from general to specific knowledge."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["query", "agent_id"],
        "properties": {
            "query": {"type": "string"},
            "agent_id": {"type": "string"},
            "max_depth": {"type": "integer", "default": 4, "minimum": 1, "maximum": 6},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
        },
    },
})
```

## Преимущества

1. **Контекстная релевантность** — естественный переход от общего к частному
2. **Эффективность поиска** — фильтрация по уровням иерархии
3. **Самоорганизация** — адаптивный рост и обрезка веток
4. **Интуитивная структура** — понятная метафора для пользователей

## План реализации

### Фаза 1: Базовая инфраструктура
- [ ] Расширить модель Memory с полями дерева
- [ ] Создать класс MemoryCategory
- [ ] Миграция существующих данных (автоматическая классификация)

### Фаза 2: Сервис KnowledgeTree
- [ ] Реализовать `_classify_query()` с LLM
- [ ] Реализовать `_build_slice_path()`
- [ ] Реализовать `_search_by_category()`
- [ ] Реализовать `_rank_by_tree_depth()`

### Фаза 3: API и интеграция
- [ ] Создать endpoint `/memories/tree-slice`
- [ ] Добавить MCP tool `memory_tree_slice`
- [ ] Интеграция с ContextService

### Фаза 4: Адаптивный рост
- [ ] Реализовать `grow_branch()`
- [ ] Реализовать `prune_dead_branches()`
- [ ] Реализовать `strengthen_frequent_paths()`
- [ ] Self-instrumentation для отслеживания роста

### Фаза 5: Тестирование и документация
- [ ] Unit тесты для KnowledgeTree
- [ ] Интеграционные тесты для API
- [ ] Документация по использованию
- [ ] Примеры и туториалы

## Технические детали

### Классификация запросов

Использовать `task_router` для выбора оптимальной модели:
- **SLM (qwen3:1.7b)** — для быстрой классификации
- **LLM (cloud)** — для сложных/многодоменных запросов

### Ранжирование

Комбинированный score:
```python
tree_score = 0.6 * path_similarity + 0.4 * depth_similarity
```

- `path_similarity` — Jaccard similarity путей категорий
- `depth_similarity` — близость по глубине к целевой категории

### Self-instrumentation

Отслеживать метрики:
- Частота использования каждой ветки
- Успешность классификации запросов
- Время выполнения среза
- Эффективность сжатия контекста

## Риски и mitigations

| Риск | Mitigation |
|------|-----------|
| Сложность классификации запросов | Использовать few-shot examples с LLM |
| Производительность при больших деревьях | Кэширование путей и индексация |
| Миграция существующих данных | Автоматическая классификация с ручной верификацией |
| Неправильная классификация | Feedback loop для коррекции |

## Связанные задачи

- [ ] Интеграция с ContextService для автоматического сжатия
- [ ] Визуализация дерева знаний (dashboard)
- [ ] Экспорт/импорт иерархии
- [ ] Поиск по поддеревьям
- [ ] Слияние/разделение веток

## Метрики успеха

- Время ответа на tree-slice < 500ms
- Точность классификации запросов > 85%
- Удовлетворённость пользователей (feedback) > 80%
- Снижение количества результатов для поиска > 40%
