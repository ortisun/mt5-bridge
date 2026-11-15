"""Generate the gRPC code from ``contracts/mt5_bridge/mt5_bridge.proto``.

Usage:
    python tools/generate_proto.py [--output DIR]

Writes the protobuf messages, the gRPC stubs and their type stubs. Without
``--output`` the files go to ``src/``, into the package
``mt5_bridge.api.grpc_api.generated``; a test passes another directory to
check that the committed files match the contract.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = ROOT / "contracts" / "mt5_bridge"
PROTO = CONTRACT_DIR / "mt5_bridge.proto"
PACKAGE_PATH = "mt5_bridge/api/grpc_api/generated"


def generate(output: Path) -> int:
    """Run protoc; the exit status is protoc's own (0 on success)."""
    output.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        # Maps the contract directory onto the package path, so the generated
        # modules import each other as mt5_bridge.api.grpc_api.generated.*.
        f"-I{PACKAGE_PATH}={CONTRACT_DIR}",
        f"--python_out={output}",
        f"--grpc_python_out={output}",
        f"--mypy_out=quiet:{output}",
        f"--mypy_grpc_out=quiet:{output}",
        str(PROTO),
    ]
    return subprocess.run(command, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the gRPC code from the contract.")
    parser.add_argument("--output", type=Path, default=ROOT / "src")
    return generate(parser.parse_args().output)


if __name__ == "__main__":
    sys.exit(main())
