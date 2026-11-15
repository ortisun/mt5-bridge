<#
.SYNOPSIS
  Start mt5-bridge in the interactive session and keep it running.

.DESCRIPTION
  Loads <InstallRoot>\config\bridge.env (KEY=VALUE lines; '#' comments) into
  this process only, waits for the MetaTrader 5 terminal, then runs the
  bridge, restarting it after an exit. Terminal disconnections are handled
  inside the bridge (terminal watchdog); this loop only covers the
  process itself. Output goes to <InstallRoot>\logs\bridge-<date>.log.
#>
param([string]$InstallRoot = 'C:\mt5-bridge')
$ErrorActionPreference = 'Continue'

$app = Join-Path $InstallRoot 'app'
$config = Join-Path $InstallRoot 'config\bridge.env'
$logs = Join-Path $InstallRoot 'logs'
if (-not (Test-Path $config)) { throw "missing $config" }
foreach ($line in Get-Content $config) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
    $name, $value = $trimmed -split '=', 2
    [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
}
if ($env:MT5_BRIDGE_CLIENT_BACKEND -ne 'real') { throw 'bridge.env must set MT5_BRIDGE_CLIENT_BACKEND=real' }

$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
while ($true) {
    $log = Join-Path $logs ("bridge-{0:yyyyMMdd}.log" -f (Get-Date))
    $deadline = (Get-Date).AddMinutes(5)
    while (-not (Get-Process terminal64 -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep 5 }
    "$(Get-Date -Format o) starting bridge" | Add-Content -Encoding ascii $log
    Push-Location $app
    # Through cmd.exe so uvicorn's stderr lands in the log as plain text;
    # Windows PowerShell 5.1 would wrap each stderr line as an error record.
    & cmd.exe /d /c "`"$uv`" run --locked --no-sync python -m mt5_bridge.main >> `"$log`" 2>&1"
    $code = $LASTEXITCODE
    Pop-Location
    "$(Get-Date -Format o) bridge exited with code $code; restarting in 15 s" | Add-Content -Encoding ascii $log
    Start-Sleep 15
}
