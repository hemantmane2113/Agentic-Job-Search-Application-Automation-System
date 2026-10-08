"""Telegram channel + interaction: only our chat is heard, stale replies can't
answer new prompts, silence is None, and the token never leaks."""

from __future__ import annotations

import urllib.error

import pytest

from naukri_agent.config import Settings
from naukri_agent.notifications import telegram as tg
from naukri_agent.notifications.telegram import TelegramChannel, TelegramError
from naukri_agent.orchestration.telegram_interaction import TelegramInteraction, build_telegram_interaction

CHAT = "4242"


class World:
    """Fake Telegram: scripted getUpdates batches + a virtual clock."""

    def __init__(self, batches=()):
        self.batches = list(batches)
        self.now = 0.0
        self.calls = []
        self.uid = 0

    def clock(self):
        return self.now

    def transport(self, method, params, timeout):
        self.calls.append((method, params))
        if method == "getUpdates":
            self.now += params.get("timeout", 0) or 0.01
            batch = self.batches.pop(0) if self.batches else []
            out = []
            for u in batch:
                self.uid += 1
                out.append({"update_id": self.uid, **u})
            return {"ok": True, "result": out}
        return {"ok": True, "result": {}}

    def channel(self):
        return TelegramChannel("TOKEN", CHAT, transport=self.transport, clock=self.clock)

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


def text(t, chat=CHAT):
    return {"message": {"chat": {"id": int(chat)}, "text": t}}


def button(data, chat=CHAT):
    return {"callback_query": {"id": "cb1", "data": data, "message": {"chat": {"id": int(chat)}}}}


def test_button_press_yes_returns_true_and_the_prompt_has_yes_no_buttons():
    w = World([[], [button("y")]])  # drain finds nothing, then the tap arrives
    assert w.channel().ask_yes_no("Apply?", 60) is True
    prompt = w.sent()[0]
    assert prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "y"
    assert any(m == "answerCallbackQuery" for m, _ in w.calls)


@pytest.mark.parametrize("reply,expected", [("Y", True), ("yes", True), ("n", False), ("No", False)])
def test_typed_yes_no_is_understood(reply, expected):
    w = World([[], [text(reply)]])
    assert w.channel().ask_yes_no("Apply?", 60) is expected


def test_messages_from_any_other_chat_are_ignored_so_silence_means_none():
    w = World([[], [text("y", chat="999")], [button("y", chat="999")]])
    assert w.channel().ask_yes_no("Apply?", 60) is None


def test_a_stale_reply_sent_before_the_prompt_cannot_answer_it():
    w = World([[text("y")], [], []])  # an old "y" is already waiting when we ask
    assert w.channel().ask_yes_no("Apply?", 60) is None


def test_no_reply_in_time_is_none_never_consent():
    assert World().channel().ask_yes_no("Apply?", 90) is None
    assert World().channel().ask_text("Question?", 90) is None


def test_unclear_text_gets_one_gentle_nudge_then_a_real_answer_counts():
    w = World([[], [text("maybe")], [text("what?")], [text("y")]])
    assert w.channel().ask_yes_no("Apply?", 300) is True
    nudges = [p for p in w.sent() if "Please answer" in p["text"]]
    assert len(nudges) == 1


def test_ask_text_returns_the_next_typed_message_and_ignores_buttons():
    w = World([[], [button("y")], [text("3 years")]])
    assert w.channel().ask_text("How many years?", 120) == "3 years"


def test_discover_chat_id_finds_the_first_sender():
    w = World([[text("hello", chat="777")]])
    assert w.channel().discover_chat_id(60) == "777"


def test_transport_errors_never_leak_the_bot_token(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("https://api.telegram.org/botSECRETTOKEN123/getUpdates failed")

    monkeypatch.setattr(tg.urllib.request, "urlopen", boom)
    with pytest.raises(TelegramError) as exc:
        tg.http_transport("SECRETTOKEN123")("getUpdates", {}, 5)
    assert "SECRETTOKEN123" not in str(exc.value) and "URLError" in str(exc.value)


# --- the interaction wording ---------------------------------------------------------------


def interaction(world):
    return TelegramInteraction(world.channel(), Settings(_env_file=None, telegram_approval_timeout_minutes=1,
                                                         telegram_answer_timeout_minutes=1))


JOB = {"title": "Data Scientist", "company": "Acme", "score": 88.5, "url": "https://www.naukri.com/job-x"}


def test_approve_job_message_names_the_job_and_carries_the_link():
    w = World([[], [button("n")]])
    assert interaction(w).approve_job(JOB) is False
    body = w.sent()[0]["text"]
    assert "Data Scientist" in body and "Acme" in body and "88.5" in body and JOB["url"] in body


def test_question_dot_accepts_the_suggestion_and_own_text_overrides_it():
    w = World([[], [text(".")]])
    assert interaction(w).ask_question(1, 2, "Notice period?", "Immediate") == "Immediate"
    assert "Immediate" in w.sent()[0]["text"] and "1/2" in w.sent()[0]["text"]
    w2 = World([[], [text("15 days")]])
    assert interaction(w2).ask_question(1, 1, "Notice period?", "Immediate") == "15 days"


def test_question_without_a_suggestion_says_so_and_dot_is_just_an_answer():
    w = World([[], [text(".")]])
    assert interaction(w).ask_question(1, 1, "Current CTC?", None) == "."
    assert "no answer for this one" in w.sent()[0]["text"]


def test_stop_and_silence_both_give_up_on_a_question():
    assert interaction(World([[], [text("/stop")]])).ask_question(1, 1, "Q?", None) is None
    assert interaction(World()).ask_question(1, 1, "Q?", None) is None


def test_confirm_submit_shows_every_question_and_answer():
    w = World([[], [button("y")]])
    assert interaction(w).confirm_submit(JOB, ["Notice?", "Years?"], ["Immediate", "3"]) is True
    body = w.sent()[0]["text"]
    assert "Q: Notice?" in body and "A: Immediate" in body and "A: 3" in body


def test_build_requires_both_token_and_chat_id():
    with pytest.raises(ValueError):
        build_telegram_interaction(Settings(_env_file=None, telegram_bot_token="t"))
