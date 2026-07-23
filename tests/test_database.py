import unittest

from broadcaster.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")

    def tearDown(self) -> None:
        self.db.close()

    def test_register_and_reactivate_destination(self) -> None:
        first = self.db.register_destination(
            alias="group-one",
            chat_id=-1001,
            thread_id=None,
            chat_title="Group One",
            registered_by=7,
        )
        self.assertTrue(first["active"])

        self.assertTrue(self.db.deactivate_destination(chat_id=-1001, thread_id=None))
        second = self.db.register_destination(
            alias="group-renamed",
            chat_id=-1001,
            thread_id=None,
            chat_title="Renamed",
            registered_by=7,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["alias"], "group-renamed")
        self.assertTrue(second["active"])

    def test_same_group_can_have_multiple_topics(self) -> None:
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
        self.assertNotEqual(one["id"], two["id"])

    def test_alias_must_be_unique(self) -> None:
        self.db.register_destination(
            alias="same",
            chat_id=-1001,
            thread_id=None,
            chat_title="One",
            registered_by=7,
        )
        with self.assertRaisesRegex(ValueError, "already used"):
            self.db.register_destination(
                alias="SAME",
                chat_id=-1002,
                thread_id=None,
                chat_title="Two",
                registered_by=7,
            )

    def test_campaign_confirmation_is_atomic(self) -> None:
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
        self.assertTrue(self.db.set_campaign_content(campaign["id"], 7, 11))
        self.assertTrue(self.db.transition_to_sending(campaign["id"], 7))
        self.assertFalse(self.db.transition_to_sending(campaign["id"], 7))

    def test_interrupted_campaign_is_not_restarted(self) -> None:
        campaign = self.db.create_campaign(
            created_by=7,
            target_ids=[],
            silent=False,
            ttl_minutes=15,
        )
        self.db.set_campaign_content(campaign["id"], 7, 11)
        self.db.transition_to_sending(campaign["id"], 7)
        self.assertEqual(self.db.recover_interrupted_campaigns(), 1)
        self.assertEqual(self.db.get_campaign(campaign["id"])["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()

