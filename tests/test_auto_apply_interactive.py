"""telegram-apply: a human approves every job, answers every question and confirms
before any submit. Silence is never a yes. Fake browser, fake interaction."""

from __future__ import annotations

import pytest

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import ApplicationHistory, ApplicationQuestion, AutoApplyAttempt
from naukri_agent.orchestration.auto_apply_runner import check_gates, run_auto_apply

from .test_auto_apply_runner import NOW, FakeClient, _patch_profile, cfg, opener, q, seed  # noqa: F401 - autouse fixture


class FakeHuman:
    """Scripted Telegram user. None == silence."""

    def __init__(self, approvals=(True,), answers=(), confirms=(True,)):
        self.approvals, self.answers, self.confirms = list(approvals), list(answers), list(confirms)
        self.asked, self.suggestions, self.confirmed_with, self.notes = [], [], [], []
        self.options_seen = []
        self.events = []

    def send_approval(self, job):
        self.asked.append(job["title"])
        self.events.append(("prompt", job["title"]))

    def wait_approval(self):
        return self.approvals.pop(0) if self.approvals else None

    def approve_job(self, job):
        self.send_approval(job)
        return self.wait_approval()

    def ask_question(self, index, total, text, suggestion, options=None):
        self.suggestions.append(suggestion)
        self.options_seen.append(options)
        return self.answers.pop(0) if self.answers else None

    def confirm_submit(self, job, questions, answers):
        self.confirmed_with.append((list(questions), list(answers)))
        return self.confirms.pop(0) if self.confirms else None

    def notify(self, text):
        self.notes.append(text)


def go(c, factory, fake, human):
    return run_auto_apply(c, session_factory=factory, open_client=opener(fake), now=NOW, interaction=human)


def test_interactive_mode_needs_only_two_switches_the_human_is_the_third():
    assert check_gates(cfg_off := _cfg_unattended_off(), interactive=True) is None
    assert "AUTO_APPLY_UNATTENDED" in check_gates(cfg_off)  # unattended still demands all three


def _cfg_unattended_off():
    from tests.digest_fakes import settings
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    return settings(d, dry_run=False, auto_apply=True, auto_apply_unattended=False,
                    auto_apply_pause_file=d / "PAUSE")


def test_dry_run_still_blocks_even_interactively(tmp_path):
    c = cfg(tmp_path, dry_run=True)
    assert "DRY_RUN=false" in check_gates(c, interactive=True)


def test_no_click_until_the_human_says_yes_and_a_no_means_nothing_happens(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("a", "040926002001", 90.0, {})])
    fake = FakeClient({urls["a"]: {}})
    r = go(c, factory, fake, FakeHuman(approvals=[False]))
    assert r.outcomes[0].outcome == "declined" and fake.clicked == [] and fake.submitted_urls == []


def test_a_yes_on_a_questionless_job_applies_and_tells_you(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("a", "040926002101", 90.0, {})])
    fake, human = FakeClient({urls["a"]: {}}), FakeHuman(approvals=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and fake.submitted_urls == [urls["a"]]
    assert human.confirmed_with == []  # no questions -> no second confirmation needed
    assert any(n.startswith("Applied:") for n in human.notes)
    with session_scope(factory) as s:
        assert s.query(ApplicationHistory).count() == 1


def test_silence_on_the_approval_stops_the_run_and_nothing_is_clicked(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("a", "040926002201", 95.0, {}), ("b", "040926002202", 90.0, {})])
    fake = FakeClient({u: {} for u in urls.values()})
    r = go(c, factory, fake, FakeHuman(approvals=[]))  # never answers
    assert r.outcomes[0].outcome == "no_reply" and "silence" in r.stopped_reason
    assert fake.clicked == [] and fake.opened == [urls["a"]]  # second job never even opened
    with session_scope(factory) as s:
        assert s.query(AutoApplyAttempt).one().outcome == "no_reply"


def test_the_humans_answers_are_typed_then_confirmed_then_submitted(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("qa", "040926002301", 90.0, {})])
    fake = FakeClient({urls["qa"]: {"questions": [q("What is your current CTC?", "q1"),
                                                  q("What is your notice period?", "q2")]}})
    human = FakeHuman(approvals=[True], answers=["5.5 LPA", "Immediate"], confirms=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1
    assert human.suggestions == [None, "Immediate"]  # CTC: no suggestion; notice: profile suggestion offered
    assert [a[2] for a in fake.answered] == ["5.5 LPA", "Immediate"]
    assert human.confirmed_with == [(["What is your current CTC?", "What is your notice period?"],
                                     ["5.5 LPA", "Immediate"])]
    with session_scope(factory) as s:
        rows = s.query(ApplicationQuestion).order_by(ApplicationQuestion.order_in_attempt).all()
        assert [x.final_answer for x in rows] == ["5.5 LPA", "Immediate"]


def test_declining_the_final_confirmation_types_nothing_and_submits_nothing(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("qa", "040926002401", 90.0, {})])
    fake = FakeClient({urls["qa"]: {"questions": [q("What is your notice period?")]}})
    r = go(c, factory, fake, FakeHuman(answers=["Immediate"], confirms=[False]))
    assert r.outcomes[0].outcome == "declined"
    assert fake.answered == [] and fake.submitted_urls == []


def test_silence_on_the_final_confirmation_is_not_a_yes(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("qa", "040926002501", 90.0, {})])
    fake = FakeClient({urls["qa"]: {"questions": [q("What is your notice period?")]}})
    r = go(c, factory, fake, FakeHuman(answers=["Immediate"], confirms=[]))
    assert r.outcomes[0].outcome == "no_reply" and fake.submitted_urls == []


def test_giving_up_on_a_question_parks_the_job_and_submits_nothing(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("qa", "040926002601", 90.0, {}), ("ok", "040926002602", 85.0, {})])
    fake = FakeClient({urls["qa"]: {"questions": [q("What is your current CTC?")]}, urls["ok"]: {}})
    human = FakeHuman(approvals=[True, True], answers=[])  # /stop or silence on the question
    r = go(c, factory, fake, human)
    assert [o.outcome for o in r.outcomes] == ["needs_human", "applied"]
    assert fake.answered == [] and fake.submitted_urls == [urls["ok"]]
    assert any("NOT submitted" in n for n in human.notes)


@pytest.mark.parametrize("fields", [3, -1])
def test_an_unreadable_screen_is_handed_back_even_when_a_human_is_present(tmp_path, fields):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("f", "040926002701", 90.0, {})])
    fake = FakeClient({urls["f"]: {"fields": fields}})
    r = go(c, factory, fake, FakeHuman())
    assert r.outcomes[0].outcome == "needs_human" and fake.submitted_urls == []


def test_a_no_reply_job_is_offered_again_the_next_day_but_a_declined_one_is_not(tmp_path):
    import datetime

    from naukri_agent.database.repositories import auto_apply_job_ids_to_skip

    c = cfg(tmp_path)
    factory, urls = seed(c, [("a", "040926002801", 90.0, {}), ("b", "040926002802", 85.0, {})])
    go(c, factory, FakeClient({u: {} for u in urls.values()}), FakeHuman(approvals=[False, None]))
    real_now = datetime.datetime.now(datetime.UTC)  # the attempts above are stamped with the real clock, so measure from it
    with session_scope(factory) as s:
        today = auto_apply_job_ids_to_skip(s, real_now)
        tomorrow = auto_apply_job_ids_to_skip(s, real_now + datetime.timedelta(days=2))
    assert len(today) == 2 and len(tomorrow) == 1  # only the declined one stays blocked
