# MnemoForge Client Setup

This guide is for a client machine that does not run the MnemoForge server. It only connects to an already running MnemoForge server over the network.

## Requirements

- Python 3.9+
- `httpx` (`pip install httpx`)
- Claude Code
- The `mcp/server.py` file from this repository, or a full clone of the repository

Qdrant, Ollama, Docker, and FastAPI are not required on the client machine.

## 1. Check Server Reachability

Replace `<SERVER_IP>` with the server machine address:

```bash
curl http://<SERVER_IP>:8000/api/v1/health
```

If the server is unreachable, check that:

- `docker compose up -d` is running on the server;
- port `8000` is open in the firewall;
- the server listens on `0.0.0.0`, not only on `127.0.0.1`;
- an `API_KEY` requirement is not missing from the client configuration.

## 2. Get The MCP Client File

Option A: copy only the stdio bridge file:

```powershell
mkdir <CLIENT_HOME>
# Copy mcp\server.py from the repository or from the server by scp/sftp.
```

```bash
mkdir -p ~/mnemoforge-client
scp user@<SERVER_IP>:/path/to/mnemoforge/mcp/server.py ~/mnemoforge-client/
```

Option B: clone the full repository:

```bash
git clone https://github.com/Utundry/mnemoforge.git mnemoforge-client
```

## 3. Install The Python Dependency

For a simple client-only setup:

```bash
pip install httpx
```

Or use a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install httpx
```

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install httpx
```

## 4. Register MnemoForge In Claude Code

Find the `claude` executable:

```powershell
# Windows VS Code extension path, adjust the wildcard if needed.
$env:USERPROFILE\.vscode\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe
```

```bash
which claude
```

Register the client bridge.

Windows:

```powershell
claude mcp add -s user `
  -e "MEMORY_SERVER_URL=http://<SERVER_IP>:8000" `
  mnemoforge `
  -- "<CLIENT_HOME>\.venv\Scripts\python.exe" "<CLIENT_HOME>\server.py"
```

Linux or macOS:

```bash
claude mcp add -s user \
  -e "MEMORY_SERVER_URL=http://<SERVER_IP>:8000" \
  mnemoforge \
  -- ~/mnemoforge-client/.venv/bin/python ~/mnemoforge-client/server.py
```

If the server requires an API key, add the expected environment variable or client header according to your MCP client configuration. For SSE clients, send `X-Api-Key: <your key>`.

Verify the registration:

```bash
claude mcp list
```

## 5. Optional Skills

If your client uses Claude skills, copy the repository skill files:

Windows:

```powershell
mkdir "$env:USERPROFILE\.claude\skills\remember"
mkdir "$env:USERPROFILE\.claude\skills\recall"
copy skills\remember\SKILL.md "$env:USERPROFILE\.claude\skills\remember\SKILL.md"
copy skills\recall\SKILL.md "$env:USERPROFILE\.claude\skills\recall\SKILL.md"
```

Linux or macOS:

```bash
mkdir -p ~/.claude/skills/remember ~/.claude/skills/recall
cp skills/remember/SKILL.md ~/.claude/skills/remember/
cp skills/recall/SKILL.md ~/.claude/skills/recall/
```

Restart VS Code after changing Claude Code MCP or skill configuration.

## 6. Smoke Test

Use a small synthetic memory:

```text
/remember --agent mypc --type fact Client machine is connected to the memory server
/recall client connection to memory server
```

## Multiple Clients

Each client can use its own `agent_id` or share a team-level `agent_id`:

```text
/remember --agent alice Prefers vim
/remember --agent team Deploy every Friday at 18:00
/recall --agent team deployment process
```

## Topology

```text
Client A (Windows)              Client B (Linux)
  Claude Code                     Claude Code
  mcp/server.py  ------------>    mcp/server.py
        |                               |
        +---------- HTTP ---------------+
                          |
                   <SERVER_IP>:8000
                   MnemoForge API
                          |
                       Qdrant
                 optional LLM providers
```
