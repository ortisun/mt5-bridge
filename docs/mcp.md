# MCP adapter

`mt5-bridge-mcp` lets an AI agent use the bridge through the
[Model Context Protocol](https://modelcontextprotocol.io/). It is a client of
the bridge: each tool is one operation of the REST contract, called over
HTTP. It runs wherever the agent runs (macOS, Linux or Windows) and needs no
terminal.

```
agent (Claude, an IDE, any MCP host)
   │  MCP: stdio or Streamable HTTP
mt5-bridge-mcp
   │  REST v1
mt5-bridge  ──  MetaTrader 5 terminal (Windows)
```

The adapter adds no trading logic and removes no control. The limits, the
kill switch and the dead-man switch are enforced by the bridge, as for any
other client.

## Access

The bridge has no client authentication and the adapter adds none: whoever
can run the adapter can read, trade and rearm the kill switch, within the
bridge's limits. Who can reach the bridge is decided by the network (the
bridge's bind address and the guest firewall, see the
[README](../README.md)).

## What the agent gets

- **Tools.** One per contract operation, named in snake case after its
  `operationId` (`getBars` is `get_bars`). Arguments are the operation's
  path, query and body fields in one flat object, with the contract's types,
  enums and decimal-string pattern. Every operation is offered, the trading
  ones included.
- **Hints.** Each tool says whether it is read-only, destructive and
  idempotent, so a host can ask the user before a trading call.
- **A guide.** The resource `mt5-bridge://guide` and the server instructions
  tell the agent the rules: decimals are strings, one `client_order_id` per
  intent, what `UNKNOWN` means, how to read an error, when to stop and ask.

## Errors

A call the bridge refuses is an MCP tool error (`isError` true) whose text is
the bridge's problem document ([errors.md](errors.md)), member for member:
`type`, `title`, `status`, `detail`, `instance`, `code`, `retryable`,
`mt5_retcode`, `mt5_last_error`, and `errors` on a 422. The adapter adds one
member, `retry_after_seconds`, from the `Retry-After` header of a 503.

```json
{
  "type": "urn:mt5-bridge:problem:order-not-found",
  "title": "Unknown order",
  "status": 404,
  "detail": "unknown client_order_id 'agent-1'",
  "instance": "/v1/orders/agent-1",
  "code": "OrderNotFound",
  "retryable": false,
  "mt5_retcode": null,
  "mt5_last_error": null
}
```

An answer that is not a problem document can only come from something between
the adapter and the bridge, such as a proxy. The adapter reports it with the
same members: type `about:blank`, the status, the code `error`, and
`retryable` false.

When no answer arrives the tool error is plain text: the bridge could not be
reached, or it did not answer within the timeout. The adapter never repeats a
call by itself. After a timeout the outcome of an order is not known; the
agent repeats the call with the same `client_order_id`, which the bridge
answers with the first order.

## Run it for one agent (stdio)

The agent's host starts the adapter as a subprocess. Install the project with
the `mcp` extra, or run it from a checkout:

```bash
uv run --locked --extra mcp mt5-bridge-mcp
```

Settings come from the environment the host gives the process:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MT5_BRIDGE_MCP_URL` | `http://127.0.0.1:8765` | address of the bridge |
| `MT5_BRIDGE_MCP_CA_FILE` | | CA certificate, when the bridge uses TLS with a private CA |
| `MT5_BRIDGE_MCP_TIMEOUT_SECONDS` | `30` | timeout of one call to the bridge |

A host configuration, in the JSON form most MCP hosts read:

```json
{
  "mcpServers": {
    "mt5-bridge": {
      "command": "uv",
      "args": ["run", "--locked", "--extra", "mcp", "--directory", "/path/to/mt5-bridge", "mt5-bridge-mcp"],
      "env": {
        "MT5_BRIDGE_MCP_URL": "http://192.168.64.5:8765"
      }
    }
  }
}
```

## Run it for several agents (Streamable HTTP)

```bash
uv run --locked --extra mcp mt5-bridge-mcp --transport http --host 127.0.0.1 --port 8766
```

The endpoint is `http://<host>:8766/mcp`. The adapter keeps no session state.

The adapter serves plain HTTP. On `127.0.0.1` it rejects requests whose
`Host` or `Origin` is not local. Bound to another address it has neither TLS
nor that check, and everyone who reaches the port reaches the bridge through
it: bind it only to an address on a network you control.

## Letting an agent trade

An agent that reaches the bridge can trade: every tool is offered and the
order tools send real orders, held back only by the bridge's limits and its
kill switch. The adapter cannot offer an agent the reading tools alone.

Start on a demo account. An agent cannot keep a heartbeat going between its
turns, so do not rely on the dead-man switch to protect orders an agent
leaves open: once an agent calls `heartbeat`, its open orders are canceled
when the calls stop.

## Traces

When the process has an active OpenTelemetry span, each call to the bridge
carries its W3C `traceparent` header, and the bridge's spans join that trace
([observability.md](observability.md)). The adapter creates no spans and
configures no exporter of its own.

## When the contract changes

The tools are generated from `contracts/mt5_bridge/openapi.yaml` into
`src/mt5_bridge/mcp_adapter/operations.json`, which is committed so the
installed adapter needs neither the contract file nor a YAML parser. After
editing the contract:

```bash
make mcp-operations
```

`tests/mcp_adapter/test_contract_tools.py` fails while the file is stale.
Extra guidance for a tool goes in `AGENT_NOTES` in
`src/mt5_bridge/mcp_adapter/contract_tools.py`. The WebSocket stream has no
MCP counterpart; an agent reads the latest state with the REST tools.

## What has been tried

The tests in `tests/mcp_adapter/` run an MCP client against the adapter and
the real bridge application over the fake terminal, on both transports, in
one process. Not tried: the `mt5-bridge-mcp` command under any MCP host
(Claude Desktop, Claude Code, an IDE), the adapter against a bridge on a
Windows guest, and TLS with a private CA.
