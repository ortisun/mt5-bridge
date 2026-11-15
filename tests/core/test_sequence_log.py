"""SequenceLog: monotonic per-stream seq, bounded replay, RESYNC_REQUIRED."""

from datetime import UTC, datetime

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.sequence_log import ResumeStatus, SequenceLog

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def make_log() -> tuple[SequenceLog, BridgeDatabase]:
    db = BridgeDatabase(":memory:")
    return SequenceLog(db), db


class TestAppend:
    def test_first_append_on_a_stream_is_sequence_one(self) -> None:
        log, db = make_log()

        with db.transaction() as conn:
            seq = log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)

        assert seq == 1

    def test_appends_are_monotonic_within_a_stream(self) -> None:
        log, db = make_log()

        with db.transaction() as conn:
            first = log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)
            second = log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)

        assert (first, second) == (1, 2)

    def test_streams_have_independent_sequences(self) -> None:
        log, db = make_log()

        with db.transaction() as conn:
            log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)
            other_first = log.append(conn, stream="status", payload_json="{}", ts_utc=NOW)

        assert other_first == 1


class TestReadFrom:
    def test_read_from_one_returns_every_retained_event_in_order(self) -> None:
        log, db = make_log()
        with db.transaction() as conn:
            log.append(conn, stream="executions", payload_json='{"n": 1}', ts_utc=NOW)
            log.append(conn, stream="executions", payload_json='{"n": 2}', ts_utc=NOW)

        result = log.read_from_no_transaction(stream="executions", from_seq=1)

        assert result.status is ResumeStatus.OK
        assert [event.seq for event in result.events] == [1, 2]

    def test_read_from_a_seq_past_the_end_returns_no_events_but_is_not_expired(self) -> None:
        log, db = make_log()
        with db.transaction() as conn:
            log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)

        result = log.read_from_no_transaction(stream="executions", from_seq=2)

        assert result.status is ResumeStatus.OK
        assert result.events == ()

    def test_read_from_an_empty_stream_at_seq_one_is_ok_and_empty(self) -> None:
        log, _db = make_log()

        result = log.read_from_no_transaction(stream="executions", from_seq=1)

        assert result.status is ResumeStatus.OK
        assert result.events == ()


class TestRetentionAndResync:
    def test_pruning_keeps_only_the_configured_window(self) -> None:
        log, db = make_log()
        with db.transaction() as conn:
            for _ in range(5):
                log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)
            log.prune(conn, stream="executions", keep_last=2)

        result = log.read_from_no_transaction(stream="executions", from_seq=1)

        assert result.status is ResumeStatus.RESYNC_REQUIRED

    def test_resuming_from_the_oldest_retained_seq_still_succeeds(self) -> None:
        log, db = make_log()
        with db.transaction() as conn:
            for _ in range(5):
                log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)
            log.prune(conn, stream="executions", keep_last=2)

        result = log.read_from_no_transaction(stream="executions", from_seq=4)

        assert result.status is ResumeStatus.OK
        assert [event.seq for event in result.events] == [4, 5]

    def test_resuming_one_before_the_oldest_retained_seq_requires_resync(self) -> None:
        log, db = make_log()
        with db.transaction() as conn:
            for _ in range(5):
                log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)
            log.prune(conn, stream="executions", keep_last=2)

        result = log.read_from_no_transaction(stream="executions", from_seq=3)

        assert result.status is ResumeStatus.RESYNC_REQUIRED
        assert result.events == ()

    def test_pruning_every_row_away_still_requires_resync_not_ok_and_empty(self) -> None:
        """Regression: an empty stream from full pruning must not look like
        an empty stream that simply never had any data (see TestReadFrom's
        "an empty stream at seq one is ok" case) — that would let a client
        believe it is caught up while it silently missed everything."""
        log, db = make_log()
        with db.transaction() as conn:
            log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)
            log.prune(conn, stream="executions", keep_last=0)

        result = log.read_from_no_transaction(stream="executions", from_seq=1)

        assert result.status is ResumeStatus.RESYNC_REQUIRED
        assert result.events == ()

    def test_a_later_prune_never_lowers_an_already_advanced_floor(self) -> None:
        log, db = make_log()
        with db.transaction() as conn:
            for _ in range(5):
                log.append(conn, stream="executions", payload_json="{}", ts_utc=NOW)
            log.prune(conn, stream="executions", keep_last=1)  # floor -> 4
            log.prune(conn, stream="executions", keep_last=10)  # would compute a lower floor

        result = log.read_from_no_transaction(stream="executions", from_seq=4)

        assert result.status is ResumeStatus.RESYNC_REQUIRED
