param(
    [string]$Root = "",
    [string]$OutDir = "backups",
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"

$legacyRootName = "qdrant_data"
$canonicalRootName = "system_data"
if ([string]::IsNullOrWhiteSpace($Root)) {
    if (-not [string]::IsNullOrWhiteSpace($env:SLOPLESSCODE_DATA_DIR)) {
        $Root = $env:SLOPLESSCODE_DATA_DIR
    } elseif (-not [string]::IsNullOrWhiteSpace($env:MNEMOFORGE_DATA_DIR)) {
        $Root = $env:MNEMOFORGE_DATA_DIR
    } elseif (Test-Path -LiteralPath $canonicalRootName -PathType Container) {
        $Root = $canonicalRootName
    } else {
        $Root = $legacyRootName
    }
}

$rootPath = $null
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "Root path not found or is not a directory: $Root"
}
$rootPath = (Resolve-Path -LiteralPath $Root).Path

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupName = "system_data-backup-$timestamp"
$stagingDir = Join-Path $OutDir $backupName
$zipFile = "$stagingDir.zip"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

# Keep a predictable top-level folder inside the backup.
$payloadRoot = Join-Path $stagingDir $canonicalRootName
New-Item -ItemType Directory -Force -Path $payloadRoot | Out-Null
Copy-Item -Recurse -Force -Path (Join-Path $rootPath "*") -Destination $payloadRoot

$copiedFiles = Get-ChildItem -Path $payloadRoot -File -Recurse -ErrorAction SilentlyContinue
$manifest = [pscustomobject]@{
    backup_name = $backupName
    created_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    source_root = $rootPath
    data_root_kind = "system_data"
    legacy_root_name = $legacyRootName
    canonical_root_name = $canonicalRootName
    file_count = @($copiedFiles).Count
    sqlite_files = @(
        Get-ChildItem -Path $payloadRoot -File -Recurse -Filter "*.db" -ErrorAction SilentlyContinue |
        ForEach-Object { $_.FullName.Replace($payloadRoot + "\", "") }
    )
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $stagingDir "manifest.json") -Encoding UTF8

if ($SkipZip) {
    Write-Host ("Backup staging directory created: " + $stagingDir)
    exit 0
}

Compress-Archive -Path (Join-Path $stagingDir "*") -DestinationPath $zipFile -Force
Remove-Item -Recurse -Force -LiteralPath $stagingDir

Write-Host ("Backup archive created: " + $zipFile)
