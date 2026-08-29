from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from src.core.models import SlotStatus
from src.database.connection import Database
from src.database.monitor_state import (
    claim_human_action_incident_notification,
    clear_human_action_incident,
    get_monitor_state,
    get_slot_state,
    record_check_attempt,
    record_verified_state,
    set_human_action_incident,
)
from src.database.schema import init_schema


class MonitorStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._tmp.name) / "state.db"))
        await self.db.connect()
        await init_schema(self.db.connection)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_failed_attempt_preserves_last_verified_state(self) -> None:
        verified_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        attempted_at = datetime(2026, 8, 29, 12, 5, tzinfo=timezone.utc)
        await record_verified_state(
            self.db.connection,
            city_key="warsaw",
            slot_state=SlotStatus.NO_SLOTS,
            verified_at=verified_at,
            details="occupied banner",
        )
        await record_check_attempt(
            self.db.connection,
            city_key="warsaw",
            attempted_at=attempted_at,
            details="CDP endpoint unavailable",
            error="cdp_unavailable",
        )

        state = await get_monitor_state(self.db.connection, "warsaw")

        assert state is not None
        self.assertEqual(state.verified_state, SlotStatus.NO_SLOTS)
        self.assertEqual(state.last_verified_at, verified_at)
        self.assertEqual(state.last_attempt_at, attempted_at)
        self.assertEqual(state.last_error, "cdp_unavailable")

    async def test_legacy_reader_does_not_treat_unknown_as_notified_state(self) -> None:
        attempted_at = datetime(2026, 8, 29, 12, 5, tzinfo=timezone.utc)
        await record_check_attempt(
            self.db.connection,
            city_key="warsaw",
            attempted_at=attempted_at,
            details="CDP endpoint unavailable",
            error="cdp_unavailable",
        )

        state, checked_at, _, error = await get_slot_state(
            self.db.connection, "warsaw"
        )

        self.assertIsNone(state)
        self.assertEqual(checked_at, attempted_at)
        self.assertEqual(error, "cdp_unavailable")

    async def test_verified_state_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            await record_verified_state(
                self.db.connection,
                city_key="warsaw",
                slot_state=SlotStatus.UNKNOWN,
                verified_at=datetime.now(timezone.utc),
                details="",
            )

    async def test_incident_notification_is_claimed_once_per_key(self) -> None:
        first_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        claimed_at = datetime(2026, 8, 29, 12, 1, tzinfo=timezone.utc)
        is_new = await set_human_action_incident(
            self.db.connection,
            city_key="warsaw",
            incident_key="target_tab_missing",
            attempted_at=first_at,
            details="Open the target tab",
            error="target_tab_missing",
        )
        self.assertTrue(is_new)
        self.assertTrue(
            await claim_human_action_incident_notification(
                self.db.connection,
                city_key="warsaw",
                incident_key="target_tab_missing",
                notified_at=claimed_at,
            )
        )
        self.assertFalse(
            await claim_human_action_incident_notification(
                self.db.connection,
                city_key="warsaw",
                incident_key="target_tab_missing",
                notified_at=claimed_at,
            )
        )

        repeated = await set_human_action_incident(
            self.db.connection,
            city_key="warsaw",
            incident_key="target_tab_missing",
            attempted_at=claimed_at,
            details="Still missing",
            error="target_tab_missing",
        )
        self.assertFalse(repeated)
        state = await get_monitor_state(self.db.connection, "warsaw")
        assert state is not None
        self.assertEqual(state.human_action_incident_notified_at, claimed_at)

        changed = await set_human_action_incident(
            self.db.connection,
            city_key="warsaw",
            incident_key="cloudflare_challenge",
            attempted_at=claimed_at,
            details="Solve the challenge",
            error="cloudflare_challenge",
        )
        self.assertTrue(changed)
        state = await get_monitor_state(self.db.connection, "warsaw")
        assert state is not None
        self.assertIsNone(state.human_action_incident_notified_at)

        await clear_human_action_incident(self.db.connection, city_key="warsaw")
        state = await get_monitor_state(self.db.connection, "warsaw")
        assert state is not None
        self.assertIsNone(state.human_action_incident_key)


class LegacyMonitorStateMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_row_is_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            connection = await aiosqlite.connect(path)
            connection.row_factory = aiosqlite.Row
            try:
                await connection.execute(
                    """
                    CREATE TABLE monitor_state (
                        city_key TEXT PRIMARY KEY,
                        slot_state TEXT NOT NULL,
                        last_check_at TEXT,
                        last_details TEXT,
                        last_error TEXT
                    )
                    """
                )
                checked_at = datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc)
                await connection.execute(
                    """
                    INSERT INTO monitor_state (
                        city_key, slot_state, last_check_at, last_details, last_error
                    )
                    VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        "warsaw",
                        SlotStatus.NO_SLOTS.value,
                        checked_at.isoformat(),
                        "occupied banner",
                    ),
                )
                await connection.commit()

                await init_schema(connection)
                state = await get_monitor_state(connection, "warsaw")

                assert state is not None
                self.assertEqual(state.verified_state, SlotStatus.NO_SLOTS)
                self.assertEqual(state.last_verified_at, checked_at)
                self.assertEqual(state.last_attempt_at, checked_at)
            finally:
                await connection.close()

    async def test_partial_migration_is_repaired_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.db"
            connection = await aiosqlite.connect(path)
            connection.row_factory = aiosqlite.Row
            try:
                await connection.execute(
                    """
                    CREATE TABLE monitor_state (
                        city_key TEXT PRIMARY KEY,
                        slot_state TEXT NOT NULL,
                        last_check_at TEXT,
                        last_details TEXT,
                        last_error TEXT,
                        last_verified_state TEXT NOT NULL DEFAULT 'UNKNOWN',
                        last_verified_at TEXT,
                        last_attempt_at TEXT,
                        human_action_incident_key TEXT,
                        human_action_incident_notified_at TEXT
                    )
                    """
                )
                checked_at = datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc)
                await connection.execute(
                    """
                    INSERT INTO monitor_state (
                        city_key, slot_state, last_check_at, last_details, last_error
                    )
                    VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        "warsaw",
                        SlotStatus.FREE_SLOTS_AVAILABLE.value,
                        checked_at.isoformat(),
                        "booking form",
                    ),
                )
                await connection.commit()

                await init_schema(connection)
                state = await get_monitor_state(connection, "warsaw")

                assert state is not None
                self.assertEqual(
                    state.verified_state, SlotStatus.FREE_SLOTS_AVAILABLE
                )
                self.assertEqual(state.last_verified_at, checked_at)
                self.assertEqual(state.last_attempt_at, checked_at)
            finally:
                await connection.close()


if __name__ == "__main__":
    unittest.main()
