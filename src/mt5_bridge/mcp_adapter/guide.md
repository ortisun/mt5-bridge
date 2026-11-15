# Using the MT5 bridge as an agent

The bridge is a gateway to one MetaTrader 5 terminal logged in to one broker
account. These tools call its REST API. The bridge has no strategy: it reads
the terminal and sends the orders it is told to send.

## Reading

- Start with `get_health`. If the terminal is not connected, market data and
  orders will fail; say so instead of retrying.
- Prices, volumes and money are decimal strings (`"128.50"`), in results and
  in arguments. Never convert them to floating point; pass them back exactly.
- `get_symbol` gives the rules of one instrument: `tick_size`, `volume_step`,
  `volume_min`, `volume_max`, the allowed filling modes and the trading phase.
  Read it before building an order.
- `get_bars` returns closed bars only, oldest first. `get_ticks` without
  `from_time_msc` returns a recent window, not the whole history.
- `list_open_orders` and `get_order` show the bridge's journal.
  `list_history_orders` and `list_history_deals` show the terminal's history.

## Trading

Trading tools act on a real broker account: an order the bridge accepts is
sent to the market.

- Confirm with the user before `submit_order`, `amend_order`, `cancel_order`,
  `replace_order`, `activate_kill_switch` and `rearm_kill_switch`, unless the
  user already gave that exact instruction.
- Use `check_order` first; it sends nothing to the market.
- Every `submit_order` needs a fresh, unique `client_order_id`. Repeating a
  call with the same id and payload is safe and returns the first order. If a
  call fails or times out, repeat it with the same id; never invent a new id
  for the same intent, or the order may be sent twice.
- An order the trade server rejected is not an error: `submit_order` succeeds
  and returns the order with `state` `REJECTED` and a `reason`.
- An order in state `UNKNOWN` means the bridge could not confirm what the
  terminal did. Do not resubmit. Read it again with `get_order` and tell the
  operator; the symbol stays blocked until it is reconciled.
- The bridge enforces its own limits (volume per order, open notional, daily
  loss, orders per minute). A refusal from a limit is final; do not split an
  order to get around it.
- `activate_kill_switch` only escalates.
  `rearm_kill_switch` is the operator's decision, not yours.
- `heartbeat` arms the dead-man switch. Once armed, open orders are canceled
  when heartbeats stop. Do not call it unless you will keep calling it.

## Errors

A failed call returns the bridge's problem document (RFC 9457) as JSON:

- `status`: the HTTP status code.
- `code`: the machine-readable kind, for example `OrderNotFound`,
  `IdempotencyConflict`, `ValidationError`, `TerminalUnavailable`. `type`
  names the same kind as a URN.
- `detail`: what went wrong this time, for a person. Report it; do not parse it.
- `retryable`: repeat the same call only when this is true. A 503 also
  carries `retry_after_seconds`; wait that long first.
- `errors`: on a `ValidationError` (422), one entry per invalid argument,
  located by `pointer` (a body field) or by `in` and `parameter`.
- `mt5_retcode`, `mt5_last_error`: what the terminal reported, or null.

An error that is plain text instead of JSON came from the adapter itself: the
bridge could not be reached, did not answer in time, or an argument is missing.
