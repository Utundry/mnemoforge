# Task Memory Capture Protocol — Постановка задачи

## Задача: Implement Task Memory Capture Protocol with local-first structured capture

**ID задачи:** `48a6a84a-77a6-453d-83bf-9f51ffa18325`
**ID улучшения:** `8a64be40-47dc-46e8-957a-4b511758d97b`
**Статус:** in_progress
**Проект:** mnemoforge

---

## Описание

Memory-first project understanding needs a disciplined task capture protocol, not only better retrieval. Add a first-class protocol that requires structured artifacts across task framing, planning, execution, verification, and closure/handoff.

---

## Текущее состояние (уже реализовано)

### 1. Модели данных ([`app/models/project_task.py`](app/models/project_task.py))

**Основные сущности:**
- `ProjectTaskRecord` — запись задачи
- `ProjectTaskChangeRecord` — изменение задачи
- `TaskStatementCurrentView` — текущее представление постановки задачи (включая `chosen_decisions`)
- `TaskStatementDiffView` — diff изменений
- `TaskStatementQualityView` — качество постановки
- `TaskCaptureCandidateRecord` — кандидат на захват артефакта
- `TaskCaptureReviewRecord` — кандидат со статусом
- `TaskStatementProjectionResponse` — проекция постановки задачи

### 2. Сервисы

**Task Statement Service** ([`app/services/task_statement_service.py`](app/services/task_statement_service.py))
- `build_task_statement_projection()` — построение проекции постановки задачи
- `_fetch_linked_improvement()` — получение связанного улучшения
- `_fetch_task_deferred_findings()` — получение отложенных находок
- `_fetch_task_capture_review()` — получение кандидатов на захват

**Task Capture Service** ([`app/services/task_capture_service.py`](app/services/task_capture_service.py))
- `build_task_capture_completion()` — построение завершения захвата задачи
- `_deterministic_candidates()` — детерминированные кандидаты
- `_local_candidates()` — локальные кандидаты (через SLM)
- `_missing_capture_fields()` — определение отсутствующих полей
- `_missing_after()` — определение отсутствующих после заполнения

**Task Capture Rules** ([`app/services/task_capture_rules.py`](app/services/task_capture_rules.py))
- `compute_task_statement_missing_artifacts()` — вычисление отсутствующих артефактов
- `compute_task_statement_incomplete()` — вычисление неполноты постановки
- `collect_labeled_task_statements()` — сбор помеченных утверждений

**Task Capture Review Service** ([`app/services/task_capture_review_service.py`](app/services/task_capture_review_service.py))
- `list_task_capture_candidates()` — список кандидатов
- `promote_task_capture_candidates()` — продвижение кандидатов

### 3. API Endpoints ([`app/routers/project_tasks.py`](app/routers/project_tasks.py))

- `POST /api/v1/projects/{project}/tasks/{task_id}/capture-candidates` — создание кандидатов
- `GET /api/v1/projects/{project}/tasks/{task_id}/capture-candidates` — получение кандидатов
- `POST /api/v1/projects/{project}/tasks/{task_id}/capture-candidates/promote` — продвижение кандидатов

### 4. Job Queue ([`app/main.py:570-598`](app/main.py:570))

- `task_capture_refresh` — фоновое обновление захвата задачи

### 5. Поддерживаемые типы артефактов

**Полностью реализованные:**
- `assumption` — предположение
- `constraint` — ограничение
- `definition_of_done` — определение выполненности
- `verification_result` — результат верификации
- `result_summary` — итоговый результат
- `handoff_summary` — итоги для передачи
- `deferred_finding` — отложенная находка (✅ реализовано через learning_store)
- `chosen_decision` — выбранное решение (✅ реализовано в TaskStatementCurrentView)

**Частично реализованные:**
- `task` — сама задача (базовый уровень) — ✅ базовая структура есть
- `task_change` — изменение задачи — ✅ реализовано через ProjectTaskChangeRecord

**Не реализованные:**
- `decision_candidate` — кандидат на решение
- `code_link` — ссылка на код
- `remaining_risk` — оставшийся риск

---

## Пробелы в текущей реализации

### 1. Неполный набор артефактов MVP (ЧАСТИЧНО ЗАКРЫТО)

**Статус:** 8 из 12 артефактов реализовано (67%)

**Реализовано:**
- ✅ `task` — базовая структура
- ✅ `assumption` — полный цикл захвата
- ✅ `constraint` — полный цикл захвата
- ✅ `definition_of_done` — полный цикл захвата
- ✅ `chosen_decision` — хранение в TaskStatementCurrentView
- ✅ `task_change` — ProjectTaskChangeRecord
- ✅ `deferred_finding` — полный цикл через learning_store
- ✅ `verification_result` — полный цикл захвата
- ✅ `result_summary` — полный цикл захвата
- ✅ `handoff_summary` — полный цикл захвата

**Отсутствует:**
- ❌ `decision_candidate` — нет отдельной модели и логики для кандидатов решений
- ❌ `code_link` — нет связи с кодом
- ❌ `remaining_risk` — нет отдельного артефакта

**Требуемые действия:**
1. Добавить модели для отсутствующих артефактов
2. Реализовать логику создания и хранения
3. Интегрировать в процесс захвата

### 2. Отсутствие разделения ролей (НЕ ЗАКРЫТО)

**Проблема:** Задача требует "cheap writers: main agent plus local SLM/background helper", но текущая реализация использует только одну модель.

**Текущая реализация:**
```python
_LOCAL_MODEL = os.getenv("LOCAL_GENERATE_MODEL", settings.learning_mirror_model or "qwen3:1.7b")
```

**Статус:** Нет конфигурации для разных ролей.

**Требуемые действия:**
1. Определить роли: main agent vs background helper
2. Настроить отдельные модели для разных ролей
3. Реализовать распределение задач между ролями

### 3. Отсутствие автоматического захвата на этапах задачи (ЧАСТИЧНО ЗАКРЫТО)

**Статус:** ✅ Автоматический захват реализован через job queue

**Реализовано:**
- ✅ Захват при создании задачи ([`project_tasks.py:84-90`](app/routers/project_tasks.py:84))
- ✅ Захват при добавлении изменения ([`project_tasks.py:130-136`](app/routers/project_tasks.py:130))
- ✅ Job queue handler для `task_capture_refresh` ([`main.py:570-598`](app/main.py:570))

**Текущая реализация:**
```python
await _enqueue_task_capture_refresh(
    queue,
    project=record.project,
    task_id=record.task_id,
    trigger="task_created",
    use_local_generation=record.status in {"active", "done"},
)
```

**Ограничения:**
- ⚠️ Нет явного event bus для расширения
- ⚠️ Триггеры захвата жёстко зашиты в коде
- ⚠️ Нет webhook поддержки для внешних систем

**Требуемые действия:**
1. Определить триггеры для каждого этапа
2. Реализовать автоматический захват при изменении статуса
3. Добавить webhook/hook систему для внешних систем

### 4. Отсутствие консолидации и разрешения конфликтов (НЕ ЗАКРЫТО)

**Проблема:** Задача требует "Reserve strong reasoning models for consolidation, conflict resolution, and promotion into governed project truth", но нет логики консолидации для task capture candidates.

**Примечание:** Консолидация существует для crystallization service, но не для task capture.

**Статус:** ❌ Нет консолидации для task capture candidates

### 5. Отсутствие интеграции с handoff completeness (НЕ ЗАКРЫТО)

**Проблема:** Задача требует "basis for handoff completeness", но нет явной проверки полноты при handoff.

**Статус:** ❌ Нет проверки полноты при создании handoff

### 6. Отсутствие интеграции с enrich-task quality (НЕ ЗАКРЫТО)

**Проблема:** Задача требует "basis for enrich-task quality", но нет явной связи с качеством обогащения.

**Статус:** ❌ Нет метрик качества задачи на основе артефактов

### 7. Отсутствие интеграции с memoir preconditions (НЕ ЗАКРЫТО)

**Проблема:** Задача требует "basis for memoir preconditions", но нет связи с сервисом мемуаров.

**Статус:** ❌ Нет проверки предусловий для создания мемуара

### 8. Отсутствие governance интеграции (НЕ ЗАКРЫТО)

**Проблема:** Задача требует "future memory-first governance", но нет интеграции с системой управления.

**Статус:** ❌ Нет правил governance на основе артефактов

---

## Целевая архитектура

### 1. Роли в протоколе захвата

```
┌─────────────────────────────────────────────────────────────┐
│                    Task Capture Protocol                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐         ┌──────────────────┐              │
│  │ Main Agent   │         │ Local SLM Helper │              │
│  │ (Cloud LLM)  │         │   (qwen3:1.7b)   │              │
│  └──────┬───────┘         └────────┬─────────┘              │
│         │                           │                        │
│         │ Complex reasoning         │ Cheap writers          │
│         │ Consolidation             │ Assumptions            │
│         │ Conflict resolution       │ Task changes           │
│         │ Promotion                 │ Deferred findings      │
│         │                           │ Verification notes     │
│         │                           │ Handoff summaries      │
│         │                           │                        │
│         └───────────┬───────────────┘                        │
│                     │                                        │
│                     ▼                                        │
│         ┌───────────────────────┐                          │
│         │  Task Capture Store   │                          │
│         │  (learning_store)     │                          │
│         └───────────┬───────────┘                          │
│                     │                                        │
│                     ▼                                        │
│         ┌───────────────────────┐                          │
│         │  Project Truth Store  │                          │
│         │  (memory_store)       │                          │
│         └───────────────────────┘                          │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 2. Жизненный цикл артефактов

```
┌─────────────────────────────────────────────────────────────┐
│                   Artifact Lifecycle                         │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  1. Creation (Local SLM)                                    │
│     └─> task_capture_candidate (status: active)             │
│                                                              │
│  2. Review (Human/Agent)                                    │
│     ├─> Promote ──> task_change (memory_store)              │
│     └─> Reject ──> task_capture_candidate (status: archived)│
│                                                              │
│  3. Consolidation (Cloud LLM)                               │
│     └─> Merge multiple candidates into canonical artifact   │
│                                                              │
│  4. Promotion (Governed Truth)                              │
│     └─> Canonical memory with governance metadata           │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 3. Этапы задачи и соответствующие артефакты

```
┌─────────────────────────────────────────────────────────────┐
│              Task Stages & Artifacts                         │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐  assumption, constraint,                   │
│  │  Framing    │  definition_of_done                        │
│  └─────────────┘                                            │
│         │                                                    │
│         ▼                                                    │
│  ┌─────────────┐  decision_candidate, chosen_decision        │
│  │  Planning   │  (if decisions needed)                      │
│  └─────────────┘                                            │
│         │                                                    │
│         ▼                                                    │
│  ┌─────────────┐  task_change, code_link                    │
│  │  Execution  │  (captured on each change)                  │
│  └─────────────┘                                            │
│         │                                                    │
│         ▼                                                    │
│  ┌─────────────┐  verification_result, remaining_risk        │
│  │ Verification│  (when status → done)                       │
│  └─────────────┘                                            │
│         │                                                    │
│         ▼                                                    │
│  ┌─────────────┐  result_summary, handoff_summary,          │
│  │  Handoff    │  deferred_finding (if applicable)           │
│  └─────────────┘                                            │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## План реализации (обновлённый)

### Этап 1: Дополнение набора артефактов (Priority: HIGH)

**Статус:** 8/12 реализовано, нужно добавить 3 типа

**Задачи:**
1. Добавить модели для отсутствующих артефактов:
   - `DecisionCandidateRecord` — кандидат на решение
   - `CodeLinkRecord` — ссылка на код
   - `RemainingRiskRecord` — оставшийся риск

2. Реализовать CRUD операции для новых артефактов через learning_store

3. Интегрировать новые артефакты в `build_task_capture_completion()`

**Файлы для изменения:**
- [`app/models/project_task.py`](app/models/project_task.py) — новые модели
- [`app/services/task_capture_service.py`](app/services/task_capture_service.py) — логика создания
- [`app/routers/project_tasks.py`](app/routers/project_tasks.py) — новые endpoints

### Этап 2: Улучшение автоматического захвата (Priority: MEDIUM)

**Статус:** ✅ Базовый автоматический захват работает

**Задачи:**
1. Добавить event bus для расширяемости
2. Добавить webhook поддержку для внешних систем
3. Улучшить триггеры захвата на основе статуса

**Файлы для изменения:**
- [`app/services/task_capture_events.py`](app/services/task_capture_events.py) — новый файл
- [`app/services/project_task_service.py`](app/services/project_task_service.py) — интеграция
- [`app/main.py`](app/main.py) — инициализация

### Этап 3: Консолидация и разрешение конфликтов (Priority: MEDIUM)

**Задачи:**
1. Реализовать логику консолидации кандидатов
2. Добавить разрешение конфликтов
3. Интегрировать с cloud LLM для сложных случаев

**Файлы для изменения:**
- [`app/services/task_capture_service.py`](app/services/task_capture_service.py) — консолидация
- [`app/services/cloud_llm.py`](app/services/cloud_llm.py) — интеграция

### Этап 4: Интеграция с handoff completeness (Priority: MEDIUM)

**Задачи:**
1. Определить критерии полноты handoff
2. Добавить валидацию при создании handoff
3. Показывать отсутствующие артефакты

**Файлы для изменения:**
- [`app/services/handoff_service.py`](app/services/handoff_service.py) — валидация
- [`app/routers/handoff.py`](app/routers/handoff.py) — API

### Этап 5: Интеграция с enrich-task quality (Priority: LOW)

**Задачи:**
1. Определить метрики качества
2. Интегрировать с enrich-task endpoint
3. Показывать качество в UI

**Файлы для изменения:**
- [`app/services/project_context_service.py`](app/services/project_context_service.py) — метрики
- [`app/routers/models.py`](app/routers/models.py) — API

### Этап 6: Интеграция с memoir preconditions (Priority: LOW)

**Задачи:**
1. Определить предусловия для мемуара
2. Проверять наличие артефактов
3. Автоматически создавать мемуары

**Файлы для изменения:**
- [`app/services/memoir_service.py`](app/services/memoir_service.py) — предусловия
- [`app/services/task_capture_service.py`](app/services/task_capture_service.py) — триггеры

### Этап 7: Governance интеграция (Priority: LOW)

**Задачи:**
1. Определить правила governance
2. Реализовать проверку соответствия
3. Добавить отчётность

**Файлы для изменения:**
- [`app/services/governance_service.py`](app/services/governance_service.py) — правила
- [`app/routers/governance.py`](app/routers/governance.py) — API

---

## Definition of Done

### Критерии завершения задачи:

1. **Полный набор артефактов MVP:**
   - [ ] Все 12 типов артефактов реализованы
   - [ ] CRUD операции работают для всех типов
   - [ ] Интеграция с `build_task_capture_completion()`

2. **Автоматический захват:**
   - [ ] Захват происходит автоматически на всех этапах
   - [ ] Триггеры определены и работают
   - [ ] Webhook/hook система работает

3. **Консолидация:**
   - [ ] Логика консолидации реализована
   - [ ] Разрешение конфликтов работает
   - [ ] Интеграция с cloud LLM

4. **Интеграции:**
   - [ ] Handoff completeness проверяется
   - [ ] Enrich-task quality показывается
   - [ ] Memoir preconditions работают
   - [ ] Governance правила применяются

5. **Тестирование:**
   - [ ] Unit tests для всех новых функций
   - [ ] Integration tests для end-to-end сценариев
   - [ ] Documentation обновлена

6. **Производительность:**
   - [ ] Локальный SLM используется для cheap writers
   - [ ] Cloud LLM используется только для consolidation
   - [ ] Latency в пределах допустимых значений

---

## Open Questions

1. **Модели для ролей:**
   - Какие конкретные модели использовать для main agent vs background helper?
   - Как настроить переключение между моделями?

2. **Триггеры автоматического захвата:**
   - Какие события должны триггерить захват?
   - Как часто должен происходить захват?

3. **Консолидация:**
   - Какой алгоритм консолидации использовать?
   - Как обрабатывать конфликты между кандидатами?

4. **Governance:**
   - Какие правила governance нужны?
   - Как применять правила автоматически?

5. **Производительность:**
   - Какие допустимые значения latency?
   - Как оптимизировать частые операции захвата?

---

## Связанные задачи

1. **Add Task Statement Projection Service** — зависит от этой задачи
2. **Drive MnemoForge to public GitHub alpha readiness** — эта задача часть пути к alpha
3. **Redesign autodocumentation** — может использовать артефакты захвата

---

## Ресурсы

- **Код:**
  - [`app/services/task_capture_service.py`](app/services/task_capture_service.py)
  - [`app/services/task_statement_service.py`](app/services/task_statement_service.py)
  - [`app/services/task_capture_rules.py`](app/services/task_capture_rules.py)
  - [`app/services/task_capture_review_service.py`](app/services/task_capture_review_service.py)
  - [`app/models/project_task.py`](app/models/project_task.py)
  - [`app/routers/project_tasks.py`](app/routers/project_tasks.py)

- **Документация:**
  - [`docs/adaptive-skillization-spec.md`](docs/adaptive-skillization-spec.md)
  - [`plans/living_documentation_memoir.md`](plans/living_documentation_memoir.md)

- **Тесты:**
  - [`tests/test_admin_integrity.py`](tests/test_admin_integrity.py)
  - [`tests/test_project_decision_options.py`](tests/test_project_decision_options.py)
