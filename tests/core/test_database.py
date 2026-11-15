"""BridgeDatabase: the journal/audit file is owner-only (plan §112)."""

import stat
from pathlib import Path

from mt5_bridge.core.database import BridgeDatabase


def test_a_file_backed_database_is_created_owner_read_write_only(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge.sqlite3"

    BridgeDatabase(str(db_path))

    mode = stat.S_IMODE(db_path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_an_in_memory_database_needs_no_filesystem_permissions() -> None:
    # Must not raise: ":memory:" has no path to chmod.
    BridgeDatabase(":memory:")
