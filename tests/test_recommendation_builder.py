"""
Stage A: recommendation ranking + application-aware filtering.

Enforces the corrected cooldown semantics EXPLICITLY:
  * recommendation_cooldown_days == 0 -> a previously-recommended but
    NOT-APPLIED job is eligible again on the NEXT run.
  * recommendation_cooldown_days  > 0 -> not eligible until that many
    days have elapsed since the last recommendation.
  * statuses in recommendation_exclude_if_status always exclude the job,
    regardless of cooldown.
"""

from __future__ import annotations

import datetime

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import ApplicationStatus, JobRecommendation, MatchDecision
from naukri_agent.database.repositories import (
    record_job_recommendation,
    upsert_application_history,
)
from naukri_agent.recommendations.builder import build_digest
from naukri_agent.recommendations.models import FreshnessLabel

from .digest_fakes import (
    FakeLLMProvider,
    add_job,
    in_memory_factory,
    make_candidate,
    make_run,
    score,
    select_static_resume,
    settings,
)

UTC = datetime.UTC


def _digest(session, cand, run, scored_ids, cfg, *, now=None, provider=None):
    return build_digest(
        session,
        candidate_id=cand.id,
        candidate_email="c@example.com",
        scored_job_ids=scored_ids,
        settings=cfg,
        run_id=run.id,
        now=now or datetime.datetime.now(UTC),
        explain_provider=provider,
    )


def test_ranks_by_score_caps_at_limit_and_writes_recommendation_rows(tmp_path):
    cfg = settings(tmp_path, daily_recommendation_limit=2)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        ids = []
        for i, sc in enumerate([91.0, 55.0, 88.0, 70.0]):
            j = add_job(s, slug=f"job{i}", ext=f"04092600{i}00")
            score(s, cand.id, j.id, overall=sc)
            select_static_resume(s, j.id, cand.id)
            ids.append(j.id)

        d = _digest(s, cand, run, ids, cfg)

        assert d.eligible_count == 4
        assert d.count == 2 and d.truncated is True
        assert [r.match_score for r in d.recommendations] == [91.0, 88.0]
        assert [r.rank for r in d.recommendations] == [1, 2]
        assert d.recommendations[0].naukri_url.startswith("https://www.naukri.com/")
        rows = s.query(JobRecommendation).filter_by(daily_run_id=run.id).all()
        assert len(rows) == 2
        assert {row.rank for row in rows} == {1, 2}


def test_min_score_and_decision_filters_exclude(tmp_path):
    cfg = settings(tmp_path, recommendation_min_score=75, recommendation_decisions=["ACCEPT"])
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        low = add_job(s, slug="low", ext="040926000001")
        score(s, cand.id, low.id, overall=60.0, decision=MatchDecision.REVIEW)
        rev = add_job(s, slug="rev", ext="040926000002")
        score(s, cand.id, rev.id, overall=90.0, decision=MatchDecision.REVIEW)
        good = add_job(s, slug="good", ext="040926000003")
        score(s, cand.id, good.id, overall=90.0, decision=MatchDecision.ACCEPT)

        d = _digest(s, cand, run, [low.id, rev.id, good.id], cfg)
        assert [r.job_id for r in d.recommendations] == [good.id]


# --- application-status exclusion (A) — regardless of cooldown ---


def test_applied_job_never_recommended_even_with_zero_cooldown(tmp_path):
    cfg = settings(tmp_path, recommendation_cooldown_days=0)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        applied = add_job(s, slug="applied", ext="040926000010")
        score(s, cand.id, applied.id, overall=99.0)
        other = add_job(s, slug="other", ext="040926000011")
        score(s, cand.id, other.id, overall=80.0)
        upsert_application_history(s, applied.id, status=ApplicationStatus.APPLIED)

        d = _digest(s, cand, run, [applied.id, other.id], cfg)
        assert [r.job_id for r in d.recommendations] == [other.id]


def test_downstream_statuses_excluded_by_default_but_rule_is_configurable(tmp_path):
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        rej = add_job(s, slug="rej", ext="040926000020")
        score(s, cand.id, rej.id, overall=95.0)
        upsert_application_history(s, rej.id, status=ApplicationStatus.REJECTED)

        default_cfg = settings(tmp_path)
        assert _digest(s, cand, run, [rej.id], default_cfg).count == 0

        run2 = make_run(s)
        only_applied_cfg = settings(tmp_path, recommendation_exclude_if_status=["APPLIED"])
        d2 = _digest(s, cand, run2, [rej.id], only_applied_cfg)
        assert [r.job_id for r in d2.recommendations] == [rej.id]  # REJECTED now allowed back


# --- cooldown semantics (B) — the corrected rule, explicit ---


def _recommend_yesterday(s, cand, job_id, days_ago):
    record_job_recommendation(
        s,
        candidate_id=cand.id,
        job_id=job_id,
        daily_run_id=None,
        rank=1,
        score_at_email=80.0,
        decision_at_email=MatchDecision.ACCEPT,
        application_status_at_email=ApplicationStatus.NOT_APPLIED,
        recommended_at=datetime.datetime.now(UTC) - datetime.timedelta(days=days_ago),
    )


def test_cooldown_zero_allows_previously_recommended_not_applied_next_run(tmp_path):
    cfg = settings(tmp_path, recommendation_cooldown_days=0)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        job = add_job(s, slug="again", ext="040926000030")
        score(s, cand.id, job.id, overall=80.0)
        _recommend_yesterday(s, cand, job.id, days_ago=1)  # recommended, NOT applied

        d = _digest(s, cand, run, [job.id], cfg)
        assert [r.job_id for r in d.recommendations] == [job.id]  # eligible again
        assert d.recommendations[0].freshness == FreshnessLabel.PREVIOUSLY_RECOMMENDED


def test_cooldown_positive_excludes_within_window_and_allows_after(tmp_path):
    cfg = settings(tmp_path, recommendation_cooldown_days=30)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        job = add_job(s, slug="cool", ext="040926000031")
        score(s, cand.id, job.id, overall=80.0)

        # last recommended 10 days ago -> still inside the 30-day window
        _recommend_yesterday(s, cand, job.id, days_ago=10)
        run1 = make_run(s)
        assert _digest(s, cand, run1, [job.id], cfg).count == 0

        # push the last recommendation to 31 days ago -> outside the window
        s.query(JobRecommendation).filter_by(job_id=job.id).delete()
        _recommend_yesterday(s, cand, job.id, days_ago=31)
        run2 = make_run(s)
        d2 = _digest(s, cand, run2, [job.id], cfg)
        assert [r.job_id for r in d2.recommendations] == [job.id]


def test_never_recommended_not_applied_is_eligible_and_labelled_new(tmp_path):
    cfg = settings(tmp_path, freshness_new_days=3)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        fresh = add_job(s, slug="fresh", ext="040926000040", first_seen_days_ago=1)
        score(s, cand.id, fresh.id, overall=85.0)
        old = add_job(s, slug="old", ext="040926000041", first_seen_days_ago=40)
        score(s, cand.id, old.id, overall=84.0)

        d = _digest(s, cand, run, [fresh.id, old.id], cfg)
        by_id = {r.job_id: r for r in d.recommendations}
        assert by_id[fresh.id].freshness == FreshnessLabel.NEWLY_DISCOVERED
        assert by_id[old.id].freshness == FreshnessLabel.SEEN_BEFORE


# --- reposts (D) — resolve to canonical ---


def test_repost_resolves_to_canonical_for_both_application_and_recommendation(tmp_path):
    cfg = settings(tmp_path, recommendation_cooldown_days=0)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        root = add_job(s, slug="pos", ext="040926000050", description="Identical JD body.")
        # a repost: same fingerprint, different URL/external id
        repost = add_job(s, slug="pos-repost", ext="040926000051", description="Identical JD body.")
        assert repost.repost_of_job_id == root.id
        score(s, cand.id, root.id, overall=90.0)
        score(s, cand.id, repost.id, overall=90.0)
        upsert_application_history(s, repost.id, status=ApplicationStatus.APPLIED)  # applied via the repost

        d = _digest(s, cand, run, [root.id, repost.id], cfg)
        assert d.count == 0  # the whole repost family is excluded (applied)


# --- LLM boundary ---


def test_llm_narrative_that_touches_application_status_is_dropped(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        job = add_job(s, slug="expl", ext="040926000060")
        score(s, cand.id, job.id, overall=88.0)

        provider = FakeLLMProvider("You already applied to this role, so skip it. Score is 12/100.")
        d = _digest(s, cand, run, [job.id], cfg, provider=provider)

        rec = d.recommendations[0]
        assert rec.explanation is None  # contradicting narrative dropped
        assert rec.explanation_source == "deterministic"
        assert rec.application_status_label == "New"  # DB-driven, LLM cannot override
        assert rec.match_score == 88.0  # deterministic score unchanged


def test_llm_narrative_is_additive_when_safe(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        job = add_job(s, slug="expl2", ext="040926000061")
        score(s, cand.id, job.id, overall=82.0)

        provider = FakeLLMProvider("Strong overlap on Python and SQL; pay is a little low.")
        d = _digest(s, cand, run, [job.id], cfg, provider=provider)
        rec = d.recommendations[0]
        assert rec.explanation and rec.explanation_source == "llm"
        assert rec.reasons == ["skills match: Python, SQL"]  # deterministic factors preserved


# --- Option A: an LLM parse failure this run can never make a job eligible ---


def _digest_pf(session, cand, run, scored_ids, cfg, parse_failed, *, now=None):
    return build_digest(
        session,
        candidate_id=cand.id,
        candidate_email="c@example.com",
        scored_job_ids=scored_ids,
        settings=cfg,
        run_id=run.id,
        now=now or datetime.datetime.now(UTC),
        explain_provider=None,
        parse_failed_job_ids=parse_failed,
    )


def test_parse_failed_job_excluded_even_with_top_score_and_accept(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        bad = add_job(s, slug="bad", ext="040926000201")
        score(s, cand.id, bad.id, overall=99.0, decision=MatchDecision.ACCEPT)
        good = add_job(s, slug="good", ext="040926000202")
        score(s, cand.id, good.id, overall=72.0, decision=MatchDecision.ACCEPT)

        d = _digest_pf(s, cand, run, [bad.id, good.id], cfg, {bad.id})

        assert [r.job_id for r in d.recommendations] == [good.id]
        assert d.eligible_count == 1
        rows = s.query(JobRecommendation).filter_by(daily_run_id=run.id).all()
        assert {row.job_id for row in rows} == {good.id}


def test_successfully_parsed_high_scoring_job_stays_eligible(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        good = add_job(s, slug="ok", ext="040926000203")
        score(s, cand.id, good.id, overall=88.0, decision=MatchDecision.ACCEPT)

        d = _digest_pf(s, cand, run, [good.id], cfg, set())
        assert [r.job_id for r in d.recommendations] == [good.id]


def test_none_parse_failed_set_suppresses_nothing(tmp_path):
    """An unconfigured / non-attempted-extraction run passes
    parse_failed_job_ids=None (the default). That must behave exactly like
    an empty set: no job is suppressed."""
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        job = add_job(s, slug="nollm", ext="040926000204")
        score(s, cand.id, job.id, overall=85.0, decision=MatchDecision.ACCEPT)

        d = build_digest(
            s,
            candidate_id=cand.id,
            candidate_email="c@example.com",
            scored_job_ids=[job.id],
            settings=cfg,
            run_id=run.id,
            now=datetime.datetime.now(UTC),
        )  # parse_failed_job_ids omitted entirely
        assert [r.job_id for r in d.recommendations] == [job.id]


def test_new_gate_composes_with_status_and_cooldown_filters(tmp_path):
    cfg = settings(tmp_path, recommendation_cooldown_days=30)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        # excluded by the new parse-failure gate
        pf = add_job(s, slug="pf", ext="040926000205")
        score(s, cand.id, pf.id, overall=99.0)
        # excluded by application status
        app = add_job(s, slug="app", ext="040926000206")
        score(s, cand.id, app.id, overall=98.0)
        upsert_application_history(s, app.id, status=ApplicationStatus.APPLIED)
        # excluded by cooldown (recommended 5 days ago, 30-day window)
        cd = add_job(s, slug="cd", ext="040926000207")
        score(s, cand.id, cd.id, overall=97.0)
        _recommend_yesterday(s, cand, cd.id, days_ago=5)
        # the only survivor
        ok = add_job(s, slug="clean", ext="040926000208")
        score(s, cand.id, ok.id, overall=75.0)

        d = _digest_pf(s, cand, run, [pf.id, app.id, cd.id, ok.id], cfg, {pf.id})
        assert [r.job_id for r in d.recommendations] == [ok.id]


# --- Phase F1 (revised): bucketed freshness-first recommendation ranking ---
#
# Buckets by POSTED date (not first_seen_at): 0 = 0-1d, 1 = 2-3d,
# 2 = 4-7d, 3 = older/unknown -> last. overall_score DESC orders within a
# bucket. Eligibility gates (ACCEPT/REVIEW, min score, status exclusion,
# cooldown, parse-failed, canonical) are unchanged.

_NOW = datetime.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _iso_days_ago(n: int) -> str:
    return (_NOW.date() - datetime.timedelta(days=n)).isoformat()


def test_recommendations_are_bucketed_by_posted_age_then_score(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        rows = [
            ("b0_today", 0, 70.0),
            ("b0_yday", 1, 95.0),
            ("b1_hi", 2, 99.0),
            ("b1_lo", 3, 60.0),
            ("b2_top", 6, 100.0),
        ]
        ids = []
        for i, (slug, days, sc) in enumerate(rows):
            j = add_job(s, slug=slug, ext="04092610%04d" % i,
                        posted_date_text=_iso_days_ago(days))
            score(s, cand.id, j.id, overall=sc)
            ids.append(j.id)

        d = _digest(s, cand, run, ids, cfg, now=_NOW)
        got = [r.job_id for r in d.recommendations]
        by_slug = {slug: ids[i] for i, (slug, _dd, _sc) in enumerate(rows)}
        assert got == [
            by_slug["b0_yday"], by_slug["b0_today"],
            by_slug["b1_hi"], by_slug["b1_lo"],
            by_slug["b2_top"],
        ]


def test_score_orders_within_a_single_freshness_bucket(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        lo = add_job(s, slug="lo", ext="040926110001", posted_date_text=_iso_days_ago(0))
        hi = add_job(s, slug="hi", ext="040926110002", posted_date_text=_iso_days_ago(1))
        score(s, cand.id, lo.id, overall=62.0)
        score(s, cand.id, hi.id, overall=88.0)
        d = _digest(s, cand, run, [lo.id, hi.id], cfg, now=_NOW)
        assert [r.job_id for r in d.recommendations] == [hi.id, lo.id]


def test_posted_date_takes_precedence_over_first_seen_at(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        a = add_job(s, slug="a", ext="040926110010",
                    posted_date_text=_iso_days_ago(7), first_seen_days_ago=0)
        b = add_job(s, slug="b", ext="040926110011",
                    posted_date_text=_iso_days_ago(0), first_seen_days_ago=12)
        score(s, cand.id, a.id, overall=90.0)
        score(s, cand.id, b.id, overall=90.0)
        d = _digest(s, cand, run, [a.id, b.id], cfg, now=_NOW)
        assert [r.job_id for r in d.recommendations] == [b.id, a.id]


def test_unknown_posted_date_sorts_last(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        known = add_job(s, slug="known", ext="040926110020", posted_date_text=_iso_days_ago(3))
        unknown = add_job(s, slug="unknown", ext="040926110021", posted_date_text=None)
        garbled = add_job(s, slug="garbled", ext="040926110022", posted_date_text="posted sometime")
        score(s, cand.id, known.id, overall=55.0)
        score(s, cand.id, unknown.id, overall=99.0)
        score(s, cand.id, garbled.id, overall=98.0)
        d = _digest(s, cand, run, [known.id, unknown.id, garbled.id], cfg, now=_NOW)
        got = [r.job_id for r in d.recommendations]
        assert got[0] == known.id
        assert set(got[1:]) == {unknown.id, garbled.id}


def test_relative_posted_labels_bucket_like_iso_dates(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        rel = add_job(s, slug="rel", ext="040926110030", posted_date_text="Just now")
        iso = add_job(s, slug="iso", ext="040926110031", posted_date_text=_iso_days_ago(1))
        old = add_job(s, slug="old", ext="040926110032", posted_date_text="5 days ago")
        score(s, cand.id, rel.id, overall=70.0)
        score(s, cand.id, iso.id, overall=80.0)
        score(s, cand.id, old.id, overall=100.0)
        d = _digest(s, cand, run, [rel.id, iso.id, old.id], cfg, now=_NOW)
        got = [r.job_id for r in d.recommendations]
        assert got[:2] == [iso.id, rel.id]
        assert got[2] == old.id


def test_final_recommendation_cap_stays_10_and_prefers_fresher_buckets(tmp_path):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        ids = {}
        plan = (
            [("b0_%d" % i, i % 2, 50.0 + i) for i in range(6)]
            + [("b1_%d" % i, 2 + (i % 2), 90.0 + i) for i in range(5)]
            + [("b2_%d" % i, 5 + i, 99.0) for i in range(2)]
        )
        for k, (slug, days, sc) in enumerate(plan):
            j = add_job(s, slug=slug, ext="0409261200%02d" % k,
                        posted_date_text=_iso_days_ago(days))
            score(s, cand.id, j.id, overall=sc)
            ids[slug] = j.id

        d = _digest(s, cand, run, list(ids.values()), cfg, now=_NOW)
        assert d.eligible_count == 13
        assert d.count == 10
        assert d.truncated is True
        kept = {r.job_id for r in d.recommendations}
        assert ids["b2_0"] not in kept and ids["b2_1"] not in kept
        for i in range(6):
            assert ids["b0_%d" % i] in kept
        assert ids["b1_0"] not in kept
        assert sum(1 for i in range(5) if ids["b1_%d" % i] in kept) == 4


def test_eligibility_gates_unchanged_under_bucketed_ranking(tmp_path):
    """Every gate still fires even when the excluded job is the freshest."""
    cfg = settings(tmp_path, recommendation_cooldown_days=30,
                   recommendation_min_score=70,
                   recommendation_decisions=["ACCEPT", "REVIEW"])
    with session_scope(in_memory_factory()) as s:
        cand = make_candidate(s)
        run = make_run(s)
        today = _iso_days_ago(0)
        rej = add_job(s, slug="rej", ext="040926130001", posted_date_text=today)
        score(s, cand.id, rej.id, overall=99.0, decision=MatchDecision.REJECT)
        lowsc = add_job(s, slug="lowsc", ext="040926130002", posted_date_text=today)
        score(s, cand.id, lowsc.id, overall=65.0)
        applied = add_job(s, slug="applied", ext="040926130003", posted_date_text=today)
        score(s, cand.id, applied.id, overall=98.0)
        upsert_application_history(s, applied.id, status=ApplicationStatus.APPLIED)
        cooled = add_job(s, slug="cooled", ext="040926130004", posted_date_text=today)
        score(s, cand.id, cooled.id, overall=97.0)
        _recommend_yesterday(s, cand, cooled.id, days_ago=5)
        good = add_job(s, slug="good", ext="040926130005", posted_date_text=_iso_days_ago(6))
        score(s, cand.id, good.id, overall=72.0)

        d = _digest(s, cand, run,
                    [rej.id, lowsc.id, applied.id, cooled.id, good.id], cfg, now=_NOW)
        assert [r.job_id for r in d.recommendations] == [good.id]
