"""
Super Memory Benchmark
======================
Measures latency and throughput for all key API operations.

Usage:
    python -m bench.benchmark [OPTIONS]

Options:
    --url       Base URL of memory server  (default: http://localhost:8000)
    --agent     Agent ID to use            (default: bench_agent)
    --n         Number of iterations       (default: 50)
    --batch     Batch size for batch test  (default: 20)
    --warmup    Warm-up requests           (default: 3)
    --no-cleanup  Skip cleanup after bench (default: cleanup enabled)
"""
from __future__ import annotations

import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False)
console = Console(width=120)
rprint = console.print

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------
SAMPLE_CONTENTS = [
    "User prefers concise answers without unnecessary padding",
    "Python is the user's primary programming language",
    "The project uses FastAPI for the REST API layer",
    "Qdrant is used as the vector database backend",
    "Ollama runs locally for generating text embeddings",
    "The team follows trunk-based development workflow",
    "Code reviews must be approved by at least two engineers",
    "All services are deployed via Docker Compose",
    "Logs are collected and stored in structured JSON format",
    "The user dislikes verbose error messages in production",
    "Redis is used for session caching across microservices",
    "API rate limiting is enforced at the gateway level",
    "Integration tests run against a real database, not mocks",
    "The user prefers dark mode in all IDE and terminal tools",
    "Async Python is preferred over threading for I/O-bound tasks",
    "The CI pipeline runs on GitHub Actions with self-hosted runners",
    "Secrets are managed via HashiCorp Vault",
    "All endpoints require JWT authentication except /health",
    "Database migrations use Alembic with auto-generated scripts",
    "The embedding model is nomic-embed-text with 768 dimensions",
]

SEARCH_QUERIES = [
    "programming language preferences",
    "how does the team deploy services",
    "database and storage solutions",
    "user interface preferences",
    "authentication and security setup",
    "testing strategy and approach",
    "CI/CD pipeline configuration",
    "logging and monitoring setup",
    "code review process",
    "async programming patterns",
]


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    name: str
    times_ms: list[float] = field(default_factory=list)
    errors: int = 0

    def record(self, elapsed_s: float) -> None:
        self.times_ms.append(elapsed_s * 1000)

    @property
    def count(self) -> int:
        return len(self.times_ms)

    @property
    def mean(self) -> float:
        return statistics.mean(self.times_ms) if self.times_ms else 0.0

    @property
    def median(self) -> float:
        return statistics.median(self.times_ms) if self.times_ms else 0.0

    @property
    def p95(self) -> float:
        if not self.times_ms:
            return 0.0
        s = sorted(self.times_ms)
        idx = max(0, int(len(s) * 0.95) - 1)
        return s[idx]

    @property
    def p99(self) -> float:
        if not self.times_ms:
            return 0.0
        s = sorted(self.times_ms)
        idx = max(0, int(len(s) * 0.99) - 1)
        return s[idx]

    @property
    def min(self) -> float:
        return min(self.times_ms) if self.times_ms else 0.0

    @property
    def max(self) -> float:
        return max(self.times_ms) if self.times_ms else 0.0

    @property
    def rps(self) -> float:
        if not self.times_ms:
            return 0.0
        return 1000.0 / self.mean if self.mean > 0 else 0.0


def _timed(fn: Callable, stats: Stats) -> any:
    t0 = time.perf_counter()
    try:
        result = fn()
        stats.record(time.perf_counter() - t0)
        return result
    except Exception as e:
        stats.errors += 1
        stats.record(time.perf_counter() - t0)
        return None


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

class Benchmark:
    def __init__(self, base_url: str, agent_id: str, timeout: float = 60.0):
        self.api = base_url.rstrip("/") + "/api/v1"
        self.agent_id = agent_id
        self.client = httpx.Client(timeout=timeout)
        self._stored_ids: list[str] = []

    def close(self) -> None:
        self.client.close()

    # ---- helpers --------------------------------------------------------

    def _post(self, path: str, json: dict) -> Optional[dict]:
        r = self.client.post(f"{self.api}{path}", json=json)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str) -> Optional[dict]:
        r = self.client.get(f"{self.api}{path}")
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str, json: Optional[dict] = None) -> None:
        if json:
            self.client.request("DELETE", f"{self.api}{path}", json=json)
        else:
            self.client.delete(f"{self.api}{path}")

    # ---- individual benchmarks -----------------------------------------

    def bench_health(self, n: int) -> Stats:
        s = Stats("GET /health")
        for _ in range(n):
            _timed(lambda: self._get("/health"), s)
        return s

    def bench_store(self, n: int, warmup: int) -> Stats:
        s = Stats("POST /memories (store)")
        contents = (SAMPLE_CONTENTS * ((n + warmup) // len(SAMPLE_CONTENTS) + 1))[: n + warmup]

        for i, content in enumerate(contents):
            payload = {
                "content": content,
                "agent_id": self.agent_id,
                "memory_type": "fact",
                "importance_score": round(0.3 + (i % 7) * 0.1, 1),
            }
            if i < warmup:
                try:
                    data = self._post("/memories", payload)
                    if data:
                        self._stored_ids.append(data["id"])
                except Exception:
                    pass
                continue

            def _store(p=payload):
                data = self._post("/memories", p)
                if data:
                    self._stored_ids.append(data["id"])
                return data

            _timed(_store, s)

        return s

    def bench_search(self, n: int) -> Stats:
        s = Stats("POST /memories/search")
        queries = (SEARCH_QUERIES * (n // len(SEARCH_QUERIES) + 1))[:n]
        for q in queries:
            payload = {"query": q, "agent_id": self.agent_id, "limit": 5}
            _timed(lambda p=payload: self._post("/memories/search", p), s)
        return s

    def bench_get(self, n: int) -> Stats:
        s = Stats("GET /memories/{id}")
        if not self._stored_ids:
            rprint("[yellow]No stored IDs available — skipping GET bench[/yellow]")
            return s
        ids = (self._stored_ids * (n // len(self._stored_ids) + 1))[:n]
        for mid in ids:
            _timed(lambda i=mid: self._get(f"/memories/{i}"), s)
        return s

    def bench_batch(self, n: int, batch_size: int) -> Stats:
        s = Stats(f"POST /memories/batch ({batch_size} items)")
        contents = (SAMPLE_CONTENTS * (batch_size // len(SAMPLE_CONTENTS) + 2))[:batch_size]
        payload = {
            "memories": [
                {"content": c, "agent_id": self.agent_id, "memory_type": "context"}
                for c in contents
            ]
        }
        for _ in range(n):
            def _batch(p=payload):
                data = self._post("/memories/batch", p)
                if data:
                    self._stored_ids.extend(data.get("created_ids", []))
                return data

            _timed(_batch, s)
        return s

    def bench_stats(self, n: int) -> Stats:
        s = Stats("GET /stats")
        for _ in range(n):
            _timed(lambda: self._get("/stats"), s)
        return s

    def cleanup(self) -> int:
        deleted = 0
        with console.status("[dim]Cleaning up bench memories...[/dim]"):
            for mid in self._stored_ids:
                try:
                    self._delete(f"/memories/{mid}")
                    deleted += 1
                except Exception:
                    pass
        return deleted


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _render_table(all_stats: list[Stats]) -> None:
    table = Table(
        title="[bold]Super Memory Benchmark Results[/bold]",
        show_lines=True,
        header_style="bold cyan",
    )
    table.add_column("Operation", min_width=30, no_wrap=True)
    table.add_column("N",      justify="right", width=5)
    table.add_column("Err",    justify="right", width=5, style="red")
    table.add_column("Mean",   justify="right", width=8)
    table.add_column("Median", justify="right", width=8)
    table.add_column("P95",    justify="right", width=8)
    table.add_column("P99",    justify="right", width=8)
    table.add_column("Min",    justify="right", width=7)
    table.add_column("Max",    justify="right", width=7)
    table.add_column("RPS",    justify="right", width=7, style="green")

    for s in all_stats:
        if s.count == 0:
            continue
        err_str = str(s.errors) if s.errors else "[green]0[/green]"
        table.add_row(
            s.name,
            str(s.count),
            err_str,
            f"{s.mean:.1f}",
            f"{s.median:.1f}",
            f"{s.p95:.1f}",
            f"{s.p99:.1f}",
            f"{s.min:.1f}",
            f"{s.max:.1f}",
            f"{s.rps:.1f}",
        )

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@app.command()
def run(
    url: str = typer.Option("http://localhost:8000", "--url", help="Memory server base URL"),
    agent: str = typer.Option("bench_agent", "--agent", help="Agent ID for benchmark data"),
    n: int = typer.Option(50, "--n", help="Number of requests per operation"),
    batch: int = typer.Option(20, "--batch", help="Batch size for batch insert test"),
    batch_n: int = typer.Option(5, "--batch-n", help="Number of batch requests"),
    warmup: int = typer.Option(3, "--warmup", help="Warm-up requests (not measured)"),
    no_cleanup: bool = typer.Option(False, "--no-cleanup", help="Skip cleanup after benchmark"),
):
    """Run performance benchmark against the Super Memory Server."""

    rprint(f"\n[bold cyan]Super Memory Benchmark[/bold cyan]")
    rprint(f"  Server : [green]{url}[/green]")
    rprint(f"  Agent  : [yellow]{agent}[/yellow]")
    rprint(f"  N      : {n} requests per operation")
    rprint(f"  Batch  : {batch} items x {batch_n} requests")
    rprint(f"  Warmup : {warmup} requests\n")

    bench = Benchmark(url, agent)

    # Check server is up
    try:
        health = bench._get("/health")
        if health["status"] != "ok":
            rprint(f"[yellow]Warning: server status is '{health['status']}'[/yellow]")
        else:
            rprint(f"[green]Server healthy[/green] — Qdrant: {health['qdrant']['reachable']}, Ollama: {health['ollama']['reachable']}\n")
    except Exception as e:
        rprint(f"[red]Cannot reach server at {url}: {e}[/red]")
        raise typer.Exit(1)

    all_stats: list[Stats] = []

    with console.status("[cyan]Benchmarking health endpoint...[/cyan]"):
        all_stats.append(bench.bench_health(n))

    with console.status("[cyan]Benchmarking stats endpoint...[/cyan]"):
        all_stats.append(bench.bench_stats(n))

    with console.status(f"[cyan]Benchmarking store (warmup={warmup}, n={n})...[/cyan]"):
        all_stats.append(bench.bench_store(n, warmup))

    with console.status(f"[cyan]Benchmarking search (n={n})...[/cyan]"):
        all_stats.append(bench.bench_search(n))

    with console.status(f"[cyan]Benchmarking get by ID (n={n})...[/cyan]"):
        all_stats.append(bench.bench_get(n))

    with console.status(f"[cyan]Benchmarking batch insert ({batch} items × {batch_n})...[/cyan]"):
        all_stats.append(bench.bench_batch(batch_n, batch))

    _render_table(all_stats)

    # Summary
    store_s = next((s for s in all_stats if "store" in s.name), None)
    search_s = next((s for s in all_stats if "search" in s.name), None)
    if store_s and search_s:
        rprint(f"\n[bold]Summary[/bold]")
        rprint(f"  Store  latency p95 : [cyan]{store_s.p95:.0f} ms[/cyan]")
        rprint(f"  Search latency p95 : [cyan]{search_s.p95:.0f} ms[/cyan]")
        rprint(f"  Store  throughput  : [green]{store_s.rps:.1f} req/s[/green]")
        rprint(f"  Search throughput  : [green]{search_s.rps:.1f} req/s[/green]")

    if not no_cleanup:
        deleted = bench.cleanup()
        rprint(f"\n[dim]Cleaned up {deleted} bench memories.[/dim]")
    else:
        rprint(f"\n[dim]Skipped cleanup ({len(bench._stored_ids)} memories left in DB).[/dim]")

    bench.close()


if __name__ == "__main__":
    app()
