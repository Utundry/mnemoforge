param (
    [switch]$NoStopDev,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

function Run-Docker {
    param(
        [Parameter(Mandatory)]
        [string[]]$Args
    )
    $commandLine = "docker compose  $($Args -join ' ')"
    Write-Host $commandLine
    $process = & docker compose @Args
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE"
    }
}

function Copy-ImprovementsDb {
    $dbPath = "qdrant_data/improvements.db"
    if (-not (Test-Path $dbPath)) {
        Write-Warning "Host improvements DB not found at $dbPath; skipping copy."
        return
    }
    Write-Host "Copying $dbPath into production container..."
    $containerId = & docker compose ps -q memory-server
    if (-not $containerId) {
        Write-Warning "Could not resolve memory-server container ID; skipping DB copy."
        return
    }
    Write-Host "Ensuring /app/qdrant_data exists inside container..."
    $mkdirArgs = @("exec", "memory-server", "mkdir", "-p", "/app/qdrant_data")
    Run-Docker $mkdirArgs
    $copyArgs = @("cp", $dbPath, "$($containerId):/app/qdrant_data/improvements.db")
    Write-Host "docker $($copyArgs -join ' ')"
    $process = & docker @copyArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to copy improvements DB to production container."
    }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = Split-Path -Parent $scriptDir
Push-Location $repoRoot
try {
    if (-not $SkipBuild) {
        Write-Host "Building updated production image (memory-server)..."
        Run-Docker @("build", "memory-server")
    }

    if (-not $NoStopDev) {
        Write-Host "Stopping dev service (memory-server-dev) so it does not conflict..."
        try {
            Run-Docker @("stop", "memory-server-dev")
        } catch {
            Write-Warning "Stopping dev service failed (it might already be stopped): $_"
        }
    }

    Write-Host "Recreating production container (memory-server)..."
    Run-Docker @("up", "-d", "--force-recreate", "--no-deps", "memory-server")

    Copy-ImprovementsDb

    $published = ""
    try {
        $published = (& docker compose port memory-server 8000 2>$null | Select-Object -First 1).Trim()
    } catch {
        $published = ""
    }
    if ($published) {
        Write-Host "Production update complete. memory-server is published at $published."
    } else {
        Write-Host "Production update complete. Run 'docker compose port memory-server 8000' to check published host port."
    }
}
finally {
    Pop-Location
}
