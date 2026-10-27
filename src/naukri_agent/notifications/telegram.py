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
  * Every set of buttons carries the id of the ONE prompt it belongs to. A tap on
    any other card's buttons (a second tap on the previous job, a card from before
    a restart) answers nothing: it is told "no longer active" and the old card's
    buttons are switched off. Without this, a duplicate "Yes" that arrived after
    the next job's card was sent counted as approval of that next job (found
    2026-10-21: Contoso Talent was "approved" 27 seconds after the previous job).
  * The buttons of a card that has just been answered turn into one grey
    "Applying... please wait" (or "No - skipped") button, so it cannot be tapped twice.
  * With instant_ack the answer is noticed within seconds even while the app is busy
    loading a page: a small background reader watches for the tap on the card just
    sent (see _watch_loop). It is the ONLY reader of Telegram while it runs; every
    other call that reads Telegram stops it first, and it never decides anything -
    wait_yes_no() still returns the answer to the caller, exactly as before.
  * No reply in time returns None -- callers must treat silence as "no".
  * The bot token appears only inside the default transport's URL. Errors are
    reported by exception TYPE only, and the token is never logged.
"""

from __future__ import annotations

import json
import logging
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

_POLL_SECONDS = 25
_WAIT_RETRY_PAUSE_S = 2  # a dropped connection while waiting for your tap: pause this long, then listen again
_WAIT_MAX_CONSECUTIVE_ERRORS = 30  # only a connection that stays down for this many tries in a row ends the wait
_YES = {"y", "yes", "ok", "apply", "go", "yep"}
_NO = {"n", "no", "skip", "stop", "nope"}
_WATCH_POLL_SECONDS = 2  # the background reader listens in slices this long, so it can be stopped within about this long
_WATCH_MAX_SECONDS = 3 * 3600  # a background reader never outlives this, whatever happens

Transport = Callable[[str, dict, float], dict]


class TelegramError(Exception):
    """A Telegram call failed. The message holds only the error TYPE, never the URL or token."""


def _ssl_context() -> ssl.SSLContext:
    """Verifying context backed by certifi's CA bundle. Python on Windows only trusts
    the roots Windows happens to have installed, and a PC that has never needed
    GoDaddy's root (Telegram's CA) fails with "self-signed certificate in
    certificate chain" even though nothing is wrong. Certificate checking stays ON;
    only the list of trusted roots is the standard, complete one."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - fall back to the system store
        return ssl.create_default_context()


def http_transport(token: str) -> Transport:
    ctx = _ssl_context()

    def call(method: str, params: dict, timeout: float) -> dict:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 401/404 here means the token was rejected
            raise TelegramError(f"{method} failed: HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            # the reason's TYPE (e.g. SSLCertVerificationError, gaierror) is safe; its text could echo the URL
            raise TelegramError(f"{method} failed: URLError/{type(exc.reason).__name__}") from None
        except Exception as exc:  # noqa: BLE001 - the URL (and token) must never leak via str(exc)
            raise TelegramError(f"{method} failed: {type(exc).__name__}") from None

    return call


class _Watch:
    """The background reader for ONE Yes/No card."""

    def __init__(self, prompt_id: str, working: str) -> None:
        self.prompt_id, self.working = prompt_id, working
        self.answered = threading.Event()  # an answer (or a fatal error) is ready
        self.stop = threading.Event()  # ask the reader to end
        self.result: bool | None = None
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None


class TelegramChannel:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        instant_ack: bool = False,
    ) -> None:
        self._chat_id = str(chat_id)
        self._transport = transport or http_transport(token)
        self._clock = clock
        self._sleep = sleep
        self._offset: int | None = None
        self._prompt_id: str | None = None  # the one prompt whose buttons are live
        self._instant_ack = instant_ack
        self._watch: _Watch | None = None
        self._working = "Applying"
        self._poll_lock = threading.Lock()  # Telegram allows ONE getUpdates reader at a time

    def _new_prompt(self) -> str:
        self._prompt_id = secrets.token_hex(3)
        return self._prompt_id

    # --- sending -----------------------------------------------------------------

    def send(self, text: str, *, yes_no: bool = False) -> None:
        params: dict[str, Any] = {"chat_id": self._chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if yes_no:
            pid = self._new_prompt()
            params["reply_markup"] = {
                "inline_keyboard": [[
                    {"text": "Yes", "callback_data": f"y:{pid}"},
                    {"text": "No", "callback_data": f"n:{pid}"},
                ]]
            }
        self._transport("sendMessage", params, 30)

    # --- receiving ---------------------------------------------------------------

    def _poll(self, seconds: int) -> list[dict]:
        with self._poll_lock:
            params: dict[str, Any] = {"timeout": seconds, "allowed_updates": ["message", "callback_query"]}
            if self._offset is not None:
                params["offset"] = self._offset
            data = self._transport("getUpdates", params, seconds + 10)
            updates = data.get("result") or []
            for u in updates:
                self._offset = max(self._offset or 0, int(u["update_id"]) + 1)
            return updates

    def _own_chat(self, update: dict) -> tuple[str, str | None, dict | None] | None:
        """(kind, value, callback) if the update is from OUR chat, else None. kind: 'text' | 'button'. The callback is
        returned unanswered: _wait() answers it once it knows whether the tap counted."""
        msg = update.get("message")
        if msg and str(msg.get("chat", {}).get("id")) == self._chat_id and msg.get("text") is not None:
            return "text", str(msg["text"]).strip(), None
        cb = update.get("callback_query")
        if cb and str(cb.get("message", {}).get("chat", {}).get("id")) == self._chat_id:
            return "button", str(cb.get("data") or ""), cb
        return None

    def _answer_cb(self, cb: dict, text: str) -> None:
        """Stop the button's spinner and show a short note. Best effort: a tap Telegram no longer accepts an answer for
        (too old) must not break anything."""
        try:
            self._transport("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": text}, 10)
        except TelegramError:
            pass

    def _set_buttons(self, cb: dict, label: str) -> None:
        """Replace the card's buttons with one inert button saying what happened, so it cannot be tapped again."""
        message_id = (cb.get("message") or {}).get("message_id")
        if message_id is None:
            return
        try:
            self._transport(
                "editMessageReplyMarkup",
                {"chat_id": self._chat_id, "message_id": message_id,
                 "reply_markup": {"inline_keyboard": [[{"text": label[:60], "callback_data": "done"}]]}},
                10,
            )
        except TelegramError:
            pass

    def _ack_button(self, cb: dict, result: Any, ack: Callable[[Any], tuple[str, str]] | None) -> None:
        """Tell the user what their tap did. `ack(result)` gives (the note shown briefly, the grey button text)."""
        if result is not None:
            toast, button = ack(result) if ack else ("Got it.", "\u2705 Received")
            self._answer_cb(cb, toast)
            self._set_buttons(cb, button)
        elif str(cb.get("data") or "") == "done":
            self._answer_cb(cb, "Already received - it is being processed.")
        else:
            self._answer_cb(cb, "That button is no longer active - use the newest message.")
            self._set_buttons(cb, "\u231b Expired")

    def poll_commands(self, seconds: int) -> list[tuple[str, int | None]]:
        """Typed messages from OUR chat as (text, sent-at epoch seconds). Button taps and other chats are ignored.
        For the phone listener, which waits for "/apply" between runs."""
        out: list[tuple[str, int | None]] = []
        for update in self._poll(seconds):
            cb = update.get("callback_query")
            if cb and str(cb.get("message", {}).get("chat", {}).get("id")) == self._chat_id:
                # A button tapped on a question that is no longer being waited on (it timed out, or the bot restarted).
                # Say so, instead of leaving the button spinning as if it did something.
                try:
                    self._transport(
                        "answerCallbackQuery",
                        {"callback_query_id": cb.get("id"), "show_alert": True,
                         "text": "That question has expired. Send /applied (or /apply) to start again."},
                        10,
                    )
                except TelegramError:
                    pass
                continue
            msg = update.get("message")
            if msg and str(msg.get("chat", {}).get("id")) == self._chat_id and msg.get("text"):
                date = msg.get("date")
                out.append((str(msg["text"]).strip(), int(date) if date is not None else None))
        return out

    def drain(self) -> None:
        """Discard everything already waiting, so only replies to the NEXT prompt are read. A button tapped meanwhile
        is told it is no longer active, instead of being left spinning."""
        self._stop_watch()  # nothing else may read Telegram while we do
        while True:
            updates = self._poll(0)
            if not updates:
                return
            for update in updates:
                cb = update.get("callback_query")
                if cb and str((cb.get("message") or {}).get("chat", {}).get("id")) == self._chat_id:
                    self._answer_cb(cb, "Already handled - use the newest message.")

    def _wait(
        self, timeout_s: float, accept: Callable[[str, str], Any],
        ack: Callable[[Any], tuple[str, str]] | None = None,
    ) -> Any:
        deadline = self._clock() + timeout_s
        errors = 0
        while self._clock() < deadline:
            remaining = deadline - self._clock()
            try:
                updates = self._poll(min(_POLL_SECONDS, max(1, int(remaining))))
            except TelegramError:
                # A dropped or slow connection must not throw away the question you are in the middle of answering.
                # Nothing was consumed (the offset only moves after a good reply), so just listen again.
                errors += 1
                if errors >= _WAIT_MAX_CONSECUTIVE_ERRORS:
                    raise
                self._sleep(_WAIT_RETRY_PAUSE_S)
                continue
            errors = 0
            found = None
            for update in updates:
                got = self._own_chat(update)
                if got is None:
                    continue
                kind, value, cb = got
                if found is not None:  # a second tap in the same batch: the first one already counted
                    if cb is not None:
                        self._answer_cb(cb, "Already received - it is being processed.")
                    continue
                result = accept(kind, value)
                if cb is not None:
                    self._ack_button(cb, result, ack)
                if result is not None:
                    found = result
            if found is not None:
                return found
        return None

    def ask_yes_no(self, text: str, timeout_s: float, *, working: str = "Applying") -> bool | None:
        self.send_yes_no(text, working=working)
        return self.wait_yes_no(timeout_s)

    @staticmethod
    def _yes_no_ack(working: str) -> Callable[[bool], tuple[str, str]]:
        """What a Yes / No tap shows at once: a Yes says the work has begun, a No says the job is skipped."""
        def ack(result: bool) -> tuple[str, str]:
            if result:
                return f"{working}\u2026 please wait.", f"\u23f3 {working}\u2026 please wait"
            return "Skipped.", "\u274c No - skipped"

        return ack

    def _yes_no_accept(self, prompt_id: str | None) -> Callable[[str, str], bool | None]:
        warned: list[int] = []

        def accept(kind: str, value: str) -> bool | None:
            if kind == "button":
                tag, _, pid = value.partition(":")
                if not pid or pid != prompt_id:
                    return None  # another card's button (an old tap, a duplicate, a card from before a restart)
                value = tag
            v = value.lower()
            if v in _YES:
                return True
            if v in _NO:
                return False
            if not warned:
                warned.append(1)
                self.send("Please answer Yes or No (tap a button, or type y / n).")
            return None

        return accept

    def send_yes_no(self, text: str, *, working: str = "Applying") -> None:
        """Ask now; read the answer later with wait_yes_no(). Anything already waiting is
        discarded first, so an old reply can never answer this prompt. Replies sent while
        the caller is busy are kept by Telegram and are read as soon as wait_yes_no() runs;
        with instant_ack they are also acknowledged at once on the phone."""
        self.drain()  # also stops the previous card's reader
        self.send(text, yes_no=True)
        self._working = working
        if self._instant_ack and self._prompt_id:
            self._start_watch(self._prompt_id, working)

    def wait_yes_no(self, timeout_s: float) -> bool | None:
        """True / False, or None if no clear answer arrived in time."""
        watch = self._watch
        if watch is not None:  # the background reader has been listening since the card went out
            got = watch.answered.wait(max(0.0, timeout_s))
            if watch.error is not None:
                self._stop_watch()
                raise watch.error
            if not got:
                self._stop_watch()  # nobody answered in time: stop listening for this card
                return None
            return watch.result  # the reader stays on to answer extra taps until the next prompt or cancel_prompt()
        return self._wait(timeout_s, self._yes_no_accept(self._prompt_id), ack=self._yes_no_ack(self._working))

    def cancel_prompt(self) -> None:
        """The card just sent is no longer being asked (its job was skipped): stop watching it, and treat any later
        tap on it as an old card's."""
        self._stop_watch()
        self._prompt_id = None

    # --- the background reader ------------------------------------------------------------------------------------

    def _start_watch(self, prompt_id: str, working: str) -> None:
        watch = _Watch(prompt_id, working)
        watch.thread = threading.Thread(target=self._watch_loop, args=(watch,), name="telegram-tap-reader", daemon=True)
        self._watch = watch
        watch.thread.start()

    def _stop_watch(self) -> None:
        watch, self._watch = self._watch, None
        if watch is None:
            return
        watch.stop.set()
        thread = watch.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_WATCH_POLL_SECONDS + 12)

    def _watch_loop(self, watch: _Watch) -> None:
        """Listen for the answer to ONE card while the app is busy elsewhere, and acknowledge taps within seconds. After
        the first answer it keeps listening only to tell any extra tap that it was already received."""
        accept = self._yes_no_accept(watch.prompt_id)
        ack = self._yes_no_ack(watch.working)
        deadline = time.monotonic() + _WATCH_MAX_SECONDS
        errors = 0
        try:
            while not watch.stop.is_set() and time.monotonic() < deadline:
                try:
                    updates = self._poll(_WATCH_POLL_SECONDS)
                except TelegramError as exc:
                    errors += 1
                    if errors >= _WAIT_MAX_CONSECUTIVE_ERRORS:
                        watch.error = exc
                        watch.answered.set()
                        return
                    watch.stop.wait(_WAIT_RETRY_PAUSE_S)
                    continue
                errors = 0
                for update in updates:
                    got = self._own_chat(update)
                    if got is None:
                        continue
                    kind, value, cb = got
                    if watch.answered.is_set():  # the first answer already counted
                        if cb is not None:
                            self._answer_cb(cb, f"Already received - {watch.working.lower()}, please wait.")
                        continue
                    result = accept(kind, value)
                    if cb is not None:
                        self._ack_button(cb, result, ack)
                    elif result is not None:  # a typed "yes" / "no": say so in a message
                        try:
                            self.send(ack(result)[0])
                        except TelegramError:
                            pass
                    if result is not None:
                        watch.result = result
                        watch.answered.set()
        except Exception as exc:  # noqa: BLE001 - a bug here must reach the caller, not die silently in a thread
            watch.error = exc
            watch.answered.set()

    def ask_text(self, text: str, timeout_s: float) -> str | None:
        """The user's next typed message, or None if nothing arrived in time."""
        self.drain()
        self.send(text)
        return self._wait(timeout_s, lambda kind, value: value if kind == "text" and value else None)

    def ask_choice(self, text: str, options: list[str], timeout_s: float) -> str | None:
        """Send the options as tappable buttons; returns the chosen option exactly as
        listed (a tap, or the option typed in any case), "/stop" if the user typed
        that, or None on silence."""
        self.drain()
        pid = self._new_prompt()
        keyboard = [[{"text": opt[:60], "callback_data": f"opt:{pid}:{i}"}] for i, opt in enumerate(options)]
        self._transport(
            "sendMessage",
            {"chat_id": self._chat_id, "text": text[:4000], "disable_web_page_preview": True,
             "reply_markup": {"inline_keyboard": keyboard}},
            30,
        )

        def accept(kind: str, value: str) -> str | None:
            if kind == "button":
                parts = value.split(":")
                if len(parts) == 3 and parts[0] == "opt" and parts[1] == self._prompt_id:
                    try:
                        return options[int(parts[2])]
                    except (ValueError, IndexError):
                        return None
                return None  # another question's button: never an answer to this one
            if kind == "text":
                low = value.strip().lower()
                if low == "/stop":
                    return "/stop"
                for opt in options:
                    if low == opt.lower():
                        return opt
            return None

        return self._wait(timeout_s, accept, ack=lambda opt: ("Got it.", "\u2705 " + opt))

    def discover_chat_id(self, timeout_s: float) -> str | None:
        """Setup helper: the chat id of the first person to message the bot."""
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            for update in self._poll(min(_POLL_SECONDS, max(1, int(deadline - self._clock())))):
                chat = (update.get("message") or {}).get("chat") or {}
                if chat.get("id") is not None:
                    return str(chat["id"])
        return None
