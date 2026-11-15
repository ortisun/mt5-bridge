"""main.boot(): the exact composition root, exercised with the fake backend."""

from decimal import Decimal
from pathlib import Path

import pytest

from mt5_bridge.api.app import create_app
from mt5_bridge.api.grpc_api.server import GrpcServeOptions
from mt5_bridge.clock import SystemClock
from mt5_bridge.core.kill_switch import KillSwitchState
from mt5_bridge.core.supervisor import AccountBindingMismatch
from mt5_bridge.main import (
    ServeOptions,
    boot,
    build_mt5_client,
    grpc_serve_options,
    resolve_terminal_connection,
    serve_options,
)
from mt5_bridge.mt5.models import Mt5Credentials, PositionAccounting, TerminalAttachment
from mt5_bridge.mt5.terminal_time import ServerWallClockTerminal
from mt5_bridge.secrets import InMemorySecretReader

from testkit.http_client import TypedTestClient


def env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    base = {
        "MT5_BRIDGE_CLIENT_BACKEND": "fake",
        "MT5_BRIDGE_DB_PATH": str(tmp_path / "bridge.db"),
    }
    base.update(overrides)
    return base


NO_SECRETS = InMemorySecretReader()


def test_boot_wires_a_working_application(tmp_path: Path) -> None:
    result = boot(env(tmp_path), NO_SECRETS)
    try:
        app = create_app(result.state)
        with TypedTestClient(app) as http:
            response = http.get("/v1/health")
        assert response.status_code == 200
        assert response.json()["kill_switch_state"] == KillSwitchState.NORMAL.value
    finally:
        result.state.worker.stop()


def test_boot_with_no_prior_orders_reconciles_nothing(tmp_path: Path) -> None:
    result = boot(env(tmp_path), NO_SECRETS)
    try:
        assert result.reconciled_resolved == ()
        assert result.reconciled_unknown == ()
    finally:
        result.state.worker.stop()


def test_boot_rejects_a_mismatched_position_accounting_expectation(tmp_path: Path) -> None:
    from mt5_bridge.core.supervisor import NettingModeMismatch

    with pytest.raises(NettingModeMismatch):
        boot(
            env(tmp_path, MT5_BRIDGE_POSITION_ACCOUNTING=PositionAccounting.HEDGING.value),
            NO_SECRETS,
        )


def test_boot_rejects_an_unknown_client_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown MT5_BRIDGE_CLIENT_BACKEND"):
        boot(env(tmp_path, MT5_BRIDGE_CLIENT_BACKEND="quantum"), NO_SECRETS)


def test_real_backend_off_windows_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="requires Windows"):
        boot(env(tmp_path, MT5_BRIDGE_CLIENT_BACKEND="real"), NO_SECRETS)


def test_boot_refuses_an_account_other_than_the_expected_login(tmp_path: Path) -> None:
    with pytest.raises(AccountBindingMismatch, match="login"):
        boot(env(tmp_path, MT5_BRIDGE_EXPECTED_LOGIN="42"), NO_SECRETS)


def test_boot_refuses_a_demo_account_for_the_live_environment(tmp_path: Path) -> None:
    with pytest.raises(AccountBindingMismatch, match="trade mode"):
        boot(env(tmp_path, MT5_BRIDGE_ENVIRONMENT="live"), NO_SECRETS)


def test_without_a_terminal_secret_the_bridge_attaches_to_the_logged_in_terminal() -> None:
    connection = resolve_terminal_connection(
        {
            "MT5_BRIDGE_EXPECTED_LOGIN": "123",
            "MT5_BRIDGE_EXPECTED_SERVER": "Broker-DEMO",
            "MT5_BRIDGE_TERMINAL_PATH": r"C:\MT5\terminal64.exe",
        },
        NO_SECRETS,
    )

    assert connection == TerminalAttachment(path=r"C:\MT5\terminal64.exe")


def test_attaching_requires_an_expected_login() -> None:
    with pytest.raises(RuntimeError, match="MT5_BRIDGE_EXPECTED_LOGIN"):
        resolve_terminal_connection({"MT5_BRIDGE_EXPECTED_SERVER": "Broker-DEMO"}, NO_SECRETS)


def test_a_terminal_secret_logs_the_terminal_in_explicitly() -> None:
    reader = InMemorySecretReader(
        {"terminal": {"login": "123", "password": "pw", "server": "Broker-DEMO"}}
    )
    connection = resolve_terminal_connection({"MT5_BRIDGE_TERMINAL_PATH": "t.exe"}, reader)
    assert connection == Mt5Credentials(
        login=123, password="pw", server="Broker-DEMO", path="t.exe"
    )


def test_a_partial_terminal_secret_is_refused() -> None:
    reader = InMemorySecretReader({"terminal": {"login": "123", "server": "S"}})
    with pytest.raises(RuntimeError, match="login, password and server"):
        resolve_terminal_connection({}, reader)


def test_boot_without_an_injected_reader_reads_the_environment(tmp_path: Path) -> None:
    result = boot(env(tmp_path))
    try:
        app = create_app(result.state)
        with TypedTestClient(app) as http:
            response = http.get("/v1/health")
        assert response.status_code == 200
    finally:
        result.state.worker.stop()


def test_attaching_requires_an_expected_server() -> None:
    with pytest.raises(RuntimeError, match="MT5_BRIDGE_EXPECTED_SERVER"):
        resolve_terminal_connection({"MT5_BRIDGE_EXPECTED_LOGIN": "123"}, NO_SECRETS)


def test_boot_refuses_a_server_other_than_the_expected_one(tmp_path: Path) -> None:
    with pytest.raises(AccountBindingMismatch, match="server"):
        boot(env(tmp_path, MT5_BRIDGE_EXPECTED_SERVER="Other-DEMO"), NO_SECRETS)


def test_the_bridge_serves_plain_http_by_default() -> None:
    assert serve_options({}) == ServeOptions(
        host="127.0.0.1", port=8765, certfile=None, keyfile=None
    )


def test_a_bind_with_both_tls_files_serves_tls() -> None:
    options = serve_options(
        {
            "MT5_BRIDGE_HOST": "192.168.64.2",
            "MT5_BRIDGE_PORT": "9000",
            "MT5_BRIDGE_TLS_CERTFILE": "cert.pem",
            "MT5_BRIDGE_TLS_KEYFILE": "key.pem",
        }
    )

    assert options == ServeOptions(
        host="192.168.64.2", port=9000, certfile="cert.pem", keyfile="key.pem"
    )


def test_a_non_loopback_bind_without_tls_serves_plain_http() -> None:
    """TLS is optional for any host, never mandatory (2026-09-29 user decision)."""
    options = serve_options({"MT5_BRIDGE_HOST": "192.168.64.2"})
    assert options == ServeOptions(host="192.168.64.2", port=8765, certfile=None, keyfile=None)


def test_tls_needs_both_files() -> None:
    with pytest.raises(RuntimeError, match="both"):
        serve_options({"MT5_BRIDGE_TLS_CERTFILE": "cert.pem"})


def test_a_declared_server_time_zone_wraps_the_terminal_client() -> None:
    client = build_mt5_client(
        env={"MT5_BRIDGE_TERMINAL_TIME_ZONE": "America/Sao_Paulo"},
        clock=SystemClock(),
        secrets=NO_SECRETS,
    )

    assert isinstance(client, ServerWallClockTerminal)


def test_without_a_declared_zone_terminal_epochs_are_taken_as_utc() -> None:
    client = build_mt5_client(env={}, clock=SystemClock(), secrets=NO_SECRETS)

    assert not isinstance(client, ServerWallClockTerminal)


def test_an_unknown_server_time_zone_stops_boot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="time zone"):
        boot(env(tmp_path, MT5_BRIDGE_TERMINAL_TIME_ZONE="Mars/Olympus_Mons"), NO_SECRETS)


def test_an_unknown_trading_day_time_zone_stops_boot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="time zone"):
        boot(env(tmp_path, MT5_BRIDGE_TRADING_DAY_TIME_ZONE="Mars/Olympus_Mons"), NO_SECRETS)


class TestDailyLossBaselineWiring:
    """Audit F1, exercised through the real composition root: a restart
    (a second `boot()` against the same on-disk database) must not silently
    reset the daily-loss baseline, and an account mismatch must fail closed."""

    def test_a_same_day_restart_reuses_the_persisted_baseline_not_a_fresh_one(
        self, tmp_path: Path
    ) -> None:
        # The fake demo account's equity is a fixed 0 (main._demo_account),
        # so the numeric retention behavior itself is covered end to end by
        # test_daily_loss_baseline.py; this proves the *wiring*: a second
        # boot of the same account, same day, must resolve against the row
        # the first boot wrote rather than flagging a false account mismatch.
        first = boot(env(tmp_path), NO_SECRETS)
        try:
            assert first.state.equity_tracker.evidence_unavailable_reason is None
            assert first.state.equity_tracker.session_start_equity == Decimal(0)
        finally:
            first.state.worker.stop()

        second = boot(env(tmp_path), NO_SECRETS)
        try:
            assert second.state.equity_tracker.evidence_unavailable_reason is None
            assert second.state.equity_tracker.session_start_equity == Decimal(0)
        finally:
            second.state.worker.stop()

    def test_a_baseline_bound_to_a_different_account_fails_closed(self, tmp_path: Path) -> None:
        from mt5_bridge.core.daily_loss_baseline import DailyLossBaseline, TradingDayPolicy
        from mt5_bridge.core.database import BridgeDatabase

        bridge_env = env(tmp_path)
        seed_db = BridgeDatabase(bridge_env["MT5_BRIDGE_DB_PATH"])
        DailyLossBaseline(seed_db).resolve(
            account_id="some-other-account",
            trading_day_policy=TradingDayPolicy("America/Sao_Paulo"),
            now=SystemClock().now_utc(),
            current_equity=Decimal(1000),
        )
        seed_db.close()

        result = boot(bridge_env, NO_SECRETS)
        try:
            assert result.state.equity_tracker.evidence_unavailable_reason is not None
            assert "some-other-account" in result.state.equity_tracker.evidence_unavailable_reason
        finally:
            result.state.worker.stop()


class TestLogSecretsCollectedAtBoot:
    def test_the_fake_backend_holds_no_terminal_password(self, tmp_path: Path) -> None:
        reader = InMemorySecretReader(
            {"terminal": {"login": "1", "password": "term-pw", "server": "Broker-DEMO"}}
        )
        result = boot(env(tmp_path), reader)
        try:
            assert result.log_secrets == frozenset()
        finally:
            result.state.worker.stop()


def test_grpc_is_not_served_unless_a_port_is_set() -> None:
    assert grpc_serve_options({}, serve_options({})) is None


def test_the_grpc_listener_uses_the_http_host_and_certificate_pair() -> None:
    env = {
        "MT5_BRIDGE_HOST": "10.0.0.5",
        "MT5_BRIDGE_GRPC_PORT": "8766",
        "MT5_BRIDGE_TLS_CERTFILE": "cert.pem",
        "MT5_BRIDGE_TLS_KEYFILE": "key.pem",
    }

    options = grpc_serve_options(env, serve_options(env))

    assert options == GrpcServeOptions(
        host="10.0.0.5", port=8766, certfile="cert.pem", keyfile="key.pem"
    )


def test_the_grpc_listener_can_bind_its_own_host() -> None:
    env = {"MT5_BRIDGE_GRPC_PORT": "8766", "MT5_BRIDGE_GRPC_HOST": "127.0.0.2"}

    options = grpc_serve_options(env, serve_options(env))

    assert options is not None
    assert (options.host, options.port) == ("127.0.0.2", 8766)


@pytest.mark.parametrize("port", ["8765", "0", "70000"], ids=["the-http-port", "zero", "too-big"])
def test_an_unusable_grpc_port_stops_startup(port: str) -> None:
    env = {"MT5_BRIDGE_GRPC_PORT": port}

    with pytest.raises(RuntimeError, match="MT5_BRIDGE_GRPC_PORT"):
        grpc_serve_options(env, serve_options(env))
