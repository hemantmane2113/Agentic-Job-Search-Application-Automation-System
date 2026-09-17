"""Phase F1: freshness-first daily discovery.

Covers the deterministic card-freshness parser and the
discover_and_store gate that runs AFTER dedup / the hard ceiling and
BEFORE fetch_job_detail: window filter, newest->oldest sort, the
per-run fresh cap, unknown-label exclusion + diagnostics, and the
empty-survivor case. No LLM, no browser, no network.
"""

from __future__ import annotations

import pytest

from naukri_agent.browser.models import JobDetail, JobListingSummary
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import RunEvent
from naukri_agent.orchestration.discovery import (
    _parse_card_age_days,
    discover_and_store,
)

from .digest_fakes import in_memory_factory, settings as _settings

# --- card-freshness parser -------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Just now", 0),
        ("just now", 0),
        ("Few hours ago", 0),
        ("Few minutes ago", 0),
        ("Few seconds ago", 0),
        ("Moments ago", 0),
        ("Today", 0),
        ("5 hours ago", 0),
        ("1 hour ago", 0),
        ("45 minutes ago", 0),
        ("Yesterday", 1),
        ("A day ago", 1),
        ("1 Day Ago", 1),
        ("1 day ago", 1),
        ("2 Days Ago", 2),
        ("3 days ago", 3),
        ("10 Days Ago", 10),
        ("30+ Days Ago", 30),
        ("1 week ago", 7),
        ("2 weeks ago", 14),
        ("1 month ago", 30),
        ("2 months ago", 60),
        ("1 year ago", 365),
    ],
)
def test_parse_card_age_days_recognised_forms(label, expected):
    assert _parse_card_age_days(label) == expected


@pytest.mark.parametrize(
    "label",
    [
        None,
        "",
        "   ",
        "Reposted",
        "Actively hiring",
        "Posted recently",
        "New",
        "Urgent hiring",
        "ago",
        "day",
        "some time back",
        "posted 3 days",  # no "ago" -> not our recognised shape
    ],
)
def test_parse_card_age_days_unknown_returns_none(label):
    assert _parse_card_age_days(label) is None


# --- discover_and_store freshness gate -----------------------------------


def _summary(url: str, posted: str | None) -> JobListingSummary:
    return JobListingSummary(
        title="t", company="c", location="Pune", url=url, posted_text=posted
    )


class _FakeClient:
    """search_jobs returns a canned list per call; fetch_job_detail
    records call order and returns a minimal distinct JobDetail."""

    def __init__(self, results_per_call: list[list[JobListingSummary]]) -> None:
        self._results = list(results_per_call)
        self._call = 0
        self.fetched: list[str] = []

    def search_jobs(self, role: str, location: str = "") -> list[JobListingSummary]:
        r = self._results[self._call] if self._call < len(self._results) else []
        self._call += 1
        return r

    def fetch_job_detail(self, url: str) -> JobDetail:
        self.fetched.append(url)
        return JobDetail(
            url=url, title="T", company="C", location="Pune",
            description=f"JD body for {url}",
        )


def _cfg(tmp_path, **over):
    base = dict(discovery_queries=["Data Scientist"], discovery_freshness_days=7,
               discovery_fresh_job_limit=60, discovery_max_total_jobs=200)
    base.update(over)
    return _settings(tmp_path, **base)


def _run(client, cfg):
    factory = in_memory_factory()
    with session_scope(factory) as s:
        result, _seq = discover_and_store(
            s, client, profile=None, settings=cfg, run_id=1, seq_start=0
        )
        fresh_ev = (
            s.query(RunEvent).filter_by(stage="discover_freshness").one()
        )
        detail = dict(fresh_ev.detail)
    return result, detail


def test_filters_out_jobs_older_than_the_7_day_window_and_counts_them(tmp_path):
    client = _FakeClient([[
        _summary("u/fresh0", "Just now"),
        _summary("u/fresh3", "3 days ago"),
        _summary("u/fresh4", "4 days ago"),
        _summary("u/fresh7", "1 week ago"),      # age 7 -> inside the window
        _summary("u/stale30", "30+ Days Ago"),   # age 30 -> out
    ]])
    result, detail = _run(client, _cfg(tmp_path))

    assert client.fetched == ["u/fresh0", "u/fresh3", "u/fresh4", "u/fresh7"]
    assert result.jobs_new == 4
    assert detail["discovered_cards"] == 5
    assert detail["within_window"] == 4
    assert detail["stale_excluded"] == 1
    assert detail["unknown_excluded"] == 0
    assert detail["selected"] == 4
    assert detail["window_days"] == 7


def test_seven_day_boundary_is_inclusive(tmp_path):
    client = _FakeClient([[
        _summary("u/exactly7", "7 days ago"),
        _summary("u/exactly7wk", "1 week ago"),
    ]])
    _result, detail = _run(client, _cfg(tmp_path))
    assert detail["within_window"] == 2
    assert detail["stale_excluded"] == 0


def test_eighth_day_is_excluded(tmp_path):
    client = _FakeClient([[
        _summary("u/day7", "7 days ago"),
        _summary("u/day8", "8 days ago"),
    ]])
    _result, detail = _run(client, _cfg(tmp_path))
    assert detail["within_window"] == 1
    assert detail["stale_excluded"] == 1


def test_unknown_or_missing_labels_are_excluded_not_treated_as_fresh(tmp_path):
    client = _FakeClient([[
        _summary("u/known", "Today"),
        _summary("u/none", None),
        _summary("u/blank", "   "),
        _summary("u/prose", "Actively hiring"),
        _summary("u/repost", "Reposted"),
    ]])
    result, detail = _run(client, _cfg(tmp_path))

    assert client.fetched == ["u/known"]
    assert result.jobs_new == 1
    assert detail["unknown_excluded"] == 4
    assert detail["within_window"] == 1
    assert detail["stale_excluded"] == 0
    assert detail["selected"] == 1


def test_survivors_are_sorted_newest_to_oldest_before_fetch(tmp_path):
    client = _FakeClient([[
        _summary("u/old", "3 days ago"),
        _summary("u/new", "Just now"),
        _summary("u/mid", "1 day ago"),
    ]])
    result, _detail = _run(client, _cfg(tmp_path))
    assert client.fetched == ["u/new", "u/mid", "u/old"]


def test_sort_is_stable_within_equal_age_keeping_naukri_order(tmp_path):
    client = _FakeClient([[
        _summary("u/x", "1 day ago"),
        _summary("u/y", "Just now"),
        _summary("u/z", "Just now"),
    ]])
    _run(client, _cfg(tmp_path))
    # y and z are both age 0 -> keep their discovered order; x (age 1) last
    assert client.fetched == ["u/y", "u/z", "u/x"]


def test_fresh_cap_of_60_limits_what_enters_fetch_and_parse(tmp_path):
    cards = [_summary(f"u/{i:02d}", "Just now") for i in range(65)]
    client = _FakeClient([cards])
    # raise the per-query ceiling so all 65 reach the freshness gate; the
    # discovery_fresh_job_limit (60) is what we are asserting binds here
    result, detail = _run(client, _cfg(tmp_path, discovery_max_jobs_per_query=100))

    assert len(client.fetched) == 60
    assert len(result.job_ids) == 60
    assert detail["within_window"] == 65
    assert detail["fresh_cap"] == 60
    assert detail["selected"] == 60
    # newest-first + stable -> the first 60 discovered cards, in order
    assert client.fetched == [f"u/{i:02d}" for i in range(60)]


def test_configurable_window_and_cap_are_honoured(tmp_path):
    cards = [_summary(f"u/{i}", "Just now") for i in range(10)]
    cards.append(_summary("u/day5", "5 days ago"))
    client = _FakeClient([cards])
    cfg = _cfg(tmp_path, discovery_freshness_days=7, discovery_fresh_job_limit=4)
    result, detail = _run(client, cfg)

    assert detail["window_days"] == 7
    assert detail["within_window"] == 11  # the "5 days ago" card is now inside 7
    assert detail["fresh_cap"] == 4
    assert detail["selected"] == 4
    assert len(client.fetched) == 4


def test_empty_survivor_set_is_not_a_failure_and_still_emits_diagnostics(tmp_path):
    client = _FakeClient([[
        _summary("u/stale", "10 days ago"),
        _summary("u/unknown", "Reposted"),
    ]])
    result, detail = _run(client, _cfg(tmp_path))

    assert client.fetched == []
    assert result.job_ids == []
    assert result.jobs_new == 0
    assert result.total_failure is False  # queries succeeded; just nothing fresh
    assert detail["discovered_cards"] == 2
    assert detail["within_window"] == 0
    assert detail["stale_excluded"] == 1
    assert detail["unknown_excluded"] == 1
    assert detail["selected"] == 0


def test_dedup_preserved_and_first_seen_freshness_label_wins(tmp_path):
    # u2 appears in both queries with different labels; the FIRST sighting
    # ("1 day ago") must be the one used, so u2 sorts after the age-0 jobs.
    q1 = [_summary("u/1", "Just now"), _summary("u/2", "1 day ago")]
    q2 = [_summary("u/2", "Just now"), _summary("u/3", "Just now")]
    client = _FakeClient([q1, q2])
    cfg = _cfg(tmp_path, discovery_queries=["Data Scientist", "ML Engineer"])
    result, detail = _run(client, cfg)

    assert detail["discovered_cards"] == 3  # u/2 deduped
    assert detail["within_window"] == 3
    # u/1 (0), u/3 (0) keep query order; u/2 (1) last -> first-label-wins
    assert client.fetched == ["u/1", "u/3", "u/2"]
    assert len(result.job_ids) == 3


def test_hard_ceiling_still_applies_before_the_freshness_gate(tmp_path):
    cards = [_summary(f"u/{i:02d}", "Just now") for i in range(10)]
    client = _FakeClient([cards])
    cfg = _cfg(tmp_path, discovery_max_total_jobs=5, discovery_fresh_job_limit=60)
    result, detail = _run(client, cfg)

    assert detail["discovered_cards"] == 10   # counted before the ceiling
    assert detail["within_window"] == 5       # ceiling cut it to 5 before the gate
    assert detail["selected"] == 5
    assert len(client.fetched) == 5


def test_freshness_runevent_detail_has_all_diagnostic_fields(tmp_path):
    client = _FakeClient([[
        _summary("u/a", "Just now"),
        _summary("u/b", "2 days ago"),
        _summary("u/c", "9 days ago"),
        _summary("u/d", None),
    ]])
    _result, detail = _run(client, _cfg(tmp_path))
    assert set(detail) == {
        "discovered_cards", "window_days", "within_window",
        "stale_excluded", "unknown_excluded", "fresh_cap", "selected",
    }
    assert detail == {
        "discovered_cards": 4, "window_days": 7, "within_window": 2,
        "stale_excluded": 1, "unknown_excluded": 1, "fresh_cap": 60, "selected": 2,
    }
