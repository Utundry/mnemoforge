# MnemoForge — Подключение клиентской машины

Эта инструкция для машины, которая **не запускает сервер** — только подключается к уже работающему mnemoforge серверу по сети.

---

## Что нужно на клиентской машине

- Python 3.9+ с `httpx` (`pip install httpx`)
- Claude Code (VSCode расширение `anthropic.claude-code`)
- Один файл: `mcp/server.py` из этого репозитория

> Qdrant, Ollama, Docker, FastAPI — **не нужны**.

---

## 1. Убедиться что сервер доступен

Заменить `<SERVER_IP>` на IP машины с сервером:

```bash
curl http://<SERVER_IP>:8000/api/v1/health
# {"status":"healthy","qdrant":{"reachable":true},"ollama":{"reachable":true}}
```

Если недоступен — проверить, что на сервере:
- `docker compose up -d` запущен
- Порт 8000 открыт в файерволе (`sudo ufw allow 8000` на Linux)
- Сервер слушает на `0.0.0.0`, а не `127.0.0.1`

---

## 2. Получить файл MCP-сервера

**Вариант A — скопировать один файл:**

```bash
# Windows
mkdir <CLIENT_HOME>
# Скопировать mcp\server.py из репозитория или с сервера по scp/sftp
```

```bash
# Linux/macOS
mkdir ~/mnemoforge-client
scp user@<SERVER_IP>:/path/to/mnemoforge/mcp/server.py ~/mnemoforge-client/
```

**Вариант B — клонировать репозиторий целиком** (если есть доступ):
```bash
git clone <repo-url> mnemoforge-client
```

---

## 3. Установить Python-зависимость

Нужна только одна библиотека:

```bash
pip install httpx
```

Или в виртуальном окружении:

```bash
# Windows
python -m venv .venv && .venv\Scripts\activate && pip install httpx

# Linux/macOS
python3 -m venv .venv && source .venv/bin/activate && pip install httpx
```

---

## 4. Зарегистрировать MCP в Claude Code

Найти путь к `claude`:

```bash
# Windows (VSCode extension)
%USERPROFILE%\.vscode\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe

# Linux/macOS
which claude
```

Зарегистрировать сервер, указав IP сервера:

**Windows:**
```powershell
claude mcp add -s user `
  -e "MEMORY_SERVER_URL=http://<SERVER_IP>:8000" `
  mnemoforge `
  -- "<CLIENT_HOME>\.venv\Scripts\python.exe" "<CLIENT_HOME>\server.py"
```

**Linux/macOS:**
```bash
claude mcp add -s user \
  -e "MEMORY_SERVER_URL=http://<SERVER_IP>:8000" \
  mnemoforge \
  -- ~/mnemoforge-client/.venv/bin/python ~/mnemoforge-client/server.py
```

Проверить:
```bash
claude mcp list
# mnemoforge: ... - ✓ Connected
```

---

## 5. Установить скилы `/remember` и `/recall`

Скопировать из папки `skills/` этого репозитория:

**Windows:**
```powershell
mkdir "$env:USERPROFILE\.claude\skills\remember"
mkdir "$env:USERPROFILE\.claude\skills\recall"
copy skills\remember\SKILL.md "$env:USERPROFILE\.claude\skills\remember\SKILL.md"
copy skills\recall\SKILL.md   "$env:USERPROFILE\.claude\skills\recall\SKILL.md"
```

**Linux/macOS:**
```bash
mkdir -p ~/.claude/skills/remember ~/.claude/skills/recall
cp skills/remember/SKILL.md ~/.claude/skills/remember/
cp skills/recall/SKILL.md   ~/.claude/skills/recall/
```

**Перезапустить VSCode.**

---

## 6. Проверить

```
/remember --agent mypc --type fact Клиентская машина подключена к серверу памяти
/recall подключение к серверу
```

---

## Несколько клиентов одновременно

Каждый клиент работает независимо. Чтобы разделить или объединить память, используй `agent_id`:

```
# Личная память конкретного пользователя
/remember --agent alice Предпочитает vim

# Общая память команды
/remember --agent team Деплой каждую пятницу в 18:00
/recall --agent team процесс деплоя
```

---

## Итоговая схема

```
Клиент A (Windows)              Клиент B (Linux)
  Claude Code                     Claude Code
  mcp/server.py  ─────────────►  mcp/server.py
        │                               │
        └──────────── HTTP ─────────────┘
                          │
                   <SERVER_IP>:8000
                   FastAPI memory-server
                          │
                       Qdrant (Docker)
                       Ollama (хост)
```
