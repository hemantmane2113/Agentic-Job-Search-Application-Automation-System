"""
Two-part daily email.

Part 1 = jobs that match but only offer "Apply on company site" (the user applies by hand).
Part 2 = jobs the app applied to after the user's Yes on Telegram, since the previous digest.
Jobs with a Naukri Apply button are left out of the email (telegram-apply handles them) and
only counted in a footer line.
"""

from __future__ import annotations

import datetime

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import DailyRunStatus
from naukri_agent.database.repositories import (
    add_application_question,
    link_application_questions_to_history,
    upsert_application_history,
)
from naukri_agent.notifications.render import render_digest
from naukri_agent.recommendations.builder import (
    applied_via_agent_since,
    build_digest,
    previous_digest_time,
)

from .digest_fakes import add_job, in_memory_factory, make_candidate, make_run, score, select_static_resume, settings

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 9, 4, 30, tzinfo=UTC)  # 10:00 IST


def _digest(s, cand, run, ids, cfg, **kw):
    return build_digest(
        s, candidate_id=cand.id, candidate_email="c@example.com", scored_job_ids=ids,
        settings=cfg, run_id=run.id, now=NOW, **kw,
    )


def _job(s, cand, slug, ext, sc, apply_type):
    j = add_job(s, slug=slug, ext=ext)
    j.apply_type = apply_type
    score(s, cand.id, j.id, overall=sc)
    select_static_resume(s, j.id, cand.id)
    return j


# --- Part 1: which jobs are listed ------------------------------------------------------------------


def test_native_jobs_are_left_out_and_counted_company_site_and_unknown_stay(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand, run = make_candidate(s), make_run(s)
        a = _job(s, cand, "co", "040926000101", 90, "company_site")
        b = _job(s, cand, "unk", "040926000102", 85, None)
        c = _job(s, cand, "nat1", "040926000103", 95, "native")
        d = _job(s, cand, "nat2", "040926000104", 80, "native")
        dig = _digest(s, cand, run, [a.id, b.id, c.id, d.id], cfg, manual_apply_only=True)
        assert dig.two_part is True
        assert {r.apply_type for r in dig.recommendations} == {"company_site", None}
        assert dig.native_waiting == 2 and dig.eligible_count == 2


def test_default_behaviour_is_unchanged_without_the_flag(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand, run = make_candidate(s), make_run(s)
        n = _job(s, cand, "nat", "040926000201", 90, "native")
        dig = _digest(s, cand, run, [n.id], cfg)
        assert dig.two_part is False and dig.count == 1 and dig.native_waiting == 0


def test_native_jobs_do_not_use_up_the_manual_lists_slots(tmp_path):
    cfg = settings(tmp_path, daily_recommendation_limit=2)
    with session_scope(in_memory_factory()) as s:
        cand, run = make_candidate(s), make_run(s)
        ids = [_job(s, cand, f"n{i}", f"04092600030{i}", 99 - i, "native").id for i in range(3)]
        ids += [_job(s, cand, f"c{i}", f"04092600031{i}", 80 - i, "company_site").id for i in range(2)]
        dig = _digest(s, cand, run, ids, cfg, manual_apply_only=True)
        assert dig.count == 2 and dig.native_waiting == 3 and dig.truncated is False


def test_an_already_applied_native_job_is_not_counted_as_waiting(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand, run = make_candidate(s), make_run(s)
        n = _job(s, cand, "done", "040926000401", 90, "native")
        upsert_application_history(s, n.id, source="agent_auto_apply_telegram")
        dig = _digest(s, cand, run, [n.id], cfg, manual_apply_only=True)
        assert dig.native_waiting == 0 and dig.count == 0


# --- Part 2: what the app applied to ------------------------------------------------------------------


def test_previous_digest_time_uses_the_last_completed_run_else_24h(tmp_path):
    with session_scope(in_memory_factory()) as s:
        assert previous_digest_time(s, None, NOW) == (NOW - datetime.timedelta(hours=24)).replace(tzinfo=None)
        old = make_run(s)
        old.status = DailyRunStatus.COMPLETED
        old.finished_at = datetime.datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
        failed = make_run(s)
        failed.status = DailyRunStatus.FAILED
        failed.finished_at = datetime.datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
        cur = make_run(s)
        s.flush()
        assert previous_digest_time(s, cur.id, NOW) == datetime.datetime(2026, 10, 8, 5, 0)


def test_applied_via_agent_since_filters_by_time_and_source_and_carries_answers(tmp_path):
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        old = _job(s, cand, "old", "040926000501", 90, "native")
        new = _job(s, cand, "new", "040926000502", 90, "native")
        manual = _job(s, cand, "man", "040926000503", 90, "company_site")
        since = datetime.datetime(2026, 10, 8, 5, 0)
        upsert_application_history(s, old.id, source="agent_auto_apply_telegram",
                                   applied_at=datetime.datetime(2026, 10, 7, 12, 0, tzinfo=UTC))
        h, _ = upsert_application_history(s, new.id, source="agent_auto_apply_telegram",
                                          applied_at=datetime.datetime(2026, 10, 8, 8, 0, tzinfo=UTC))
        upsert_application_history(s, manual.id, source="manual_cli",
                                   applied_at=datetime.datetime(2026, 10, 8, 9, 0, tzinfo=UTC))
        add_application_question(s, job_id=new.id, attempt_id="a1", order_in_attempt=1,
                                 question_text="Available for walk-in?", final_answer="Yes")
        link_application_questions_to_history(s, "a1", h.id)

        got = applied_via_agent_since(s, since)
        assert [g.job_title for g in got] == ["Data Scientist"] and len(got) == 1
        assert got[0].job_url.endswith("040926000502")
        assert got[0].answers == [("Available for walk-in?", "Yes")]


# --- rendering -------------------------------------------------------------------------------------------


def _rendered(tmp_path, *, applied=True, manual=True, native=2):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand, run = make_candidate(s), make_run(s)
        ids = []
        if manual:
            ids.append(_job(s, cand, "co", "040926000601", 90, "company_site").id)
        n = _job(s, cand, "tg", "040926000602", 92, "native")
        ids.append(n.id)
        for i in range(native - 1):
            ids.append(_job(s, cand, f"w{i}", f"04092600061{i}", 70, "native").id)
        if applied:
            h, _ = upsert_application_history(s, n.id, source="agent_auto_apply_telegram",
                                              applied_at=datetime.datetime(2026, 10, 8, 8, 0, tzinfo=UTC))
            add_application_question(s, job_id=n.id, attempt_id="a1", order_in_attempt=1,
                                     question_text="Available for walk-in?", final_answer="Yes")
            link_application_questions_to_history(s, "a1", h.id)
        dig = _digest(s, cand, run, ids, cfg, manual_apply_only=True)
        if applied:
            dig.applied_via_agent = applied_via_agent_since(s, datetime.datetime(2026, 10, 8, 5, 0))
        return render_digest(dig, cfg)


def test_email_has_both_parts_with_counts_and_subject(tmp_path):
    msg = _rendered(tmp_path)
    assert "1 to apply yourself, 1 applied for you" in msg.subject
    body = msg.text_body
    assert body.index("PART 1") < body.index("PART 2")
    assert "PART 1 — APPLY YOURSELF ON THE COMPANY'S WEBSITE (1)" in body
    assert "PART 2 — APPLIED FOR YOU VIA TELEGRAM (1)" in body
    assert "https://www.naukri.com/job-listings-co-040926000601" in body
    # Part 2 shows the answers that were sent and the time in IST (08:00 UTC = 01:30 PM IST)
    assert "Q: Available for walk-in?" in body and "A: Yes" in body
    assert "08 Oct 2026, 01:30 PM" in body
    # the native job that is still waiting is only counted, never linked, in Part 1
    assert "1 more matching job with a Naukri Apply button is ready for Telegram" in body


def test_empty_parts_say_so(tmp_path):
    msg = _rendered(tmp_path, applied=False, manual=False, native=0)
    body = msg.text_body
    assert "None today." in body
    assert "Nothing was applied via Telegram since the last digest." in body
    assert "0 to apply yourself, 0 applied for you" in msg.subject
    # the one native job is unapplied, so it is only counted in the footer
    assert "1 more matching job with a Naukri Apply button is ready for Telegram" in body


def test_two_part_email_contains_no_secrets(tmp_path):
    body = _rendered(tmp_path).text_body.lower()
    for word in ("password", "token", "smtp_"):
        assert word not in body


def test_no_footer_when_no_native_job_is_waiting(tmp_path):
    body = _rendered(tmp_path, applied=True, native=1).text_body
    assert "waiting for you" not in body
