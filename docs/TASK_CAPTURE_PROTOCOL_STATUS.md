# Task Memory Capture Protocol — Текущий статус

## Обзор

Документ содержит актуальный статус реализации Task Memory Capture Protocol в SuperMemory.

---

## Прогресс реализации

### Общий прогресс: 67% (8 из 12 артефактов реализовано)

| Артефакт | Статус | Примечания |
|----------|--------|------------|
| `task` | ✅ | Базовая структура реализована |
| `assumption` | ✅ | Полный цикл захвата |
| `constraint` | ✅ | Полный цикл захвата |
| `definition_of_done` | ✅ | Полный цикл захвата |
| `chosen_decision` | ✅ | Хранение в TaskStatementCurrentView |
| `task_change` | ✅ | ProjectTaskChangeRecord |
| `deferred_finding` | ✅ | Полный цикл через learning_store |
| `verification_result` | ✅ | Полный цикл захвата |
| `result_summary` | ✅ | Полный цикл захвата |
| `handoff_summary` | ✅ | Полный цикл захвата |
| `decision_candidate` | ❌ | Не реализован |
| `code_link` | ❌ | Не реализован |
| `remaining_risk` | ❌ | Не реализован |

> `task_checkpoint` now exists as a structured `task_change` convention tagged `task_checkpoint`, so planning/blocked/interrupted/handoff checkpoints are stored through the existing task_change pipeline.

---

## Статус по пробелам

### Пробел 1: Неполный набор артефактов MVP
**Статус:** ЧАСТИЧНО ЗАКРЫТО (67%)

**Что реализовано:**
- ✅ 10 из 12 типов артефактов
- ✅ CRUD для всех реализованных типов
- ✅ Интеграция с `build_task_capture_completion()`

**Что нужно добавить:**
- ❌ `decision_candidate` — кандидат на решение
- ❌ `code_link` — ссылка на код
- ❌ `remaining_risk` — оставшийся риск

### Пробел 2: Отсутствие разделения ролей
**Статус:** НЕ ЗАКРЫТО

**Текущее состояние:**
- Одна модель для всех операций: `qwen3:1.7b`
- Нет конфигурации для main agent vs helper

**Требуется:**
- Конфигурация для разных ролей
- Маршрутизация по сложности задач

### Пробел 3: Отсутствие автоматического захвата
**Статус:** ЧАСТИЧНО ЗАКРЫТО

**Что реализовано:**
- ✅ Захват при создании задачи ([`project_tasks.py:84-90`](app/routers/project_tasks.py:84))
- ✅ Захват при добавлении изменения ([`project_tasks.py:130-136`](app/routers/project_tasks.py:130))
- ✅ Job queue handler для `task_capture_refresh` ([`main.py:570-598`](app/main.py:570))

**Ограничения:**
- ⚠️ Нет явного event bus для расширения
- ⚠️ Триггеры захвата жёстко зашиты в коде
- ⚠️ Нет webhook поддержки

### Пробел 4: Отсутствие консолидации
**Статус:** НЕ ЗАКРЫТО

**Примечание:** Консолидация существует для crystallization service, но не для task capture candidates.

**Требуется:**
- Логика консолидации для task capture candidates
- Разрешение конфликтов
- Интеграция с cloud LLM

### Пробел 5: Отсутствие handoff completeness
**Статус:** НЕ ЗАКРЫТО

**Требуется:**
- Проверка полноты при создании handoff
- Критерии полноты по этапам
- Показ отсутствующих артефактов

### Пробел 6: Отсутствие enrich-task quality
**Статус:** НЕ ЗАКРЫТО

**Требуется:**
- Метрики качества на основе артефактов
- Интеграция с enrich-task endpoint
- Показ качества в UI

### Пробел 7: Отсутствие memoir preconditions
**Статус:** НЕ ЗАКРЫТО

**Требуется:**
- Проверка предусловий для создания мемуара
- Проверка наличия необходимых артефактов
- Авто-создание мемуаров

### Пробел 8: Отсутствие governance интеграции
**Статус:** НЕ ЗАКРЫТО

**Требуется:**
- Правила governance на основе артефактов
- Проверка соответствия правилам
- Отчётность по соблюдению протокола

---

## Приоритеты реализации

### HIGH (Срочно)
1. **Пробел 1:** Добавить 3 отсутствующих артефакта
   - `decision_candidate`
   - `code_link`
   - `remaining_risk`

### MEDIUM (Важно)
2. **Пробел 2:** Разделение ролей (main agent vs helper)
3. **Пробел 3:** Улучшение автоматического захвата (event bus, webhooks)
4. **Пробел 4:** Консолидация кандидатов

### LOW (Желательно)
5. **Пробел 5:** Handoff completeness
6. **Пробел 6:** Enrich-task quality
7. **Пробел 7:** Memoir preconditions
8. **Пробел 8:** Governance интеграция

---

## Следующие шаги

1. **Добавить `decision_candidate` артефакт**
   - Создать модель `DecisionCandidateRecord`
   - Реализовать CRUD через learning_store
   - Интегрировать в `build_task_capture_completion()`

2. **Добавить `code_link` артефакт**
   - Создать модель `CodeLinkRecord`
   - Реализовать CRUD через learning_store
   - Интегрировать в `build_task_capture_completion()`

3. **Добавить `remaining_risk` артефакт**
   - Создать модель `RemainingRiskRecord`
   - Реализовать CRUD через learning_store
   - Интегрировать в `build_task_capture_completion()`

4. **Реализовать разделение ролей**
   - Добавить конфигурацию в `app/config.py`
   - Реализовать маршрутизацию по сложности
   - Интегрировать с существующим кодом

---

## Связанные документы

- [`TASK_CAPTURE_PROTOCOL_SPEC.md`](TASK_CAPTURE_PROTOCOL_SPEC.md) — Полная спецификация
- [`TASK_CAPTURE_GAP_RESOLUTION.md`](TASK_CAPTURE_GAP_RESOLUTION.md) — Варианты разрешения пробелов
