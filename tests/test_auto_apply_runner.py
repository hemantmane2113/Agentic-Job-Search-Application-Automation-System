"""Unattended auto-apply: gates, selection, caps, question handling, and the
rule that anything uncertain submits NOTHING. Fake browser client throughout."""

from __future__ import annotations

import datetime
from contextlib import contextmanager

import pytest

from naukri_agent.browser.models import ApplyQuestionPrompt, ApplySubmissionResult
from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import (
    ApplicationHistory,
    ApplicationQuestion,
    AutoApplyAttempt,
    Job,
    JobExtraction,
    JobMatch,
)
from naukri_agent.database.repositories import upsert_application_history, upsert_candidate
from naukri_agent.matching.models import ExperienceProfile, MatchDecision
from naukri_agent.orchestration.auto_apply_runner import check_gates, run_auto_apply
from naukri_agent.resume.models import MasterResume

from .digest_fakes import add_job, score, settings

CAND = CandidateProfile(
    full_name="X", email="x@example.com", phone="1", skills=["Python", "SQL"],
    years_experience=3.0, notice_period_days=0, preferred_locations=["Pune"],
)
EXP = ExperienceProfile(total_years=3.0, skill_years={"python": 3.1})
NOW = datetime.datetime(2026, 10, 8, 12, 0, tzinfo=datetime.UTC)


class FakeClient:
    """pages: url -> dict(type, questions, fields, submitted, raise_on_click)."""

    def __init__(self, pages):
        self.pages = pages
        self.url = None
        self.opened, self.clicked, self.answered, self.submitted_urls, self.shots = [], [], [], [], []

    def open_job_page(self, url):
        self.url = url
        self.opened.append(url)

    def _p(self):
        return self.pages[self.url]

    def detect_apply_type(self):
        return self._p().get("type", "native")

    def prepare_next_application(self):
        pass

    def click_apply(self):
        if self._p().get("raise_on_click"):
            raise RuntimeError("page changed")
        self.clicked.append(self.url)

    def list_questions(self):
        return list(self._p().get("questions", []))

    def application_question_field_count(self):
        return self._p().get("fields", 0)

    def submit_answer(self, control_id, answer):
        self.answered.append((self.url, control_id, answer))

    def screenshot(self, path):
        self.shots.append(str(path))
        return True

    def submit_application(self):
        self.submitted_urls.append(self.url)
        ok = self._p().get("submitted", True)
        return ApplySubmissionResult(submitted=ok, notes=["confirmed: test"] if ok else ["no confirmation"])


def opener(fake):
    @contextmanager
    def _open(_settings):
        yield fake

    return _open


def cfg(tmp_path, **over):
    base = dict(
        dry_run=False, auto_apply=True, auto_apply_unattended=True,
        auto_apply_pause_file=tmp_path / "PAUSE", inspection_output_dir=tmp_path / "insp",
        email_sender="file", threshold_review=50, auto_apply_daily_cap=3,
    )
    base.update(over)
    return settings(tmp_path, **base)


@pytest.fixture(autouse=True)
def _patch_profile(monkeypatch):
    monkeypatch.setattr("naukri_agent.candidate.models.load_candidate_profile", lambda *_: CAND)
    monkeypatch.setattr(
        "naukri_agent.resume.models.load_master_resume", lambda *_: MasterResume(professional_summary="s")
    )
    monkeypatch.setattr("naukri_agent.matching.experience_matcher.build_experience_profile", lambda *_: EXP)


def seed(cfg_, specs):
    """specs: (slug, ext, score, kwargs). Returns (factory, {slug: url})."""
    factory = init_db(cfg_)
    urls = {}
    with session_scope(factory) as s:
        cand_row = upsert_candidate(s, CAND)
        s.flush()
        cid = cand_row.id
        for slug, ext, sc, kw in specs:
            job = add_job(s, slug=slug, ext=ext, title=f"{slug} role", company="Acme")
            job.apply_type = kw.get("apply_type", "native")
            job.last_seen_at = kw.get("last_seen", NOW)
            score(s, cid, job.id, overall=sc, decision=kw.get("decision", MatchDecision.ACCEPT))
            if kw.get("extraction", True):
                ex = JobExtraction(job_id=job.id, extraction_version=1, is_current=True)
                s.add(ex)
                s.flush()
                s.query(JobMatch).filter_by(job_id=job.id).one().job_extraction_id = ex.id
            urls[slug] = job.url
    return factory, urls


def run(cfg_, factory, fake, **kw):
    return run_auto_apply(cfg_, session_factory=factory, open_client=opener(fake), now=NOW, **kw)


def q(text, cid="q1"):
    return ApplyQuestionPrompt(control_id=cid, question_text=text)


# --- gates ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "over,needle",
    [
        ({"auto_apply": False}, "AUTO_APPLY=true"),
        ({"dry_run": True}, "DRY_RUN=false"),
        ({"auto_apply_unattended": False}, "AUTO_APPLY_UNATTENDED=true"),
    ],
)
def test_every_switch_is_required_and_nothing_is_opened_without_it(tmp_path, over, needle):
    c = cfg(tmp_path, **over)
    assert needle in check_gates(c)
    called = []

    @contextmanager
    def boom(_s):
        called.append(1)
        yield None

    r = run_auto_apply(c, session_factory=init_db(c), open_client=boom, now=NOW)
    assert r.blocked_reason and not called and not r.outcomes


def test_pause_file_stops_everything(tmp_path):
    c = cfg(tmp_path)
    (tmp_path / "PAUSE").write_text("stop")
    assert "paused" in check_gates(c)


# --- the happy path ----------------------------------------------------------------


def test_applies_to_a_question_free_native_job_records_it_and_emails_a_summary(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("a", "040926000501", 90.0, {})])
    fake = FakeClient({urls["a"]: {}})
    r = run(c, factory, fake)
    assert r.applied == 1 and r.outcomes[0].outcome == "applied"
    assert fake.clicked == [urls["a"]] and fake.submitted_urls == [urls["a"]]
    assert len(fake.shots) == 2  # before and after submit
    with session_scope(factory) as s:
        hist = s.query(ApplicationHistory).one()
        assert hist.source == "agent_auto_apply_unattended"
        assert s.query(AutoApplyAttempt).one().outcome == "applied"
    sent = list((tmp_path / "emails").glob("digest_*.txt"))
    assert sent and "1 applied" in sent[0].read_text(encoding="utf-8")


def test_daily_cap_limits_applications_and_best_scores_go_first(tmp_path):
    c = cfg(tmp_path, auto_apply_daily_cap=3)
    factory, urls = seed(c, [(f"j{i}", f"04092600060{i}", 80.0 + i, {}) for i in range(5)])
    fake = FakeClient({u: {} for u in urls.values()})
    r = run(c, factory, fake)
    assert r.applied == 3
    assert fake.submitted_urls == [urls["j4"], urls["j3"], urls["j2"]]


def test_cap_already_used_in_the_last_24h_means_nothing_is_opened(tmp_path):
    c = cfg(tmp_path, auto_apply_daily_cap=1)
    factory, urls = seed(c, [("a", "040926000701", 90.0, {}), ("b", "040926000702", 85.0, {})])
    run(c, factory, FakeClient({u: {} for u in urls.values()}))  # uses the one allowed application
    fake2 = FakeClient({u: {} for u in urls.values()})
    r = run(c, factory, fake2)
    assert r.stopped_reason == "daily cap already reached" and fake2.opened == []


# --- who is eligible ------------------------------------------------------------------


def test_only_accept_native_recent_unapplied_untried_parsed_jobs_are_eligible(tmp_path):
    c = cfg(tmp_path)
    old = NOW - datetime.timedelta(days=30)
    factory, urls = seed(
        c,
        [
            ("good", "040926000801", 90.0, {}),
            ("review", "040926000802", 75.0, {"decision": MatchDecision.REVIEW}),
            ("company", "040926000803", 95.0, {"apply_type": "company_site"}),
            ("unknown", "040926000804", 95.0, {"apply_type": None}),
            ("stale", "040926000805", 95.0, {"last_seen": old}),
            ("noparse", "040926000806", 95.0, {"extraction": False}),
            ("applied", "040926000807", 95.0, {}),
        ],
    )
    with session_scope(factory) as s:
        upsert_application_history(s, s.query(Job).filter(Job.url == urls["applied"]).one().id)
    fake = FakeClient({u: {} for u in urls.values()})
    r = run(c, factory, fake)
    assert fake.opened == [urls["good"]] and r.candidates_considered == 1


# --- questions -------------------------------------------------------------------------


def test_an_unanswerable_question_submits_nothing_parks_the_job_and_moves_on(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("ctc", "040926000901", 95.0, {}), ("easy", "040926000902", 90.0, {})])
    fake = FakeClient({urls["ctc"]: {"questions": [q("What is your current CTC?")]}, urls["easy"]: {}})
    r = run(c, factory, fake)
    assert [o.outcome for o in r.outcomes] == ["needs_human", "applied"]
    assert fake.answered == [] and fake.submitted_urls == [urls["easy"]]
    assert r.outcomes[0].questions == ["What is your current CTC?"]
    with session_scope(factory) as s:
        row = s.query(ApplicationQuestion).filter(ApplicationQuestion.question_text.like("%CTC%")).one()
        assert row.final_answer is None
    again = run(c, factory, FakeClient({u: {} for u in urls.values()}))  # never retried
    assert again.candidates_considered == 0


def test_one_unanswerable_question_among_answerable_ones_still_submits_nothing(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("mix", "040926001001", 90.0, {})])
    fake = FakeClient(
        {urls["mix"]: {"questions": [q("What is your notice period?", "q1"), q("Do you hold a PhD?", "q2")]}}
    )
    r = run(c, factory, fake)
    assert r.outcomes[0].outcome == "needs_human" and fake.answered == [] and fake.submitted_urls == []


def test_answerable_questions_are_typed_from_the_profile_then_submitted_and_saved(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("qa", "040926001101", 90.0, {})])
    fake = FakeClient(
        {
            urls["qa"]: {
                "questions": [
                    q("How many years of experience do you have in Python?", "q1"),
                    q("What is your notice period?", "q2"),
                ]
            }
        }
    )
    r = run(c, factory, fake)
    assert r.applied == 1
    assert [a[2] for a in fake.answered] == ["3.1", "Immediate"]
    with session_scope(factory) as s:
        rows = s.query(ApplicationQuestion).order_by(ApplicationQuestion.order_in_attempt).all()
        assert [x.final_answer for x in rows] == ["3.1", "Immediate"]
        assert all(x.application_id is not None for x in rows)


@pytest.mark.parametrize("fields", [2, -1])
def test_unreadable_input_fields_on_a_questionless_screen_park_the_job(tmp_path, fields):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("f", "040926001201", 90.0, {})])
    fake = FakeClient({urls["f"]: {"fields": fields}})
    r = run(c, factory, fake)
    assert r.outcomes[0].outcome == "needs_human" and fake.submitted_urls == []


# --- failure handling -------------------------------------------------------------------


def test_unconfirmed_submit_stops_the_run_and_is_not_recorded_as_applied(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("u", "040926001301", 95.0, {}), ("v", "040926001302", 90.0, {})])
    fake = FakeClient({urls["u"]: {"submitted": False}, urls["v"]: {}})
    r = run(c, factory, fake)
    assert r.outcomes[0].outcome == "unconfirmed" and "stopped" in r.stopped_reason
    assert fake.opened == [urls["u"]]  # the second job was never touched
    with session_scope(factory) as s:
        assert s.query(ApplicationHistory).count() == 0


def test_any_exception_stops_the_run_and_the_summary_flags_it(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("e", "040926001401", 95.0, {}), ("g", "040926001402", 90.0, {})])
    fake = FakeClient({urls["e"]: {"raise_on_click": True}, urls["g"]: {}})
    r = run(c, factory, fake)
    assert r.outcomes[0].outcome == "failed" and fake.opened == [urls["e"]]
    body = list((tmp_path / "emails").glob("digest_*.txt"))[0].read_text(encoding="utf-8")
    assert "PROBLEMS" in body


def test_a_job_that_lost_its_apply_button_is_skipped_without_clicking(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("n", "040926001501", 90.0, {})])
    fake = FakeClient({urls["n"]: {"type": "company_site"}})
    r = run(c, factory, fake)
    assert r.outcomes[0].outcome == "not_native" and fake.clicked == []


def test_auto_apply_is_unreachable_from_the_daily_pipeline_discovery_and_scheduler():
    import inspect

    from naukri_agent.orchestration import discovery, pipeline
    from naukri_agent.scheduler import daemon

    for mod in (pipeline, discovery, daemon):
        assert "auto_apply_runner" not in inspect.getsource(mod)
