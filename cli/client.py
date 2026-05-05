"""
MnemoForge CLI — interact with the memory server.

Usage examples:
  python -m cli.client store --content "User prefers short answers" --agent agent1 --type preference --importance 0.8
  python -m cli.client search --query "how does user like to communicate" --agent agent1
  python -m cli.client get <uuid>
  python -m cli.client delete <uuid>
  python -m cli.client health
  python -m cli.client stats
  python -m cli.client cleanup --min-importance 0.2 --max-age-days 30
  python -m cli.client ingest-file /path/to/notes.md --agent agent1
  python -m cli.client ingest-dir /path/to/docs --agent agent1 --recursive
"""
from __future__ import annotations

import os
from typing import Optional
from uuid import UUID

import httpx
import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="MnemoForge CLI")
console = Console()

BASE_URL = os.environ.get("MEMORY_SERVER_URL", "http://localhost:8000")
API = f"{BASE_URL}/api/v1"


def _client() -> httpx.Client:
    return httpx.Client(base_url=API, timeout=30.0)


def _handle(resp: httpx.Response):
    if resp.status_code >= 400:
        rprint(f"[red]Error {resp.status_code}:[/red] {resp.text}")
        raise typer.Exit(code=1)
    return resp.json()


# ── store ──────────────────────────────────────────────────────────────────────

@app.command()
def store(
    content: str = typer.Option(..., "--content", "-c", help="Memory text"),
    agent: str = typer.Option(..., "--agent", "-a", help="Agent ID"),
    type: str = typer.Option("fact", "--type", "-t", help="fact|preference|experience|task|context"),
    category: str = typer.Option("general", "--category"),
    importance: float = typer.Option(0.5, "--importance", "-i", help="0.0 – 1.0"),
    source: str = typer.Option("conversation", "--source"),
    tags: list[str] = typer.Option([], "--tag", help="Repeat for multiple tags"),
    session: Optional[str] = typer.Option(None, "--session"),
):
    """Store a new memory."""
    payload = {
        "content": content,
        "agent_id": agent,
        "memory_type": type,
        "category": category,
        "importance_score": importance,
        "source": source,
        "tags": tags,
        "session_id": session,
    }
    with _client() as c:
        data = _handle(c.post("/memories", json=payload))
    rprint(f"[green]Stored:[/green] {data['id']}")
    rprint(data)


# ── search ─────────────────────────────────────────────────────────────────────

@app.command()
def search(
    query: str = typer.Option(..., "--query", "-q"),
    agent: Optional[str] = typer.Option(None, "--agent", "-a"),
    type: Optional[str] = typer.Option(None, "--type", "-t"),
    category: Optional[str] = typer.Option(None, "--category"),
    limit: int = typer.Option(5, "--limit", "-n"),
    min_score: float = typer.Option(0.0, "--min-score"),
):
    """Semantic search across memories."""
    payload = {
        "query": query,
        "agent_id": agent,
        "memory_type": type,
        "category": category,
        "limit": limit,
        "min_score": min_score,
    }
    with _client() as c:
        results = _handle(c.post("/memories/search", json=payload))

    table = Table(title=f"Search: '{query}'", show_lines=True)
    table.add_column("Score", style="cyan", width=6)
    table.add_column("Sim", style="blue", width=6)
    table.add_column("Type", width=12)
    table.add_column("Content", min_width=40)
    table.add_column("ID", width=36)

    for r in results:
        m = r["memory"]
        table.add_row(
            f"{r['score']:.3f}",
            f"{r['similarity']:.3f}",
            m["memory_type"],
            m["content"][:120],
            m["id"],
        )
    console.print(table)


# ── get ────────────────────────────────────────────────────────────────────────

@app.command()
def get(memory_id: str = typer.Argument(..., help="Memory UUID")):
    """Fetch a memory by ID."""
    with _client() as c:
        data = _handle(c.get(f"/memories/{memory_id}"))
    rprint(data)


# ── delete ─────────────────────────────────────────────────────────────────────

@app.command()
def delete(memory_id: str = typer.Argument(..., help="Memory UUID")):
    """Delete a memory by ID."""
    with _client() as c:
        resp = c.delete(f"/memories/{memory_id}")
        if resp.status_code == 204:
            rprint(f"[green]Deleted:[/green] {memory_id}")
        else:
            _handle(resp)


# ── health ─────────────────────────────────────────────────────────────────────

@app.command()
def health():
    """Check server health."""
    with _client() as c:
        data = _handle(c.get("/health"))
    status_color = "green" if data["status"] == "ok" else "yellow"
    rprint(f"[{status_color}]Status: {data['status']}[/{status_color}]")
    rprint(f"  Qdrant reachable: {data['qdrant']['reachable']}")
    rprint(f"  Ollama reachable: {data['ollama']['reachable']}")


# ── stats ──────────────────────────────────────────────────────────────────────

@app.command()
def stats():
    """Show collection statistics."""
    with _client() as c:
        data = _handle(c.get("/stats"))
    rprint(data)


# ── cleanup ────────────────────────────────────────────────────────────────────

@app.command()
def cleanup(
    agent: Optional[str] = typer.Option(None, "--agent", "-a"),
    min_importance: float = typer.Option(0.2, "--min-importance"),
    max_age_days: int = typer.Option(30, "--max-age-days"),
):
    """Delete old / low-importance memories."""
    payload = {
        "agent_id": agent,
        "min_importance": min_importance,
        "max_age_days": max_age_days,
    }
    with _client() as c:
        data = _handle(c.request("DELETE", "/memories/cleanup", json=payload))
    rprint(f"[yellow]Deleted {data['deleted_count']} memories[/yellow]")


# ── ingest-file ────────────────────────────────────────────────────────────────

@app.command(name="ingest-file")
def ingest_file(
    path: str = typer.Argument(..., help="Path to file (.md, .txt, ...)"),
    agent: str = typer.Option(..., "--agent", "-a"),
    type: str = typer.Option("context", "--type", "-t"),
    category: str = typer.Option("document", "--category"),
    importance: float = typer.Option(0.5, "--importance", "-i"),
    tags: list[str] = typer.Option([], "--tag"),
    session: Optional[str] = typer.Option(None, "--session"),
):
    """Parse a file from disk and store its chunks as memories."""
    payload = {
        "path": path,
        "cwd": os.getcwd() if not os.path.isabs(path) else None,
        "agent_id": agent,
        "memory_type": type,
        "category": category,
        "importance_score": importance,
        "tags": tags,
        "session_id": session,
    }
    with _client() as c:
        data = _handle(c.post("/ingest/file", json=payload))
    rprint(f"[green]Inserted:[/green] {data['inserted']}  Failed: {data['failed']}  Skipped: {data['skipped']}")


# ── ingest-dir ─────────────────────────────────────────────────────────────────

@app.command(name="ingest-dir")
def ingest_dir(
    path: str = typer.Argument(..., help="Path to directory"),
    agent: str = typer.Option(..., "--agent", "-a"),
    type: str = typer.Option("context", "--type", "-t"),
    category: str = typer.Option("document", "--category"),
    importance: float = typer.Option(0.5, "--importance", "-i"),
    extensions: list[str] = typer.Option([], "--ext", help="Filter extensions, e.g. --ext md --ext txt"),
    no_recursive: bool = typer.Option(False, "--no-recursive"),
    tags: list[str] = typer.Option([], "--tag"),
    session: Optional[str] = typer.Option(None, "--session"),
):
    """Recursively parse all supported files in a directory and store as memories."""
    payload = {
        "path": path,
        "cwd": os.getcwd() if not os.path.isabs(path) else None,
        "agent_id": agent,
        "memory_type": type,
        "category": category,
        "importance_score": importance,
        "extensions": extensions,
        "recursive": not no_recursive,
        "tags": tags,
        "session_id": session,
    }
    with _client() as c:
        data = _handle(c.post("/ingest/dir", json=payload))
    rprint(
        f"[green]Files processed:[/green] {data['files_processed']}  "
        f"Inserted: {data['inserted']}  Failed: {data['failed']}  Skipped: {data['skipped']}"
    )


if __name__ == "__main__":
    app()
