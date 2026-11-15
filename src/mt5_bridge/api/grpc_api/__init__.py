"""The gRPC form of the API: the same operations as the REST routes and the
WebSocket stream, over the contract ``contracts/mt5_bridge/mt5_bridge.proto``.

``generated/`` holds the code ``make proto`` produces from that file; it is
never edited by hand. Every service reads the same ``AppState`` the FastAPI
app does, so both transports share one worker, one journal and one kill
switch.
"""
