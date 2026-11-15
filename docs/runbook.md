# MT5 Bridge Runbook

Operator procedures for the bridge. The Windows/UTM deployment below was
validated on the Rico-DEMO account in September 2026; live accounts remain
out of scope. Citations such as `plan §107` or `audit F1` are explained in
[design-rules.md](design-rules.md).

## VM restart

The bridge's state (`OrderJournal`, `SequenceLog`, `KillSwitch`,
`poller_cursors`, `daily_loss_baseline`) lives in one VM-local SQLite (WAL)
file, so a restart of the bridge process — or of the VM itself — does not
lose it. On startup, `main.boot()` runs `verify_netting_mode` (hard failure
on a mismatch between the terminal's reported account mode and the
deployment's expectation) and then `reconcile_on_boot`, which resolves any
order left in `RECEIVED` by a prior crash against the terminal's own
`orders_get`/`history_orders_get`. Do not delete or replace the SQLite file
to "clear" a bad state: that discards the kill-switch's persisted arming,
the journal's idempotency record, and the daily-loss baseline, which is the
one thing standing between a restart and either a duplicate order or a
forgiven loss. After a restart, check `GET /v1/health` before assuming the
bridge is usable: `terminal_connected`, `algo_trading_enabled`, and
`clock_drift_within_threshold` must all be true, and `kill_switch_state`
must be `NORMAL` (a restart never clears an armed kill switch by design).

## Daily-loss baseline and unpriced MARKET orders (audit F1/F2)

`LimitsGuard`'s daily-loss baseline is now a persisted, account-bound,
trading-day figure (`daily_loss_baseline` table), not a value reseeded from
current equity at every boot. `MT5_BRIDGE_TRADING_DAY_TIME_ZONE` (default
`America/Sao_Paulo`, B3's own trading day) defines the rollover boundary
explicitly. If the persisted row belongs to a different account than the one
the terminal just bound to, boot does not overwrite it and does not use it
either: `LimitsGuard` fails closed (rejects every new, risk-increasing
submission) until an operator resolves the mismatch — check the boot log for
"daily-loss baseline unavailable" and either restore the correct account or
clear the row deliberately (an intentional, recorded action, not an automatic
fallback). A daily-loss figure the bridge has not refreshed within
`MT5_BRIDGE_EQUITY_MAX_AGE_SECONDS` (default 30s) fails closed the same way.

A MARKET order now needs a fresh quote (`MT5_BRIDGE_QUOTE_MAX_AGE_SECONDS`,
default 5s) to bound its notional for the max-open-notional check; a missing
or stale quote for the requested symbol rejects the order before it ever
reaches the terminal (`GET /v1/orders/{client_order_id}`'s `reason` names
which). This is a pre-trade gate, not a live-feed requirement: once
`ticks`/`bars` polling is running normally for the symbol, quotes stay fresh
on their own.

None of this blocks `POST /v1/kill-switch {"action": "FLATTEN"}` (plan §107
EmergencyReductionPolicy): its closing orders are marked reduce-only and skip
the max-open-notional, max-daily-loss, stale-evidence and fresh-quote checks
above by design — an exhausted or unreadable daily-loss figure, a full
open-notional book, or a missing/stale quote (e.g. mid-disconnect) is
precisely when a reduction may be needed. The symbol allowlist, the
per-symbol UNKNOWN block, clock drift, and idempotency still apply to it
unchanged.

## Disconnected terminal

`GET /v1/terminal` reports `terminal_info().connected` fresh on every call.
The WS `status` channel now also reports it (`terminal_connected`, alongside
`kill_switch_state`): every status message carries both fields, so a status
subscriber genuinely knows the connection state without polling `/v1/terminal`.
A background watchdog (`core.terminal_watchdog`, polled at
`MT5_BRIDGE_WATCHDOG_POLL_INTERVAL_SECONDS`, default 5s) detects a dropped
terminal, attempts to re-establish the session with bounded exponential
backoff (`MT5_BRIDGE_WATCHDOG_BACKOFF_BASE_SECONDS`/`_MAX_SECONDS`, defaults
1s/60s), and publishes the transition on `status`. While the watchdog reports
disconnected, `OrderService.submit_order` rejects every submission with reason
`"terminal disconnected"` before any terminal round trip — unconditionally,
including `FLATTEN`'s `emergency_reduction` path, since a disconnected
terminal cannot honor any command regardless of intent. The bridge still does
not assume any order's state changed during a disconnection and does not
resend anything, ever.

Confirm the terminal application is actually running and logged in in the
VM's interactive desktop session (a Session 0 service cannot see it — this is
why 072 requires auto-logon + "run at logon", not a Windows service). Once
the terminal reconnects (the watchdog's own reconnect succeeds, or an operator
restarts the terminal and the next watchdog tick observes it), call
`POST /v1/heartbeat` to keep the
dead-man switch armed, then reconcile: pull `GET /v1/orders` and compare
against what the last-known state was before the gap. Do not manually
resubmit an order believed lost; query `GET /v1/orders/{client_order_id}` by
its known `client_order_id` instead — resubmission is idempotent by design,
but a *new* `client_order_id` for the same intent is not the same order.

## Trading-phase evidence and auction participation (audit F4b)

`GET /v1/symbols/{symbol}` reports `trading_phase`
(`CONTINUOUS`/`PRE_OPEN_AUCTION`/`CLOSING_AUCTION`/`HALTED`/`CLOSED`), a
bridge-side approximation from that symbol's own `trading_allowed` and
declared `sessions` (`core.trading_phase.classify_trading_phase`, using
`MT5_BRIDGE_AUCTION_WINDOW_SECONDS`, default 300) — never a broker-confirmed
exchange phase; qualifying it against B3's actual auction schedule is external
to this bridge (ADR-078). The WS `status` channel publishes a phase-change
event (`symbol`/`trading_phase`) for every symbol a connection already
watches through `ticks`/`bars`/`book`. `GET /v1/health`'s
`auction_participation_supported` is the separate, bridge-wide capability
declaration (`MT5_BRIDGE_AUCTION_PARTICIPATION_SUPPORTED`, default `false`):
a client (a B3 routing gate, for one) should only admit an
`AUCTION`-phase route when this is `true`;
`HALTED`/`CLOSED`/`UNKNOWN` stay blocked regardless. Flipping this to `true`
on this deployment is an operator decision that should follow real auction
qualification, not merely enabling the flag.

## An order is UNKNOWN

`UNKNOWN` means the bridge could not confirm what the terminal did — a
crash between sending and journaling, a lost response on `order_send`,
`cancel_order`, or `modify_order`, or a boot-reconciliation match that was
not unique. It blocks new orders on the same instrument until resolved.
Do not treat `UNKNOWN` as either "probably filled" or "probably not sent."
Query the terminal directly: `GET /v1/orders/{client_order_id}` reports the
last known `ticket`; if one was recorded, check `GET /v1/positions` and
`GET /v1/history/deals?from_epoch=...&to_epoch=...` around that window for
a deal against that ticket. Once the true outcome is established outside
the bridge (by this manual check), the owning system (the client's OMS,
per plan §56) records the resolution; the bridge itself
never auto-resolves an `UNKNOWN` order into a guess. An operator can ask the
bridge to re-examine one entry with `POST /v1/orders/{client_order_id}/reconcile`
(audited): a known ticket takes the terminal's state; an entry without a
ticket takes a unique magic+tag candidate, or becomes `REJECTED` only when the
terminal holds no order or deal with this bridge's magic in a window covering
the entry. Anything else stays `UNKNOWN` and keeps the symbol blocked.

## Rearming the kill switch

`POST /v1/kill-switch` only escalates (`HALT_NEW_ORDERS` → `CANCEL_ALL` →
`FLATTEN`); it never lowers the state. The only way back to `NORMAL` is
`DELETE /v1/kill-switch` — by design, no automatic rule rearms it.
Before rearming: confirm what triggered it (dead-man switch expiry, a
manual `HALT`/`CANCEL_ALL`/`FLATTEN`, or an automatic rule upstream),
confirm `GET /v1/orders` and `GET /v1/positions` reflect a reconciled,
understood state (a `FLATTEN` is a best-effort reduction attempt, not a
confirmed flat position — check it landed), and confirm the condition that
triggered the halt no longer holds. A dead-man expiry fires once per lease:
after its CANCEL_ALL the switch stays disarmed until the next heartbeat,
so the rearm holds. Rearming does not restart pollers or
clear `UNKNOWN` orders; it only allows new submissions again.

## Traces, metrics and logs

[observability.md](observability.md) lists the variables, the span names and
the metric names. What an operator needs day to day:

- Metrics: `GET /v1/metrics` (Prometheus text) always works and needs no
  configuration. Besides the kill-switch, dead-man, terminal and poll gauges
  it has `http_server_request_duration_seconds` and
  `http_server_active_requests`.
- Finding the trace of a failed request: the console log line of the failure
  has `trace_id` and `span_id` whenever it was written inside a request. With
  trace export on, search the backend for that trace id; the trace shows the
  request, the order operation, the wait for the terminal worker and the
  terminal call. A client that sends a `traceparent` header sees the bridge's
  spans under its own.
- Turning export on or off: set or remove `OTEL_TRACES_EXPORTER`,
  `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER` and
  `OTEL_EXPORTER_OTLP_ENDPOINT` in `config\bridge.env` and restart the
  bridge. Left unset, nothing is exported.
- The collector is down: the bridge keeps serving. The exporter logs its
  failed attempts on the console and drops what it could not send; nothing is
  replayed later.
- `otlp` selected but no endpoint set: the signal stays off and the boot log
  has a warning that names the missing variable.
- At shutdown the bridge flushes what is still queued, for at most five
  seconds per signal. A process that is killed loses its queued spans and
  log records; the console log is not affected.

## Windows/UTM deployment

The guest runs Windows 11 on Apple Silicon; the terminal and the pinned
`MetaTrader5` wheel run as x64 under emulation with uv-managed x64 CPython
3.14. `C:\mt5-bridge` holds `app\` (a copy of this repository),
`config\bridge.env`, `tls\`, `data\` (the SQLite journal) and `logs\`;
`deploy\install-windows.ps1 -BindAddress <VM IP>` syncs the environment,
restricts the tree to the operator, Administrators and SYSTEM, admits the
bridge port only from the host, and registers the at-logon tasks
`mt5-terminal` and `mt5-bridge` in the interactive session.
A redeploy replaces only `app\`.

- Credentials: none in `bridge.env`. The bridge attaches to the terminal the
  operator logged into; boot refuses another login/server
  (`MT5_BRIDGE_EXPECTED_LOGIN`/`_SERVER`), a non-DEMO account in `demo` or
  `paper`, or a hedging account.
- Access: the API has no client authentication. The firewall rule (the bridge
  port, only from the host address) and the bind address are the access
  control.
- Auto-logon needs the Windows password, so the operator configures it
  (Sysinternals Autologon stores it encrypted); without it the tasks start
  only after a manual logon.
- Transport: the bridge binds the VM address over TLS today; the host pins
  `~/.config/mt5-bridge/bridge-cert.pem`. TLS is an operator choice,
  not a requirement (2026-09-29 user decision): `serve_options` (`main.py`)
  serves plain HTTP on any host when `MT5_BRIDGE_TLS_CERTFILE`/`_KEYFILE` are
  unset, and switching this running VM to plaintext is a deliberate operator
  step that requires redeploying `app\`,
  never a silent effect of removing the files on disk.
  Check `curl --cacert <cert> https://<VM IP>:8765/v1/health`
  (drop `--cacert` and use `http://` if this deployment switches to plaintext).
- Time: the Rico-DEMO terminal reports its server's wall clock as epochs;
  `MT5_BRIDGE_TERMINAL_TIME_ZONE=America/Sao_Paulo` converts them to true UTC
  once. After any broker or terminal change, compare the first M1 bar of a
  B3 session with its 10:00 America/Sao_Paulo open before trusting times.
- The bridge trades on the Rico-DEMO account only (2026-09-26, M8/075
  paper trading): terminal Algo Trading on and, in `bridge.env`,
  allowlist `PETR4,VALE3`, at most 100 shares
  per order, 10 orders/minute, R$20,000 open notional, R$2,000 daily loss.
  Boot still refuses any non-DEMO account; a live account needs a new decision.
- Demo validation: a client-side integration suite run against the demo
  guest; resting-order flows skip while B3 is
  closed (all 9 passed in B3 hours on 2026-09-28).
- Depth: the bridge subscribes a symbol with `market_book_add` on its first
  book request after each terminal (re)attach, so that first answer can be
  empty; an empty book afterwards means the broker publishes none right now.
- Logs: `main()` now disables uvicorn's own logging configuration
  (`log_config=None`) so every uvicorn record propagates to root, where a
  redaction filter scrubs the terminal password (when the bridge logs in
  itself) before anything is written; uvicorn's access/error lines lose their own colored formatting as
  a result (a cosmetic change, not a functional one). This deployment has not
  been redeployed with this change yet — redeploying `app\` (see above) is a
  required operator step before it takes effect on the Rico-DEMO VM.
