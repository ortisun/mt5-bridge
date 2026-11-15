"""Adapter settings, read from the environment of the process the agent starts."""

from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_URL = "http://127.0.0.1:8765"


class ConfigError(ValueError):
    """A setting is invalid; the message names the variable."""


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    bridge_url: str
    ca_file: str | None
    timeout_seconds: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AdapterConfig:
        try:
            timeout = float(env.get("MT5_BRIDGE_MCP_TIMEOUT_SECONDS", "30"))
        except ValueError as error:
            raise ConfigError("MT5_BRIDGE_MCP_TIMEOUT_SECONDS must be a number") from error
        if timeout <= 0:
            raise ConfigError("MT5_BRIDGE_MCP_TIMEOUT_SECONDS must be greater than zero")

        return cls(
            bridge_url=env.get("MT5_BRIDGE_MCP_URL", DEFAULT_URL).rstrip("/"),
            ca_file=env.get("MT5_BRIDGE_MCP_CA_FILE") or None,
            timeout_seconds=timeout,
        )
