"""Bridge configuration, read only from the environment (plan §112: secrets

and configuration only through environment variables, never in code or
logs). A boolean setting is true only on an explicit truthy value: an unset
or malformed variable never switches a feature on.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from mt5_bridge.mt5.models import PositionAccounting

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in _TRUE_VALUES


def _optional_int(raw: str | None) -> int | None:
    return int(raw) if raw else None


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    magic: int
    db_path: str
    dead_man_timeout_seconds: float
    clock_drift_threshold_seconds: float
    expected_position_accounting: PositionAccounting
    reconciliation_window_hours: int
    execution_poll_overlap_seconds: int
    sequence_log_retention: int
    environment: str
    execution_poll_interval_seconds: float
    dead_man_sweep_interval_seconds: float
    clock_drift_probe_interval_seconds: float
    equity_snapshot_interval_seconds: float
    market_poll_backlog_limit: int
    max_request_body_bytes: int
    expected_login: int | None
    expected_server: str | None
    trading_day_time_zone: str
    equity_max_age_seconds: float
    quote_max_age_seconds: float
    auction_participation_supported: bool
    auction_window_seconds: float
    watchdog_poll_interval_seconds: float
    watchdog_backoff_base_seconds: float
    watchdog_backoff_max_seconds: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> BridgeSettings:
        return cls(
            magic=int(env.get("MT5_BRIDGE_MAGIC", "20260926")),
            db_path=env.get("MT5_BRIDGE_DB_PATH", "mt5_bridge.sqlite3"),
            dead_man_timeout_seconds=float(env.get("MT5_BRIDGE_DEAD_MAN_TIMEOUT_SECONDS", "30")),
            clock_drift_threshold_seconds=float(
                env.get("MT5_BRIDGE_CLOCK_DRIFT_THRESHOLD_SECONDS", "2")
            ),
            expected_position_accounting=PositionAccounting(
                env.get("MT5_BRIDGE_POSITION_ACCOUNTING", PositionAccounting.NETTING.value)
            ),
            reconciliation_window_hours=int(
                env.get("MT5_BRIDGE_RECONCILIATION_WINDOW_HOURS", "24")
            ),
            execution_poll_overlap_seconds=int(
                env.get("MT5_BRIDGE_EXECUTION_POLL_OVERLAP_SECONDS", "60")
            ),
            sequence_log_retention=int(env.get("MT5_BRIDGE_SEQUENCE_LOG_RETENTION", "10000")),
            environment=env.get("MT5_BRIDGE_ENVIRONMENT", "demo"),
            execution_poll_interval_seconds=float(
                env.get("MT5_BRIDGE_EXECUTION_POLL_INTERVAL_SECONDS", "1")
            ),
            dead_man_sweep_interval_seconds=float(
                env.get("MT5_BRIDGE_DEAD_MAN_SWEEP_INTERVAL_SECONDS", "1")
            ),
            clock_drift_probe_interval_seconds=float(
                env.get("MT5_BRIDGE_CLOCK_DRIFT_PROBE_INTERVAL_SECONDS", "60")
            ),
            equity_snapshot_interval_seconds=float(
                env.get("MT5_BRIDGE_EQUITY_SNAPSHOT_INTERVAL_SECONDS", "5")
            ),
            market_poll_backlog_limit=int(env.get("MT5_BRIDGE_MARKET_POLL_BACKLOG_LIMIT", "1000")),
            max_request_body_bytes=int(env.get("MT5_BRIDGE_MAX_REQUEST_BODY_BYTES", "65536")),
            expected_login=_optional_int(env.get("MT5_BRIDGE_EXPECTED_LOGIN")),
            expected_server=env.get("MT5_BRIDGE_EXPECTED_SERVER") or None,
            trading_day_time_zone=env.get("MT5_BRIDGE_TRADING_DAY_TIME_ZONE", "America/Sao_Paulo"),
            equity_max_age_seconds=float(env.get("MT5_BRIDGE_EQUITY_MAX_AGE_SECONDS", "30")),
            quote_max_age_seconds=float(env.get("MT5_BRIDGE_QUOTE_MAX_AGE_SECONDS", "5")),
            auction_participation_supported=_parse_bool(
                env.get("MT5_BRIDGE_AUCTION_PARTICIPATION_SUPPORTED", "false")
            ),
            auction_window_seconds=float(env.get("MT5_BRIDGE_AUCTION_WINDOW_SECONDS", "300")),
            watchdog_poll_interval_seconds=float(
                env.get("MT5_BRIDGE_WATCHDOG_POLL_INTERVAL_SECONDS", "5")
            ),
            watchdog_backoff_base_seconds=float(
                env.get("MT5_BRIDGE_WATCHDOG_BACKOFF_BASE_SECONDS", "1")
            ),
            watchdog_backoff_max_seconds=float(
                env.get("MT5_BRIDGE_WATCHDOG_BACKOFF_MAX_SECONDS", "60")
            ),
        )
