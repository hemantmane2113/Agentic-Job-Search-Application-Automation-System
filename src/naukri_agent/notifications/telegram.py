"""
Telegram as the human-in-the-loop channel: the app asks, the user answers on
their phone.

Uses the official Bot API over HTTPS long-polling (getUpdates), so it works
from a home PC with no public address, no webhook and no extra dependency.

Safety properties, each covered by tests:
  * Only messages from the ONE configured chat id are ever acted on; anyone
    else who finds the bot is ignored.
  * Stale messages are drained before every prompt, so an old "y" can never
    answer a new question.
  * No reply in time returns None -- callers must treat silence as "no".
  * The bot token appears only inside the default transport's URL. Errors are
    reported by exception TYPE only, and the token is never logged.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

_POLL_SECONDS = 25
_YES = {"y", "yes", "ok", "apply", "go", "yep"}
_NO = {"n", "no", "skip", "stop", "nope"}

Transport = Callable[[str, dict, float], dict]


class TelegramError(Exception):
    """A Telegram call failed. The message holds only the error TYPE, never the URL or token."""


def http_transport(token: str) -> Transport:
    def call(method: str, params: dict, timeout: float) -> dict:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - the URL (and token) must never leak via str(exc)
            raise TelegramError(f"{method} failed: {type(exc).__name__}") from None

    return call


class TelegramChannel:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._chat_id = str(chat_id)
        self._transport = transport or http_transport(token)
        self._clock = clock
        self._offset: int | None = None

    # --- sending -----------------------------------------------------------------

    def send(self, text: str, *, yes_no: bool = False) -> None:
        params: dict[str, Any] = {"chat_id": self._chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if yes_no:
            params["reply_markup"] = {
                "inline_keyboard": [[{"text": "Yes", "callback_data": "y"}, {"text": "No", "callback_data": "n"}]]
            }
        self._transport("sendMessage", params, 30)

    # --- receiving ---------------------------------------------------------------

    def _poll(self, seconds: int) -> list[dict]:
        params: dict[str, Any] = {"timeout": seconds, "allowed_updates": ["message", "callback_query"]}
        if self._offset is not None:
            params["offset"] = self._offset
        data = self._transport("getUpdates", params, seconds + 10)
        updates = data.get("result") or []
        for u in updates:
            self._offset = max(self._offset or 0, int(u["update_id"]) + 1)
        return updates

    def _own_chat(self, update: dict) -> tuple[str, str | None] | None:
        """(kind, value) if the update is from OUR chat, else None. kind: 'text' | 'button'."""
        msg = update.get("message")
        if msg and str(msg.get("chat", {}).get("id")) == self._chat_id and msg.get("text") is not None:
            return "text", str(msg["text"]).strip()
        cb = update.get("callback_query")
        if cb and str(cb.get("message", {}).get("chat", {}).get("id")) == self._chat_id:
            try:
                self._transport("answerCallbackQuery", {"callback_query_id": cb.get("id")}, 10)
            except TelegramError:
                pass
            return "button", str(cb.get("data") or "")
        return None

    def drain(self) -> None:
        """Discard everything already waiting, so only replies to the NEXT prompt are read."""
        while self._poll(0):
            pass

    def _wait(self, timeout_s: float, accept: Callable[[str, str], Any]) -> Any:
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            remaining = deadline - self._clock()
            for update in self._poll(min(_POLL_SECONDS, max(1, int(remaining)))):
                got = self._own_chat(update)
                if got is None:
                    continue
                result = accept(*got)
                if result is not None:
                    return result
        return None

    def ask_yes_no(self, text: str, timeout_s: float) -> bool | None:
        """True / False, or None if no clear answer arrived in time."""
        self.drain()
        self.send(text, yes_no=True)
        warned = []

        def accept(kind: str, value: str) -> bool | None:
            v = value.lower()
            if v in _YES:
                return True
            if v in _NO:
                return False
            if not warned:
                warned.append(1)
                self.send("Please answer Yes or No (tap a button, or type y / n).")
            return None

        return self._wait(timeout_s, accept)

    def ask_text(self, text: str, timeout_s: float) -> str | None:
        """The user's next typed message, or None if nothing arrived in time."""
        self.drain()
        self.send(text)
        return self._wait(timeout_s, lambda kind, value: value if kind == "text" and value else None)

    def discover_chat_id(self, timeout_s: float) -> str | None:
        """Setup helper: the chat id of the first person to message the bot."""
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            for update in self._poll(min(_POLL_SECONDS, max(1, int(deadline - self._clock())))):
                chat = (update.get("message") or {}).get("chat") or {}
                if chat.get("id") is not None:
                    return str(chat["id"])
        return None
