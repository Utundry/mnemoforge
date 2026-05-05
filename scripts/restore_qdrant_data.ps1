param(
    [Parameter(Mandatory = $true)]
    [string]$ArchivePath,
    [string]$Root = "qdrant_data",
    [switch]$NoBackup
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    throw "Archive not found: $ArchivePath"
}

$archiveFullPath = (Resolve-Path -LiteralPath $ArchivePath).Path
$rootPath = [System.IO.Path]::GetFullPath($Root)
if ($rootPath -match "^[A-Za-z]:\\?$") {
    throw "Refusing to restore into a drive root path: $rootPath"
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tempExtractDir = Join-Path ([System.IO.Path]::GetTempPath()) ("mnemoforge-restore-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tempExtractDir | Out-Null

try {
    Expand-Archive -LiteralPath $archiveFullPath -DestinationPath $tempExtractDir -Force

    $payloadRoot = Join-Path $tempExtractDir "qdrant_data"
    if (-not (Test-Path -LiteralPath $payloadRoot -PathType Container)) {
        # Backward compatibility with older backups that store files at archive root.
        $payloadRoot = $tempExtractDir
    }

    $payloadFiles = Get-ChildItem -Path $payloadRoot -File -Recurse -ErrorAction SilentlyContinue
    if (@($payloadFiles).Count -eq 0) {
        throw "Archive payload is empty: $archiveFullPath"
    }

    if (Test-Path -LiteralPath $rootPath -PathType Container) {
        if (-not $NoBackup) {
            $preRestoreBackup = "$rootPath.pre-restore-$timestamp"
            Write-Host ("Creating pre-restore backup: " + $preRestoreBackup)
            Copy-Item -Recurse -Force -LiteralPath $rootPath -Destination $preRestoreBackup
        }
        Remove-Item -Recurse -Force -LiteralPath $rootPath
    }

    New-Item -ItemType Directory -Force -Path $rootPath | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $payloadRoot "*") -Destination $rootPath

    $restoredFiles = Get-ChildItem -Path $rootPath -File -Recurse -ErrorAction SilentlyContinue
    Write-Host ("Restore complete. Restored files: " + @($restoredFiles).Count)
    Write-Host ("Target root: " + $rootPath)
}
finally {
    if (Test-Path -LiteralPath $tempExtractDir) {
        Remove-Item -Recurse -Force -LiteralPath $tempExtractDir
    }
}
