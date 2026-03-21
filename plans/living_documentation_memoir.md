# Living Documentation — Мемуар постановки задачи

> Этот документ фиксирует эволюцию задачи: от первоначального плана до реализации.
> Написан после завершения, чтобы будущий читатель понял не только *что* сделано, но и *почему именно так*.

---

## Исходная постановка (задача `700d20d3`)

**Цель:** агент в любой момент вызывает `GET /docs/status` и получает актуальную картину состояния проекта.

Первоначальный план предполагал:
- Генерацию документации через Cloud LLM (GLM) для всех секций
- Кэширование результата в Qdrant (`category=docs_cache`)
- TTL 6 часов + stale-while-revalidate стратегия
- Автоматический пересчёт по таймеру
- HTML генерируется сервером при каждом rebuild и пишется в `static/status.html`

---

## Изменение 1: Стратегия генерации секций

**Исходно:** все секции через LLM (~120 сек на полный rebuild).

**Проблема:** большинство секций — это просто агрегация данных. LLM здесь не добавляет ценности, только замедляет.

**Решение:** разделить секции на два класса:
- **Детерминированные** (`features`, `pending`, `skills`, `performance`) — агрегация без LLM, ~5 сек
- **GLM-секции** (`overview`, `architecture`) — prose-нарратив и синтез компонентов, ~15 сек

**Итог:** ~20 сек вместо ~120 сек. LLM используется только там, где он реально нужен.

---

## Изменение 2: Стратегия кэширования (ключевое)

**Исходно:** TTL 6 часов + stale-while-revalidate.

**Проблема, поднятая в обсуждении:**
> "Я не вижу смысла в перегенерации документации если проект не менялся с момента предыдущей генерации"

TTL — это суррогат для случаев, когда мы не знаем, когда изменились данные. Но у нас есть события:
- `PATCH /improvements/{id}/resolve` — состояние проекта изменилось
- `POST /project/ingest` — компоненты изменились
- `POST /project/refresh` — компоненты изменились

**Решение:** event-driven инвалидация без TTL.
- Кэш валиден бессрочно, пока не произойдёт изменяющее событие
- При событии: `invalidate → submit docs_rebuild job`
- Rebuild запускается только если реально что-то изменилось (`ingested` или `updated` не пустые)

**Что убрали из плана:**
- TTL 6 часов
- Stale-while-revalidate логику
- `force=true` параметр (оставили как dev-инструмент для ручного сброса)

---

## Изменение 3: Хранилище кэша

**Исходно:** Qdrant, `category=docs_cache`, с embedding-вектором.

**Проблема:** docs cache — это key-value по project_id. Семантический поиск не нужен, значит embedding — лишние вычисления и сложность.

**Решение:** простой JSON-файл `qdrant_data/docs_cache/{project}.json`.
- Быстро, без зависимостей, легко отлаживать
- Path traversal защищён через `_cache_key()` — безопасный filename из project_id (hash-fallback для нестандартных id)

---

## Изменение 4: HTML дашборд

**Исходно:** HTML генерируется сервером при каждом rebuild, пишется в файл.

**В обсуждении:**
> "я предпочитаю интерактив"

Статически сгенерированный HTML — это снимок в момент времени. Интерактивный SPA — живой дашборд.

**Решение:** `static/status.html` — статичный SPA-шаблон.
- Сервер его не трогает, отдаёт как `FileResponse`
- JS в браузере сам тянет `GET /docs/status` (JSON)
- Кнопка Rebuild → `POST /docs/rebuild` → polling `GET /tasks/{job_id}`
- Секции рендерятся через `marked.js` (Markdown → HTML прямо в браузере)
- `DocsService.get_docs_html()` — убран как ненужный

---

## Итоговая архитектура

```
Browser                     FastAPI                      Background
  │                            │                              │
  ├─ GET /docs/status.html ────► FileResponse(static/status.html)
  │                            │
  ├─ GET /docs/status ─────────► load_docs_cache(project)
  │   ◄── DocsStatus (JSON) ──┘
  │
  ├─ POST /docs/rebuild ───────► queue.submit("docs_rebuild")  ──► _docs_rebuild_handler
  │   ◄── { job_id } ─────────┘                                       │
  │                                                                     ├─ fetch improvements
  ├─ GET /tasks/{job_id} ──────► poll status                           ├─ fetch skills
  │   ◄── { status: "done" } ─┘                                        ├─ fetch components
  │                                                                     ├─ gen sections (det + GLM)
  └─ (auto-refresh UI) ────────► GET /docs/status                       └─ save JSON cache

Event triggers (auto-invalidate):
  PATCH /improvements/{id}/resolve  →  invalidate + submit rebuild
  POST /project/ingest (if changed) →  invalidate + submit rebuild
  POST /project/refresh (if changed) → invalidate + submit rebuild
```

---

## Что осталось без изменений

- 5 API эндпоинтов (`/rebuild`, `/status`, `/status.html`, `/status.md`, `/section/{name}`)
- 6 секций документа
- Фоновый rebuild через JobQueue (существующий паттерн проекта)
- Cloud LLM для prose-секций с fallback на детерминированный текст

---

## Ключевые уроки

1. **TTL — суррогат событий.** Если у тебя есть события, не нужен таймер.
2. **Embedding только для семантического поиска.** Key-value — это файл или словарь.
3. **SPA лучше серверного рендеринга** там, где данные меняются и пользователь хочет кнопку.
4. **LLM дорогой — используй его только там, где шаблон не справляется.**
