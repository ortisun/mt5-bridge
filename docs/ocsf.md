# OCSF mapping

Every JSON log line, every audit record and every message on `/v1/stream` or
on the gRPC `Stream` call is an event of the [Open Cybersecurity Schema Framework](https://schema.ocsf.io/),
schema version **1.9.0** (the stable version the site served on 2026-11-15).

- The vocabulary (version, class and category ids and names, activities,
  severities, object builders) lives in one module,
  [`src/mt5_bridge/core/ocsf.py`](../src/mt5_bridge/core/ocsf.py). No other
  module writes a `class_uid` or an attribute name by hand.
- The tests validate what the service emits against
  [`tests/contracts/ocsf/ocsf-1.9.0-subset.schema.json`](../tests/contracts/ocsf/ocsf-1.9.0-subset.schema.json).
  [`tools/vendor_ocsf_schema.py`](../tools/vendor_ocsf_schema.py) generates
  that file from the JSON Schema export of the site
  (`https://schema.ocsf.io/schema/1.9.0/classes/<class>`); it is the only
  step that needs the network. The subset keeps `additionalProperties:
  false`, so an attribute OCSF does not define fails the tests.
- The WebSocket shapes are part of the contract:
  [`contracts/mt5_bridge/ws_messages.schema.json`](../contracts/mt5_bridge/ws_messages.schema.json).
  So is the gRPC form of the same events, `StreamEvent` in
  [`contracts/mt5_bridge/mt5_bridge.proto`](../contracts/mt5_bridge/mt5_bridge.proto);
  see [gRPC stream messages](#grpc-stream-messages).

## Classes in use

| Class | `class_uid` | Category | Used for |
| --- | --- | --- | --- |
| [Base Event](https://schema.ocsf.io/1.9.0/classes/base_event) | 0 | 0 Uncategorized | plain log lines; `ticks`, `bars`, `book`, `account` messages |
| [Entity Management](https://schema.ocsf.io/1.9.0/classes/entity_management) | 3004 | 3 Identity & Access Management | `executions` messages |
| [Application Lifecycle](https://schema.ocsf.io/1.9.0/classes/application_lifecycle) | 6002 | 6 Application Activity | start and stop log lines; `status` messages |
| [API Activity](https://schema.ocsf.io/1.9.0/classes/api_activity) | 6003 | 6 Application Activity | audit records; `pong` and `RESYNC_REQUIRED` messages |
| [Application Error](https://schema.ocsf.io/1.9.0/classes/application_error) | 6008 | 6 Application Activity | log lines at `ERROR` or above, or with an exception |

`type_uid` is always `class_uid * 100 + activity_id`. Activity 99 (Other)
always comes with an `activity_name`, as the schema requires.

## Attributes every event has

| Attribute | Value |
| --- | --- |
| `class_uid`, `category_uid`, `activity_id`, `type_uid` | the classification (required by OCSF) |
| `class_name`, `category_name`, `activity_name`, `type_name` | the captions; left out of the market-data messages |
| `time` | when it happened, milliseconds since the epoch, UTC |
| `severity_id` (and `severity`) | see the tables below |
| `metadata.version` | `1.9.0` |
| `metadata.product` | `name` `mt5-bridge`, `vendor_name`, `version` (the installed release version) |
| `unmapped` | whatever has no OCSF attribute |

## Logs

`core.log_formatting.JsonLogFormatter` renders each record. The secret
redaction filter runs before it and also scrubs strings nested inside
structured `extra=` fields, so the OCSF attributes of a record are redacted
like its message.

A call site chooses the class with a helper from `core.ocsf`:

```python
logger.info("mt5-bridge starting", extra=ocsf.lifecycle_log_extra(ocsf.LifecycleActivity.START))
logger.info("...", extra=ocsf.log_extra(ocsf.API_ACTIVITY, ocsf.ApiActivity.READ, attributes=...))
```

A record without one is still a valid event:

| Record | Class and activity |
| --- | --- |
| level below `ERROR`, no exception | Base Event, activity 99 `Log` |
| level `ERROR` or above, or an exception | Application Error, activity 1 General Error |
| `extra=ocsf.lifecycle_log_extra(...)` (`main()` at start and stop) | Application Lifecycle, activity 3 Start or 4 Stop, with `app` |
| the `mt5_bridge.audit` logger | API Activity (the audit event, see below) |
| a record that cannot be serialized | Application Error, with a fixed message |

| Field of the record | OCSF attribute |
| --- | --- |
| creation time | `time` |
| level | `severity_id`/`severity`; the level name is `metadata.log_level` |
| formatted message | `message` |
| logger name | `metadata.log_name` |
| (generated) | `metadata.uid`, a unique id per line |
| `MT5_BRIDGE_ENVIRONMENT` | `metadata.labels` |
| exception text, stack text | `unmapped.exception`, `unmapped.stack` |
| any other `extra=` field | `unmapped.<name>` |

| Python level | `severity_id` |
| --- | --- |
| `DEBUG`, `INFO` | 1 Informational |
| `WARNING` | 3 Medium |
| `ERROR` | 4 High |
| `CRITICAL` | 6 Fatal |

### Trace and span ids

OCSF 1.9.0 carries trace context in the
[`trace` profile](https://schema.ocsf.io/1.9.0/profiles/trace): a top-level
`trace` object with `trace.uid` and `trace.span.uid`. Two limits of that
version shape what the formatter writes when a span is active:

- The profile applies only to API Activity and HTTP Activity. For an API
  Activity record the trace id is `trace.uid` and `metadata.profiles` is
  `["trace"]`. For every other class it is `unmapped.trace_id`.
- A `span` object requires the span's `start_time` and `end_time`. A log
  line written inside a running span has no end time, so the span id is
  always `unmapped.span_id`.

## Audit records

`api.audit.AuditLog.record` stores each write request in SQLite and writes
the same event to the `mt5_bridge.audit` logger. `AuditRecord.to_ocsf()` is
an API Activity event:

| Field of the record | OCSF attribute |
| --- | --- |
| `ts_utc` | `time` |
| `method` | `activity_id`: `POST` 1 Create, `GET` 2 Read, `PATCH`/`PUT` 3 Update, `DELETE` 4 Delete; also `http_request.http_method` |
| `method` and `path` | `api.operation` (`"POST /v1/orders"`) |
| `path` | `http_request.url.path` |
| `account_id` | `actor.user.uid` |
| `src_host`, `src_port` (the request's peer) | `src_endpoint.ip` (or `.name` when it is not an address), `src_endpoint.port` |
| `outcome` | `status_code`; `status_id` is 2 Failure for `REJECTED`, `CANCEL_FAILED`, `CANCELED_NEW_REJECTED`, `BOTH_OPEN`, 0 Unknown for `UNKNOWN`, 1 Success otherwise |
| `detail` | `status_detail` |
| `payload_hash` | `unmapped.payload_hash` |
| (constant) | `metadata.event_code` `audit` |

`severity_id` is 1 for a success, 2 (Low) for a failure, 3 (Medium) for an
unknown outcome.

The bridge does not identify its callers. API Activity requires an `actor`,
so the actor is the terminal account the request acts on; the caller is
described only by `src_endpoint`.

### gRPC calls

A gRPC write call is audited at the same point as its REST counterpart, with
the same `account_id`, `outcome`, peer address and status rules. What
differs is how the call is named:

| Field of the record | Value | OCSF attribute |
| --- | --- | --- |
| `method` | `GRPC` | none; there is no `http_request` |
| `path` | `/mt5_bridge.v1.Trading/SubmitOrder` | `api.operation`, without the leading slash |
| (the RPC's name) | | `activity_id`, from its leading verb, below |
| `payload_hash` | SHA-256 of the request message, serialized deterministically; none for a request without fields | `unmapped.payload_hash` |
| `src_host`, `src_port` | parsed from the gRPC peer (`ipv4:10.0.0.7:5000`) | `src_endpoint` |

Every gRPC call is an HTTP POST, so the method cannot set the activity. The
leading verb of the RPC does, and a test checks that each RPC gets the
activity of the REST operation it mirrors:

| Verb | `activity_id` | RPCs audited |
| --- | --- | --- |
| `Submit`, `Replace`, `Reconcile`, `Activate` | 1 Create | `SubmitOrder`, `ReplaceOrder`, `ReconcileOrder`, `ActivateKillSwitch` |
| `Amend` | 3 Update | `AmendOrder` |
| `Cancel`, `Rearm` | 4 Delete | `CancelOrder`, `RearmKillSwitch` |
| `Get`, `List`, `Check` | 2 Read | none: reads are not audited |
| any other | 99, named after the RPC | none |

## WebSocket messages

A client dispatches on `metadata.event_code`: a channel name, `pong` or
`RESYNC_REQUIRED`.

| `metadata.event_code` | Class | Activity | Domain payload |
| --- | --- | --- | --- |
| `ticks` | Base Event | 99 `Tick` | `unmapped.payload` |
| `bars` | Base Event | 99 `Bar` | `unmapped.payload` |
| `book` | Base Event | 99 `Book Snapshot` | `unmapped.payload` |
| `account` | Base Event | 99 `Account Snapshot` | `unmapped.payload` |
| `status` | Application Lifecycle | 99 `State Report` | `unmapped.payload`; `app` is the bridge |
| `executions` | Entity Management | from the order state | `entity.data` |
| `pong` | API Activity | 99 `Ping`, status Success | none |
| `RESYNC_REQUIRED` | API Activity | 2 Read, status Failure | none |

Sequencing and time, for every data message:

| Meaning | OCSF attribute |
| --- | --- |
| channel | `metadata.event_code` |
| stream id (`executions`, `ticks:<symbol>`, `bars:<symbol>:<timeframe>`, ...) | `metadata.log_name` |
| sequence number, monotonic per stream id | `metadata.sequence` |
| event time in true UTC | `time` (milliseconds) |
| the time the source reported, untouched | `metadata.original_time`, a decimal string |
| unit of that raw time (`seconds`, `milliseconds`) | `unmapped.source_time_unit` |
| whether `time` is the exchange's or the bridge's (`exchange_reported`, `bridge_observed`) | `unmapped.time_basis` |
| when the bridge received the datum | `metadata.processed_time` (milliseconds) |

The mapping does not touch sequencing or replay: `executions` is durable
and resumable by `metadata.sequence`, the other streams count per connection.
The sequence log stores the domain payload, and the event is built when it
is sent.

**Market data and account** have no class in OCSF. They are Base Events with
only the required attributes plus `activity_name`: no captions, no `message`.
The classification and `metadata.version`/`metadata.product` add about 200
bytes to each message.

**Executions.** The order is a `managed_entity` (`entity.type_id` 99,
`entity.type` `Order`, `entity.uid` the `client_order_id`) and `entity.data`
is the execution payload (`client_order_id`, `symbol`, `state`, `ticket`,
`mt5_retcode`, `reason`).

| Order state | `activity_id` | `status_id` | `severity_id` |
| --- | --- | --- | --- |
| `RECEIVED`, `SUBMITTED`, `ACKNOWLEDGED` | 1 Create | 1 Success | 1 |
| `REJECTED` | 1 Create | 2 Failure | 2 Low |
| `PARTIALLY_FILLED`, `FILLED`, `PENDING_CANCEL`, `PENDING_REPLACE`, `REPLACED` | 3 Update | 1 Success | 1 |
| `CANCELED`, `EXPIRED` | 4 Delete | 1 Success | 1 |
| `UNKNOWN` | 0 Unknown | 0 Unknown | 4 High |

`status_code` is `mt5_retcode` as a string and `status_detail` is `reason`,
each present when the payload value is not null. `metadata.uid` is
`executions:<sequence>` and `metadata.correlation_uid` is the
`client_order_id`.

**Status.** `severity_id` is 1 while the kill switch is `NORMAL` and the
terminal is connected, 4 (High) otherwise. The payload (`kill_switch_state`,
`terminal_connected`, and `symbol` with `trading_phase` on a phase message)
is under `unmapped.payload`.

**Control replies** are the result of a client op, so they are API Activity:
`api.operation` is the op (`ping`, `subscribe` or `resume`), `actor.user.uid`
the terminal account, `src_endpoint` the connection's peer.
`RESYNC_REQUIRED` also has `status_code` `RESYNC_REQUIRED`, a
`status_detail`, `severity_id` 3 and the stream id in `metadata.log_name`.

## gRPC stream messages

The `Stream` call of `mt5_bridge.v1.Streaming` carries the same events. The
bridge builds one OCSF event per message, as above, and renders it either as
JSON on the WebSocket or as a `StreamEvent`; the classification, the
severity, the status, the metadata and the sequence numbers are therefore the
same on both, and a client dispatches on `metadata.event_code` in both.

The fields of `StreamEvent` have the names of the OCSF attributes:

| OCSF attribute on `/v1/stream` | `StreamEvent` field |
| --- | --- |
| `class_uid`, `category_uid`, `activity_id`, `type_uid`, `activity_name` | the same names |
| `time`, `severity_id` | the same names |
| `status_id`, `status_code`, `status_detail`, `message` | the same names, absent where the JSON has none |
| `metadata.version`, `metadata.product`, `metadata.event_code` | `metadata.version`, `metadata.product`, `metadata.event_code` |
| `metadata.log_name`, `metadata.sequence`, `metadata.original_time`, `metadata.processed_time`, `metadata.uid`, `metadata.correlation_uid` | the same names under `metadata`, absent where the JSON has none |
| `api.operation`, `actor.user`, `src_endpoint` (control replies) | `api.operation`, `actor.user`, `src_endpoint` |
| `unmapped.source_time_unit`, `unmapped.time_basis` | `source_time_unit`, `time_basis` (enums; unspecified on a control reply) |
| `unmapped.payload` (`ticks`, `bars`, `book`, `account`, `status`) | the `payload` oneof: `tick`, `bar`, `book`, `account`, `status` |
| `entity.data` (`executions`) | the `payload` oneof: `execution` |

What the protobuf form leaves out, and why:

- The captions `class_name`, `category_name`, `type_name`, `severity` and
  `status`. Each repeats an identifier that is present.
- `entity.type_id`, `entity.type` and `entity.uid` of an execution: constant,
  or equal to `execution.client_order_id` and `metadata.correlation_uid`.
- `app` of a status message: it is `metadata.product`.

The payload is typed, not JSON in a string: decimals stay strings, and the
order state, kill-switch state, trading phase, timeframe and book side are
the enums the unary calls use. There is no `unmapped` member, so nothing of
the protobuf form sits outside its schema.

Sequencing is the WebSocket's: `executions` is durable and resumable by
`metadata.sequence` through the `resume` operation, the other streams count
per call, and `RESYNC_REQUIRED` is an API Activity event with no payload.
The source endpoint of a control reply is the gRPC peer.

## Where the fit is weak

- **Executions as Entity Management.** It is the only class about create,
  update and delete of a named entity, but it sits in the Identity & Access
  Management category and an order is not an identity object. The
  alternative is a Base Event, which says less.
- **Status as Application Lifecycle.** The class has Start, Stop, Enable and
  Disable activities; a state report is none of them, so the activity is 99.
- **Market data and account as Base Events.** OCSF has no market-data class.
  Almost all of the content is under `unmapped`, which OCSF tells consumers
  not to rely on; here the contract fixes its structure.
- **`actor`.** Required by API Activity, but the bridge has no caller
  identity to put there.
- **Span id.** No valid place in 1.9.0 for the span of a log line; see above.
- **Sub-millisecond time.** `timestamp_t` is whole milliseconds, so the
  microseconds of the bridge's own clock are dropped from `time` and
  `metadata.processed_time`.
- **`vendor_name`.** The repository names no vendor; `PRODUCT_VENDOR_NAME` in
  `core/ocsf.py` is `mt5-bridge` until the owner chooses one.

## Not done here

- uvicorn's access lines are rendered as Base Events from their message
  text; they are not parsed into API Activity or HTTP Activity events.
- No OCSF extension is defined. Defining one for market data and order
  events is the schema's own recommended alternative to `unmapped`.
