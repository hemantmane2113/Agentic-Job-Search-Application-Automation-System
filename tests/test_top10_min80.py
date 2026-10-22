"""The defaults you asked for: read 50 jobs in full, list only those scoring 80 or more, best score first, at most 10."""

from __future__ import annotations

import datetime

from naukri_agent.config import Settings
from naukri_agent.database.base import session_scope
from naukri_agent.matching.models import MatchDecision
from naukri_agent.notifications.render import render_digest
from naukri_agent.orchestration import discovery as discovery_mod
from naukri_agent.recommendations.builder import _min_score, build_digest

from .digest_fakes import add_job, in_memory_factory, make_candidate, make_run, score, select_static_resume, settings
from .test_discovery_freshness import _FakeClient, _summary

NOW = datetime.datetime(2026, 10, 9, 5, 0, tzinfo=datetime.UTC)
PLAIN = Settings(_env_file=None)  # none of the test helper's legacy overrides


def decision_for(sc):
    return MatchDecision.ACCEPT if sc >= 80 else (MatchDecision.REVIEW if sc >= 70 else MatchDecision.REJECT)


def digest(scores, *, posted=None, cfg=PLAIN, two_part=False):
    f = in_memory_factory()
    with session_scope(f) as s:
        cand, run = make_candidate(s), make_run(s)
        ids, titles = [], {}
        for i, sc in enumerate(scores):
            j = add_job(s, slug=f"j{i}", ext=f"04092630{i:04d}", title=f"role{i}")
            if posted:
                j.posted_date_text = posted[i]
            score(s, cand.id, j.id, overall=sc, decision=decision_for(sc))
            select_static_resume(s, j.id, cand.id)
            ids.append(j.id)
            titles[j.id] = f"role{i}"
        d = build_digest(s, candidate_id=cand.id, candidate_email="c@example.com", scored_job_ids=ids, settings=cfg,
                         run_id=run.id, now=NOW, manual_apply_only=two_part)
        return d, [r.job_title for r in d.recommendations], [r.match_score for r in d.recommendations]


# --- the defaults ---------------------------------------------------------------------------------------------------------


def test_the_defaults_are_a_pool_of_fifty_and_a_bar_of_eighty():
    assert PLAIN.discovery_fresh_job_limit == 50
    assert PLAIN.recommendation_decisions == ["ACCEPT"]
    assert PLAIN.recommendation_rank_freshness_first is False
    assert _min_score(PLAIN) == PLAIN.threshold_accept == 80
    assert PLAIN.daily_recommendation_limit == 10


# --- only 80 or more ---------------------------------------------------------------------------------------------------------


def test_only_jobs_scoring_eighty_or_more_are_listed_and_exactly_eighty_counts():
    _d, titles, scores = digest([95.0, 85.0, 80.0, 79.9, 72.0, 60.0])
    assert scores == [95.0, 85.0, 80.0] and titles == ["role0", "role1", "role2"]


def test_fewer_than_ten_is_fine_and_nothing_pads_the_list():
    d, titles, _ = digest([91.0, 82.0, 71.0])
    assert d.count == 2 and d.truncated is False and len(titles) == 2


def test_a_high_score_with_a_review_decision_is_not_listed():
    f = in_memory_factory()
    with session_scope(f) as s:  # e.g. an experience conflict caps a 90 down to REVIEW
        cand, run = make_candidate(s), make_run(s)
        j = add_job(s, slug="capped", ext="040926300999")
        score(s, cand.id, j.id, overall=90.0, decision=MatchDecision.REVIEW)
        d = build_digest(s, candidate_id=cand.id, candidate_email="c@example.com", scored_job_ids=[j.id], settings=PLAIN, run_id=run.id, now=NOW)
    assert d.count == 0


def test_the_email_states_the_bar_and_the_plan():
    d, _t, _s = digest([95.0, 85.0], two_part=True)
    d.daily_total, d.telegram_slots, d.part1_limit = 10, 4, 6
    body = render_digest(d, PLAIN).text_body
    assert d.min_score == 80.0
    assert "Only jobs scoring 80 or more are listed, so fewer is normal." in body


def test_the_bar_can_be_lowered_in_settings():
    _d, _t, scores = digest([95.0, 75.0, 65.0], cfg=Settings(_env_file=None, recommendation_min_score=70, recommendation_decisions=["ACCEPT", "REVIEW"]))
    assert scores == [95.0, 75.0]


# --- the top ten, by score ---------------------------------------------------------------------------------------------------


def test_with_more_than_ten_qualifying_the_ten_best_scores_are_kept_in_order():
    scores = [80 + (i * 1.3) % 19 for i in range(14)]
    d, _t, got = digest(scores)
    assert len(got) == 10 and got == sorted(scores, reverse=True)[:10]
    assert d.eligible_count == 14 and d.truncated is True


def test_a_higher_score_beats_a_fresher_job_by_default():
    today, week_ago = "2026-10-09", "2026-10-02"
    _d, titles, scores = digest([84.0, 96.0], posted=[today, week_ago])
    assert scores == [96.0, 84.0] and titles == ["role1", "role0"]


def test_the_old_newest_first_order_is_still_available_as_a_setting():
    today, week_ago = "2026-10-09", "2026-10-02"
    _d, titles, scores = digest([84.0, 96.0], posted=[today, week_ago], cfg=Settings(_env_file=None, recommendation_rank_freshness_first=True))
    assert scores == [84.0, 96.0]


def test_freshness_only_breaks_ties_between_equal_scores():
    _d, titles, _s = digest([90.0, 90.0], posted=["2026-10-02", "2026-10-09"])
    assert titles == ["role1", "role0"]


# --- the pool of fifty ---------------------------------------------------------------------------------------------------------


def test_fifty_jobs_are_opened_and_read_in_full_shared_equally_between_the_two_resume_groups(tmp_path):
    def cards(tag, n):
        return [_summary(f"{tag}/{i:03d}", "Just now") for i in range(n)]

    client = _FakeClient([cards("ds", 40), cards("ml", 40)])
    cfg = settings(tmp_path, discovery_queries=["Data Scientist@Pune", "ML Engineer@Pune"], resume_registry_path=tmp_path / "none.yaml")
    f = in_memory_factory()
    with session_scope(f) as s:
        result, _ = discovery_mod.discover_and_store(
            s, client, profile=None, settings=cfg, run_id=1, seq_start=0, role_groups={"data scientist": "ds", "ml engineer": "ml"}
        )
    assert len(client.fetched) == 50 == len(result.job_ids)
    assert sum(u.startswith("ds/") for u in client.fetched) == 25 and sum(u.startswith("ml/") for u in client.fetched) == 25
