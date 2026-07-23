from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias TEXT NOT NULL COLLATE NOCASE UNIQUE,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                chat_title TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                registered_by INTEGER NOT NULL,
                registered_at TEXT NOT NULL,
                last_verified_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS destinations_location_unique
            ON destinations(chat_id, COALESCE(thread_id, -1));

            CREATE TABLE IF NOT EXISTS campaigns (
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

            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
                destination_id INTEGER NOT NULL REFERENCES destinations(id),
                status TEXT NOT NULL,
                telegram_message_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                error_code INTEGER,
                error_summary TEXT,
                sent_at TEXT,
                UNIQUE(campaign_id, destination_id)
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def register_destination(
        self,
        *,
        alias: str,
        chat_id: int,
        thread_id: int | None,
        chat_title: str,
        registered_by: int,
    ) -> dict[str, Any]:
        now = iso_time()
        existing = self.connection.execute(
            """
            SELECT * FROM destinations
            WHERE chat_id = ? AND COALESCE(thread_id, -1) = COALESCE(?, -1)
            """,
            (chat_id, thread_id),
        ).fetchone()
        try:
            if existing:
                self.connection.execute(
                    """
                    UPDATE destinations
                    SET alias = ?, chat_title = ?, active = 1,
                        registered_by = ?, registered_at = ?, last_verified_at = ?
                    WHERE id = ?
                    """,
                    (alias, chat_title, registered_by, now, now, existing["id"]),
                )
                destination_id = int(existing["id"])
            else:
                cursor = self.connection.execute(
                    """
                    INSERT INTO destinations
                        (alias, chat_id, thread_id, chat_title, active,
                         registered_by, registered_at, last_verified_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (alias, chat_id, thread_id, chat_title, registered_by, now, now),
                )
                destination_id = int(cursor.lastrowid)
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise ValueError(f"Alias '{alias}' is already used by another destination") from exc
        return self.get_destination(destination_id)  # type: ignore[return-value]

    def get_destination(self, destination_id: int) -> dict[str, Any] | None:
        return self._row(
            self.connection.execute(
                "SELECT * FROM destinations WHERE id = ?", (destination_id,)
            ).fetchone()
        )

    def list_destinations(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE active = 1" if active_only else ""
        rows = self.connection.execute(
            f"SELECT * FROM destinations {where} ORDER BY alias COLLATE NOCASE"  # noqa: S608
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_destinations(self, aliases: Iterable[str] | None) -> tuple[list[dict[str, Any]], list[str]]:
        active = self.list_destinations(active_only=True)
        if aliases is None:
            return active, []
        by_alias = {row["alias"].casefold(): row for row in active}
        resolved: list[dict[str, Any]] = []
        missing: list[str] = []
        seen: set[int] = set()
        for alias in aliases:
            row = by_alias.get(alias.casefold())
            if row is None:
                missing.append(alias)
            elif int(row["id"]) not in seen:
                resolved.append(row)
                seen.add(int(row["id"]))
        return resolved, missing

    def deactivate_destination(self, *, chat_id: int, thread_id: int | None) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE destinations SET active = 0
            WHERE chat_id = ? AND COALESCE(thread_id, -1) = COALESCE(?, -1)
            """,
            (chat_id, thread_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def deactivate_chat(self, chat_id: int) -> int:
        cursor = self.connection.execute(
            "UPDATE destinations SET active = 0 WHERE chat_id = ?", (chat_id,)
        )
        self.connection.commit()
        return cursor.rowcount

    def migrate_destination_chat(self, destination_id: int, new_chat_id: int) -> None:
        self.connection.execute(
            "UPDATE destinations SET chat_id = ?, last_verified_at = ? WHERE id = ?",
            (new_chat_id, iso_time(), destination_id),
        )
        self.connection.commit()

    def create_campaign(
        self,
        *,
        created_by: int,
        target_ids: list[int],
        silent: bool,
        ttl_minutes: int,
    ) -> dict[str, Any]:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE campaigns SET status = 'cancelled', completed_at = ?
            WHERE created_by = ? AND status IN ('awaiting_content', 'ready')
            """,
            (iso_time(now), created_by),
        )
        cursor = self.connection.execute(
            """
            INSERT INTO campaigns
                (created_by, target_ids, status, silent, created_at, expires_at)
            VALUES (?, ?, 'awaiting_content', ?, ?, ?)
            """,
            (
                created_by,
                json.dumps(target_ids),
                int(silent),
                iso_time(now),
                iso_time(now + timedelta(minutes=ttl_minutes)),
            ),
        )
        self.connection.commit()
        return self.get_campaign(int(cursor.lastrowid))  # type: ignore[return-value]

    def get_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        row = self._row(
            self.connection.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        )
        if row is not None:
            row["target_ids"] = json.loads(row["target_ids"])
        return row

    def get_open_campaign(self, created_by: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM campaigns
            WHERE created_by = ? AND status IN ('awaiting_content', 'ready')
            ORDER BY id DESC LIMIT 1
            """,
            (created_by,),
        ).fetchone()
        result = self._row(row)
        if result is not None:
            result["target_ids"] = json.loads(result["target_ids"])
        return result

    def set_campaign_content(self, campaign_id: int, source_chat_id: int, source_message_id: int) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE campaigns
            SET source_chat_id = ?, source_message_id = ?, status = 'ready'
            WHERE id = ? AND status = 'awaiting_content'
            """,
            (source_chat_id, source_message_id, campaign_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def transition_to_sending(self, campaign_id: int, created_by: int) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE campaigns SET status = 'sending', confirmed_at = ?
            WHERE id = ? AND created_by = ? AND status = 'ready' AND expires_at >= ?
            """,
            (iso_time(), campaign_id, created_by, iso_time()),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def cancel_campaign(self, campaign_id: int, created_by: int) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE campaigns SET status = 'cancelled', completed_at = ?
            WHERE id = ? AND created_by = ? AND status IN ('awaiting_content', 'ready')
            """,
            (iso_time(), campaign_id, created_by),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def cancel_open_campaigns(self, created_by: int) -> int:
        cursor = self.connection.execute(
            """
            UPDATE campaigns SET status = 'cancelled', completed_at = ?
            WHERE created_by = ? AND status IN ('awaiting_content', 'ready')
            """,
            (iso_time(), created_by),
        )
        self.connection.commit()
        return cursor.rowcount

    def finish_campaign(self, campaign_id: int, status: str = "completed") -> None:
        self.connection.execute(
            "UPDATE campaigns SET status = ?, completed_at = ? WHERE id = ? AND status = 'sending'",
            (status, iso_time(), campaign_id),
        )
        self.connection.commit()

    def recover_interrupted_campaigns(self) -> int:
        cursor = self.connection.execute(
            """
            UPDATE campaigns SET status = 'interrupted', completed_at = ?
            WHERE status = 'sending'
            """,
            (iso_time(),),
        )
        self.connection.commit()
        return cursor.rowcount

    def record_delivery(
        self,
        *,
        campaign_id: int,
        destination_id: int,
        status: str,
        attempts: int,
        telegram_message_id: int | None = None,
        error_code: int | None = None,
        error_summary: str | None = None,
    ) -> None:
        sent_at = iso_time() if status == "sent" else None
        self.connection.execute(
            """
            INSERT INTO deliveries
                (campaign_id, destination_id, status, telegram_message_id,
                 attempts, error_code, error_summary, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign_id, destination_id) DO UPDATE SET
                status = excluded.status,
                telegram_message_id = excluded.telegram_message_id,
                attempts = excluded.attempts,
                error_code = excluded.error_code,
                error_summary = excluded.error_summary,
                sent_at = excluded.sent_at
            """,
            (
                campaign_id,
                destination_id,
                status,
                telegram_message_id,
                attempts,
                error_code,
                error_summary,
                sent_at,
            ),
        )
        self.connection.commit()

    def delivery_summary(self, campaign_id: int) -> dict[str, int]:
        summary = {"sent": 0, "failed": 0, "skipped": 0}
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM deliveries WHERE campaign_id = ? GROUP BY status",
            (campaign_id,),
        ).fetchall()
        for row in rows:
            summary[str(row["status"])] = int(row["count"])
        return summary

    def failed_deliveries(self, campaign_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT destinations.alias, deliveries.error_summary
            FROM deliveries JOIN destinations ON destinations.id = deliveries.destination_id
            WHERE deliveries.campaign_id = ? AND deliveries.status = 'failed'
            ORDER BY destinations.alias COLLATE NOCASE
            """,
            (campaign_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_campaigns(self, created_by: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT campaigns.*,
                   SUM(CASE WHEN deliveries.status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
                   SUM(CASE WHEN deliveries.status = 'failed' THEN 1 ELSE 0 END) AS failed_count
            FROM campaigns
            LEFT JOIN deliveries ON deliveries.campaign_id = campaigns.id
            WHERE campaigns.created_by = ?
            GROUP BY campaigns.id
            ORDER BY campaigns.id DESC LIMIT ?
            """,
            (created_by, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_state_int(self, key: str, default: int = 0) -> int:
        row = self.connection.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return default if row is None else int(row["value"])

    def set_state_int(self, key: str, value: int) -> None:
        self.connection.execute(
            """
            INSERT INTO bot_state(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
        self.connection.commit()

