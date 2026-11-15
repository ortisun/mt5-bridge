# Design rules cited in the code

Comments, docstrings and contract descriptions cite the design plan, its
decision records and audit findings by number. This page states the rule
behind each citation.

## Plan sections (`plan §N`, `plan section N`)

| Citation | Rule it points to |
| --- | --- |
| §3.13, §84 Observability | Logs are one JSON object per record and pass through secret redaction before they are written. The process exposes Prometheus metrics (submit-to-ack latency, rejections by reason, `UNKNOWN` orders, terminal disconnects, poll timing) and can export OpenTelemetry traces; nothing is exported by default. |
| §56 OMS | Order states. `UNKNOWN` is mandatory: when the bridge cannot confirm what the terminal did, the order becomes `UNKNOWN`, blocks new orders on the same instrument until reconciled, and is never retried blindly or resolved by a guess. Cancel-replace returns a discriminated outcome. |
| §86 Idempotency | A `client_order_id` identifies one order intent. Repeating a request with the same id returns the recorded outcome and never sends a second order. |
| §107 Pre-trade risk gate and kill switch | Hard limits (volume per order, orders per minute, open notional, daily loss). The kill switch only escalates (`HALT_NEW_ORDERS`, `CANCEL_ALL`, `FLATTEN`) and only an explicit rearm returns it to `NORMAL`. The emergency reduction policy: orders that only reduce exposure are never blocked by the notional, daily-loss, stale-evidence or fresh-quote checks. |
| §112 Security | Secrets never live in code, environment files or logs. The journal and audit file are readable by the owner only. Every write request is recorded in an append-only audit log. |
| §114 MT5 bridge | The design of this service: one worker thread owns the terminal connection; boot binds the process to one environment and account; an independent `LimitsGuard`; a dead-man switch; a watchdog that reconnects a dropped terminal with bounded backoff and never resends an order; clock drift above the threshold blocks trading; netting accounts only; trading-phase and auction evidence is an approximation from terminal data; the bridge keeps no long-term tick history. |
| §116 Technical sources | Behaviour taken from external documentation records the source and the date it was reviewed. The `MetaTrader5` Python documentation was reviewed on 2026-09-20 against package 5.0.6180; review the type stub again when that pin changes. |

## Numbered rules (`rule N`, `plan rule N`)

| Citation | Rule |
| --- | --- |
| rule 28 | No decision uses a forming bar. Only closed bars are delivered as bars. |
| rule 30 | The time unit of every external source (seconds, milliseconds) is declared and validated, never inferred from the size of the number. |
| rule 33 | Order prices, quantities and money use `Decimal`. Conversion from the terminal's floats happens at one tested boundary. |
| rule 40 | `MetaTrader5` is imported only inside this service. Clients reach B3 only through the HTTP and WebSocket contract. |
| rule 43 | Pending and `UNKNOWN` orders keep consuming risk capacity. Consumed capacity is never silently reset. |

## Decision records (`ADR-NNN`)

| Citation | Decision |
| --- | --- |
| ADR-025 | The bridge's REST and WebSocket contract, journal and time discipline. The contract files are written first and are the source of truth. `Mt5Client` takes typed intentions, not the library's raw request dictionary. Idempotency has two commit points: `RECEIVED` before the terminal call, then the outcome together with the execution event. The fake terminal simplifies some behaviour, for example a fill that flips a net position. The "W13b amendment" records the reviewed version of the `MetaTrader5` documentation. |
| ADR-078 | Qualifying the bridge's trading-phase approximation against B3's real auction schedule needs confirmation from the broker and happens outside the bridge. |
| ADR-079 | Credentials come from environment variables of the user that runs the service: when needed, the terminal login, password and server. None is written to the repository, to `bridge.env` or to a log. |
| ADR-095 | Telemetry bootstrap: OpenTelemetry is configured before anything that records a metric or span. |

## Audit findings (`audit F…`)

| Citation | Finding and the fix it led to |
| --- | --- |
| audit F1 | The daily-loss baseline was reseeded from current equity at every boot, which forgave the day's loss. It is now persisted per account and trading day; a missing, stale or mismatched figure makes `LimitsGuard` reject new exposure. |
| audit F2 | A MARKET order without a price had no notional to check. It now needs a fresh quote, and its exposure stays reserved across restarts. |
| audit F4b | Clients had no evidence of the trading phase. `GET /v1/symbols/{symbol}` and the `status` channel now report `trading_phase`, and `GET /v1/health` reports whether auction participation is supported. |

## Delivery labels

`M8` and `M8/068` to `M8/075` are milestone steps: 068 to 072 built the
platform-independent core and the Windows deployment, 069 established that
the Rico-DEMO terminal reports server wall-clock time as epochs, and 075 was
paper trading on the demo account. Labels such as `010 T023` or `201 T100`
are task numbers and carry no rule.
