param(
    [switch]$NoBuild,
    [switch]$SkipOllamaPreflight,
    [switch]$KeepServices
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

Write-Host "[docker-remote-mcp-e2e] project root: $Root"
Write-Host "[docker-remote-mcp-e2e] checking docker compose config"
docker compose --profile test config --quiet

if (-not $SkipOllamaPreflight) {
    Write-Host "[docker-remote-mcp-e2e] checking host Ollama at http://localhost:11434/api/tags"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:11434/api/tags" -TimeoutSec 5 | Out-Null
        Write-Host "[docker-remote-mcp-e2e] host Ollama reachable"
    }
    catch {
        Write-Warning "Host Ollama is not reachable. The e2e may fail when the server needs embeddings. Use -SkipOllamaPreflight only if the test profile is configured for another embedding source."
    }
}

$composeArgs = @(
    "compose",
    "--profile", "test",
    "up",
    "--abort-on-container-exit",
    "--exit-code-from", "mcp-e2e-test-runner"
)
if (-not $NoBuild) {
    $composeArgs += "--build"
}
$composeArgs += "mcp-e2e-test-runner"

try {
    Write-Host "[docker-remote-mcp-e2e] running: docker $($composeArgs -join ' ')"
    & docker @composeArgs
}
finally {
    if (-not $KeepServices) {
        Write-Host "[docker-remote-mcp-e2e] stopping test-only services"
        docker compose --profile test stop mcp-e2e-test-runner memory-server-test qdrant-test
    }
    else {
        Write-Host "[docker-remote-mcp-e2e] keeping test services running for inspection"
    }
}
