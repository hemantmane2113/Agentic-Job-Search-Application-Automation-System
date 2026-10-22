"""A button belongs to ONE prompt. A second tap on a card that was already answered (the app takes a few seconds to react,
so people tap again), or a tap on an older job's card, must never answer the next prompt.

Found 2026-10-21: Eduitalent was "approved" 27 seconds after the previous job finished, and AXA 14 seconds after
Assertive - too fast for anyone to read a card. The buttons carried only "y" / "n", so a late duplicate Yes that
arrived after the next card was sent counted as approval of that next job."""

from __future__ import annotations

from naukri_agent.config import Settings
from naukri_agent.orchestration.telegram_interaction import TelegramInteraction

from .test_telegram import World, button, text

JOB_A = {"title": "Data Scientist", "company": "Acme", "score": 88.5, "url": "https://www.naukri.com/job-a"}
JOB_B = {"title": "ML Engineer", "company": "Beta", "score": 90.0, "url": "https://www.naukri.com/job-b"}


def interaction(world):
    return TelegramInteraction(world.channel(), Settings(_env_file=None, telegram_approval_timeout_minutes=1,
                                                         telegram_answer_timeout_minutes=1))


def methods(w, name):
    return [p for m, p in w.calls if m == name]


# --- the heart of it: a duplicate Yes cannot approve the NEXT job --------------------------------------------------------


def test_a_second_yes_on_the_previous_card_does_not_approve_the_next_job():
    w = World([[], [button("y")]])
    ia = interaction(w)
    ia.send_approval(JOB_A)
    assert ia.wait_approval() is True  # the first tap counts
    old_id = w.live_id
    ia.send_approval(JOB_B)  # the next card goes out; the user, unsure the first tap worked, taps card A again
    assert w.live_id != old_id
    w.batches.append([button(f"y:{old_id}", stale=True)])
    assert ia.wait_approval() is None  # silence: the next job is NOT approved by the old card


def test_the_next_job_is_approved_only_by_its_own_card():
    w = World([[], [button("y")]])
    ia = interaction(w)
    ia.send_approval(JOB_A)
    assert ia.wait_approval() is True
    old_id = w.live_id
    ia.send_approval(JOB_B)
    w.batches.append([button(f"y:{old_id}", stale=True), button("y")])  # an old tap, then a real tap on card B
    assert ia.wait_approval() is True


def test_a_card_from_before_a_restart_with_the_old_bare_buttons_answers_nothing():
    w = World([[], [button("y", stale=True)]])  # the old format: just "y", no prompt id
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert ch.wait_yes_no(60) is None


def test_a_tap_on_an_old_question_does_not_answer_the_next_question():
    w = World([[], [button("opt:0")]])
    ch = w.channel()
    assert ch.ask_choice("Q1?", ["Yes", "No"], 60) == "Yes"
    q1_id = w.live_id
    ch.drain()
    w.batches.append([button(f"opt:{q1_id}:1", stale=True)])  # a late second tap on question 1 ("No")
    assert ch.ask_choice("Q2?", ["Immediate", "15 days", "30 days"], 60) is None  # not taken as an answer to Q2


def test_typed_answers_still_work_after_a_stale_tap():
    w = World([[], [button("y", stale=True), text("yes")]])
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert ch.wait_yes_no(60) is True


# --- what the user sees --------------------------------------------------------------------------------------------------


def test_an_answered_card_turns_into_one_grey_applying_button_and_the_tap_is_acknowledged():
    w = World([[], [button("y", message_id=77)]])
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert ch.wait_yes_no(60) is True
    ack = methods(w, "answerCallbackQuery")
    assert len(ack) == 1 and ack[0]["text"] == "Applying\u2026 please wait."
    edit = methods(w, "editMessageReplyMarkup")
    assert len(edit) == 1 and edit[0]["message_id"] == 77
    row = edit[0]["reply_markup"]["inline_keyboard"]
    assert len(row) == 1 and len(row[0]) == 1  # no Yes/No left to tap again
    assert row[0][0]["text"] == "\u23f3 Applying\u2026 please wait"


def test_a_no_is_shown_as_skipped():
    w = World([[], [button("n", message_id=5)]])
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert ch.wait_yes_no(60) is False
    assert methods(w, "editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "\u274c No - skipped"
    assert methods(w, "answerCallbackQuery")[0]["text"] == "Skipped."


def test_a_chosen_option_is_shown_on_the_card():
    w = World([[], [button("opt:1", message_id=9)]])
    assert w.channel().ask_choice("Notice period?", ["Immediate", "15 days"], 60) == "15 days"
    assert "15 days" in methods(w, "editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"][0][0]["text"]


def test_a_tap_on_an_old_card_is_told_so_and_that_cards_buttons_are_switched_off():
    w = World([[], [button("y:abcdef", stale=True, message_id=3)]])
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert ch.wait_yes_no(30) is None
    ack = methods(w, "answerCallbackQuery")
    assert len(ack) == 1 and "no longer active" in ack[0]["text"]
    edit = methods(w, "editMessageReplyMarkup")
    assert len(edit) == 1 and edit[0]["message_id"] == 3 and "Expired" in edit[0]["reply_markup"]["inline_keyboard"][0][0]["text"]


def test_two_taps_in_the_same_batch_count_once_and_the_second_is_told_it_was_received():
    w = World([[], [button("y", message_id=1), button("y", message_id=1)]])
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert ch.wait_yes_no(60) is True
    ack = [a["text"] for a in methods(w, "answerCallbackQuery")]
    assert ack == ["Applying\u2026 please wait.", "Already received - it is being processed."]
    assert len(methods(w, "editMessageReplyMarkup")) == 1


def test_tapping_the_grey_received_button_only_says_it_was_received():
    w = World([[], [button("done", message_id=1)]])
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert ch.wait_yes_no(30) is None
    assert "Already received" in methods(w, "answerCallbackQuery")[0]["text"]
    assert methods(w, "editMessageReplyMarkup") == []  # nothing to switch off


def test_buttons_tapped_while_the_app_was_busy_are_answered_when_the_next_prompt_clears_the_queue():
    w = World([[button("y", stale=True)], [], []])  # a tap is waiting when the next prompt is sent
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert [a["text"] for a in methods(w, "answerCallbackQuery")] == ["Already handled - use the newest message."]
    assert ch.wait_yes_no(30) is None


def test_other_chats_are_still_never_answered_or_edited():
    w = World([[], [button("y", chat="999", message_id=1)]])
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert ch.wait_yes_no(30) is None
    assert methods(w, "answerCallbackQuery") == [] and methods(w, "editMessageReplyMarkup") == []


def test_a_failing_acknowledgement_never_breaks_the_answer():
    from naukri_agent.notifications.telegram import TelegramChannel, TelegramError

    w = World([[], [button("y", message_id=1)]])
    real = w.transport

    def transport(method, params, timeout):
        if method in ("answerCallbackQuery", "editMessageReplyMarkup"):
            raise TelegramError(f"{method} failed: HTTPError")
        return real(method, params, timeout)

    ch = TelegramChannel("T", "4242", transport=transport, clock=w.clock)
    ch.send_yes_no("Apply?")
    assert ch.wait_yes_no(60) is True


def test_the_job_card_tells_the_user_to_tap_once():
    w = World([[], [button("y")]])
    interaction(w).approve_job(JOB_A)
    body = w.sent()[0]["text"]
    assert "Tap Yes or No once" in body and "Applying... please wait" in body
