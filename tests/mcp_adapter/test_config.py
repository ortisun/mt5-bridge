"""Adapter settings."""

import pytest

from mt5_bridge.mcp_adapter.config import DEFAULT_URL, AdapterConfig, ConfigError


def test_the_default_is_the_local_bridge() -> None:
    config = AdapterConfig.from_env({})

    assert config.bridge_url == DEFAULT_URL
    assert config.ca_file is None
    assert config.timeout_seconds == 30


def test_the_address_the_ca_file_and_the_timeout_come_from_the_environment() -> None:
    config = AdapterConfig.from_env(
        {
            "MT5_BRIDGE_MCP_URL": "https://10.0.0.5:8765/",
            "MT5_BRIDGE_MCP_CA_FILE": "/etc/bridge/ca.pem",
            "MT5_BRIDGE_MCP_TIMEOUT_SECONDS": "2.5",
        }
    )

    assert config.bridge_url == "https://10.0.0.5:8765"
    assert config.ca_file == "/etc/bridge/ca.pem"
    assert config.timeout_seconds == 2.5


@pytest.mark.parametrize("value", ["soon", "0", "-1"])
def test_a_timeout_that_is_not_a_positive_number_is_refused(value: str) -> None:
    with pytest.raises(ConfigError, match="MT5_BRIDGE_MCP_TIMEOUT_SECONDS"):
        AdapterConfig.from_env({"MT5_BRIDGE_MCP_TIMEOUT_SECONDS": value})
