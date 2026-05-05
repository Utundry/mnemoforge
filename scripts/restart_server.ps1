[CmdletBinding()]
param(
  [ValidateSet("local", "docker")]
  [string]$Mode = "local",

  [int]$Port = 0,
  [string]$ListenHost = "",
  [switch]$Reload,
  [switch]$WaitHealthy,
  [int]$WaitSeconds = 15,
  [bool]$FailOnUnhealthy = $true,
  [switch]$AllowSharedDb,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Read-DotEnvValue {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Key
  )
  if (!(Test-Path $Path)) { return $null }
  foreach ($line in (Get-Content $Path -ErrorAction SilentlyContinue)) {
    $t = ""
    if ($null -ne $line) { $t = "$line" }
    $t = $t.Trim()
    if (!$t -or $t.StartsWith("#")) { continue }
    $m = [regex]::Match($t, "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")
    if (!$m.Success) { continue }
    if ($m.Groups[1].Value -ne $Key) { continue }
    $v = $m.Groups[2].Value.Trim()
    if (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'"))) {
      $v = $v.Substring(1, $v.Length - 2)
    }
    return $v
  }
  return $null
}

function Stop-ByPidFile {
  param([string]$PidPath)
  if (!(Test-Path $PidPath)) { return $false }
  $raw = (Get-Content $PidPath -Raw -ErrorAction SilentlyContinue).Trim()
  if (!$raw) { return $false }
  $procId = 0
  if (![int]::TryParse($raw, [ref]$procId)) { return $false }
  $removePidFile = $false
  try {
    try {
      $null = Get-Process -Id $procId -ErrorAction Stop
    } catch {
      # Stale pidfile: process is gone.
      $removePidFile = $true
      return $false
    }

    if ($DryRun) {
      Write-Host "DRYRUN: Stop-Process -Id $procId -Force"
      $removePidFile = $true
      return $true
    }

    try {
      Stop-Process -Id $procId -Force -ErrorAction Stop
      $removePidFile = $true
      return $true
    } catch {
      # Couldn't stop the process — keep pidfile so the user can inspect it.
      $removePidFile = $false
      return $false
    }
  } finally {
    if ($removePidFile) {
      if ($DryRun) {
        Write-Host "DRYRUN: Remove-Item $PidPath -Force"
      } else {
        Remove-Item $PidPath -Force -ErrorAction SilentlyContinue | Out-Null
      }
    }
  }
}

function Get-ListeningPids {
  param([int]$Port)
  try {
    $lines = netstat -ano -p tcp | Select-String -Pattern "LISTENING"
    if (!$lines) { return @() }

    $pids = @()
    foreach ($m in $lines) {
      $parts = ($m.Line -split "\s+") | Where-Object { $_ -ne "" }
      if ($parts.Count -lt 5) { continue }
      $local = $parts[1]
      $pidRaw = $parts[$parts.Count - 1]
      if ($local -notmatch (":$Port$")) { continue }
      $resolvedPid = 0
      if ([int]::TryParse($pidRaw, [ref]$resolvedPid)) {
        $pids += $resolvedPid
      }
    }

    return @($pids | Select-Object -Unique)
  } catch {
    return @()
  }
}

function Stop-ByPort {
  param([int]$Port)
  try {
    # Get-NetTCPConnection часто требует повышенных прав; netstat работает почти всегда.
    $pids = @(Get-ListeningPids -Port $Port)
    if (!$pids -or $pids.Count -eq 0) { return $false }

    foreach ($procId in $pids) {
      if ($DryRun) {
        Write-Host "DRYRUN: Stop-Process -Id $procId -Force (port $Port)"
      } else {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
      }
    }
    return $true
  } catch {
    return $false
  }
}

function Wait-PortReleased {
  param([int]$Port, [int]$Seconds = 10)
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    $listeners = @(Get-ListeningPids -Port $Port)
    if ($listeners.Count -eq 0) {
      return $true
    }
    Start-Sleep -Milliseconds 250
  }
  return $false
}

function Wait-ListenerPids {
  param([int]$Port, [int]$Seconds = 5)
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    $listeners = @(Get-ListeningPids -Port $Port)
    if ($listeners.Count -gt 0) {
      return $listeners
    }
    Start-Sleep -Milliseconds 250
  }
  return @()
}

function Wait-ServerPid {
  param(
    [int]$Port,
    [string]$ApiKey,
    [int]$Seconds = 5
  )
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $headers = @{}
      if ($ApiKey) {
        $headers["X-Api-Key"] = $ApiKey
      }
      $r = Invoke-RestMethod -TimeoutSec 2 -Uri "http://127.0.0.1:$Port/api/v1/admin/status" -Method Get -Headers $headers
      $resolvedPid = 0
      if ($r -and [int]::TryParse("$($r.pid)", [ref]$resolvedPid) -and $resolvedPid -gt 0) {
        return $resolvedPid
      }
    } catch {}
    Start-Sleep -Milliseconds 250
  }
  return 0
}

function Wait-LoggedServerPid {
  param(
    [string]$ErrLogPath,
    [int]$Seconds = 5
  )
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    try {
      if (Test-Path $ErrLogPath) {
        $matches = Select-String -Path $ErrLogPath -Pattern 'Started server process \[(\d+)\]' -AllMatches
        if ($matches) {
          $last = $matches | Select-Object -Last 1
          $resolvedPid = 0
          if ($last.Matches.Count -gt 0 -and [int]::TryParse($last.Matches[0].Groups[1].Value, [ref]$resolvedPid) -and $resolvedPid -gt 0) {
            return $resolvedPid
          }
        }
      }
    } catch {}
    Start-Sleep -Milliseconds 250
  }
  return 0
}

function Test-ProcessAlive {
  param([int]$ProcessId)
  if ($ProcessId -le 0) { return $false }
  try {
    $null = Get-Process -Id $ProcessId -ErrorAction Stop
    return $true
  } catch {
    return $false
  }
}

function Read-JsonFile {
  param([string]$Path)
  try {
    if (!(Test-Path $Path)) { return $null }
    return Get-Content $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
  } catch {
    return $null
  }
}

function Test-RuntimeOwnerActive {
  param(
    [object]$Owner,
    [double]$StaleSeconds = 120.0
  )
  if ($null -eq $Owner) { return $false }
  try {
    $updatedAt = [double]$Owner.updated_at
    if ($updatedAt -le 0) { return $false }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    return (($now - $updatedAt) -lt $StaleSeconds)
  } catch {
    return $false
  }
}

function Get-RunningComposeServices {
  try {
    $raw = docker compose ps --services --filter status=running 2>$null
    if (!$raw) { return @() }
    return @($raw | Where-Object { $_ })
  } catch {
    return @()
  }
}

function Assert-LocalRuntimeCanOwnDb {
  param(
    [string]$RepoRoot,
    [switch]$AllowSharedDb
  )
  if ($AllowSharedDb -or $env:MNEMOFORGE_ALLOW_SHARED_DB_RUNTIME -eq "1") { return }

  $ownerPath = Join-Path $RepoRoot "qdrant_data\runtime_owner.json"
  $owner = Read-JsonFile -Path $ownerPath
  if ((Test-RuntimeOwnerActive -Owner $owner) -and "$($owner.runtime_kind)" -ne "host") {
    throw "Refusing to start host runtime: qdrant_data is actively owned by $($owner.owner_id). Stop that runtime or pass -AllowSharedDb for an explicit unsafe override."
  }

  $services = @(Get-RunningComposeServices)
  if ($services -contains "memory-server-dev") {
    throw "Refusing to start host runtime: Docker service memory-server-dev is running and uses the same qdrant_data directory. Stop it or pass -AllowSharedDb for an explicit unsafe override."
  }
}

function Stop-UvicornFallback {
  param([int]$Port)
  try {
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop
    $match = $procs | Where-Object {
      ($_.CommandLine -like "*uvicorn*app.main:app*") -and ($_.CommandLine -like "*--port $Port*")
    } | Select-Object -First 1
    if (!$match) { return $false }
    $procId = [int]$match.ProcessId
    if ($DryRun) {
      Write-Host "DRYRUN: Stop-Process -Id $procId -Force (uvicorn cmdline match)"
    } else {
      Stop-Process -Id $procId -Force
    }
    return $true
  } catch {
    return $false
  }
}

function Wait-Health {
  param([int]$Port, [int]$Seconds)
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    try {
      # Use 127.0.0.1 (avoid IPv6 ::1 localhost resolution on Windows when server listens on IPv4 only)
      $r = Invoke-RestMethod -TimeoutSec 2 -Uri "http://127.0.0.1:$Port/api/v1/health" -Method Get
      # /health returns status: ok|degraded; older scripts expected "healthy".
      if ($r -and ($r.status -eq "ok" -or $r.status -eq "healthy" -or $r.status -eq "degraded")) {
        return $true
      }
    } catch {}
    Start-Sleep -Milliseconds 400
  }
  return $false
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $repoRoot
try {
  $envPath = Join-Path $repoRoot ".env"
  $apiKey = $env:API_KEY
  if (!$apiKey) {
    $apiKey = Read-DotEnvValue -Path $envPath -Key "API_KEY"
  }
  if ($Port -le 0) {
    $p = Read-DotEnvValue -Path $envPath -Key "SERVER_PORT"
    if ($p) { [void][int]::TryParse($p, [ref]$Port) }
  }
  if ($Port -le 0) { $Port = 8000 }

  if (!$ListenHost) {
    $h = Read-DotEnvValue -Path $envPath -Key "SERVER_HOST"
    $ListenHost = if ($h) { $h } else { "0.0.0.0" }
  }

  if (!$PSBoundParameters.ContainsKey("WaitHealthy")) {
    $WaitHealthy = $true
  }

  if ($Mode -eq "docker") {
    if ($DryRun) {
      Write-Host "DRYRUN: docker compose restart memory-server"
    } else {
      docker compose restart memory-server
    }
    exit 0
  }

  Assert-LocalRuntimeCanOwnDb -RepoRoot $repoRoot -AllowSharedDb:$AllowSharedDb

  $pidFile = Join-Path $repoRoot ".server.pid"
  $null = Stop-ByPidFile -PidPath $pidFile
  Start-Sleep -Milliseconds 300
  if (@(Get-ListeningPids -Port $Port).Count -gt 0) {
    $null = Stop-ByPort -Port $Port
    Start-Sleep -Milliseconds 300
  }
  if (@(Get-ListeningPids -Port $Port).Count -gt 0) {
    $null = Stop-UvicornFallback -Port $Port
  }
  if (-not (Wait-PortReleased -Port $Port -Seconds 10)) {
    $stuck = @(Get-ListeningPids -Port $Port)
    throw "Port $Port is still busy after restart pre-stop sequence. Listening PIDs: $($stuck -join ', ')"
  }

  $python = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
  if (!(Test-Path $python)) {
    throw "Python venv not found: $python. Create venv first (see SETUP.md)."
  }

  $logDir = Join-Path $repoRoot "logs"
  if ($DryRun) {
    Write-Host "DRYRUN: New-Item -ItemType Directory -Force $logDir"
  } else {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  }

  $stdout = Join-Path $logDir "uvicorn.log"
  $stderr = Join-Path $logDir "uvicorn.err.log"
  $ts = Get-Date -Format "yyyyMMdd-HHmmss"

  function Move-WithRetry {
    param(
      [Parameter(Mandatory = $true)][string]$Src,
      [Parameter(Mandatory = $true)][string]$Dst,
      [int]$Retries = 6,
      [int]$DelayMs = 250
    )
    for ($i = 0; $i -lt $Retries; $i++) {
      try {
        Move-Item -Force -ErrorAction Stop $Src $Dst
        return $true
      } catch {
        if ($i -ge ($Retries - 1)) { return $false }
        Start-Sleep -Milliseconds $DelayMs
      }
    }
    return $false
  }

  # Default: keep stable names. If rotation fails due to file locks, fall back to per-run log filenames.
  $stdoutRun = $stdout
  $stderrRun = $stderr

  if (Test-Path $stdout) {
    $bak = Join-Path $logDir "uvicorn.$ts.bak.log"
    if ($DryRun) {
      Write-Host "DRYRUN: Move-Item $stdout -> $bak"
    } else {
      $moved = Move-WithRetry -Src $stdout -Dst $bak
      if (-not $moved) {
        Write-Warning "Cannot rotate $stdout (locked). Using per-run log file for this start."
        $stdoutRun = Join-Path $logDir "uvicorn.$ts.log"
      }
    }
  }
  if (Test-Path $stderr) {
    $bak = Join-Path $logDir "uvicorn.$ts.bak.err.log"
    if ($DryRun) {
      Write-Host "DRYRUN: Move-Item $stderr -> $bak"
    } else {
      $moved = Move-WithRetry -Src $stderr -Dst $bak
      if (-not $moved) {
        Write-Warning "Cannot rotate $stderr (locked). Using per-run log file for this start."
        $stderrRun = Join-Path $logDir "uvicorn.$ts.err.log"
      }
    }
  }

  $args = @(
    "-m", "uvicorn",
    "app.main:app",
    "--app-dir", $repoRoot,
    "--host", $ListenHost,
    "--port", "$Port"
  )
  if ($Reload) { $args += "--reload" }

  if ($DryRun) {
    Write-Host "DRYRUN: Start-Process -FilePath $python -ArgumentList $($args -join ' ')"
    Write-Host "DRYRUN: (stdout) $stdoutRun"
    Write-Host "DRYRUN: (stderr) $stderrRun"
    Write-Host "DRYRUN: Write pid to $pidFile"
  } else {
    $proc = Start-Process -FilePath $python `
      -ArgumentList $args `
      -WorkingDirectory $repoRoot `
      -RedirectStandardOutput $stdoutRun `
      -RedirectStandardError $stderrRun `
      -PassThru `
      -WindowStyle Hidden
    Set-Content -Path $pidFile -Value $proc.Id -Encoding ascii
  }

  if ($WaitHealthy -and -not $DryRun) {
    $ok = Wait-Health -Port $Port -Seconds $WaitSeconds
    if (!$ok) {
      $msg = "Server started but health check did not pass in ${WaitSeconds}s. Check logs: $stdoutRun / $stderrRun"
      $stderrTail = ""
      try {
        if (Test-Path $stderrRun) {
          $stderrTail = (Get-Content -Path $stderrRun -Tail 40 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
        }
      } catch {}
      if ($FailOnUnhealthy) {
        if ($stderrTail) {
          throw "$msg`n---- stderr tail ----`n$stderrTail"
        }
        throw $msg
      }
      Write-Warning $msg
    } else {
      $serverPid = 0
      $listenerPids = @(Wait-ListenerPids -Port $Port -Seconds 5)
      if ($serverPid -le 0) {
        $serverPid = Wait-ServerPid -Port $Port -ApiKey $apiKey -Seconds 5
      }
      if ($serverPid -le 0) {
        $serverPid = Wait-LoggedServerPid -ErrLogPath $stderrRun -Seconds 5
      }
      if ($serverPid -le 0 -and $listenerPids.Count -eq 1) {
        $serverPid = $listenerPids[0]
      }
      if ($serverPid -le 0 -and (Test-ProcessAlive -ProcessId $proc.Id)) {
        $serverPid = $proc.Id
      }
      if ($serverPid -gt 0 -and (Test-ProcessAlive -ProcessId $serverPid)) {
        Set-Content -Path $pidFile -Value $serverPid -Encoding ascii
      } else {
        if ($listenerPids.Count -eq 1) {
          Set-Content -Path $pidFile -Value $listenerPids[0] -Encoding ascii
        } elseif ($listenerPids.Count -gt 1) {
          Write-Warning "Multiple listeners detected on port ${Port}: $($listenerPids -join ', '). Keeping initial pidfile."
        } else {
          Write-Warning "Health check passed but no listener PID could be resolved for port $Port. Keeping initial pidfile."
        }
      }
    }
  }

  Write-Host "OK: server restarted on http://127.0.0.1:$Port (mode=$Mode reload=$Reload)"
  if (!$DryRun) {
    Write-Host "PID: $(Get-Content $pidFile -Raw)"
    Write-Host "Logs: $stdoutRun"
  }
}
finally {
  Pop-Location
}
