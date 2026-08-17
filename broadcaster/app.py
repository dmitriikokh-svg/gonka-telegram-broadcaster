from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .api import TelegramAPIError
from .database import Database, utc_now
from .settings import Settings


LOGGER = logging.getLogger(__name__)
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")
ADMIN_STATUSES = {"administrator", "creator"}
SELECTION_PAGE_SIZE = 20

HELP_TEXT = """Gonka Support Broadcaster

/whoami — show your numeric Telegram user ID
/broadcast — broadcast to all active destinations
/broadcast alias1 alias2 — broadcast to selected destinations
/broadcast_silent — broadcast without a notification sound
/broadcast_select — choose destinations with buttons
/broadcast_select_silent — choose destinations for a silent broadcast
/groups — list active destinations
/history — show recent broadcasts
/cancel — cancel the current draft or time entry
/cancel ID — cancel a scheduled broadcast

In a group or topic:
/register alias — register a destination
/unregister — deactivate a destination

Only an authorized operator can start a broadcast in a private chat."""


class BroadcasterApp:
    def __init__(
        self,
        api: Any,
        database: Database,
        settings: Settings,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.api = api
        self.db = database
        self.settings = settings
        self.sleeper = sleeper
        self.clock = clock
        self.schedule_timezone = (
            timezone.utc
            if self.settings.schedule_timezone == "UTC"
            else ZoneInfo(self.settings.schedule_timezone)
        )
        self.bot_user_id: int | None = None
        self.bot_username = ""

    def initialize(self) -> None:
        me = self.api.get_me()
        self.bot_user_id = int(me["id"])
        self.bot_username = str(me.get("username", ""))
        interrupted = self.db.recover_interrupted_campaigns()
        if interrupted:
            LOGGER.warning(
                "Marked %s interrupted campaign(s); no automatic resend",
                interrupted,
            )
        LOGGER.info("Started @%s (id=%s)", self.bot_username, self.bot_user_id)

    def run_forever(self) -> None:
        self.initialize()
        offset = self.db.get_state_int("update_offset", 0)
        while True:
            try:
                self._run_due_campaigns()
                updates = self.api.get_updates(
                    offset=offset,
                    timeout=self.settings.poll_timeout_seconds,
                )
                for update in updates:
                    update_id = int(update["update_id"])
                    try:
                        self.process_update(update)
                    except Exception:
                        LOGGER.exception(
                            "Failed to process Telegram update %s",
                            update_id,
                        )
                    offset = max(offset, update_id + 1)
                    self.db.set_state_int("update_offset", offset)
                self._run_due_campaigns()
            except TelegramAPIError as exc:
                LOGGER.warning(
                    "Telegram polling error %s: %s",
                    exc.error_code,
                    exc.description,
                )
                self.sleeper(3)
            except KeyboardInterrupt:
                LOGGER.info("Stopped")
                return

    def process_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self._handle_message(update["message"])
        elif "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "my_chat_member" in update:
            self._handle_membership(update["my_chat_member"])

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_user_ids

    @staticmethod
    def _parse_command(text: str) -> tuple[str, list[str]]:
        parts = text.strip().split()
        if not parts or not parts[0].startswith("/"):
            return "", []
        command = parts[0][1:].split("@", 1)[0].casefold()
        args: list[str] = []
        for raw in parts[1:]:
            args.extend(item for item in raw.split(",") if item)
        return command, args

    def _handle_message(self, message: dict[str, Any]) -> None:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        if "id" not in sender or "id" not in chat:
            return

        user_id = int(sender["id"])
        chat_id = int(chat["id"])
        chat_type = str(chat.get("type", ""))
        text = str(message.get("text", ""))
        command, args = self._parse_command(text)

        if chat_type != "private":
            if command == "register":
                self._register_destination(message, args)
            elif command == "unregister":
                self._unregister_destination(message)
            elif command == "whoami":
                self._send(
                    chat_id,
                    "For security, use /whoami in a private chat with the bot.",
                )
            return

        if command in {"start", "help"}:
            self._send(chat_id, HELP_TEXT)
            return

        if command == "whoami":
            self._send(chat_id, f"Your Telegram user ID: {user_id}")
            return

        if not self._is_admin(user_id):
            if command or text:
                self._send(
                    chat_id,
                    "Access denied. Send /whoami and add this ID "
                    "to ADMIN_USER_IDS.",
                )
            return

        if command in {"broadcast", "broadcast_silent"}:
            self._start_campaign(
                chat_id,
                user_id,
                args,
                silent=command == "broadcast_silent",
            )
        elif command in {"broadcast_select", "broadcast_select_silent"}:
            self._start_selection_campaign(
                chat_id,
                user_id,
                silent=command == "broadcast_select_silent",
            )
        elif command == "groups":
            self._show_groups(chat_id)
        elif command == "history":
            self._show_history(chat_id, user_id)
        elif command == "cancel":
            if args:
                if len(args) != 1 or not args[0].isdigit():
                    self._send(
                        chat_id,
                        "Usage: /cancel or /cancel BROADCAST_ID",
                    )
                else:
                    campaign_id = int(args[0])
                    changed = self.db.cancel_campaign(
                        campaign_id,
                        user_id,
                    )
                    self._send(
                        chat_id,
                        (
                            f"Broadcast #{campaign_id} canceled."
                            if changed
                            else "The broadcast was not found or can no "
                            "longer be canceled."
                        ),
                    )
            else:
                count = self.db.cancel_open_campaigns(user_id)
                self._send(
                    chat_id,
                    "Draft canceled."
                    if count
                    else "There is no active draft.",
                )
        elif command:
            self._send(chat_id, "Unknown command. Use /help.")
        else:
            self._accept_campaign_content(message, user_id)

    def _register_destination(
        self,
        message: dict[str, Any],
        args: list[str],
    ) -> None:
        sender = message["from"]
        chat = message["chat"]
        user_id = int(sender["id"])
        chat_id = int(chat["id"])
        thread_id = message.get("message_thread_id")

        if not self._is_admin(user_id):
            self._send(
                chat_id,
                "Only an authorized operator can register a destination.",
                thread_id,
            )
            return

        if len(args) != 1 or not ALIAS_RE.fullmatch(args[0]):
            self._send(
                chat_id,
                "Usage: /register alias\n"
                "Alias: 2–40 characters using A-Z, a-z, 0-9, _ or -.",
                thread_id,
            )
            return

        if self.bot_user_id is None:
            me = self.api.get_me()
            self.bot_user_id = int(me["id"])

        try:
            operator = self.api.get_chat_member(chat_id, user_id)
            bot_member = self.api.get_chat_member(
                chat_id,
                int(self.bot_user_id),
            )
        except TelegramAPIError as exc:
            self._send(
                chat_id,
                f"Could not check permissions: {exc.description}",
                thread_id,
            )
            return

        if operator.get("status") not in ADMIN_STATUSES:
            self._send(
                chat_id,
                "The operator must be an administrator of this group.",
                thread_id,
            )
            return

        bot_status = bot_member.get("status")
        bot_can_send = bot_status in {
            "member",
            "administrator",
            "creator",
        } or (
            bot_status == "restricted"
            and bool(bot_member.get("can_send_messages"))
        )

        if not bot_can_send:
            self._send(
                chat_id,
                "The bot is not allowed to send messages in this group.",
                thread_id,
            )
            return

        try:
            destination = self.db.register_destination(
                alias=args[0],
                chat_id=chat_id,
                thread_id=(
                    int(thread_id)
                    if thread_id is not None
                    else None
                ),
                chat_title=str(chat.get("title", chat_id)),
                registered_by=user_id,
            )
        except ValueError as exc:
            self._send(chat_id, str(exc), thread_id)
            return

        topic = (
            f", topic {destination['thread_id']}"
            if destination["thread_id"] is not None
            else ""
        )
        self._send(
            chat_id,
            f"Destination registered: {destination['alias']} "
            f"({destination['chat_title']}{topic}).",
            thread_id,
        )

    def _unregister_destination(
        self,
        message: dict[str, Any],
    ) -> None:
        sender = message["from"]
        chat = message["chat"]
        user_id = int(sender["id"])
        chat_id = int(chat["id"])
        thread_id = message.get("message_thread_id")

        if not self._is_admin(user_id):
            self._send(
                chat_id,
                "Only an authorized operator can deactivate "
                "a destination.",
                thread_id,
            )
            return

        try:
            member = self.api.get_chat_member(chat_id, user_id)
        except TelegramAPIError as exc:
            self._send(
                chat_id,
                f"Could not check permissions: {exc.description}",
                thread_id,
            )
            return

        if member.get("status") not in ADMIN_STATUSES:
            self._send(
                chat_id,
                "The operator must be an administrator of this group.",
                thread_id,
            )
            return

        changed = self.db.deactivate_destination(
            chat_id=chat_id,
            thread_id=(
                int(thread_id)
                if thread_id is not None
                else None
            ),
        )
        self._send(
            chat_id,
            (
                "Destination deactivated."
                if changed
                else "This destination is not registered."
            ),
            thread_id,
        )

    def _start_selection_campaign(
        self,
        chat_id: int,
        user_id: int,
        *,
        silent: bool,
    ) -> None:
        destinations = self.db.list_destinations(active_only=True)
        if not destinations:
            self._send(
                chat_id,
                "There are no active destinations. "
                "Run /register in a group first.",
            )
            return

        campaign = self.db.create_selection_campaign(
            created_by=user_id,
            silent=silent,
            ttl_minutes=self.settings.draft_ttl_minutes,
            now=self._now_utc(),
        )
        self._send(
            chat_id,
            f"Select recipients for broadcast #{campaign['id']}.\n"
            "Tap one or more groups, then tap Continue.\n"
            f"Mode: {'silent' if silent else 'standard'}",
            reply_markup=self._selection_keyboard(campaign, page=0),
        )

    def _selection_keyboard(
        self,
        campaign: dict[str, Any],
        *,
        page: int,
    ) -> dict[str, Any]:
        destinations = self.db.list_destinations(active_only=True)
        page_count = max(
            1,
            (len(destinations) + SELECTION_PAGE_SIZE - 1)
            // SELECTION_PAGE_SIZE,
        )
        page = max(0, min(page, page_count - 1))
        start = page * SELECTION_PAGE_SIZE
        visible = destinations[start : start + SELECTION_PAGE_SIZE]
        selected_ids = {int(item) for item in campaign["target_ids"]}
        campaign_id = int(campaign["id"])

        rows: list[list[dict[str, str]]] = []
        for destination in visible:
            destination_id = int(destination["id"])
            marker = "✅" if destination_id in selected_ids else "⬜"
            alias = str(destination["alias"])
            title = str(destination["chat_title"])
            max_title_length = max(0, 60 - len(marker) - len(alias) - 4)
            if len(title) > max_title_length:
                title = (
                    title[: max(0, max_title_length - 1)] + "…"
                    if max_title_length
                    else ""
                )
            label = (
                f"{marker} {title} · {alias}"
                if title
                else f"{marker} {alias}"
            )
            rows.append(
                [
                    {
                        "text": label,
                        "callback_data": (
                            f"pick:{campaign_id}:{destination_id}:{page}"
                        ),
                    }
                ]
            )

        if page_count > 1:
            navigation: list[dict[str, str]] = []
            if page > 0:
                navigation.append(
                    {
                        "text": "◀️ Previous",
                        "callback_data": f"pickpage:{campaign_id}:{page - 1}",
                    }
                )
            navigation.append(
                {
                    "text": f"{page + 1}/{page_count}",
                    "callback_data": f"pickpage:{campaign_id}:{page}",
                }
            )
            if page < page_count - 1:
                navigation.append(
                    {
                        "text": "Next ▶️",
                        "callback_data": f"pickpage:{campaign_id}:{page + 1}",
                    }
                )
            rows.append(navigation)

        rows.extend(
            [
                [
                    {
                        "text": f"➡️ Continue with {len(selected_ids)}",
                        "callback_data": f"pickdone:{campaign_id}",
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data": f"cancel:{campaign_id}",
                    }
                ],
            ]
        )
        return {"inline_keyboard": rows}

    def _refresh_selection_keyboard(
        self,
        chat_id: int,
        message_id: int,
        campaign: dict[str, Any],
        *,
        page: int,
    ) -> None:
        try:
            self.api.edit_message_reply_markup(
                chat_id,
                message_id,
                reply_markup=self._selection_keyboard(campaign, page=page),
            )
        except TelegramAPIError as exc:
            LOGGER.info(
                "Could not refresh destination picker (code=%s): %s",
                exc.error_code,
                exc.description,
            )

    def _handle_selection_callback(
        self,
        query_id: str,
        user_id: int,
        chat_id: int,
        message_id: int,
        parts: list[str],
    ) -> None:
        action = parts[0]
        try:
            campaign_id = int(parts[1])
            if action == "pick":
                destination_id = int(parts[2])
                page = int(parts[3])
            elif action == "pickpage":
                page = int(parts[2])
        except (IndexError, ValueError):
            self._answer_callback(query_id, "Invalid button", show_alert=True)
            return

        campaign = self.db.get_campaign(campaign_id)
        if (
            campaign is None
            or int(campaign["created_by"]) != user_id
            or campaign["status"] != "selecting_targets"
        ):
            self._answer_callback(
                query_id,
                "Selection has already been processed or expired",
            )
            return

        if action == "pick":
            result = self.db.toggle_campaign_target(
                campaign_id,
                user_id,
                destination_id,
                now=self._now_utc(),
            )
            if result is None:
                self._answer_callback(
                    query_id,
                    "Destination is unavailable or selection expired",
                )
                return
            selected, count = result
            campaign = self.db.get_campaign(campaign_id)
            self._answer_callback(
                query_id,
                f"{'Selected' if selected else 'Removed'} · total {count}",
            )
            if campaign is not None and message_id:
                self._refresh_selection_keyboard(
                    chat_id,
                    message_id,
                    campaign,
                    page=page,
                )
            return

        if action == "pickpage":
            self._answer_callback(query_id, "")
            if message_id:
                self._refresh_selection_keyboard(
                    chat_id,
                    message_id,
                    campaign,
                    page=page,
                )
            return

        if not campaign["target_ids"]:
            self._answer_callback(
                query_id,
                "Select at least one destination",
                show_alert=True,
            )
            return
        if not self.db.finish_target_selection(
            campaign_id,
            user_id,
            now=self._now_utc(),
        ):
            self._answer_callback(
                query_id,
                "Selection has already been processed or expired",
            )
            return

        selected_destinations = [
            self.db.get_destination(int(destination_id))
            for destination_id in campaign["target_ids"]
        ]
        labels = [
            f"{destination['chat_title']} · {destination['alias']}"
            for destination in selected_destinations
            if destination is not None
        ]
        shown = ", ".join(labels[:20])
        if len(labels) > 20:
            shown += f", and {len(labels) - 20} more"

        self._answer_callback(query_id, "Recipients selected")
        if message_id:
            try:
                self.api.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    reply_markup={"inline_keyboard": []},
                )
            except TelegramAPIError as exc:
                LOGGER.info(
                    "Could not close destination picker (code=%s): %s",
                    exc.error_code,
                    exc.description,
                )
        self._send(
            chat_id,
            f"Recipients selected ({len(labels)}): {shown}\n\n"
            "Now send the bot one complete message. "
            "It will not be broadcast yet.",
        )

    def _start_campaign(
        self,
        chat_id: int,
        user_id: int,
        aliases: list[str],
        *,
        silent: bool,
    ) -> None:
        destinations, missing = self.db.resolve_destinations(
            aliases if aliases else None
        )

        if missing:
            self._send(
                chat_id,
                "Active destinations not found: " + ", ".join(missing),
            )
            return

        if not destinations:
            self._send(
                chat_id,
                "There are no active destinations. "
                "Run /register in a group first.",
            )
            return

        campaign = self.db.create_campaign(
            created_by=user_id,
            target_ids=[
                int(item["id"])
                for item in destinations
            ],
            silent=silent,
            ttl_minutes=self.settings.draft_ttl_minutes,
            now=self._now_utc(),
        )

        target_text = ", ".join(
            item["alias"]
            for item in destinations
        )
        self._send(
            chat_id,
            f"Draft #{campaign['id']} created.\n"
            f"Recipients ({len(destinations)}): {target_text}\n"
            f"Mode: {'silent' if silent else 'standard'}\n\n"
            "Now send the bot one complete message. "
            "It will not be broadcast yet.",
        )

    def _accept_campaign_content(
        self,
        message: dict[str, Any],
        user_id: int,
    ) -> None:
        chat_id = int(message["chat"]["id"])
        campaign = self.db.get_open_campaign(user_id)

        if (
            campaign is not None
            and campaign["status"] == "selecting_targets"
        ):
            self._send(
                chat_id,
                "Finish selecting recipients with the buttons first.",
            )
            return

        if (
            campaign is not None
            and campaign["status"] == "awaiting_schedule"
        ):
            self._accept_schedule_time(message, user_id, campaign)
            return

        if (
            campaign is None
            or campaign["status"] != "awaiting_content"
        ):
            self._send(
                chat_id,
                "Start a broadcast with /broadcast first.",
            )
            return

        try:
            self.api.copy_message(
                chat_id,
                chat_id,
                int(message["message_id"]),
            )
        except TelegramAPIError as exc:
            self._send(
                chat_id,
                "Could not create a preview. "
                "Send a different message.\n"
                f"Reason: {exc.description}",
            )
            return

        content_saved = self.db.set_campaign_content(
            campaign["id"],
            chat_id,
            int(message["message_id"]),
        )
        if not content_saved:
            self._send(
                chat_id,
                "The draft has already been changed or canceled. "
                "Start again.",
            )
            return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": (
                            f"✅ Send now to "
                            f"{len(campaign['target_ids'])} groups"
                        ),
                        "callback_data": (
                            f"send:{campaign['id']}"
                        ),
                    }
                ],
                [
                    {
                        "text": "🕒 Schedule",
                        "callback_data": (
                            f"schedule:{campaign['id']}"
                        ),
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data": (
                            f"cancel:{campaign['id']}"
                        ),
                    }
                ],
            ]
        }

        self._send(
            chat_id,
            f"The preview for broadcast #{campaign['id']} "
            "is shown above.\n"
            "Check the text, links, attachment, "
            "and number of recipients.",
            reply_markup=keyboard,
        )

    def _handle_callback(
        self,
        query: dict[str, Any],
    ) -> None:
        query_id = str(query.get("id", ""))
        sender = query.get("from") or {}
        user_id = int(sender.get("id", 0))
        data = str(query.get("data", ""))
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", user_id))
        message_id = int(message.get("message_id", 0))

        if not self._is_admin(user_id):
            self._answer_callback(
                query_id,
                "Access denied",
                show_alert=True,
            )
            return

        parts = data.split(":")
        if parts[0] in {"pick", "pickpage", "pickdone"}:
            expected = {"pick": 4, "pickpage": 3, "pickdone": 2}
            if len(parts) != expected[parts[0]]:
                self._answer_callback(
                    query_id,
                    "Invalid button",
                    show_alert=True,
                )
                return
            self._handle_selection_callback(
                query_id,
                user_id,
                chat_id,
                message_id,
                parts,
            )
            return

        try:
            action, raw_id = data.split(":", 1)
            campaign_id = int(raw_id)
        except (ValueError, TypeError):
            self._answer_callback(
                query_id,
                "Invalid button",
                show_alert=True,
            )
            return

        if action == "cancel":
            changed = self.db.cancel_campaign(
                campaign_id,
                user_id,
            )
            self._answer_callback(
                query_id,
                (
                    "Draft canceled"
                    if changed
                    else "Draft has already been processed"
                ),
            )
            if changed:
                self._send(
                    chat_id,
                    f"Broadcast #{campaign_id} canceled.",
                )
            return

        if action == "schedule":
            changed = self.db.transition_to_awaiting_schedule(
                campaign_id,
                user_id,
                now=self._now_utc(),
            )
            self._answer_callback(
                query_id,
                (
                    "Send the date and time"
                    if changed
                    else "Broadcast has already been processed or expired"
                ),
            )
            if changed:
                example_date = (
                    self._now_utc()
                    .astimezone(self.schedule_timezone)
                    .date()
                    + timedelta(days=1)
                )
                self._send(
                    chat_id,
                    f"When should broadcast #{campaign_id} be sent?\n"
                    "Send the date and time as:\n"
                    "YYYY-MM-DD HH:MM\n\n"
                    f"Time zone: {self.settings.schedule_timezone}\n"
                    f"Example: {example_date:%Y-%m-%d} 14:30",
                )
            return

        if action != "send":
            self._answer_callback(
                query_id,
                "Unknown action",
                show_alert=True,
            )
            return

        if not self.db.transition_to_sending(
            campaign_id,
            user_id,
        ):
            self._answer_callback(
                query_id,
                "Broadcast has already been processed or expired",
            )
            return

        # A callback acknowledgement is only a Telegram UI
        # convenience. It may already be too old after a local
        # restart, but a valid confirmed campaign must still be
        # delivered exactly once.
        self._answer_callback(
            query_id,
            "Broadcast started",
        )
        self._deliver_campaign(
            chat_id,
            campaign_id,
        )

    def _now_utc(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _parse_schedule_time(self, raw: str) -> datetime:
        try:
            local_naive = datetime.strptime(
                raw.strip(),
                "%Y-%m-%d %H:%M",
            )
        except ValueError as exc:
            raise ValueError(
                "Use the format YYYY-MM-DD HH:MM."
            ) from exc

        candidates = []
        for fold in (0, 1):
            candidate = local_naive.replace(
                tzinfo=self.schedule_timezone,
                fold=fold,
            )
            round_trip = (
                candidate.astimezone(timezone.utc)
                .astimezone(self.schedule_timezone)
                .replace(tzinfo=None)
            )
            if round_trip == local_naive:
                candidates.append(candidate)

        if not candidates:
            raise ValueError(
                "That local time does not exist because of a daylight-saving "
                "clock change. Choose a different time."
            )
        if (
            len(candidates) == 2
            and candidates[0].utcoffset() != candidates[1].utcoffset()
        ):
            raise ValueError(
                "That local time is ambiguous because of a daylight-saving "
                "clock change. Choose a different minute."
            )
        return candidates[0].astimezone(timezone.utc)

    def _format_schedule_time(self, value: datetime) -> str:
        return value.astimezone(self.schedule_timezone).strftime(
            "%Y-%m-%d %H:%M"
        )

    def _accept_schedule_time(
        self,
        message: dict[str, Any],
        user_id: int,
        campaign: dict[str, Any],
    ) -> None:
        chat_id = int(message["chat"]["id"])
        text = str(message.get("text", ""))
        try:
            scheduled_at = self._parse_schedule_time(text)
        except ValueError as exc:
            self._send(
                chat_id,
                f"Could not set the schedule. {exc}\n"
                f"Time zone: {self.settings.schedule_timezone}",
            )
            return

        now = self._now_utc()
        if scheduled_at <= now:
            self._send(
                chat_id,
                "The scheduled time must be in the future.\n"
                f"Time zone: {self.settings.schedule_timezone}",
            )
            return

        if not self.db.schedule_campaign(
            int(campaign["id"]),
            user_id,
            scheduled_at,
            now=now,
        ):
            self._send(
                chat_id,
                "The draft has already been changed, canceled, or expired. "
                "Start again.",
            )
            return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "❌ Cancel scheduled broadcast",
                        "callback_data": f"cancel:{campaign['id']}",
                    }
                ]
            ]
        }
        self._send(
            chat_id,
            f"Broadcast #{campaign['id']} scheduled for "
            f"{self._format_schedule_time(scheduled_at)} "
            f"({self.settings.schedule_timezone}).",
            reply_markup=keyboard,
        )

    def _run_due_campaigns(self) -> None:
        now = self._now_utc()
        for campaign in self.db.due_scheduled_campaigns(now=now):
            campaign_id = int(campaign["id"])
            if not self.db.claim_due_campaign(campaign_id, now=now):
                continue
            operator_chat_id = int(
                campaign["source_chat_id"] or campaign["created_by"]
            )
            LOGGER.info(
                "Starting scheduled broadcast %s (scheduled_at=%s)",
                campaign_id,
                campaign["scheduled_at"],
            )
            self._deliver_campaign(operator_chat_id, campaign_id)

    def _answer_callback(
        self,
        query_id: str,
        text: str,
        *,
        show_alert: bool = False,
    ) -> None:
        try:
            self.api.answer_callback_query(
                query_id,
                text,
                show_alert=show_alert,
            )
        except TelegramAPIError as exc:
            LOGGER.info(
                "Could not acknowledge callback query "
                "(code=%s): %s",
                exc.error_code,
                exc.description,
            )

    def _deliver_campaign(
        self,
        operator_chat_id: int,
        campaign_id: int,
    ) -> None:
        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            return

        consecutive_failures = 0
        stop_queue = False
        target_ids = campaign["target_ids"]

        for index, destination_id in enumerate(target_ids):
            destination = self.db.get_destination(
                int(destination_id)
            )

            if (
                destination is None
                or not destination["active"]
            ):
                self.db.record_delivery(
                    campaign_id=campaign_id,
                    destination_id=int(destination_id),
                    status="skipped",
                    attempts=0,
                    error_summary="destination is inactive",
                )
                continue

            if stop_queue:
                self.db.record_delivery(
                    campaign_id=campaign_id,
                    destination_id=int(destination_id),
                    status="skipped",
                    attempts=0,
                    error_summary=(
                        "queue stopped after five "
                        "consecutive failures"
                    ),
                )
                continue

            sent, attempts, result, error = (
                self._copy_with_retry(
                    campaign,
                    destination,
                )
            )

            if sent:
                consecutive_failures = 0
                self.db.record_delivery(
                    campaign_id=campaign_id,
                    destination_id=int(destination_id),
                    status="sent",
                    attempts=attempts,
                    telegram_message_id=(
                        int(result.get("message_id"))
                        if result
                        else None
                    ),
                )
            else:
                consecutive_failures += 1
                self.db.record_delivery(
                    campaign_id=campaign_id,
                    destination_id=int(destination_id),
                    status="failed",
                    attempts=attempts,
                    error_code=(
                        error.error_code
                        if error
                        else 0
                    ),
                    error_summary=(
                        error.description
                        if error
                        else "unknown error"
                    )[:500],
                )
                if consecutive_failures >= 5:
                    stop_queue = True

            if (
                index < len(target_ids) - 1
                and self.settings.send_delay_seconds
            ):
                self.sleeper(
                    self.settings.send_delay_seconds
                )

        self.db.finish_campaign(campaign_id)
        summary = self.db.delivery_summary(campaign_id)

        lines = [
            f"Broadcast #{campaign_id} completed.",
            "",
            f"Sent: {summary.get('sent', 0)}",
            f"Failed: {summary.get('failed', 0)}",
            f"Skipped: {summary.get('skipped', 0)}",
        ]

        failures = self.db.failed_deliveries(campaign_id)
        if failures:
            lines.extend(["", "Failures:"])
            lines.extend(
                f"- {row['alias']}: {row['error_summary']}"
                for row in failures[:15]
            )
            if len(failures) > 15:
                lines.append(
                    f"- and {len(failures) - 15} more"
                )

        if stop_queue:
            lines.extend(
                [
                    "",
                    "The queue was stopped after five "
                    "consecutive failures.",
                ]
            )

        self._send(
            operator_chat_id,
            "\n".join(lines),
        )

    def _copy_with_retry(
        self,
        campaign: dict[str, Any],
        destination: dict[str, Any],
    ) -> tuple[
        bool,
        int,
        dict[str, Any] | None,
        TelegramAPIError | None,
    ]:
        last_error: TelegramAPIError | None = None
        attempts = 0

        for attempt in range(1, 4):
            attempts = attempt
            try:
                result = self.api.copy_message(
                    int(destination["chat_id"]),
                    int(campaign["source_chat_id"]),
                    int(campaign["source_message_id"]),
                    message_thread_id=(
                        int(destination["thread_id"])
                        if destination["thread_id"] is not None
                        else None
                    ),
                    disable_notification=bool(
                        campaign["silent"]
                    ),
                )
                return True, attempts, result, None

            except TelegramAPIError as exc:
                last_error = exc

                if exc.migrate_to_chat_id is not None:
                    self.db.migrate_destination_chat(
                        int(destination["id"]),
                        int(exc.migrate_to_chat_id),
                    )
                    destination["chat_id"] = int(
                        exc.migrate_to_chat_id
                    )
                    continue

                if (
                    exc.error_code == 429
                    and exc.retry_after is not None
                ):
                    self.sleeper(
                        max(
                            1,
                            min(
                                int(exc.retry_after),
                                60,
                            ),
                        )
                    )
                    continue

                if exc.error_code == 0:
                    self.sleeper(
                        2 ** (attempt - 1)
                    )
                    continue

                break

        return False, attempts, None, last_error

    def _show_groups(
        self,
        chat_id: int,
    ) -> None:
        destinations = self.db.list_destinations(
            active_only=True
        )

        if not destinations:
            self._send(
                chat_id,
                "There are no active destinations.",
            )
            return

        lines = [
            f"Active destinations: {len(destinations)}",
            "",
        ]

        for destination in destinations:
            topic = (
                f", topic={destination['thread_id']}"
                if destination["thread_id"] is not None
                else ""
            )
            lines.append(
                f"- {destination['alias']}: "
                f"{destination['chat_title']} "
                f"(chat={destination['chat_id']}{topic})"
            )

        self._send(
            chat_id,
            "\n".join(lines),
        )

    def _show_history(
        self,
        chat_id: int,
        user_id: int,
    ) -> None:
        rows = self.db.recent_campaigns(user_id)

        if not rows:
            self._send(
                chat_id,
                "Broadcast history is empty.",
            )
            return

        lines = [
            "Recent broadcasts:",
            "",
        ]

        for row in rows:
            schedule = ""
            if row["scheduled_at"]:
                scheduled_at = datetime.fromisoformat(
                    str(row["scheduled_at"])
                )
                schedule = (
                    "; scheduled for "
                    f"{self._format_schedule_time(scheduled_at)} "
                    f"({self.settings.schedule_timezone})"
                )
            lines.append(
                f"#{row['id']} — {row['status']}; "
                f"sent {row['sent_count'] or 0}, "
                f"failed {row['failed_count'] or 0}; "
                f"{row['created_at']}{schedule}"
            )

        self._send(
            chat_id,
            "\n".join(lines),
        )

    def _handle_membership(
        self,
        membership: dict[str, Any],
    ) -> None:
        chat = membership.get("chat") or {}
        new_status = (
            membership.get("new_chat_member") or {}
        ).get("status")

        if (
            "id" in chat
            and new_status in {"left", "kicked"}
        ):
            count = self.db.deactivate_chat(
                int(chat["id"])
            )
            if count:
                LOGGER.info(
                    "Deactivated %s destination(s) "
                    "after bot removal from chat",
                    count,
                )

    def _send(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.api.send_message(
            chat_id,
            text,
            message_thread_id=(
                int(thread_id)
                if thread_id is not None
                else None
            ),
            reply_markup=reply_markup,
        )
