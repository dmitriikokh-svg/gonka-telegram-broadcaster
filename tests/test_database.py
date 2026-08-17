import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from broadcaster.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")

    def tearDown(self) -> None:
        self.db.close()

    def test_register_and_reactivate_destination(
        self,
    ) -> None:
        first = self.db.register_destination(
            alias="group-one",
            chat_id=-1001,
            thread_id=None,
            chat_title="Group One",
            registered_by=7,
        )
        self.assertTrue(first["active"])

        self.assertTrue(
            self.db.deactivate_destination(
                chat_id=-1001,
                thread_id=None,
            )
        )

        second = self.db.register_destination(
            alias="group-renamed",
            chat_id=-1001,
            thread_id=None,
            chat_title="Renamed",
            registered_by=7,
        )
        self.assertEqual(
            first["id"],
            second["id"],
        )
        self.assertEqual(
            second["alias"],
            "group-renamed",
        )
        self.assertTrue(second["active"])

    def test_same_group_can_have_multiple_topics(
        self,
    ) -> None:
        one = self.db.register_destination(
            alias="topic-one",
            chat_id=-1001,
            thread_id=10,
            chat_title="Forum",
            registered_by=7,
        )
        two = self.db.register_destination(
            alias="topic-two",
            chat_id=-1001,
            thread_id=20,
            chat_title="Forum",
            registered_by=7,
        )
        self.assertNotEqual(
            one["id"],
            two["id"],
        )

    def test_alias_must_be_unique(self) -> None:
        self.db.register_destination(
            alias="same",
            chat_id=-1001,
            thread_id=None,
            chat_title="One",
            registered_by=7,
        )

        with self.assertRaisesRegex(
            ValueError,
            "already used",
        ):
            self.db.register_destination(
                alias="SAME",
                chat_id=-1002,
                thread_id=None,
                chat_title="Two",
                registered_by=7,
            )

    def test_campaign_confirmation_is_atomic(
        self,
    ) -> None:
        destination = self.db.register_destination(
            alias="group",
            chat_id=-1001,
            thread_id=None,
            chat_title="Group",
            registered_by=7,
        )
        campaign = self.db.create_campaign(
            created_by=7,
            target_ids=[destination["id"]],
            silent=False,
            ttl_minutes=15,
        )
        self.assertTrue(
            self.db.set_campaign_content(
                campaign["id"],
                7,
                11,
            )
        )
        self.assertTrue(
            self.db.transition_to_sending(
                campaign["id"],
                7,
            )
        )
        self.assertFalse(
            self.db.transition_to_sending(
                campaign["id"],
                7,
            )
        )

    def test_interrupted_campaign_is_not_restarted(
        self,
    ) -> None:
        campaign = self.db.create_campaign(
            created_by=7,
            target_ids=[],
            silent=False,
            ttl_minutes=15,
        )
        self.db.set_campaign_content(
            campaign["id"],
            7,
            11,
        )
        self.db.transition_to_sending(
            campaign["id"],
            7,
        )

        self.assertEqual(
            self.db.recover_interrupted_campaigns(),
            1,
        )
        self.assertEqual(
            self.db.get_campaign(
                campaign["id"]
            )["status"],
            "interrupted",
        )

    def test_scheduled_campaign_is_claimed_atomically_when_due(
        self,
    ) -> None:
        now = datetime(
            2026,
            8,
            17,
            10,
            0,
            tzinfo=timezone.utc,
        )
        campaign = self.db.create_campaign(
            created_by=7,
            target_ids=[],
            silent=False,
            ttl_minutes=15,
            now=now,
        )
        self.db.set_campaign_content(
            campaign["id"],
            7,
            11,
        )

        self.assertTrue(
            self.db.transition_to_awaiting_schedule(
                campaign["id"],
                7,
                now=now,
            )
        )

        due_at = now + timedelta(hours=1)

        self.assertTrue(
            self.db.schedule_campaign(
                campaign["id"],
                7,
                due_at,
                now=now,
            )
        )
        self.assertEqual(
            self.db.due_scheduled_campaigns(
                now=(
                    due_at
                    - timedelta(seconds=1)
                )
            ),
            [],
        )

        due = self.db.due_scheduled_campaigns(
            now=due_at
        )
        self.assertEqual(
            [row["id"] for row in due],
            [campaign["id"]],
        )
        self.assertTrue(
            self.db.claim_due_campaign(
                campaign["id"],
                now=due_at,
            )
        )
        self.assertFalse(
            self.db.claim_due_campaign(
                campaign["id"],
                now=due_at,
            )
        )

    def test_existing_database_is_migrated_without_losing_campaigns(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            path = (
                Path(directory)
                / "broadcaster.sqlite3"
            )
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_by INTEGER NOT NULL,
                    source_chat_id INTEGER,
                    source_message_id INTEGER,
                    target_ids TEXT NOT NULL,
                    status TEXT NOT NULL,
                    silent INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    completed_at TEXT
                );

                INSERT INTO campaigns (
                    created_by,
                    target_ids,
                    status,
                    silent,
                    created_at,
                    expires_at
                ) VALUES (
                    7,
                    '[]',
                    'ready',
                    0,
                    '2026-08-17T10:00:00+00:00',
                    '2026-08-17T10:15:00+00:00'
                );
                """
            )
            connection.close()

            migrated = Database(str(path))
            try:
                campaign = (
                    migrated.get_campaign(1)
                )
                columns = {
                    row["name"]
                    for row
                    in migrated.connection.execute(
                        "PRAGMA table_info(campaigns)"
                    ).fetchall()
                }

                self.assertIn(
                    "scheduled_at",
                    columns,
                )
                self.assertEqual(
                    campaign["status"],
                    "ready",
                )
                self.assertIsNone(
                    campaign["scheduled_at"]
                )
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()
