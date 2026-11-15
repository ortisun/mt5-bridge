# Observability

The bridge emits traces, metrics and logs through
[OpenTelemetry](https://opentelemetry.io/). One bootstrap,
[`core/observability.py`](../src/mt5_bridge/core/observability.py), builds
the three signals over one shared resource; `main()` installs it once and its
shutdown flushes all three when the process exits.

By default nothing leaves the process: no exporter is created, the metrics
are served only at `GET /v1/metrics`, and the logs go only to the console.
Export starts when a variable below selects it.

## Signals

| Signal | Always on | Optional export |
| --- | --- | --- |
| Traces | spans are created, so console log lines written inside a request carry `trace_id` and `span_id` | OTLP/HTTP or console |
| Metrics | Prometheus text at `GET /v1/metrics` | OTLP/HTTP (periodic push) or console |
| Logs | one JSON object per line on the console, secrets redacted | OTLP/HTTP or console, with trace and span ids |

The exported log records are a second copy of the console lines, produced by
a separate `logging` handler
([`core/log_export.py`](../src/mt5_bridge/core/log_export.py)); the console
format does not change when export is on. The redaction filter runs before
both.

## Resource

Every span, metric and log record carries the same resource attributes:

| Attribute | Value |
| --- | --- |
| `service.name` | `mt5-bridge`, or `OTEL_SERVICE_NAME` |
| `service.version` | the installed package version |
| `service.instance.id` | a random id per process |
| `deployment.environment.name` | `MT5_BRIDGE_ENVIRONMENT`, when set |
| `host.name`, `host.arch`, `os.type` | the machine |
| `process.pid`, `process.executable.name`, `process.runtime.name`, `process.runtime.version` | the process |
| anything in `OTEL_RESOURCE_ATTRIBUTES` | as given; it overrides the rows above except `service.name` when `OTEL_SERVICE_NAME` is set |

The process command line is not recorded.

## Configuration

All of these are ordinary, non-secret environment variables (on the Windows
guest: lines in `config\bridge.env`). The standard OpenTelemetry names are
honoured; the two `MT5_BRIDGE_OTEL_*` names are older, still work, and win
when both are set.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OTEL_TRACES_EXPORTER` | `none` | `none`, `console` or `otlp` |
| `OTEL_METRICS_EXPORTER` | `none` | `none`, `console` or `otlp`; `prometheus` is accepted and means `none`, because the pull endpoint is always on |
| `OTEL_LOGS_EXPORTER` | `none` | `none`, `console` or `otlp` |
| `MT5_BRIDGE_OTEL_EXPORTER` | unset | older name for the trace exporter only; overrides `OTEL_TRACES_EXPORTER` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | base URL of the collector, for example `http://192.168.64.1:4318`; `/v1/traces`, `/v1/metrics` and `/v1/logs` are appended |
| `MT5_BRIDGE_OTEL_EXPORTER_OTLP_ENDPOINT` | unset | older name for the base URL; overrides `OTEL_EXPORTER_OTLP_ENDPOINT`. A value that already ends in `/v1/traces` is accepted |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | unset | full URL for one signal, used as written; overrides both base URLs |
| `OTEL_SERVICE_NAME` | `mt5-bridge` | `service.name` |
| `OTEL_RESOURCE_ATTRIBUTES` | unset | extra resource attributes, `key=value,key=value` |
| `OTEL_TRACES_SAMPLER` | `parentbased_always_on` | `always_on`, `always_off`, `traceidratio`, `parentbased_always_on`, `parentbased_always_off`, `parentbased_traceidratio` |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | the ratio for the `traceidratio` samplers, 0 to 1 |
| `OTEL_METRIC_EXPORT_INTERVAL` | `60000` | milliseconds between metric pushes |
| `OTEL_SDK_DISABLED` | `false` | `true` switches every exporter off |
| `MT5_BRIDGE_ENVIRONMENT` | `demo` | also `deployment.environment.name` |

Three things differ from the OpenTelemetry defaults, on purpose:

- An exporter variable left unset means `none`, not `otlp`.
- `otlp` without an endpoint does not fall back to `localhost:4318`; the
  signal stays off and boot logs a warning.
- Only OTLP over HTTP with protobuf is supported
  (`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`); there is no gRPC exporter.

The OTLP exporters read a few more standard variables straight from the
process environment: `OTEL_EXPORTER_OTLP_TIMEOUT`,
`OTEL_EXPORTER_OTLP_COMPRESSION`, `OTEL_EXPORTER_OTLP_CERTIFICATE` and
`OTEL_EXPORTER_OTLP_HEADERS`, and the batch settings `OTEL_BSP_*` and
`OTEL_BLRP_*`. Do not put a credential in `OTEL_EXPORTER_OTLP_HEADERS`: the
bridge's redaction does not know that value. Point the bridge at a collector
that needs none, and let the collector hold the credentials of whatever it
forwards to.

## Pointing the bridge at a collector

Run an [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)
with an OTLP/HTTP receiver somewhere the bridge can reach, for example on the
host, bound to the host-only address:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 192.168.64.1:4318
exporters:
  debug: {}
service:
  pipelines:
    traces: { receivers: [otlp], exporters: [debug] }
    metrics: { receivers: [otlp], exporters: [debug] }
    logs: { receivers: [otlp], exporters: [debug] }
```

Then set, in the bridge's environment:

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://192.168.64.1:4318
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
```

and restart the bridge. Each signal is independent; set only the ones you
want. On the Windows guest the firewall rule from the install script covers
only the inbound bridge port; check that the guest can open an outbound
connection to the collector.

To see spans without a collector, set `OTEL_TRACES_EXPORTER=console`; they
are printed as JSON to standard output.

A collector that is down does not affect the bridge: export runs on
background threads, failed batches are dropped after the exporter's retries,
and the failure is logged on the console only.

## Traces

A client that sends a W3C Trace Context header
([`traceparent`](https://www.w3.org/TR/trace-context/), optionally
`tracestate`) gets its request recorded as a child of its own span.
Without the header each request starts a new trace. A gRPC client sends the
same two names as metadata entries of the call.

| Span | Kind | Where | Notes |
| --- | --- | --- | --- |
| `{METHOD} {route}`, e.g. `POST /v1/orders` | server | every HTTP request except `/v1/health` and `/v1/metrics` | HTTP semantic conventions: `http.request.method`, `http.route`, `url.path`, `url.scheme`, `http.response.status_code`, `server.address`, `server.port`, `network.protocol.version`, `user_agent.original`. Error status on a 5xx answer or an unhandled exception; a 4xx is not an error |
| `GET /v1/stream` | server | one per WebSocket connection, from handshake to disconnect | `network.protocol.name=websocket`, `mt5_bridge.ws.accepted`, `mt5_bridge.ws.close_code`, `mt5_bridge.ws.messages_sent`, `mt5_bridge.ws.messages_received`. Exported when the connection closes |
| `mt5_bridge.ws.subscribe`, `mt5_bridge.ws.unsubscribe`, `mt5_bridge.ws.resume` | internal | one per client stream operation, child of the connection span | `mt5_bridge.ws.operation`, `mt5_bridge.ws.channel`, `mt5_bridge.ws.symbol_count` |
| `{service}/{method}`, e.g. `mt5_bridge.v1.Trading/SubmitOrder` | server | every unary gRPC call except `GetHealth` | RPC semantic conventions: `rpc.system.name` (`grpc`), `rpc.method`, `rpc.response.status_code` (the status name, `OK` included), `server.address`, `server.port`, `user_agent.original`. Error status and `error.type` when the call ends `UNKNOWN`, `DEADLINE_EXCEEDED`, `UNIMPLEMENTED`, `INTERNAL`, `UNAVAILABLE` or `DATA_LOSS`; any other status is not an error |
| `mt5_bridge.v1.Streaming/Stream` | server | one per gRPC `Stream` call, from its start to its end | the attributes above, `mt5_bridge.grpc.messages_sent`, `mt5_bridge.grpc.messages_received`. A call the client ends has the status `CANCELLED`. Exported when the call ends |
| `mt5_bridge.grpc.subscribe`, `mt5_bridge.grpc.unsubscribe`, `mt5_bridge.grpc.resume` | internal | one per client operation on a gRPC stream, child of the call's span | `mt5_bridge.grpc.operation`, `mt5_bridge.grpc.channel`, `mt5_bridge.grpc.symbol_count` |
| `mt5_bridge.order.submit` | internal | `OrderService.submit_order` | `mt5_bridge.order.client_order_id`, `.symbol`, `.side`, `.order_type`, `.time_in_force`, `.state`, `.replayed`. Error status when the order ends `UNKNOWN` |
| `mt5_bridge.order.cancel`, `mt5_bridge.order.amend`, `mt5_bridge.order.reconcile` | internal | the matching `OrderService` method | `mt5_bridge.order.client_order_id` |
| `mt5_bridge.order.replace` | internal | `OrderService.replace_order` | `mt5_bridge.order.operation_id`, `.original_client_order_id`, `.new_client_order_id` |
| `mt5_bridge.order.cancel_all`, `mt5_bridge.order.flatten` | internal | kill-switch and dead-man actions | `flatten` has one `mt5_bridge.order.submit` child per position |
| `mt5_bridge.reconciliation.boot` | internal | once at boot | `mt5_bridge.reconciliation.pending`, `.resolved`, `.unknown` |
| `mt5_bridge.worker.dispatch` | internal | from handing a command to the terminal worker until its result is back | `mt5_bridge.worker.priority` (`order`, `execution_poll`, `market_poll`), `mt5_bridge.worker.command` (the function that issued it) |
| `mt5_bridge.mt5.call` | internal | on the worker thread, around the terminal call; child of the dispatch span | `mt5_bridge.worker.queue_wait_seconds` |

Every order span also carries one `mt5_bridge.order.finalized` event per
journal update, with the client order id, the new state and the terminal
return code.

A failed span has the error status, an `error.type` attribute and an
`exception` event with the type, the message and the stack trace. The known
secret values are replaced with `[redacted]` in the message and the stack
trace before they are recorded.

### What is not traced, on purpose

- `/v1/health` and `/v1/metrics`, and the gRPC call `GetHealth`: they are
  polled; a span each would be noise. They are still counted in the HTTP
  and RPC metrics.
- Market-data messages on the WebSocket or on a gRPC stream, and the polls
  that produce them. A connection or a `Stream` call is one span however
  many ticks, bars or book snapshots it carries. That span is not the active
  span while the stream runs, so log lines written from the stream loop
  carry no trace id.
- The background loops: execution polling, the dead-man sweep, the clock
  drift probe, the equity snapshot and the terminal watchdog. Their terminal
  calls create no span because no trace is active. The exception is the
  dead-man sweep when it fires: its `mt5_bridge.order.cancel_all` is a trace
  of its own.
- Request and response bodies, request headers other than `User-Agent`,
  query strings and the client address. For gRPC: the messages, the metadata
  other than `user-agent`, and the peer.
- Prices, volumes, balances, account numbers and the terminal credentials.

## Metrics

`GET /v1/metrics` serves every metric below in Prometheus text format; with
`OTEL_METRICS_EXPORTER=otlp` the same metrics are also pushed. Prometheus
rewrites the names: dots become underscores and the unit is appended
(`http.server.request.duration` is `http_server_request_duration_seconds`).

| Metric | Type | Attributes |
| --- | --- | --- |
| `http.server.request.duration` | histogram, seconds | `http.request.method`, `http.route`, `http.response.status_code`, `url.scheme`, `error.type` |
| `http.server.active_requests` | up-down counter | `http.request.method`, `url.scheme` |
| `mt5_bridge.ws.active_connections` | up-down counter | none |
| `rpc.server.call.duration` | histogram, seconds | `rpc.system.name`, `rpc.method`, `rpc.response.status_code`, `error.type` |
| `mt5_bridge.grpc.active_streams` | up-down counter | none |
| `mt5_bridge.order.submit_ack.duration` | histogram, seconds | `symbol` |
| `mt5_bridge.order.rejected` | counter | `reason`, `symbol` |
| `mt5_bridge.order.unknown` | counter | `symbol` |
| `mt5_bridge.order.state`, `mt5_bridge.kill_switch.transition`, `mt5_bridge.stream.gap`, `mt5_bridge.stream.reconnect` | counter | see the call sites |
| `mt5_bridge.kill_switch.state` | gauge | `state` |
| `mt5_bridge.dead_man_switch.armed`, `mt5_bridge.dead_man_switch.expired` | gauge | none |
| `mt5_bridge.terminal.connected` | gauge | none |
| `mt5_bridge.clock_drift.seconds` | gauge | none |
| `mt5_bridge.poll.tick_latency`, `mt5_bridge.poll.bar_latency`, `mt5_bridge.poll.execution_latency` | gauge, seconds | none |

The first three are new with the tracing work and the two after them belong
to the gRPC server; the names of the others did not change.

## Logs

The console line format is described by
[`core/log_formatting.py`](../src/mt5_bridge/core/log_formatting.py) and is
not changed by anything on this page. A line written while a span is active
has `trace_id` and `span_id`, which is how a log line is matched to a trace.

With `OTEL_LOGS_EXPORTER=otlp` each record is also exported with:

- the message as the body, and the severity (`WARNING` becomes `WARN`,
  `CRITICAL` becomes `FATAL`);
- the trace and span ids of the active span;
- `code.file.path`, `code.function.name`, `code.line.number`;
- every `extra=` field of the record;
- `exception.stacktrace` when the record has an exception.

Records from the OpenTelemetry SDK and from its HTTP client are not
exported, so an export failure cannot feed itself. They still reach the
console.

## Why an in-house middleware

The server spans come from a small ASGI middleware,
[`api/telemetry.py`](../src/mt5_bridge/api/telemetry.py), not from
`opentelemetry-instrumentation-fastapi` or `-asgi`:

- Those packages open a span for every WebSocket `receive` and `send`. On
  this service that is a span per market-data message.
- They are pre-release (`0.66b*` for this SDK), and would add four pinned
  dependencies (`opentelemetry-instrumentation`,
  `opentelemetry-instrumentation-asgi`, `opentelemetry-util-http`,
  `asgiref`) to a service that pins every dependency exactly.
- They patch the framework at import time. The middleware is added in
  `create_app` like the body-size one and is tested the same way.

The cost is that the HTTP attribute names are written by hand; they follow
the stable HTTP semantic conventions.

The gRPC server is instrumented the same way, in
[`api/grpc_api/telemetry.py`](../src/mt5_bridge/api/grpc_api/telemetry.py),
and for the same reasons: `opentelemetry-instrumentation-grpc` is
pre-release and adds pinned dependencies. The
handlers of the three services already pass through one wrapper for the
error mapping; the span and the duration are taken there, with the span
helpers of `core/tracing.py`. A call the gRPC library answers by itself (an
unknown method, an oversized message) never reaches a handler and has no
span.
