"""The gRPC server: the three services of the contract on one port.

Built inside the running event loop (``api.app``'s lifespan starts and stops
it next to the background loops), so its handlers share the loop, the worker
and every piece of ``AppState`` with the FastAPI app.
"""

from dataclasses import dataclass
from pathlib import Path

import grpc

from mt5_bridge.api.app_state import AppState
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2_grpc as pb_grpc
from mt5_bridge.api.grpc_api.market_data import MarketDataService
from mt5_bridge.api.grpc_api.streaming import StreamingService
from mt5_bridge.api.grpc_api.telemetry import GrpcTelemetry
from mt5_bridge.api.grpc_api.trading import TradingService

SHUTDOWN_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class GrpcServeOptions:
    """``port`` 0 lets the operating system choose (tests). TLS is used when
    both files are set, the same pair the HTTP listener serves."""

    host: str
    port: int
    certfile: str | None = None
    keyfile: str | None = None


@dataclass(frozen=True, slots=True)
class RunningGrpcServer:
    server: grpc.aio.Server
    port: int

    async def stop(self) -> None:
        await self.server.stop(SHUTDOWN_GRACE_SECONDS)


async def start_grpc_server(state: AppState, options: GrpcServeOptions) -> RunningGrpcServer:
    server = grpc.aio.server(
        options=(
            # The same request size limit the HTTP middleware enforces.
            ("grpc.max_receive_message_length", state.settings.max_request_body_bytes),
        )
    )
    address = f"{options.host}:{options.port}"
    if options.certfile is not None and options.keyfile is not None:
        credentials = grpc.ssl_server_credentials(
            [(Path(options.keyfile).read_bytes(), Path(options.certfile).read_bytes())]
        )
        port = server.add_secure_port(address, credentials)
    else:
        port = server.add_insecure_port(address)
    telemetry = GrpcTelemetry(state.meter, server_address=options.host, server_port=port)
    pb_grpc.add_MarketDataServicer_to_server(MarketDataService(state, telemetry), server)
    pb_grpc.add_TradingServicer_to_server(TradingService(state, telemetry), server)
    pb_grpc.add_StreamingServicer_to_server(StreamingService(state, telemetry), server)
    await server.start()
    return RunningGrpcServer(server=server, port=port)
