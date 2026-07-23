from __future__ import annotations

import logging

from .api import TelegramAPI
from .app import BroadcasterApp
from .database import Database
from .settings import Settings


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database = Database(settings.database_path)
    api = TelegramAPI(
        settings.token,
        request_timeout=max(45, settings.poll_timeout_seconds + 10),
    )
    try:
        BroadcasterApp(api, database, settings).run_forever()
    finally:
        database.close()


if __name__ == "__main__":
    main()

