import unittest

from broadcaster.api import TelegramAPIError
from broadcaster.app import BroadcasterApp
from broadcaster.database import Database
from broadcaster.settings import Settings


class FakeAPI:
    def __init__(self) -> None:
        self.sent = []
        self.copied = []
        self.callbacks = []
        self.members = {}
        self.copy_failures = {}
        self.next_message_id = 1000

    def get_me(self):
        return {"id": 999, "username": "gonkasupport_bot"}

    def send_message(
        self,
        chat_id,
        text,
        *,
        message_thread_id=None,
        reply_markup=None,
        disable_notification=False,
    ):
        self.next_message_id += 1
        value = {
            "chat_id": chat_id,
            "text": text,
            "thread_id": message_thread_id,
            "reply_markup": reply_markup,
            "disable_notification": disable_notification,
            "message_id": self.next_message_id,
        }
        self.sent.append(value)
        return value

    def copy_message(
        self,
        chat_id,
        from_chat_id,
        message_id,
        *,
        message_thread_id=None,
        disable_notification=False,
    ):
        failures = self.copy_failures.get(chat_id, [])
        if failures:
            raise failures.pop(0)
        self.next_message_id += 1
        value = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "source_message_id": message_id,
            "thread_id": message_thread_id,
            "disable_notification": disable_notification,
            "message_id": self.next_message_id,
        }
        self.copied.append(value)
        return value

    def answer_callback_query(self, callback_query_id, text="", *, show_alert=False):
        self.callbacks.append((callback_query_id, text, show_alert))
        return True

    def get_chat_member(self, chat_id, user_id):
        return self.members.get((chat_id, user_id), {"status": "member"})


def private_message(user_id, message_id, text=None):
    message = {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": user_id, "type": "private"},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": message_id, "message": message}


def group_message(user_id, chat_id, message_id, text, *, thread_id=None):
    message = {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
        "text": text,
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    return {"update_id": message_id, "message": message}


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.api = FakeAPI()
        self.settings = Settings(
            token="test",
            admin_user_ids=frozenset({1}),
            send_delay_seconds=0,
        )
        self.app = BroadcasterApp(self.api, self.db, self.settings, sleeper=lambda _: None)
        self.app.bot_user_id = 999
        self.app.bot_username = "gonkasupport_bot"

    def tearDown(self) -> None:
        self.db.close()

    def register(self, alias, chat_id, thread_id=None):
        return self.db.register_destination(
            alias=alias,
            chat_id=chat_id,
            thread_id=thread_id,
            chat_title=alias,
            registered_by=1,
        )

    def test_whoami_works_without_admin_access(self) -> None:
        self.app.process_update(private_message(55, 1, "/whoami"))
        self.assertIn("55", self.api.sent[-1]["text"])

    def test_unauthorized_user_cannot_start_campaign(self) -> None:
        self.register("one", -1001)
        self.app.process_update(private_message(55, 1, "/broadcast"))
        self.assertIn("Доступ запрещён", self.api.sent[-1]["text"])
        self.assertIsNone(self.db.get_open_campaign(55))

    def test_group_registration_requires_both_admin_checks(self) -> None:
        self.api.members[(-1001, 1)] = {"status": "administrator"}
        self.api.members[(-1001, 999)] = {"status": "member"}
        self.app.process_update(group_message(1, -1001, 1, "/register validators"))
        destinations = self.db.list_destinations()
        self.assertEqual(len(destinations), 1)
        self.assertEqual(destinations[0]["alias"], "validators")

    def test_topic_registration_preserves_thread_id(self) -> None:
        self.api.members[(-1001, 1)] = {"status": "creator"}
        self.api.members[(-1001, 999)] = {"status": "administrator"}
        self.app.process_update(
            group_message(1, -1001, 1, "/register alerts", thread_id=42)
        )
        destination = self.db.list_destinations()[0]
        self.assertEqual(destination["thread_id"], 42)
        self.assertEqual(self.api.sent[-1]["thread_id"], 42)

    def test_campaign_sends_once_after_confirmation(self) -> None:
        self.register("one", -1001)
        self.register("two", -1002, thread_id=88)

        self.app.process_update(private_message(1, 1, "/broadcast"))
        campaign = self.db.get_open_campaign(1)
        self.app.process_update(private_message(1, 2, "Hello groups"))
        self.assertEqual(self.db.get_campaign(campaign["id"])["status"], "ready")

        callback = {
            "update_id": 3,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 1},
                "data": f"send:{campaign['id']}",
                "message": {"chat": {"id": 1, "type": "private"}},
            },
        }
        self.app.process_update(callback)
        target_copies = [item for item in self.api.copied if item["chat_id"] < 0]
        self.assertEqual(len(target_copies), 2)
        self.assertEqual(target_copies[1]["thread_id"], 88)
        self.assertEqual(self.db.delivery_summary(campaign["id"])["sent"], 2)

        self.app.process_update(callback)
        target_copies_again = [item for item in self.api.copied if item["chat_id"] < 0]
        self.assertEqual(len(target_copies_again), 2)

    def test_selected_alias_and_silent_mode(self) -> None:
        self.register("one", -1001)
        self.register("two", -1002)
        self.app.process_update(private_message(1, 1, "/broadcast_silent two"))
        campaign = self.db.get_open_campaign(1)
        self.app.process_update(private_message(1, 2, "Silent"))
        self.app.process_update(
            {
                "update_id": 3,
                "callback_query": {
                    "id": "callback-2",
                    "from": {"id": 1},
                    "data": f"send:{campaign['id']}",
                    "message": {"chat": {"id": 1, "type": "private"}},
                },
            }
        )
        target_copies = [item for item in self.api.copied if item["chat_id"] < 0]
        self.assertEqual([item["chat_id"] for item in target_copies], [-1002])
        self.assertTrue(target_copies[0]["disable_notification"])

    def test_single_destination_failure_does_not_break_others(self) -> None:
        self.register("one", -1001)
        self.register("two", -1002)
        self.api.copy_failures[-1001] = [TelegramAPIError(403, "bot was kicked")]
        self.app.process_update(private_message(1, 1, "/broadcast"))
        campaign = self.db.get_open_campaign(1)
        self.app.process_update(private_message(1, 2, "Hello"))
        self.app.process_update(
            {
                "update_id": 3,
                "callback_query": {
                    "id": "callback-3",
                    "from": {"id": 1},
                    "data": f"send:{campaign['id']}",
                    "message": {"chat": {"id": 1, "type": "private"}},
                },
            }
        )
        summary = self.db.delivery_summary(campaign["id"])
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["sent"], 1)
        self.assertIn("bot was kicked", self.api.sent[-1]["text"])

    def test_removal_deactivates_all_chat_topics(self) -> None:
        self.register("one", -1001, 10)
        self.register("two", -1001, 20)
        self.app.process_update(
            {
                "update_id": 3,
                "my_chat_member": {
                    "chat": {"id": -1001},
                    "new_chat_member": {"status": "kicked"},
                },
            }
        )
        self.assertEqual(self.db.list_destinations(), [])


if __name__ == "__main__":
    unittest.main()

