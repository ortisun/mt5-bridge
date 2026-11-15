# Changelog

One section per release, newest first, written by `make bump` from the commit
messages (docs/release.md). An entry with the scope **contract** changes the
REST, WebSocket or gRPC contract.

## v1.0.0 (2026-11-15)

### Feat

- **mcp**: add an mcp adapter that exposes the contract to ai agents
- **contract**: add a gRPC API next to REST and the WebSocket stream
- **contract**: publish logs, audit entries and stream messages as OCSF events
- **core**: add the OpenTelemetry observability layer
- **contract**: report every error as an RFC 9457 problem document
- **deploy**: add the Windows install and start scripts
- **api**: serve the REST routes and the WebSocket stream
- **core**: add the order service, the journal and the safety controls
- **mt5**: add the terminal client port, the real client and the fake terminal
- **contract**: define the REST and WebSocket contract
