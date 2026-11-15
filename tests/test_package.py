"""Acceptance tests for the independently installed bridge distribution."""

from importlib import import_module, metadata, resources


def test_bridge_distribution_installs_its_public_package() -> None:
    package = import_module("mt5_bridge")

    assert package.__name__ == "mt5_bridge"
    assert metadata.metadata("mt5-bridge")["Name"] == "mt5-bridge"


def test_bridge_distribution_includes_its_typing_marker() -> None:
    assert resources.files("mt5_bridge").joinpath("py.typed").is_file()
