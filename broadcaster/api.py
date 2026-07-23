from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any


class TelegramAPIError(RuntimeError):
    def __init__(
        self,
        error_code: int,
        description: str,
        *,
        retry_after: int | None = None,
        migrate_to_chat_id: int | None = None,
    ) -> None:
        super().__init__(description)
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after
        self.migrate_to_chat_id = migrate_to_chat_id


class TelegramAPI:
    def __init__(self, token: str, *, request_timeout: int = 45) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.request_timeout = request_timeout

    def call(self, method: str, **payload: Any) -> Any:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return self._decode(method, raw, fallback_code=exc.code)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise TelegramAPIError(0, f"Network error calling {method}: {exc}") from exc
        return self._decode(method, raw)

    @staticmethod
    def _decode(method: str, raw: bytes, *, fallback_code: int = 0) -> Any:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramAPIError(fallback_code, f"Invalid Telegram response for {method}") from exc
        if not data.get("ok"):
            parameters = data.get("parameters") or {}
            raise TelegramAPIError(
                int(data.get("error_code", fallback_code)),
                str(data.get("description", f"Telegram method {method} failed")),
                retry_after=parameters.get("retry_after"),
                migrate_to_chat_id=parameters.get("migrate_to_chat_id"),
            )
        return data.get("result")

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, *, offset: int, timeout: int) -> list[dict[str, Any]]:
        return self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query", "my_chat_member"],
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", **payload)

    def copy_message(
        self,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        *,
        message_thread_id: int | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
            "disable_notification": disable_notification,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self.call("copyMessage", **payload)

    def answer_callback_query(
        self, callback_query_id: str, text: str = "", *, show_alert: bool = False
    ) -> bool:
        return bool(
            self.call(
                "answerCallbackQuery",
                callback_query_id=callback_query_id,
                text=text,
                show_alert=show_alert,
            )
        )

    def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return self.call("getChatMember", chat_id=chat_id, user_id=user_id)

