"""``mt5-bridge-mcp``: serve the adapter over stdio (default) or Streamable HTTP."""

import argparse
import os
import sys

import anyio
import httpx2

from mt5_bridge.mcp_adapter.config import AdapterConfig, ConfigError
from mt5_bridge.mcp_adapter.server import build_http_app, build_server


def _http_client(config: AdapterConfig) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=config.bridge_url,
        timeout=config.timeout_seconds,
        verify=config.ca_file or True,
    )


async def _serve_stdio(config: AdapterConfig) -> None:
    from mcp.server.stdio import stdio_server

    async with _http_client(config) as http:
        server = build_server(config, http)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def _serve_http(config: AdapterConfig, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(build_http_app(config, _http_client(config), host=host), host=host, port=port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mt5-bridge-mcp", description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="bind address for --transport http")
    parser.add_argument("--port", type=int, default=8766, help="port for --transport http")
    args = parser.parse_args(argv)
    transport: str = args.transport

    try:
        config = AdapterConfig.from_env(os.environ)
    except ConfigError as error:
        sys.exit(f"mt5-bridge-mcp: {error}")

    if transport == "http":
        _serve_http(config, args.host, args.port)
    else:
        anyio.run(_serve_stdio, config)


if __name__ == "__main__":
    main()
