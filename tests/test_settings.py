import unittest

from broadcaster.settings import Settings, parse_admin_ids


class SettingsTests(unittest.TestCase):
    def test_parse_admin_ids(self) -> None:
        self.assertEqual(parse_admin_ids("123, 456,123"), frozenset({123, 456}))

    def test_empty_admin_list_is_allowed_for_bootstrap(self) -> None:
        settings = Settings.from_env({"TELEGRAM_BOT_TOKEN": "test-token"})
        self.assertEqual(settings.admin_user_ids, frozenset())

    def test_token_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "TELEGRAM_BOT_TOKEN"):
            Settings.from_env({})

    def test_invalid_admin_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "numeric"):
            parse_admin_ids("@dmitrii")


if __name__ == "__main__":
    unittest.main()

