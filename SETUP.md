# Super Memory — Инструкция по установке на новую машину

Локальный сервер семантической памяти для ИИ-агентов.
Стек: **FastAPI + Qdrant (Docker) + Ollama (хост) + nomic-embed-text**

---

## Требования к железу

| Компонент | Минимум | Рекомендуется |
|-----------|---------|---------------|
| RAM | 8 GB | 16+ GB |
| VRAM | — | 4 GB (для embedding GPU) |
| Диск | 5 GB свободно | 10+ GB |
| ОС | Windows 10/11, Linux, macOS | — |

---

## 1. Установить зависимости

### 1.1 Docker Desktop
- Windows/macOS: скачать с [docker.com](https://docker.com/products/docker-desktop)
- Linux: `sudo apt install docker.io docker-compose-plugin`

Убедиться что Docker запущен: `docker --version`

### 1.2 Ollama

**Windows:**
```powershell
winget install Ollama.Ollama
```
или скачать установщик с [ollama.com](https://ollama.com)

**Linux:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

**macOS:**
```bash
brew install ollama
```

Запустить сервис (если не запустился автоматически):
```bash
ollama serve   # Linux/macOS
# Windows: Ollama запускается как системный сервис автоматически
```

### 1.3 Загрузить embedding-модель
```bash
ollama pull nomic-embed-text
```

Проверить:
```bash
curl http://localhost:11434/api/embed -d '{"model":"nomic-embed-text","input":"test"}'
```
Ожидаемый ответ: JSON с массивом `embeddings`.

### 1.4 Python 3.11+
- Windows: `winget install Python.Python.3.11`
- Linux: `sudo apt install python3.11 python3.11-venv`
- macOS: `brew install python@3.11`

---

## 2. Получить код проекта

```bash
git clone <repo-url> D:/work/supermemory   # Windows
# или
git clone <repo-url> ~/supermemory         # Linux/macOS
cd supermemory
```

> Если git-репозитория нет — скопировать папку проекта на новую машину.

---

## 3. Создать виртуальное окружение

**Windows:**
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**Linux/macOS:**
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Настроить конфигурацию

Создать файл `.env` в корне проекта:

```env
# Qdrant
QDRANT_HOST=localhost
QDRANT_PORT=6333
QDRANT_IN_MEMORY=false
QDRANT_COLLECTION_NAME=agent_memories

# Ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSIONS=768

# Сервер
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
LOG_LEVEL=INFO
API_PREFIX=/api/v1

# Бизнес-логика
MAX_SEARCH_RESULTS=20
CLEANUP_MIN_IMPORTANCE=0.2
CLEANUP_MAX_AGE_DAYS=30
```

> **Важно:** `EMBEDDING_DIMENSIONS=768` — реальная размерность nomic-embed-text.
> Не менять без пересоздания Qdrant-коллекции.

---

## 5. Запустить сервисы

### Вариант A: Docker Compose (рекомендуется для продакшена)

```bash
docker compose up -d
```

Это поднимет:
- **Qdrant** на порту 6333/6334
- **memory-server** на порту 8000

Проверить логи:
```bash
docker compose logs -f memory-server
```

### Вариант B: Только Qdrant в Docker + сервер локально (для разработки)

```bash
# Терминал 1 — Qdrant
docker compose up qdrant -d

# Терминал 2 — FastAPI сервер
source .venv/bin/activate  # или .venv\Scripts\activate на Windows
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Windows (PowerShell) — быстрый перезапуск сервера (с PID + логами):

```powershell
.\scripts\restart_server.ps1 -Reload
```

---

## 6. Проверить работоспособность

```bash
# Health check
curl http://localhost:8000/api/v1/health

# Ожидаемый ответ:
# {"status":"healthy","qdrant":{"reachable":true},"ollama":{"reachable":true}}
```

```bash
# Сохранить воспоминание
curl -X POST http://localhost:8000/api/v1/memories \
  -H "Content-Type: application/json" \
  -d '{"content":"Test memory","agent_id":"test","memory_type":"fact","importance_score":0.5}'

# Поиск
curl -X POST http://localhost:8000/api/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query":"test","agent_id":"test","limit":3}'
```

---

## 7. Подключить к Claude Code (MCP)

### Найти путь к `claude.exe`

**Windows:**
```powershell
# Обычно находится здесь:
%LOCALAPPDATA%\Programs\Claude\claude.exe
# или в расширении VSCode:
%USERPROFILE%\.vscode\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe
```

**Linux/macOS:**
```bash
which claude
```

### Зарегистрировать MCP-сервер

**Windows** (указать реальный путь к проекту):
```powershell
claude mcp add -s user `
  -e "MEMORY_SERVER_URL=http://localhost:8000" `
  super-memory `
  -- "D:\work\supermemory\.venv\Scripts\python.exe" "D:\work\supermemory\mcp\server.py"
```

**Linux/macOS:**
```bash
claude mcp add -s user \
  -e "MEMORY_SERVER_URL=http://localhost:8000" \
  super-memory \
  -- /path/to/supermemory/.venv/bin/python /path/to/supermemory/mcp/server.py
```

Проверить:
```bash
claude mcp list
# super-memory: ... - ✓ Connected
```

---

## 8. Установить скилы `/remember` и `/recall`

```bash
# Windows
mkdir "%USERPROFILE%\.claude\skills\remember"
mkdir "%USERPROFILE%\.claude\skills\recall"
# Скопировать SKILL.md из папки проекта:
copy skills\remember\SKILL.md "%USERPROFILE%\.claude\skills\remember\SKILL.md"
copy skills\recall\SKILL.md "%USERPROFILE%\.claude\skills\recall\SKILL.md"

# Linux/macOS
mkdir -p ~/.claude/skills/remember ~/.claude/skills/recall
cp skills/remember/SKILL.md ~/.claude/skills/remember/
cp skills/recall/SKILL.md ~/.claude/skills/recall/
```

> **Перезапустить VSCode** после установки скилов и MCP.

---

## 9. Использование

```
/remember User prefers dark mode in all tools
/remember --type task --importance 0.8 Review PR #42 before Friday
/remember --agent project1 --type context Docker runs on port 5433

/recall user preferences
/recall --type task pending work
/recall what does the user prefer for code style
```

Claude также автоматически вызывает скилы при словах:
*"запомни"*, *"вспомни"*, *"remember that"*, *"do you know about"*

---

## Устранение проблем

### `ollama: command not found`
Ollama не в PATH. Перезапустить терминал после установки или добавить в PATH вручную.

### `connection refused` на порту 8000
```bash
docker compose ps        # проверить статус контейнеров
docker compose logs memory-server  # посмотреть ошибки
```

### `connection refused` на порту 11434
Ollama не запущен:
```bash
ollama serve   # Linux/macOS
# Windows: запустить Ollama из меню пуск
```

### MCP статус `✗ Failed` вместо `✓ Connected`
```bash
# Проверить путь к Python
"D:\work\supermemory\.venv\Scripts\python.exe" -c "import httpx; print('OK')"

# Проверить что сервер доступен
curl http://localhost:8000/api/v1/health
```

### Другие embedding-модели (если нужно)

| Модель | VRAM | Размерность | Команда |
|--------|------|-------------|---------|
| nomic-embed-text | ~274 MB | 768 | `ollama pull nomic-embed-text` |
| mxbai-embed-large | ~669 MB | 1024 | `ollama pull mxbai-embed-large` |
| all-minilm | ~46 MB | 384 | `ollama pull all-minilm` |

При смене модели: обновить `.env` (`EMBEDDING_DIMENSIONS`) и **удалить Qdrant-коллекцию**:
```bash
curl -X DELETE http://localhost:6333/collections/agent_memories
# После этого коллекция пересоздастся автоматически при следующем запросе
```
