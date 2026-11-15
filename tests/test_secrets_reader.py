"""The secret readers: environment variables in production, a fake in tests."""

from mt5_bridge.secrets import (
    EnvironmentSecretReader,
    InMemorySecretReader,
    terminal_credentials,
)


def test_the_terminal_credentials_come_from_their_variables_trimmed() -> None:
    reader = EnvironmentSecretReader(
        {
            "MT5_BRIDGE_LOGIN": "123\r\n",
            "MT5_BRIDGE_PASSWORD": " pw ",
            "MT5_BRIDGE_SERVER": "Broker-DEMO",
        }
    )

    assert terminal_credentials(reader) == {
        "login": "123",
        "password": "pw",
        "server": "Broker-DEMO",
    }


def test_no_terminal_variable_means_no_terminal_secret() -> None:
    assert terminal_credentials(EnvironmentSecretReader({})) is None


def test_a_partly_set_terminal_secret_comes_back_partial() -> None:
    reader = EnvironmentSecretReader({"MT5_BRIDGE_LOGIN": "123"})

    assert terminal_credentials(reader) == {"login": "123"}


def test_an_unknown_path_holds_nothing() -> None:
    assert EnvironmentSecretReader({"MT5_BRIDGE_LOGIN": "123"}).read("other") is None


def test_the_in_memory_reader_returns_a_copy_of_what_it_was_given() -> None:
    reader = InMemorySecretReader({"terminal": {"login": "1"}})

    assert reader.read("terminal") == {"login": "1"}
    assert reader.read("other") is None
