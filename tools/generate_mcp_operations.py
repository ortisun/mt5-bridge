"""Rewrite the MCP adapter's operations.json from the REST contract.

Run after any change to contracts/mt5_bridge/openapi.yaml:

    make mcp-operations
"""

from pathlib import Path

import yaml

from mt5_bridge.mcp_adapter.contract_tools import render_operations

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "mt5_bridge" / "openapi.yaml"
TARGET = ROOT / "src" / "mt5_bridge" / "mcp_adapter" / "operations.json"


def main() -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    TARGET.write_text(render_operations(document), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
