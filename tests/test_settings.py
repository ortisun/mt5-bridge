"""BridgeSettings.from_env: safe defaults, explicit parsing, no inference."""

from mt5_bridge.mt5.models import PositionAccounting
from mt5_bridge.settings import BridgeSettings


def test_defaults_are_safe_when_nothing_is_set() -> None:
    settings = BridgeSettings.from_env({})

    assert settings.expected_position_accounting is PositionAccounting.NETTING


def test_numeric_fields_are_read_from_the_environment() -> None:
    settings = BridgeSettings.from_env(
        {
            "MT5_BRIDGE_MAGIC": "555",
            "MT5_BRIDGE_DEAD_MAN_TIMEOUT_SECONDS": "45",
            "MT5_BRIDGE_CLOCK_DRIFT_THRESHOLD_SECONDS": "1.5",
        }
    )

    assert settings.magic == 555
    assert settings.dead_man_timeout_seconds == 45.0
    assert settings.clock_drift_threshold_seconds == 1.5


def test_position_accounting_is_read_explicitly_not_inferred() -> None:
    settings = BridgeSettings.from_env({"MT5_BRIDGE_POSITION_ACCOUNTING": "HEDGING"})

    assert settings.expected_position_accounting is PositionAccounting.HEDGING


def test_environment_defaults_to_demo_and_is_read_explicitly() -> None:
    assert BridgeSettings.from_env({}).environment == "demo"
    assert BridgeSettings.from_env({"MT5_BRIDGE_ENVIRONMENT": "paper"}).environment == "paper"


def test_background_loop_intervals_have_safe_small_defaults() -> None:
    settings = BridgeSettings.from_env({})

    assert 0 < settings.execution_poll_interval_seconds <= 5
    assert 0 < settings.dead_man_sweep_interval_seconds <= 5
    assert settings.dead_man_sweep_interval_seconds < settings.dead_man_timeout_seconds
    assert 0 < settings.equity_snapshot_interval_seconds <= 30
    assert settings.clock_drift_probe_interval_seconds > 0


def test_worker_and_request_limits_are_read_from_the_environment() -> None:
    settings = BridgeSettings.from_env(
        {
            "MT5_BRIDGE_MARKET_POLL_BACKLOG_LIMIT": "50",
            "MT5_BRIDGE_MAX_REQUEST_BODY_BYTES": "1024",
        }
    )

    assert settings.market_poll_backlog_limit == 50
    assert settings.max_request_body_bytes == 1024


def test_trading_day_time_zone_defaults_to_b3_and_is_read_from_the_environment() -> None:
    default = BridgeSettings.from_env({})
    overridden = BridgeSettings.from_env({"MT5_BRIDGE_TRADING_DAY_TIME_ZONE": "UTC"})

    assert default.trading_day_time_zone == "America/Sao_Paulo"
    assert overridden.trading_day_time_zone == "UTC"


def test_equity_and_quote_max_age_have_safe_defaults_and_are_read_from_the_environment() -> None:
    default = BridgeSettings.from_env({})
    overridden = BridgeSettings.from_env(
        {
            "MT5_BRIDGE_EQUITY_MAX_AGE_SECONDS": "60",
            "MT5_BRIDGE_QUOTE_MAX_AGE_SECONDS": "2",
        }
    )

    assert default.equity_max_age_seconds == 30.0
    assert default.quote_max_age_seconds == 5.0
    assert overridden.equity_max_age_seconds == 60.0
    assert overridden.quote_max_age_seconds == 2.0


def test_auction_participation_defaults_closed_and_requires_an_explicit_true() -> None:
    assert BridgeSettings.from_env({}).auction_participation_supported is False
    assert (
        BridgeSettings.from_env(
            {"MT5_BRIDGE_AUCTION_PARTICIPATION_SUPPORTED": "true"}
        ).auction_participation_supported
        is True
    )
    assert (
        BridgeSettings.from_env(
            {"MT5_BRIDGE_AUCTION_PARTICIPATION_SUPPORTED": "garbage"}
        ).auction_participation_supported
        is False
    )


def test_auction_window_has_a_safe_default_and_is_read_from_the_environment() -> None:
    default = BridgeSettings.from_env({})
    overridden = BridgeSettings.from_env({"MT5_BRIDGE_AUCTION_WINDOW_SECONDS": "60"})

    assert default.auction_window_seconds == 300.0
    assert overridden.auction_window_seconds == 60.0


def test_watchdog_settings_have_safe_defaults_and_are_read_from_the_environment() -> None:
    default = BridgeSettings.from_env({})
    overridden = BridgeSettings.from_env(
        {
            "MT5_BRIDGE_WATCHDOG_POLL_INTERVAL_SECONDS": "1",
            "MT5_BRIDGE_WATCHDOG_BACKOFF_BASE_SECONDS": "2",
            "MT5_BRIDGE_WATCHDOG_BACKOFF_MAX_SECONDS": "30",
        }
    )

    assert default.watchdog_poll_interval_seconds == 5.0
    assert default.watchdog_backoff_base_seconds == 1.0
    assert default.watchdog_backoff_max_seconds == 60.0
    assert overridden.watchdog_poll_interval_seconds == 1.0
    assert overridden.watchdog_backoff_base_seconds == 2.0
    assert overridden.watchdog_backoff_max_seconds == 30.0
