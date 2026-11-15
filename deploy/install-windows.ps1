<#
.SYNOPSIS
  Install or update mt5-bridge in the Windows/UTM guest.

.DESCRIPTION
  Run elevated as the interactive user that owns the MetaTrader 5 session.
  The application tree is expected in <InstallRoot>\app (a copy of
  this repository); configuration, TLS material, the SQLite journal and
  logs live beside it and survive every redeploy. The script:
    - syncs the locked environment with uv on x64 CPython 3.14 (the
      MetaTrader5 wheel is win_amd64 only; Windows on ARM runs it emulated);
    - restricts the install tree to the user, Administrators and SYSTEM;
    - admits inbound TCP to the bridge port only from the host address;
    - registers two at-logon tasks in the interactive session: the terminal
      (a Session 0 service cannot see it) and the bridge, restarted on failure.
  Windows auto-logon is a separate, manual step: it needs the account
  password and is configured by the operator (see docs/runbook.md).
#>
param(
    [string]$InstallRoot = 'C:\mt5-bridge',
    [string]$HostAddress = '192.168.64.1',
    [Parameter(Mandatory = $true)][string]$BindAddress,
    [int]$Port = 8765,
    [string]$TerminalPath = 'C:\Program Files\MetaTrader 5\terminal64.exe',
    [string]$Python = 'cpython-3.14-windows-x86_64-none'
)
# Cmdlets below use -ErrorAction Stop; native tools (uv) are checked by exit
# code, because Windows PowerShell 5.1 turns any native stderr line into a
# terminating error under a global 'Stop' preference.
$ErrorActionPreference = 'Continue'

$user = "$env:USERDOMAIN\$env:USERNAME"
$app = Join-Path $InstallRoot 'app'
if (-not (Test-Path (Join-Path $app 'pyproject.toml'))) { throw "application tree not found in $app" }
foreach ($dir in 'config', 'data', 'logs', 'tls') { New-Item -ItemType Directory -Force (Join-Path $InstallRoot $dir) -ErrorAction Stop | Out-Null }

# Only the operator account, Administrators and SYSTEM may read the tree (TLS key,
# journal): set the ACL on the root and make every child inherit it (container
# inheritance flags do not apply to files, so the grant is never applied with /T).
icacls $InstallRoot /inheritance:r /grant:r "${user}:(OI)(CI)F" 'Administrators:(OI)(CI)F' 'SYSTEM:(OI)(CI)F' /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "restricting the ACL of $InstallRoot failed with exit code $LASTEXITCODE" }
icacls (Join-Path $InstallRoot '*') /reset /T /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "resetting child ACLs under $InstallRoot failed with exit code $LASTEXITCODE" }

$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path $uv)) { throw "uv not found at $uv; install it for $user first" }
& $uv python install $Python 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "uv python install failed with exit code $LASTEXITCODE" }
Push-Location $app
try {
    & $uv sync --locked --no-dev --python $Python 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }
} finally { Pop-Location }

$ruleName = 'mt5-bridge'
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -ErrorAction Stop -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort $Port -LocalAddress $BindAddress -RemoteAddress $HostAddress | Out-Null

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$terminalAction = New-ScheduledTaskAction -Execute $TerminalPath
Register-ScheduledTask -ErrorAction Stop -TaskName 'mt5-terminal' -Action $terminalAction -Principal $principal `
    -Trigger $trigger -Settings $settings -Force | Out-Null

$bridgeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$bridgeTrigger.Delay = 'PT30S'
$start = Join-Path $app 'deploy\start-bridge.ps1'
$bridgeAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$start`" -InstallRoot `"$InstallRoot`""
Register-ScheduledTask -ErrorAction Stop -TaskName 'mt5-bridge' -Action $bridgeAction -Principal $principal `
    -Trigger $bridgeTrigger -Settings $settings -Force | Out-Null

"installed: app=$app bind=${BindAddress}:$Port allowed-from=$HostAddress tasks='mt5-terminal','mt5-bridge'"
