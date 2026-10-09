"""Part 3 of the digest: company-website jobs from the last few days and where each one stands (waiting for your
answer, applied through company website, not applied, ignored), not counting today's Part 1."""

from __future__ import annotations

import datetime
import pathlib

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import ApplicationStatus
from naukri_agent.database.repositories import record_followup_answer, upsert_application_history
from naukri_agent.notifications.render import render_digest
from naukri_agent.orchestration.pipeline import run_daily_recommendations
from naukri_agent.recommendations.builder import build_digest, company_site_status_since

from .digest_fakes import add_job, in_memory_factory, make_candidate, make_run, score, select_static_resume
from .test_daily_pipeline import _VALID_EXTRACTION, _fake_discover, _PerJobLLM, _prep
from .test_followup import NOW, cfg, seed


def states(rows):
    return [(c.job_title, c.state, c.label) for c in rows]


def test_each_job_shows_where_it_stands_with_the_waiting_ones_first():
    with session_scope(in_memory_factory()) as s:
        a, b, c, d = seed(s, 4)  # role0..role3, newest recommendation first
        record_followup_answer(s, b, "applied", ignore_after_later=4)
        record_followup_answer(s, c, "not_applying", ignore_after_later=4)
        for _ in range(4):
            record_followup_answer(s, d, "later", ignore_after_later=4)
        got = company_site_status_since(s, NOW, 7)
        assert states(got)[0] == ("role0", "waiting", "waiting for your answer")  # waiting jobs come first
        assert set(states(got)[1:]) == {
            ("role1", "applied", "applied through company website"),
            ("role2", "not_applied", "not applied"),
            ("role3", "ignored", "ignored"),
        }


def test_a_waiting_job_carries_its_link_and_how_many_times_it_was_put_off():
    with session_scope(in_memory_factory()) as s:
        a, *_ = seed(s, 1)
        record_followup_answer(s, a, "later", ignore_after_later=4)
        record_followup_answer(s, a, "later", ignore_after_later=4)
        (row,) = company_site_status_since(s, NOW, 7)
        assert row.later_count == 2 and row.direct_link == "https://co0.example/jobs/0"


def test_todays_part_one_jobs_are_not_repeated_and_naukri_apply_or_old_jobs_are_left_out():
    with session_scope(in_memory_factory()) as s:
        a, b, c = seed(s, 3)
        assert [r.job_title for r in company_site_status_since(s, NOW, 7, exclude_job_ids={a})] == ["role1", "role2"]
    with session_scope(in_memory_factory()) as s:
        seed(s, 2, apply_type="native")
        assert company_site_status_since(s, NOW, 7) == []
    with session_scope(in_memory_factory()) as s:
        seed(s, 2, days_ago=10)
        assert company_site_status_since(s, NOW, 7) == []


def test_a_job_you_applied_to_later_with_an_interview_shows_as_other_but_keeps_its_label():
    with session_scope(in_memory_factory()) as s:
        a, *_ = seed(s, 1)
        upsert_application_history(s, a)
        upsert_application_history(s, a, status=ApplicationStatus.INTERVIEW)
        (row,) = company_site_status_since(s, NOW, 7)
        assert row.state == "other" and row.label == "applied through company website"


def test_the_list_is_capped():
    with session_scope(in_memory_factory()) as s:
        seed(s, 8)
        assert len(company_site_status_since(s, NOW, 7, limit=5)) == 5


# --- in the email -----------------------------------------------------------------------------------------------------------


def digest_text(tmp_path, populate, **over):
    c = cfg(tmp_path, **over)
    with session_scope(in_memory_factory()) as s:
        ids = populate(s)
        cand, run = make_candidate(s, email="today@example.com"), make_run(s)
        today = add_job(s, slug="today", ext="069999999999", title="Today Role", company="TodayCo")
        today.apply_type = "company_site"
        score(s, cand.id, today.id, overall=90.0)
        select_static_resume(s, today.id, cand.id)
        d = build_digest(s, candidate_id=cand.id, candidate_email="c@example.com", scored_job_ids=[today.id],
                         settings=c, run_id=run.id, now=NOW, manual_apply_only=True)
        d.company_site_status = company_site_status_since(s, NOW, 7, {r.job_id for r in d.recommendations})
        return render_digest(d, c).text_body, ids


def test_part_three_is_in_the_email_after_part_two(tmp_path):
    def populate(s):
        a, b, c = seed(s, 3)
        record_followup_answer(s, b, "applied", ignore_after_later=4)
        record_followup_answer(s, c, "not_applying", ignore_after_later=4)
        return a, b, c

    body, _ = digest_text(tmp_path, populate, telegram_remote_start=True)
    assert body.index("PART 1") < body.index("PART 2") < body.index("PART 3")
    part3 = body[body.index("PART 3"):]
    assert "PART 3 — COMPANY-WEBSITE JOBS: WHERE THEY STAND (3)" in part3
    assert "1 still waiting for your answer: send /applied to your Telegram bot." in part3
    assert "Status: waiting for your answer" in part3 and "Direct apply link: https://co0.example/jobs/0" in part3
    assert "Status: applied through company website" in part3 and "Status: not applied" in part3
    assert "Today Role" not in part3  # today's Part 1 job is not repeated


def test_without_phone_control_part_three_points_at_mark_applied(tmp_path):
    body, _ = digest_text(tmp_path, lambda s: seed(s, 1), telegram_remote_start=False)
    assert "run naukri-agent mark-applied <job>" in body[body.index("PART 3"):]


def test_a_settled_job_shows_the_day_and_no_link_clutter(tmp_path):
    def populate(s):
        a, *_ = seed(s, 1)
        record_followup_answer(s, a, "applied", ignore_after_later=4)

    part3 = digest_text(tmp_path, populate)[0].split("PART 3")[1]
    assert "Status: applied through company website — " in part3 and "Direct apply link" not in part3


def test_with_nothing_to_show_part_three_says_so(tmp_path):
    body, _ = digest_text(tmp_path, lambda s: None)
    assert "PART 3 — COMPANY-WEBSITE JOBS: WHERE THEY STAND (0)" in body and "No company-website jobs from the last 7 days" in body


def test_the_footer_explains_part_three(tmp_path):
    body, _ = digest_text(tmp_path, lambda s: None)
    assert "Part 3 shows what you told the app about company-website jobs." in body


# --- through the real daily run ------------------------------------------------------------------------------------------------


def test_the_daily_run_puts_part_three_in_the_email(tmp_path):
    _prep(tmp_path)
    c = cfg(tmp_path, dry_run=True, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()
    with session_scope(factory) as s:  # a company-website job from yesterday's digest that nobody has answered
        cand, run = make_candidate(s, email="old@example.com"), make_run(s)
        old = add_job(s, slug="old", ext="061026508888", title="Yesterday Role", company="OldCo")
        old.apply_type = "company_site"
        from naukri_agent.database.repositories import record_job_recommendation
        from naukri_agent.matching.models import MatchDecision

        record_job_recommendation(
            s, candidate_id=cand.id, job_id=old.id, daily_run_id=run.id, rank=1, score_at_email=88.0,
            decision_at_email=MatchDecision.ACCEPT, application_status_at_email=ApplicationStatus.NOT_APPLIED,
            recommended_at=datetime.datetime(2026, 9, 8, 5, 0),
        )
    result = run_daily_recommendations(
        c, now=datetime.datetime(2026, 9, 9, 5, 0, tzinfo=datetime.UTC),
        discover_fn=_fake_discover([("ds", "040926000001", 0)]),
        extraction_provider=_PerJobLLM({}, _VALID_EXTRACTION), session_factory=factory,
    )
    assert result.status == "COMPLETED"
    body = pathlib.Path(result.email_path).read_text(encoding="utf-8")
    part3 = body[body.index("PART 3"):]
    assert "Yesterday Role — OldCo" in part3 and "Status: waiting for your answer" in part3
