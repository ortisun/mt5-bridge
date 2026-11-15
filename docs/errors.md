# Errors

Every 4xx and 5xx response of the REST API is a Problem Details document
([RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)) sent as
`application/problem+json`. The schema is `Problem` in
[`contracts/mt5_bridge/openapi.yaml`](../contracts/mt5_bridge/openapi.yaml);
the catalogue of types is `CATALOGUE` in
[`src/mt5_bridge/api/errors.py`](../src/mt5_bridge/api/errors.py), and a test
fails when the table below and that catalogue disagree.

## Example

```http
HTTP/1.1 503 Service Unavailable
Content-Type: application/problem+json
Retry-After: 5

{
  "type": "urn:mt5-bridge:problem:terminal-unavailable",
  "title": "Terminal unavailable",
  "status": 503,
  "detail": "terminal is not connected",
  "instance": "/v1/account",
  "code": "TerminalUnavailable",
  "retryable": true,
  "mt5_retcode": null,
  "mt5_last_error": null
}
```

## Members

| Member | From | Meaning |
| --- | --- | --- |
| `type` | RFC 9457 | Identifies the kind of problem. Compare it as a string; it is not a link. |
| `title` | RFC 9457 | Short summary of the kind. The same for every occurrence. |
| `status` | RFC 9457 | The HTTP status code of the response. |
| `detail` | RFC 9457 | What went wrong this time, for a person. Do not parse it. |
| `instance` | RFC 9457 | The path of the request that failed, without the query string. |
| `code` | bridge | Machine-readable name of the kind, one per `type`. |
| `retryable` | bridge | `true` when the same request can succeed later. |
| `mt5_retcode` | bridge | The trade-server return code, or `null`. |
| `mt5_last_error` | bridge | The terminal's last error, or `null`. |
| `errors` | bridge | Only on a validation problem: one entry per invalid value. |

`type` and `code` name the same thing. Switch on either; `type` is the
standard member. A client must ignore members it does not know.

A rejection by the trade server is not a problem document. `POST /v1/orders`
answers `201` with an order whose `state` is `REJECTED` and whose `reason`
says why; the HTTP request itself succeeded.

### `errors`

```json
{
  "type": "urn:mt5-bridge:problem:validation-error",
  "title": "Invalid request",
  "status": 422,
  "detail": "the request has 2 invalid value(s); see errors",
  "instance": "/v1/orders",
  "code": "ValidationError",
  "retryable": false,
  "mt5_retcode": null,
  "mt5_last_error": null,
  "errors": [
    { "pointer": "#/volume", "detail": "Field required" },
    { "pointer": "#/side", "detail": "Input should be 'BUY' or 'SELL'" }
  ]
}
```

A value in the request body is located by `pointer`, a JSON Pointer
([RFC 6901](https://www.rfc-editor.org/rfc/rfc6901)) in URI-fragment form.
A query, path or header value is located by `in` and `parameter`:
`{ "in": "query", "parameter": "timeframe", "detail": "..." }`. Malformed
JSON has the pointer `#`, the whole body.

## Problem types

The type is a URN, `urn:mt5-bridge:problem:<name>`. The bridge has no public
host name, so a URL would name a host nobody controls; RFC 9457 §3.1.1 allows
a type that cannot be dereferenced. `mt5-bridge` is not a URN namespace
registered with IANA: the string is an identifier private to this API.

| Type | Status | `code` | Retryable | When |
| --- | --- | --- | --- | --- |
| `urn:mt5-bridge:problem:symbol-not-found` | 404 | `NotFound` | no | The terminal does not know the symbol. |
| `urn:mt5-bridge:problem:order-not-found` | 404 | `OrderNotFound` | no | No journal entry has this `client_order_id`. |
| `urn:mt5-bridge:problem:idempotency-conflict` | 409 | `IdempotencyConflict` | no | A `client_order_id` or `operation_id` was reused with a different payload. |
| `urn:mt5-bridge:problem:order-state-conflict` | 409 | `OrderStateConflict` | no | The order's state does not allow an amend or a reconcile. |
| `urn:mt5-bridge:problem:unsupported-venue-capability` | 409 | `UnsupportedVenueCapability` | no | The symbol advertises no filling mode the bridge supports. |
| `urn:mt5-bridge:problem:payload-too-large` | 413 | `PayloadTooLarge` | no | The body exceeds `MT5_BRIDGE_MAX_REQUEST_BODY_BYTES`. |
| `urn:mt5-bridge:problem:validation-error` | 422 | `ValidationError` | no | A body, query or path value is invalid. Carries `errors`. |
| `urn:mt5-bridge:problem:internal-error` | 500 | `InternalError` | no | The bridge failed. The body never quotes the cause; it is in the bridge log. |
| `urn:mt5-bridge:problem:worker-unavailable` | 503 | `WorkerUnavailable` | yes, after 1 s | The worker queue is saturated or the worker stopped. The terminal never saw the command. |
| `urn:mt5-bridge:problem:terminal-unavailable` | 503 | `TerminalUnavailable` | yes, after 5 s | The terminal is not connected. |
| `urn:mt5-bridge:problem:clock-drift-exceeded` | 503 | `ClockDriftExceeded` | yes, after 60 s | A cancel-replace was refused because the clock drift is over its threshold. Nothing was journaled. |

A status raised by the HTTP layer with nothing to add (an unknown path, a
method the path does not allow) has the type `about:blank`, the status
phrase as `title`, and the `code` `error` (RFC 9457 §4.2.1).

## HTTP semantics

| Status | Header | Rule |
| --- | --- | --- |
| 405 | `Allow` | [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110) §15.5.6. |
| 503 | `Retry-After: <seconds>` | RFC 9110 §10.2.3. The value is the type's, from the table above. |

## WebSocket

`/v1/stream` has no error message; it reports a fault by closing
([RFC 6455](https://www.rfc-editor.org/rfc/rfc6455)).

| Fault | What the client sees |
| --- | --- |
| A client message that is not JSON or does not match `ws_messages.schema.json` | Close code `1008` (policy violation), reason `invalid client message`. |
| A failure inside the bridge | Close code `1011` (internal error), sent by the server. |

`RESYNC_REQUIRED` is a control message, not an error: the executions cursor
fell outside the retained window, and the client reloads through REST.
