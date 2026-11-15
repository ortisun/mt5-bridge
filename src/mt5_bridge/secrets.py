"""Where the bridge's own secrets come from: the process environment (ADR-079).

``MT5_BRIDGE_LOGIN``, ``MT5_BRIDGE_PASSWORD`` and ``MT5_BRIDGE_SERVER`` hold
the terminal credentials for when the bridge must log the terminal in itself.
The operator sets them as system variables of the service user; none is ever
written to the repository or to a log (``core.log_redaction``).

``SecretReader`` is the seam ``main.boot`` reads through, so a test injects
an ``InMemorySecretReader`` and never touches the real environment.
"""

from collections.abc import Mapping
from typing import Protocol

TERMINAL_PATH = "terminal"

_TERMINAL_VARIABLES = {
    "login": "MT5_BRIDGE_LOGIN",
    "password": "MT5_BRIDGE_PASSWORD",
    "server": "MT5_BRIDGE_SERVER",
}


class SecretReader(Protocol):
    def read(self, path: str) -> Mapping[str, str] | None: ...


class InMemorySecretReader:
    """Test/development fake; never a production reader."""

    def __init__(self, secrets: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self._secrets = {path: dict(values) for path, values in (secrets or {}).items()}

    def read(self, path: str) -> Mapping[str, str] | None:
        values = self._secrets.get(path)
        return None if values is None else dict(values)


class EnvironmentSecretReader:
    """Reads the secrets from environment variables.

    Values are trimmed: a newline pasted after a value must not reach the
    terminal login. An unset or blank variable is
    absent, so a terminal secret with only some of its variables set comes
    back partial and ``main.resolve_terminal_connection`` refuses it.
    """

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def read(self, path: str) -> Mapping[str, str] | None:
        if path == TERMINAL_PATH:
            values = {
                key: value
                for key, variable in _TERMINAL_VARIABLES.items()
                if (value := self._value(variable)) is not None
            }
            return values or None
        return None

    def _value(self, variable: str) -> str | None:
        return self._env.get(variable, "").strip() or None


def terminal_credentials(reader: SecretReader) -> Mapping[str, str] | None:
    """``login``/``password``/``server`` when the bridge must log the terminal in itself."""
    return reader.read(TERMINAL_PATH)


__all__ = [
    "TERMINAL_PATH",
    "EnvironmentSecretReader",
    "InMemorySecretReader",
    "SecretReader",
    "terminal_credentials",
]
