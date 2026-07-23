from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def parse_admin_ids(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise ValueError("ADMIN_USER_IDS must contain numeric Telegram user IDs") from exc
        if value <= 0:
            raise ValueError("ADMIN_USER_IDS must contain positive Telegram user IDs")
        values.add(value)
    return frozenset(values)


def _positive_float(raw: str, name: str, *, allow_zero: bool = False) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")
    return value


@dataclass(frozen=True)
class Settings:
    token: str
    admin_user_ids: frozenset[int]
    database_path: str = "./data/broadcaster.sqlite3"
    log_level: str = "INFO"
    poll_timeout_seconds: int = 30
    send_delay_seconds: float = 0.2
    draft_ttl_minutes: int = 15

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        poll_timeout = int(
            _positive_float(source.get("POLL_TIMEOUT_SECONDS", "30"), "POLL_TIMEOUT_SECONDS")
        )
        draft_ttl = int(
            _positive_float(source.get("DRAFT_TTL_MINUTES", "15"), "DRAFT_TTL_MINUTES")
        )
        send_delay = _positive_float(
            source.get("SEND_DELAY_SECONDS", "0.2"),
            "SEND_DELAY_SECONDS",
            allow_zero=True,
        )

        return cls(
            token=token,
            admin_user_ids=parse_admin_ids(source.get("ADMIN_USER_IDS", "")),
            database_path=source.get("DATABASE_PATH", "./data/broadcaster.sqlite3").strip()
            or "./data/broadcaster.sqlite3",
            log_level=source.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            poll_timeout_seconds=poll_timeout,
            send_delay_seconds=send_delay,
            draft_ttl_minutes=draft_ttl,
        )

