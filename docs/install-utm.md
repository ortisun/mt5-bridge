# Installing the bridge on the Windows guest in UTM

The MetaTrader 5 terminal and the bridge run together inside a Windows guest
of [UTM](https://mac.getutm.app/) on macOS. The host, and every client on
it, reaches the bridge over the network between host and guest.

## What has been tested and what has not

- The scripts in `deploy/` were used to install the bridge on a Windows 11
  guest with a Rico demo account in September 2026.
- Nothing on this page was run again since then, and the install root, the
  task names, the firewall rule name and the default secrets mount were
  renamed after that run. The scripts need Windows and cannot run on macOS,
  so the checks of this repository do not cover them.
- `make package` (step 1) was run on macOS and produces the archive. Unpacking
  it in the guest (step 4) is new and has not been tried there.
- The certificate command in step 5 is a suggestion. How the certificate of
  the current guest was made is not recorded.
- The Windows job in CI installs the locked environment on a hosted Windows
  x64 machine. That is not this guest, and the job has not run yet.

Treat the first install from this repository as a test of this page.

## What ends up in the guest

```
C:\mt5-bridge\
  app\      this repository at one commit, with its .venv
  config\   bridge.env
  tls\      bridge-cert.pem, bridge-key.pem (only when serving HTTPS)
  data\     mt5_bridge.sqlite3: order journal, kill switch, sequence log
  logs\     bridge-<date>.log
```

Two scheduled tasks start at logon of the Windows user, in the interactive
session: `mt5-terminal` (the terminal) and `mt5-bridge`
(the bridge, 30 seconds later, restarted when it exits). A firewall rule
named `mt5-bridge` admits the bridge port only from the host
address.

## Before you start

On the host (macOS):

- UTM with a Windows 11 guest. On Apple Silicon the guest is Windows on ARM;
  the terminal, the `MetaTrader5` wheel and Python run as x64 under
  emulation.
- The guest's network in UTM is the shared network, so host and guest can
  reach each other. Note the two addresses: the host as the guest sees it
  (`192.168.64.1` is what `install-windows.ps1` assumes) and the guest's own
  address (`ipconfig` in the guest).

In the guest:

- MetaTrader 5 from the broker, installed and logged in to the account.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed
  for the Windows user that owns the terminal session. The scripts expect it
  at `%USERPROFILE%\.local\bin\uv.exe`.

## Steps

### 1. Package the application on the host

From the repository root, on the commit to deploy (a release tag, once
releases exist):

```bash
make package
```

It writes `dist/mt5-bridge-app.zip` with the committed files under `app/`.
Uncommitted changes are not included. Once releases exist, the same archive
is attached to each GitHub Release, so it can be downloaded instead.

### 2. Copy the archive into the guest

Use a UTM shared directory or any other way to place
`mt5-bridge-app.zip` in the guest.

### 3. Stop a running bridge (redeploy only)

In an elevated PowerShell in the guest:

```powershell
Stop-ScheduledTask -TaskName 'mt5-bridge'
```

Then end any `python.exe` still running from `C:\mt5-bridge\app`,
and remove the old tree:

```powershell
Remove-Item -Recurse -Force C:\mt5-bridge\app
```

Remove only `app\`. Never delete `data\`: the SQLite file holds the kill
switch state, the idempotency record of every order and the daily-loss
baseline.

### 4. Unpack

```powershell
New-Item -ItemType Directory -Force C:\mt5-bridge | Out-Null
Expand-Archive -Path <path to>\mt5-bridge-app.zip -DestinationPath C:\mt5-bridge
```

`C:\mt5-bridge\app\pyproject.toml` must exist afterwards.

### 5. Transport: HTTPS or plain HTTP

The bridge serves HTTPS when both `MT5_BRIDGE_TLS_CERTFILE` and
`MT5_BRIDGE_TLS_KEYFILE` are set, and plain HTTP when both are unset. For
HTTPS, one way to make a self-signed certificate for the guest address, on
the host:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout bridge-key.pem -out bridge-cert.pem -subj "/CN=mt5-bridge" -addext "subjectAltName=IP:<guest IP>"
```

Copy both files to `C:\mt5-bridge\tls\` in the guest and keep
`bridge-cert.pem` on the host; clients pin it as their CA file. The key
stays out of every repository.

### 6. Secrets in environment variables

The bridge reads its secrets from environment variables of the Windows user
that runs it, not from `bridge.env`.

The bridge attaches to the terminal that is already logged in, and then it
needs no secret: skip this step. Only when it must log the terminal in
itself, set the account, all three variables, in PowerShell in the guest:

```powershell
[Environment]::SetEnvironmentVariable('MT5_BRIDGE_LOGIN', '<account number>', 'User')
[Environment]::SetEnvironmentVariable('MT5_BRIDGE_PASSWORD', '<password>', 'User')
[Environment]::SetEnvironmentVariable('MT5_BRIDGE_SERVER', '<server>', 'User')
```

A user variable reaches a process started after it was set: log off and on
again (or restart the guest) before starting the tasks.

### 7. Configuration

Copy `app\deploy\bridge.env.example` to
`C:\mt5-bridge\config\bridge.env` and fill in every `<...>`:

| Setting | Value |
| --- | --- |
| `MT5_BRIDGE_CLIENT_BACKEND` | `real` (the start script refuses anything else) |
| `MT5_BRIDGE_ENVIRONMENT` | `demo` or `paper` for a demo account; boot refuses a non-demo account in these |
| `MT5_BRIDGE_EXPECTED_LOGIN`, `MT5_BRIDGE_EXPECTED_SERVER` | the account and server the terminal is logged in to; boot refuses any other |
| `MT5_BRIDGE_POSITION_ACCOUNTING` | `NETTING`; boot refuses a hedging account |
| `MT5_BRIDGE_HOST`, `MT5_BRIDGE_PORT` | the guest address and the port (8765) |
| `MT5_BRIDGE_TLS_CERTFILE`, `MT5_BRIDGE_TLS_KEYFILE` | the files from step 5, or remove both lines for plain HTTP |
| `MT5_BRIDGE_TERMINAL_TIME_ZONE` | `America/Sao_Paulo` when the terminal reports its server's wall clock as epochs (true for Rico demo); unset for a terminal that reports UTC |

No broker password goes in this file (step 6). Limits
(`MT5_BRIDGE_SYMBOL_ALLOWLIST`, `MT5_BRIDGE_MAX_VOLUME_PER_ORDER`,
`MT5_BRIDGE_MAX_ORDERS_PER_MINUTE`, `MT5_BRIDGE_MAX_OPEN_NOTIONAL`,
`MT5_BRIDGE_MAX_DAILY_LOSS`) are set here too.

### 8. Install

In an elevated PowerShell, as the Windows user that owns the terminal
session:

```powershell
powershell -ExecutionPolicy Bypass -File C:\mt5-bridge\app\deploy\install-windows.ps1 -BindAddress <guest IP>
```

Parameters: `-HostAddress` (default `192.168.64.1`), `-Port` (default
8765), `-InstallRoot`, `-TerminalPath`. The script:

1. creates `config\`, `data\`, `logs\` and `tls\`;
2. restricts the whole tree to the user, Administrators and SYSTEM;
3. installs x64 CPython 3.14 with uv and syncs the locked environment
   without dev dependencies;
4. replaces the firewall rule;
5. registers the two at-logon tasks.

It prints one line starting with `installed:` when it finishes.

The API has no client authentication: whoever can reach the port can read,
trade and rearm the kill switch. Access control is the network's job. The
firewall rule admits the port only on `-BindAddress` and only from
`-HostAddress`, and the bridge listens only on `MT5_BRIDGE_HOST`; keep both
as narrow as the setup allows.

### 9. Start

Log off and on again, or start the tasks by hand:

```powershell
Start-ScheduledTask -TaskName 'mt5-terminal'
Start-ScheduledTask -TaskName 'mt5-bridge'
```

The bridge waits up to five minutes for `terminal64.exe`, then starts.
Output goes to `C:\mt5-bridge\logs\bridge-<date>.log`.

For the bridge to start without anyone logging in, configure Windows
auto-logon for that user by hand (Sysinternals Autologon stores the password
encrypted). The script does not do it, because it needs the Windows
password.

### 10. Verify from the host

```bash
curl --cacert bridge-cert.pem https://<guest IP>:8765/v1/health
```

For plain HTTP, drop `--cacert` and use `http://`. In the answer,
`terminal_connected`, `algo_trading_enabled` and
`clock_drift_within_threshold` must be true and `kill_switch_state` must be
`NORMAL`. Then compare the first one-minute bar of a B3 session with the
10:00 America/Sao_Paulo open before trusting any timestamp.

The bridge accepts orders as soon as it runs. The terminal sends them only
while its Algo Trading button is on: enable it after these reads are right.

## When something fails

| Symptom | Where to look |
| --- | --- |
| `application tree not found` | step 4: `app\pyproject.toml` is missing |
| `uv not found` | uv is not installed for this Windows user |
| the task runs but nothing listens | `logs\bridge-<date>.log`; a failed boot is logged there and the loop retries every 15 seconds |
| boot refuses the account | `MT5_BRIDGE_EXPECTED_LOGIN`, `MT5_BRIDGE_EXPECTED_SERVER`, a hedging account, or a non-demo account in `demo` or `paper` |
| the host cannot connect | the firewall rule admits only `-HostAddress`; check both addresses |
| the first book request is empty | expected once after each terminal attach |

Operating procedures after the install are in [runbook.md](runbook.md).

## Removing the install

```powershell
Unregister-ScheduledTask -TaskName 'mt5-bridge' -Confirm:$false
Unregister-ScheduledTask -TaskName 'mt5-terminal' -Confirm:$false
Get-NetFirewallRule -DisplayName 'mt5-bridge' | Remove-NetFirewallRule
```

Keep `C:\mt5-bridge\data\` if the account may be used again.
