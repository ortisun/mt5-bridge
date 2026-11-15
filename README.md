# mt5-bridge

A gateway between a Windows MetaTrader 5 terminal and anything that wants to
trade or read B3 through it. It is a FastAPI service with a REST v1 API and
one WebSocket stream, optionally with the same API over gRPC, and it is the
only code that imports the `MetaTrader5` library. Clients talk to it over
HTTP or gRPC and never touch the terminal.

The bridge has no strategy, signal or portfolio logic. It does four things:

- reads the terminal: account, symbols, bars, ticks, book depth, positions,
  order and deal history;
- sends orders idempotently: a `client_order_id` is journaled before the
  terminal is called, so a repeated request never becomes a second order, and
  an outcome it could not confirm is reported as `UNKNOWN`, never guessed;
- keeps its own safety controls, independent of the client: per-order and
  notional limits, a daily-loss limit, a kill switch that only escalates, and
  a dead-man switch that cancels open orders when the heartbeats stop;
- streams ticks, bars, book, executions, account and status with durable,
  resumable sequence numbers for executions.

## The contract

The contract is part of this repository and is the source of truth for the
server and for every client:

| File | What it defines |
| --- | --- |
| [`contracts/mt5_bridge/openapi.yaml`](contracts/mt5_bridge/openapi.yaml) | REST v1 (OpenAPI 3.1), `info.version` `1.0.0` |
| [`contracts/mt5_bridge/ws_messages.schema.json`](contracts/mt5_bridge/ws_messages.schema.json) | `/v1/stream` messages (JSON Schema 2020-12) |
| [`contracts/mt5_bridge/mt5_bridge.proto`](contracts/mt5_bridge/mt5_bridge.proto) | the same operations and stream over gRPC (proto3, package `mt5_bridge.v1`) |

Every message on the stream, every log line and every audit record is an
event of the [Open Cybersecurity Schema Framework](https://schema.ocsf.io/)
1.9.0. [`docs/ocsf.md`](docs/ocsf.md) has the mapping.

Rules that hold for the contract:

- Prices, volumes and money travel as decimal strings, never as JSON numbers,
  so a client cannot round one through `float`.
- Every 4xx and 5xx response is a Problem Details document (RFC 9457,
  `application/problem+json`). [`docs/errors.md`](docs/errors.md) lists the
  problem types. A failed gRPC call reports the same problem types.
- A change to the API changes these files first, in the same commit as the
  server code. `tests/contracts/` checks that both documents are valid and
  that real REST responses and WebSocket messages conform to them.
- The gRPC contract follows the other two: `tests/contracts/` checks that
  every REST operation has an RPC, that every stream channel and every
  problem type exists in it, and that the generated code under
  `src/mt5_bridge/api/grpc_api/generated/` is what the `.proto` produces.
  After editing the `.proto`, run `make proto`.
- Clients depend on these files and nothing else in this repository. A
  client that keeps a copy should record the commit or tag it copied from.

`info.version` follows SemVer and covers the three files. It is `1.0.0`.

### gRPC

gRPC is off by default. Setting `MT5_BRIDGE_GRPC_PORT` serves it on that
port, next to the HTTP listener, in the same process: both read the same
journal, kill switch and terminal worker, so an order submitted through one
is visible through the other and shares its idempotency key.

| Service | What it is |
| --- | --- |
| `mt5_bridge.v1.MarketData` | health, terminal, account, symbols, bars, ticks, book |
| `mt5_bridge.v1.Trading` | orders, positions, history, kill switch, heartbeat |
| `mt5_bridge.v1.Streaming` | one bidirectional call, the gRPC form of `/v1/stream` |

- A failed call ends with a gRPC status and the entry
  `mt5-bridge-problem-bin` in the trailing metadata: a serialized `Problem`
  with the members of the REST problem document.
  [`docs/errors.md`](docs/errors.md) lists the status of each problem type.
- Each message of the stream is a `StreamEvent`: the OCSF event `/v1/stream`
  sends, with the same classification, metadata and sequence numbers
  ([`docs/ocsf.md`](docs/ocsf.md)).
- A write call is recorded in the audit log like a REST write.
- The listener uses the HTTP listener's certificate pair: with
  `MT5_BRIDGE_TLS_CERTFILE` and `MT5_BRIDGE_TLS_KEYFILE` set it serves TLS,
  otherwise plain text. `MT5_BRIDGE_GRPC_HOST` binds another address than
  `MT5_BRIDGE_HOST`.
- `GET /v1/metrics` has no gRPC form; Prometheus scrapes it over HTTP.

## Releases

The repository follows Git Flow. A release is a tag `v<x.y.z>` on `main`,
with notes in [CHANGELOG.md](CHANGELOG.md) and on the GitHub Release. The
notes of every release start by saying whether the contract changed, and
from which version to which. The release version and the contract version
are separate numbers; the release gate refuses a release whose contract
files changed without a higher contract version. The procedure is in
[`docs/release.md`](docs/release.md). No release exists yet.

## Layout

```
contracts/mt5_bridge/   the REST, WebSocket and gRPC contract
src/mt5_bridge/api/     routes, WebSocket, gRPC services, request and response models
src/mt5_bridge/api/grpc_api/   the gRPC services and the code generated from the .proto
src/mt5_bridge/core/    journal, sequence log, limits, kill switch, pollers
src/mt5_bridge/mt5/     Mt5Client protocol, fake terminal, real terminal client
src/mt5_bridge/mcp_adapter/  MCP server for AI agents; a client of the REST contract
typings/MetaTrader5/    minimal type stub, so Pyright works off Windows
tests/                  unit and contract tests, all against the fake terminal
deploy/                 Windows install and start scripts, example configuration
tools/                  release helpers and the MCP tool generator
docs/                   install guide, release procedure, runbook, errors and notes
```

`src/mt5_bridge/mt5/real_client.py` is the only module allowed to import
`MetaTrader5`; import-linter enforces it.

## AI agents (MCP)

`mt5-bridge-mcp` exposes the REST contract to AI agents as
[Model Context Protocol](https://modelcontextprotocol.io/) tools, one per
operation. It runs beside the agent on macOS, Linux or Windows and calls the
bridge over HTTP. Like the bridge, it has no access control of its own:
whoever can run the adapter can do everything the bridge allows. Setup, the
trading rules and what has been tried are in [`docs/mcp.md`](docs/mcp.md).

```bash
uv run --locked --extra mcp mt5-bridge-mcp
```

The adapter imports nothing from the service and the service does not import
it or the MCP SDK; import-linter enforces both. The Windows install does not
include it.

## Development and tests

Python 3.14 and [uv](https://docs.astral.sh/uv/). From the repository root:

```bash
make sync
```

```bash
make check
```

`make check` runs the lock-file check, Ruff (lint and format), Pyright in
strict mode, import-linter, the tests and the build. Each step also runs
alone (`make lint`, `make types`, `make architecture`, `make test`,
`make build`); [CONTRIBUTING.md](CONTRIBUTING.md) has the commands behind
them and the rules for commits, branches, releases and the contract version.

Every test runs on macOS or Linux against `mt5.fake_client.FakeMt5Client`, a
deterministic fake terminal; none needs Windows or a real terminal.
`MetaTrader5` is declared with the `sys_platform == "win32"` marker, so
`uv sync` installs it only on Windows. Elsewhere Pyright checks
`real_client.py` against the stub in `typings/MetaTrader5/` and prints one
expected warning that the module source is missing.

What the tests do not cover: the real terminal client, the Windows guest,
IPC with the terminal and networking between host and guest. Those were
checked by hand on a demo account (see the runbook) and need a new check
after any change to `real_client.py` or to the `MetaTrader5` pin.

## Running it locally

```bash
uv run --locked python -m mt5_bridge.main
```

Settings come from the environment (`MT5_BRIDGE_*`, see
[`settings.py`](src/mt5_bridge/settings.py) and
[`main.py`](src/mt5_bridge/main.py)). So do the secrets, as variables of the
user that runs the service:

| Variable | Value |
| --- | --- |
| `MT5_BRIDGE_LOGIN`, `MT5_BRIDGE_PASSWORD`, `MT5_BRIDGE_SERVER` | the terminal account, only when the bridge must log the terminal in itself; set all three or none |

Without the terminal variables the bridge attaches to the terminal that is
already logged in. Secret values are redacted from the logs.

The API has no client authentication: whoever can reach the port can read,
trade and rearm the kill switch. Access control is the network's job. The
bridge binds `MT5_BRIDGE_HOST` (default `127.0.0.1`), and on the Windows
guest the install script adds a firewall rule that admits the port only from
the host address. The gRPC port, when one is set, is as open as the HTTP
port and is not covered by that rule: admit it the same way.

Traces, metrics and logs go through OpenTelemetry. Metrics are always served
at `GET /v1/metrics`; nothing is sent to a collector until the standard
`OTEL_*` variables select it. [docs/observability.md](docs/observability.md)
has the variables, the span names and the metric names.

The default is `MT5_BRIDGE_CLIENT_BACKEND=fake`: demo data from the fake
terminal. `MT5_BRIDGE_CLIENT_BACKEND=real` needs Windows; without a terminal secret it
attaches to the terminal the operator already logged in.

## Installing on the Windows guest

The terminal and the bridge run together inside a Windows guest (UTM on
macOS); the host reaches the bridge over HTTP or HTTPS on the guest's
address. In short:

1. `make package` on the host writes `dist/mt5-bridge-app.zip`.
2. Unpack it in the guest to `C:\mt5-bridge`, which creates `app\`.
3. Copy `app\deploy\bridge.env.example` to `config\bridge.env` and fill it
   in. No broker credential goes in this file.
4. Run `app\deploy\install-windows.ps1 -BindAddress <guest IP>` from an
   elevated PowerShell. It syncs the locked environment, restricts the tree,
   opens the port only to the host and registers the at-logon tasks for the
   terminal and the bridge.
5. From the host, check `GET /v1/health`.

[`docs/install-utm.md`](docs/install-utm.md) has every step, including TLS,
the secret variables, redeploy and removal. The install scripts need Windows:
they were used on the demo guest in September 2026 and have not been run
again since.

The install root is `C:\mt5-bridge` (`-InstallRoot` overrides it), the
scheduled tasks are `mt5-terminal` and `mt5-bridge`, and the firewall rule is
named `mt5-bridge`.

Operating procedures (restart, disconnected terminal, an `UNKNOWN` order,
rearming the kill switch) are in [`docs/runbook.md`](docs/runbook.md).

## Design rules

Comments in the code cite design rules by number (`plan §114`, `ADR-025`,
`rule 33`, `audit F1`). [`docs/design-rules.md`](docs/design-rules.md) states
the rule behind each citation.

## Licence

No licence has been chosen yet; see [CONTRIBUTING.md](CONTRIBUTING.md).
