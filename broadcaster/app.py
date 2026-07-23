from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from .api import TelegramAPIError
from .database import Database
from .settings import Settings


LOGGER = logging.getLogger(__name__)
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")
ADMIN_STATUSES = {"administrator", "creator"}

HELP_TEXT = """Gonka Support Broadcaster

/whoami — показать ваш числовой Telegram ID
/broadcast — рассылка во все активные группы
/broadcast alias1 alias2 — рассылка в выбранные группы
/broadcast_silent — то же самое без звукового уведомления
/groups — список направлений
/history — последние рассылки
/cancel — отменить текущий черновик

В группе или теме:
/register alias — зарегистрировать направление
/unregister — отключить направление

Рассылку может запускать только разрешённый оператор в личном чате."""


class BroadcasterApp:
    def __init__(
        self,
        api: Any,
        database: Database,
        settings: Settings,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api = api
        self.db = database
        self.settings = settings
        self.sleeper = sleeper
        self.bot_user_id: int | None = None
        self.bot_username = ""

    def initialize(self) -> None:
        me = self.api.get_me()
        self.bot_user_id = int(me["id"])
        self.bot_username = str(me.get("username", ""))
        interrupted = self.db.recover_interrupted_campaigns()
        if interrupted:
            LOGGER.warning("Marked %s interrupted campaign(s); no automatic resend", interrupted)
        LOGGER.info("Started @%s (id=%s)", self.bot_username, self.bot_user_id)

    def run_forever(self) -> None:
        self.initialize()
        offset = self.db.get_state_int("update_offset", 0)
        while True:
            try:
                updates = self.api.get_updates(
                    offset=offset,
                    timeout=self.settings.poll_timeout_seconds,
                )
                for update in updates:
                    update_id = int(update["update_id"])
                    try:
                        self.process_update(update)
                    except Exception:
                        LOGGER.exception("Failed to process Telegram update %s", update_id)
                    offset = max(offset, update_id + 1)
                    self.db.set_state_int("update_offset", offset)
            except TelegramAPIError as exc:
                LOGGER.warning("Telegram polling error %s: %s", exc.error_code, exc.description)
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
                self._send(chat_id, "Для безопасности используйте /whoami в личном чате с ботом.")
            return

        if command in {"start", "help"}:
            self._send(chat_id, HELP_TEXT)
            return
        if command == "whoami":
            self._send(chat_id, f"Ваш Telegram user ID: {user_id}")
            return

        if not self._is_admin(user_id):
            if command or text:
                self._send(
                    chat_id,
                    "Доступ запрещён. Отправьте /whoami и добавьте этот ID в ADMIN_USER_IDS.",
                )
            return

        if command in {"broadcast", "broadcast_silent"}:
            self._start_campaign(chat_id, user_id, args, silent=command == "broadcast_silent")
        elif command == "groups":
            self._show_groups(chat_id)
        elif command == "history":
            self._show_history(chat_id, user_id)
        elif command == "cancel":
            count = self.db.cancel_open_campaigns(user_id)
            self._send(chat_id, "Черновик отменён." if count else "Активного черновика нет.")
        elif command:
            self._send(chat_id, "Неизвестная команда. Используйте /help.")
        else:
            self._accept_campaign_content(message, user_id)

    def _register_destination(self, message: dict[str, Any], args: list[str]) -> None:
        sender = message["from"]
        chat = message["chat"]
        user_id = int(sender["id"])
        chat_id = int(chat["id"])
        thread_id = message.get("message_thread_id")

        if not self._is_admin(user_id):
            self._send(chat_id, "Регистрация доступна только разрешённому оператору.", thread_id)
            return
        if len(args) != 1 or not ALIAS_RE.fullmatch(args[0]):
            self._send(
                chat_id,
                "Использование: /register alias\nAlias: 2–40 символов A-Z, a-z, 0-9, _ или -.",
                thread_id,
            )
            return
        if self.bot_user_id is None:
            me = self.api.get_me()
            self.bot_user_id = int(me["id"])

        try:
            operator = self.api.get_chat_member(chat_id, user_id)
            bot_member = self.api.get_chat_member(chat_id, int(self.bot_user_id))
        except TelegramAPIError as exc:
            self._send(chat_id, f"Не удалось проверить права: {exc.description}", thread_id)
            return

        if operator.get("status") not in ADMIN_STATUSES:
            self._send(chat_id, "Оператор должен быть администратором этой группы.", thread_id)
            return
        bot_status = bot_member.get("status")
        bot_can_send = bot_status in {"member", "administrator", "creator"} or (
            bot_status == "restricted" and bool(bot_member.get("can_send_messages"))
        )
        if not bot_can_send:
            self._send(chat_id, "У бота нет права отправлять сообщения в эту группу.", thread_id)
            return

        try:
            destination = self.db.register_destination(
                alias=args[0],
                chat_id=chat_id,
                thread_id=int(thread_id) if thread_id is not None else None,
                chat_title=str(chat.get("title", chat_id)),
                registered_by=user_id,
            )
        except ValueError as exc:
            self._send(chat_id, str(exc), thread_id)
            return
        topic = f", тема {destination['thread_id']}" if destination["thread_id"] is not None else ""
        self._send(
            chat_id,
            f"Направление зарегистрировано: {destination['alias']} ({destination['chat_title']}{topic}).",
            thread_id,
        )

    def _unregister_destination(self, message: dict[str, Any]) -> None:
        sender = message["from"]
        chat = message["chat"]
        user_id = int(sender["id"])
        chat_id = int(chat["id"])
        thread_id = message.get("message_thread_id")
        if not self._is_admin(user_id):
            self._send(chat_id, "Отключение доступно только разрешённому оператору.", thread_id)
            return
        try:
            member = self.api.get_chat_member(chat_id, user_id)
        except TelegramAPIError as exc:
            self._send(chat_id, f"Не удалось проверить права: {exc.description}", thread_id)
            return
        if member.get("status") not in ADMIN_STATUSES:
            self._send(chat_id, "Оператор должен быть администратором этой группы.", thread_id)
            return
        changed = self.db.deactivate_destination(
            chat_id=chat_id,
            thread_id=int(thread_id) if thread_id is not None else None,
        )
        self._send(
            chat_id,
            "Направление отключено." if changed else "Это направление не зарегистрировано.",
            thread_id,
        )

    def _start_campaign(
        self, chat_id: int, user_id: int, aliases: list[str], *, silent: bool
    ) -> None:
        destinations, missing = self.db.resolve_destinations(aliases if aliases else None)
        if missing:
            self._send(chat_id, "Не найдены активные направления: " + ", ".join(missing))
            return
        if not destinations:
            self._send(chat_id, "Нет активных направлений. Сначала выполните /register в группе.")
            return
        campaign = self.db.create_campaign(
            created_by=user_id,
            target_ids=[int(item["id"]) for item in destinations],
            silent=silent,
            ttl_minutes=self.settings.draft_ttl_minutes,
        )
        target_text = ", ".join(item["alias"] for item in destinations)
        self._send(
            chat_id,
            f"Черновик #{campaign['id']} создан.\n"
            f"Получатели ({len(destinations)}): {target_text}\n"
            f"Режим: {'без звука' if silent else 'обычный'}\n\n"
            "Теперь отправьте боту одно готовое сообщение. Оно ещё не будет разослано.",
        )

    def _accept_campaign_content(self, message: dict[str, Any], user_id: int) -> None:
        chat_id = int(message["chat"]["id"])
        campaign = self.db.get_open_campaign(user_id)
        if campaign is None or campaign["status"] != "awaiting_content":
            self._send(chat_id, "Сначала создайте рассылку командой /broadcast.")
            return
        try:
            self.api.copy_message(chat_id, chat_id, int(message["message_id"]))
        except TelegramAPIError as exc:
            self._send(
                chat_id,
                "Не удалось создать предпросмотр. Отправьте другое сообщение.\n"
                f"Причина: {exc.description}",
            )
            return
        if not self.db.set_campaign_content(campaign["id"], chat_id, int(message["message_id"])):
            self._send(chat_id, "Черновик уже изменён или отменён. Начните заново.")
            return
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": f"✅ Разослать в {len(campaign['target_ids'])} групп",
                        "callback_data": f"send:{campaign['id']}",
                    }
                ],
                [{"text": "❌ Отмена", "callback_data": f"cancel:{campaign['id']}"}],
            ]
        }
        self._send(
            chat_id,
            f"Предпросмотр рассылки #{campaign['id']} показан выше.\n"
            "Проверьте текст, ссылки, вложение и количество получателей.",
            reply_markup=keyboard,
        )

    def _handle_callback(self, query: dict[str, Any]) -> None:
        query_id = str(query.get("id", ""))
        sender = query.get("from") or {}
        user_id = int(sender.get("id", 0))
        data = str(query.get("data", ""))
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", user_id))

        if not self._is_admin(user_id):
            self._answer_callback(query_id, "Доступ запрещён", show_alert=True)
            return
        try:
            action, raw_id = data.split(":", 1)
            campaign_id = int(raw_id)
        except (ValueError, TypeError):
            self._answer_callback(query_id, "Некорректная кнопка", show_alert=True)
            return

        if action == "cancel":
            changed = self.db.cancel_campaign(campaign_id, user_id)
            self._answer_callback(
                query_id, "Черновик отменён" if changed else "Черновик уже обработан"
            )
            if changed:
                self._send(chat_id, f"Рассылка #{campaign_id} отменена.")
            return
        if action != "send":
            self._answer_callback(query_id, "Неизвестное действие", show_alert=True)
            return
        if not self.db.transition_to_sending(campaign_id, user_id):
            self._answer_callback(query_id, "Рассылка уже обработана или просрочена")
            return

        # A callback acknowledgement is only a Telegram UI convenience. It may
        # already be too old after a local restart, but a valid confirmed
        # campaign must still be delivered exactly once.
        self._answer_callback(query_id, "Рассылка запущена")
        self._deliver_campaign(chat_id, campaign_id)

    def _answer_callback(self, query_id: str, text: str, *, show_alert: bool = False) -> None:
        try:
            self.api.answer_callback_query(query_id, text, show_alert=show_alert)
        except TelegramAPIError as exc:
            LOGGER.info(
                "Could not acknowledge callback query (code=%s): %s",
                exc.error_code,
                exc.description,
            )

    def _deliver_campaign(self, operator_chat_id: int, campaign_id: int) -> None:
        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            return
        consecutive_failures = 0
        stop_queue = False
        target_ids = campaign["target_ids"]

        for index, destination_id in enumerate(target_ids):
            destination = self.db.get_destination(int(destination_id))
            if destination is None or not destination["active"]:
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
                    error_summary="queue stopped after five consecutive failures",
                )
                continue

            sent, attempts, result, error = self._copy_with_retry(campaign, destination)
            if sent:
                consecutive_failures = 0
                self.db.record_delivery(
                    campaign_id=campaign_id,
                    destination_id=int(destination_id),
                    status="sent",
                    attempts=attempts,
                    telegram_message_id=int(result.get("message_id")) if result else None,
                )
            else:
                consecutive_failures += 1
                self.db.record_delivery(
                    campaign_id=campaign_id,
                    destination_id=int(destination_id),
                    status="failed",
                    attempts=attempts,
                    error_code=error.error_code if error else 0,
                    error_summary=(error.description if error else "unknown error")[:500],
                )
                if consecutive_failures >= 5:
                    stop_queue = True
            if index < len(target_ids) - 1 and self.settings.send_delay_seconds:
                self.sleeper(self.settings.send_delay_seconds)

        self.db.finish_campaign(campaign_id)
        summary = self.db.delivery_summary(campaign_id)
        lines = [
            f"Рассылка #{campaign_id} завершена.",
            "",
            f"Успешно: {summary.get('sent', 0)}",
            f"Ошибки: {summary.get('failed', 0)}",
            f"Пропущено: {summary.get('skipped', 0)}",
        ]
        failures = self.db.failed_deliveries(campaign_id)
        if failures:
            lines.extend(["", "Ошибки:"])
            lines.extend(
                f"- {row['alias']}: {row['error_summary']}" for row in failures[:15]
            )
            if len(failures) > 15:
                lines.append(f"- и ещё {len(failures) - 15}")
        if stop_queue:
            lines.extend(["", "Очередь остановлена после пяти последовательных ошибок."])
        self._send(operator_chat_id, "\n".join(lines))

    def _copy_with_retry(
        self, campaign: dict[str, Any], destination: dict[str, Any]
    ) -> tuple[bool, int, dict[str, Any] | None, TelegramAPIError | None]:
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
                    disable_notification=bool(campaign["silent"]),
                )
                return True, attempts, result, None
            except TelegramAPIError as exc:
                last_error = exc
                if exc.migrate_to_chat_id is not None:
                    self.db.migrate_destination_chat(
                        int(destination["id"]), int(exc.migrate_to_chat_id)
                    )
                    destination["chat_id"] = int(exc.migrate_to_chat_id)
                    continue
                if exc.error_code == 429 and exc.retry_after is not None:
                    self.sleeper(max(1, min(int(exc.retry_after), 60)))
                    continue
                if exc.error_code == 0:
                    self.sleeper(2 ** (attempt - 1))
                    continue
                break
        return False, attempts, None, last_error

    def _show_groups(self, chat_id: int) -> None:
        destinations = self.db.list_destinations(active_only=True)
        if not destinations:
            self._send(chat_id, "Активных направлений нет.")
            return
        lines = [f"Активные направления: {len(destinations)}", ""]
        for destination in destinations:
            topic = (
                f", topic={destination['thread_id']}"
                if destination["thread_id"] is not None
                else ""
            )
            lines.append(
                f"- {destination['alias']}: {destination['chat_title']}"
                f" (chat={destination['chat_id']}{topic})"
            )
        self._send(chat_id, "\n".join(lines))

    def _show_history(self, chat_id: int, user_id: int) -> None:
        rows = self.db.recent_campaigns(user_id)
        if not rows:
            self._send(chat_id, "История рассылок пуста.")
            return
        lines = ["Последние рассылки:", ""]
        for row in rows:
            lines.append(
                f"#{row['id']} — {row['status']}; "
                f"успешно {row['sent_count'] or 0}, ошибок {row['failed_count'] or 0}; "
                f"{row['created_at']}"
            )
        self._send(chat_id, "\n".join(lines))

    def _handle_membership(self, membership: dict[str, Any]) -> None:
        chat = membership.get("chat") or {}
        new_status = (membership.get("new_chat_member") or {}).get("status")
        if "id" in chat and new_status in {"left", "kicked"}:
            count = self.db.deactivate_chat(int(chat["id"]))
            if count:
                LOGGER.info("Deactivated %s destination(s) after bot removal from chat", count)

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
            message_thread_id=int(thread_id) if thread_id is not None else None,
            reply_markup=reply_markup,
        )
