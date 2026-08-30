from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "data" / "monitor.db"


def _resolve_database_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()


def _parse_database_path() -> Path:
    parser = argparse.ArgumentParser(
        description=(
            "Clear monitor latch/cooldown state and pending outbox rows. "
            "Preserves the subscribers table."
        )
    )
    parser.add_argument(
        "database_path",
        nargs="?",
        default=str(_DEFAULT_DB),
        help="SQLite path (default: <repo>/data/monitor.db)",
    )
    return _resolve_database_path(parser.parse_args().database_path)


def reset_monitor_state(database_path: Path) -> None:
    if not database_path.exists():
        return
    connection = sqlite3.connect(database_path)
    try:
        connection.busy_timeout = 5000
        connection.execute("PRAGMA foreign_keys = ON")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "notification_deliveries" in tables:
            connection.execute(
                "DELETE FROM notification_deliveries WHERE status = 'PENDING'"
            )
        if "monitor_state" in tables:
            connection.execute("DELETE FROM monitor_state")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    reset_monitor_state(_parse_database_path())


if __name__ == "__main__":
    main()
