param(
    [string]$Root = "qdrant_data",
    [string]$OutDir = "backups"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Root)) {
    throw "Root path not found: $Root"
}

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $OutDir ("qdrant_data-backup-" + $ts)

New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Recurse -Force -Path (Join-Path $Root "*") -Destination $dest

Write-Host ("Backup created: " + $dest)

