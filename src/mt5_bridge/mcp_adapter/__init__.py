"""MCP adapter: exposes the bridge's REST contract to AI agents as MCP tools.

The adapter is a client of the bridge. It reaches the bridge only over HTTP,
through the operations in ``contracts/mt5_bridge/openapi.yaml``, and imports
nothing from ``api``, ``core`` or ``mt5`` (import-linter enforces it). Limits,
the kill switch and the trading gate therefore stay enforced by the bridge,
not by this layer.
"""
