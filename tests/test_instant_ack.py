"""A tap on a job card is answered within seconds - "Applying... please wait" - even while the app is busy loading the page.

The app can only read Telegram when it is not driving the browser, which is 10-30 seconds after a card goes out, so people
tapped again, thinking the first tap had not worked. A small background reader now watches the one card that was just sent.
These tests use real threads and a fake Telegram that blocks like the real long-poll, so the timing is the real timing."""

from __future__ import annotations

import queue
import threading
import time

import pytest

from naukri_agent.config import Settings
from naukri_agent.notifications import telegram as tg
from naukri_agent.notifications.telegram import TelegramChannel, TelegramError
from naukri_agent.orchestration.auto_apply_runner import AutoApplyOutcome, _status_line
from naukri_agent.orchestration.telegram_interaction import TelegramInteraction, build_telegram_interaction

CHAT = "4242"
JOB = {"title": "Data Scientist", "company": "Acme", "score": 88.5, "url": "https://www.naukri.com/job-a"}


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(tg, "_WATCH_POLL_SECONDS", 0.05)
    monkeypatch.setattr(tg, "_WAIT_RETRY_PAUSE_S", 0.01)


class Live:
    """A fake Telegram whose getUpdates really blocks (briefly) until something is queued, like the real long-poll."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.calls: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        self.uid = 0
        self.live_id = None
        self.inflight = 0
        self.max_inflight = 0
        self.fail_polls = 0

    def transport(self, method, params, timeout):
        with self.lock:
            self.calls.append((method, params))
            if method == "sendMessage":
                for row in (params.get("reply_markup") or {}).get("inline_keyboard") or []:
                    for b in row:
                        parts = str(b.get("callback_data", "")).split(":")
                        if parts[0] in ("y", "n") and len(parts) == 2:
                            self.live_id = parts[1]
        if method == "getUpdates":
            with self.lock:
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
                failing = self.fail_polls > 0
                if failing:
                    self.fail_polls -= 1
            try:
                if failing:
                    raise TelegramError("getUpdates failed: TimeoutError")
                try:
                    u = self.q.get(timeout=min(params.get("timeout", 0) or 0.01, 0.05))
                except queue.Empty:
                    return {"ok": True, "result": []}
                with self.lock:
                    self.uid += 1
                    return {"ok": True, "result": [{"update_id": self.uid, **u}]}
            finally:
                with self.lock:
                    self.inflight -= 1
        return {"ok": True, "result": {"message_id": 1}}

    def channel(self, instant=True):
        return TelegramChannel("T", CHAT, transport=self.transport, instant_ack=instant)

    def tap(self, which="y", message_id=7, pid=None, chat=CHAT):
        data = f"{which}:{pid or self.live_id}" if which in ("y", "n") else which
        self.q.put({"callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": int(chat)}, "message_id": message_id}}})

    def type(self, text):
        self.q.put({"message": {"chat": {"id": int(CHAT)}, "text": text}})

    def seen(self, method):
        with self.lock:
            return [p for m, p in self.calls if m == method]


def until(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- the point of it all: the tap is answered while the app is still busy --------------------------------------------------


def test_a_yes_is_acknowledged_while_the_app_is_still_busy_and_the_answer_is_kept():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    w.tap("y")  # the app is "loading the page" and has not asked for the answer yet
    assert until(lambda: len(w.seen("editMessageReplyMarkup")) == 1), "the tap was not acknowledged while the app was busy"
    assert w.seen("editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "⏳ Applying… please wait"
    assert w.seen("answerCallbackQuery")[0]["text"] == "Applying… please wait."
    assert ch.wait_yes_no(5) is True  # ...and the answer is still there when the app finally asks
    ch._stop_watch()


def test_a_no_is_acknowledged_the_same_way():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    w.tap("n")
    assert until(lambda: len(w.seen("editMessageReplyMarkup")) == 1)
    assert w.seen("editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "❌ No - skipped"
    assert ch.wait_yes_no(5) is False
    ch._stop_watch()


def test_extra_taps_after_the_first_are_told_it_was_already_received_and_change_nothing():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    w.tap("y")
    assert until(lambda: len(w.seen("editMessageReplyMarkup")) == 1)
    w.tap("y")  # the user, unsure, taps again
    w.tap("n")  # and even the other button
    assert until(lambda: len(w.seen("answerCallbackQuery")) == 3)
    assert [a["text"] for a in w.seen("answerCallbackQuery")][1:] == ["Already received - applying, please wait."] * 2
    assert len(w.seen("editMessageReplyMarkup")) == 1
    assert ch.wait_yes_no(5) is True  # the first answer stands; a later No does not undo it
    ch._stop_watch()


def test_a_typed_yes_is_acknowledged_with_a_message():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    w.type("yes")
    assert until(lambda: any(p["text"] == "Applying… please wait." for p in w.seen("sendMessage")))
    assert ch.wait_yes_no(5) is True
    ch._stop_watch()


def test_silence_still_means_no_and_the_reader_stops():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    thread = ch._watch.thread
    assert ch.wait_yes_no(0.2) is None
    assert until(lambda: not thread.is_alive()) and ch._watch is None


def test_the_confirmation_of_typed_answers_says_submitting():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Submit these answers?", working="Submitting")
    w.tap("y")
    assert until(lambda: len(w.seen("editMessageReplyMarkup")) == 1)
    assert w.seen("editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "⏳ Submitting… please wait"
    ch._stop_watch()


# --- it never answers for the wrong card -----------------------------------------------------------------------------------


def test_a_tap_on_an_old_card_while_the_app_is_busy_is_refused_not_acknowledged_as_applying():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply to A?")
    ch.send_yes_no("Apply to B?")  # the next card replaces the first reader
    old = "abcdef"
    w.tap("y", pid=old)
    assert until(lambda: len(w.seen("answerCallbackQuery")) == 1)
    assert "no longer active" in w.seen("answerCallbackQuery")[0]["text"]
    assert "Expired" in w.seen("editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"][0][0]["text"]
    assert ch.wait_yes_no(0.2) is None  # B was not approved by A's button
    ch._stop_watch()


def test_a_skipped_job_stops_being_listened_for_and_a_later_tap_is_not_acknowledged_as_applying():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    thread = ch._watch.thread
    ch.cancel_prompt()  # the job turned out to have no Apply button
    assert not thread.is_alive()
    pid = w.live_id
    w.tap("y", pid=pid)
    time.sleep(0.2)
    assert w.seen("editMessageReplyMarkup") == [] and w.seen("answerCallbackQuery") == []  # nobody is listening, nothing is claimed
    # ...and when the next prompt clears the queue, the tap is told it is old
    ch.send_yes_no("Apply to the next one?")
    assert until(lambda: any("Already handled" in a["text"] for a in w.seen("answerCallbackQuery")))
    assert ch.wait_yes_no(0.2) is None
    ch._stop_watch()


def test_other_chats_are_never_acknowledged():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    w.tap("y", chat="999")
    time.sleep(0.2)
    assert w.seen("answerCallbackQuery") == [] and w.seen("editMessageReplyMarkup") == []
    assert ch.wait_yes_no(0.2) is None


# --- only one reader of Telegram at a time -------------------------------------------------------------------------------------


def test_the_reader_and_the_apps_own_questions_never_read_telegram_at_the_same_time():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    w.tap("y")
    assert ch.wait_yes_no(5) is True  # the reader stays on, listening for extra taps
    assert ch.ask_choice("Question 1?", ["Yes", "No"], 0.3) is None  # asking stops the reader first, then reads itself
    ch.send_yes_no("Apply to the next?")
    assert ch.wait_yes_no(0.2) is None
    assert w.max_inflight == 1, "two getUpdates were in flight at once - Telegram would refuse the second"


def test_nothing_reads_telegram_after_the_run_stops_listening():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    ch.drain()
    before = len(w.seen("getUpdates"))
    time.sleep(0.2)
    assert len(w.seen("getUpdates")) == before


# --- failures ----------------------------------------------------------------------------------------------------------------


def test_a_dropped_connection_while_the_reader_listens_is_survived():
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    w.fail_polls = 3  # three polls by the reader now fail; it must keep listening and still hear the tap
    w.tap("y")
    assert ch.wait_yes_no(5) is True
    ch._stop_watch()


def test_a_connection_that_stays_down_reaches_the_caller_as_an_error(monkeypatch):
    monkeypatch.setattr(tg, "_WAIT_MAX_CONSECUTIVE_ERRORS", 3)
    w = Live()
    ch = w.channel()
    ch.send_yes_no("Apply?")
    w.fail_polls = 10_000
    with pytest.raises(TelegramError):
        ch.wait_yes_no(5)


def test_the_old_behaviour_is_kept_when_instant_ack_is_off():
    w = Live()
    ch = w.channel(instant=False)
    ch.send_yes_no("Apply?")
    assert ch._watch is None
    w.tap("y")
    time.sleep(0.15)
    assert w.seen("editMessageReplyMarkup") == []  # nobody is listening until the app asks, as before
    assert ch.wait_yes_no(5) is True


# --- the interaction and the messages around it --------------------------------------------------------------------------------


def test_the_interaction_cancels_a_skipped_card_and_its_card_text_says_what_will_happen():
    w = Live()
    ia = TelegramInteraction(w.channel(), Settings(_env_file=None, telegram_approval_timeout_minutes=1))
    ia.send_approval(JOB)
    body = w.seen("sendMessage")[0]["text"]
    assert "Applying... please wait" in body and "Data Scientist" in body
    thread = ia._ch._watch.thread
    ia.cancel_approval()
    assert not thread.is_alive()


def test_a_fake_channel_without_cancel_is_fine():
    class Plain:
        def send_yes_no(self, text, **kw):
            pass

    TelegramInteraction(Plain(), Settings(_env_file=None)).cancel_approval()  # must not raise


def test_the_switch_in_the_settings_builds_the_right_channel():
    on = build_telegram_interaction(Settings(_env_file=None, telegram_bot_token="t", telegram_chat_id="1"))
    off = build_telegram_interaction(Settings(_env_file=None, telegram_bot_token="t", telegram_chat_id="1", telegram_instant_ack=False))
    assert on._ch._instant_ack is True and off._ch._instant_ack is False


def test_a_no_gets_a_skipped_message_and_applied_keeps_its_usual_message():
    o = AutoApplyOutcome(job_id=1, title="Data Scientist", company="Acme", url="u", outcome="declined", detail="you said no")
    assert _status_line(o) == "Skipped: Data Scientist - Acme - you said no. Nothing was applied."
    applied = AutoApplyOutcome(job_id=1, title="Data Scientist", company="Acme", url="u", outcome="applied", detail="",
                               resume_id="data_scientist")
    assert _status_line(applied) == "Applied: Data Scientist - Acme\nResume: data_scientist"
