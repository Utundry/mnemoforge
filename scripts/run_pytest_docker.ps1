[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Build,
    [switch]$NoBuild,
    [string]$Service = "mcp-e2e-test-runner",
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Invoke-DockerChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$DockerArgs,
        [switch]$AllowFailure
    )

    & docker @DockerArgs
    $exitCode = $LASTEXITCODE
    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw "docker $($DockerArgs -join ' ') failed with exit code $exitCode"
    }
    if ($AllowFailure) {
        Write-Verbose "docker $($DockerArgs -join ' ') exited with $exitCode"
    }
}

Write-Host "[pytest-docker] project root: $Root"
Write-Host "[pytest-docker] service: $Service"
Write-Host "[pytest-docker] checking docker compose test profile"
Invoke-DockerChecked @("compose", "--profile", "test", "config", "--quiet")

if ($Build -and $NoBuild) {
    throw "Use either -Build or -NoBuild, not both."
}

$imageId = ""
if (-not $Build -and -not $NoBuild) {
    & docker compose --profile test images -q $Service | ForEach-Object {
        if (-not $imageId -and $_) {
            $imageId = $_.Trim()
        }
    }
}

if ($Build -or (-not $NoBuild -and -not $imageId)) {
    if ($Build) {
        Write-Host "[pytest-docker] building test runner image (-Build requested)"
    }
    else {
        Write-Host "[pytest-docker] building test runner image (image not found)"
    }
    Invoke-DockerChecked @("compose", "--profile", "test", "build", $Service)
}
else {
    Write-Host "[pytest-docker] using existing test runner image; source/tests are mounted from the workspace"
}

$runArgs = @(
    "compose",
    "--profile", "test",
    "run",
    "--rm",
    $Service,
    "python",
    "-m",
    "pytest"
)

if ($PytestArgs -and $PytestArgs.Count -gt 0) {
    $runArgs += $PytestArgs
    Write-Host "[pytest-docker] running: docker $($runArgs -join ' ')"
}
else {
    Write-Host "[pytest-docker] running full test suite"
}

try {
    Invoke-DockerChecked $runArgs
}
finally {
    Write-Host "[pytest-docker] stopping test-only services"
    Invoke-DockerChecked @("compose", "--profile", "test", "stop", "mcp-e2e-test-runner", "memory-server-test", "qdrant-test") -AllowFailure | Out-Null
}
