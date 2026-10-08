"""Discovery must give every searched ROLE an equal turn. Before this, cards were cut to the first
200 in search order, so with five roles only the first ones (Data Scientist, ML Engineer) ever
reached the freshness gate and the daily cap; Data Analyst and AI/ML jobs were never fetched."""

from __future__ import annotations

import pytest

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import RunEvent
from naukri_agent.orchestration.discovery import _fair_card_order, _fair_select, _round_robin, discover_and_store

from .test_discovery_freshness import _cfg, _FakeClient, _summary
from .digest_fakes import in_memory_factory

ROLES = ["Data Scientist", "ML Engineer", "AI Engineer", "AI/ML Engineer", "Data Analyst"]


def run(client, cfg, role_groups=None):
    factory = in_memory_factory()
    with session_scope(factory) as s:
        result, _ = discover_and_store(
            s, client, profile=None, settings=cfg, run_id=1, seq_start=0,
            role_groups={} if role_groups is None else role_groups,
        )
        roles = dict(s.query(RunEvent).filter_by(stage="discover_roles").one().detail)["groups"]
        fresh = dict(s.query(RunEvent).filter_by(stage="discover_freshness").one().detail)
    return result, roles, fresh


def tag(role: str) -> str:
    return role.replace("/", "_")  # "AI/ML Engineer" must not add a path segment to the fake URL


def cards(role: str, n: int, label="Just now", city="c"):
    return [_summary(f"{tag(role)}/{city}/{i:03d}", label) for i in range(n)]


def test_a_cut_off_no_longer_drops_the_last_roles(tmp_path):
    """The real shape: 5 roles x 3 cities x 20 fresh cards, ceiling 200, daily cap 60."""
    queries, results = [], []
    for role in ROLES:
        for city in ("Pune", "Mumbai", "Chennai"):
            queries.append(f"{role}@{city}")
            results.append(cards(role, 20, city=city))
    client = _FakeClient(results)
    cfg = _cfg(tmp_path, discovery_queries=queries, discovery_max_total_jobs=200, discovery_fresh_job_limit=60)
    result, roles, fresh = run(client, cfg)

    assert fresh["discovered_cards"] == 300 and len(client.fetched) == 60
    per = {r: sum(1 for u in client.fetched if u.startswith(tag(r) + "/")) for r in ROLES}
    assert per == {r: 12 for r in ROLES}  # an equal share each; the old code gave Data Analyst none
    assert all(roles[r]["selected"] == 12 for r in ROLES)


def test_the_card_ceiling_is_shared_equally_too(tmp_path):
    queries = [f"{r}@Pune" for r in ROLES]
    client = _FakeClient([cards(r, 40) for r in ROLES])
    cfg = _cfg(tmp_path, discovery_queries=queries, discovery_max_total_jobs=50, discovery_fresh_job_limit=60)
    _result, roles, fresh = run(client, cfg)
    assert fresh["discovered_cards"] == 200 and fresh["within_window"] == 50
    assert all(roles[r]["within_window"] == 10 for r in ROLES)


def test_a_role_with_few_fresh_jobs_leaves_its_unused_slots_to_the_others(tmp_path):
    client = _FakeClient([cards("A", 2), cards("B", 100)])
    cfg = _cfg(tmp_path, discovery_queries=["A@Pune", "B@Pune"], discovery_fresh_job_limit=10, discovery_max_jobs_per_query=200)
    _result, roles, _fresh = run(client, cfg)
    assert roles["A"]["selected"] == 2 and roles["B"]["selected"] == 8


def test_an_older_role_is_not_crowded_out_by_a_fresher_one(tmp_path):
    client = _FakeClient([cards("old", 20, label="5 days ago"), cards("new", 20, label="Just now")])
    cfg = _cfg(tmp_path, discovery_queries=["old@Pune", "new@Pune"], discovery_fresh_job_limit=10)
    _result, roles, _fresh = run(client, cfg)
    assert roles["old"]["selected"] == 5 and roles["new"]["selected"] == 5


def test_within_a_role_the_freshest_still_win(tmp_path):
    client = _FakeClient([[_summary("A/old", "4 days ago"), _summary("A/new", "Just now"), _summary("A/mid", "2 days ago")]])
    cfg = _cfg(tmp_path, discovery_queries=["A@Pune"], discovery_fresh_job_limit=2)
    run(client, cfg)
    assert client.fetched == ["A/new", "A/mid"]


def test_a_roles_cities_take_turns_under_the_ceiling(tmp_path):
    client = _FakeClient([cards("A", 10, city="pune"), cards("A", 10, city="mumbai"), cards("A", 10, city="chennai")])
    cfg = _cfg(tmp_path, discovery_queries=["A@Pune", "A@Mumbai", "A@Chennai"], discovery_max_total_jobs=9)
    run(client, cfg)
    by_city = {c: sum(1 for u in client.fetched if f"/{c}/" in u) for c in ("pune", "mumbai", "chennai")}
    assert by_city == {"pune": 3, "mumbai": 3, "chennai": 3}


def test_a_card_found_by_two_roles_is_fetched_once(tmp_path):
    shared = _summary("shared/1", "Just now")
    client = _FakeClient([[shared, _summary("A/1", "Just now")], [shared, _summary("B/1", "Just now")]])
    cfg = _cfg(tmp_path, discovery_queries=["A@Pune", "B@Pune"])
    result, roles, fresh = run(client, cfg)
    assert sorted(client.fetched) == ["A/1", "B/1", "shared/1"] and fresh["discovered_cards"] == 3
    assert sum(r["cards"] for r in roles.values()) == 3


# --- the pure helpers ----------------------------------------------------------------------------------------------


def test_round_robin_takes_turns_and_handles_uneven_lanes():
    assert _round_robin([[1, 2, 3], [10], [100, 200]]) == [1, 10, 100, 2, 200, 3]
    assert _round_robin([]) == [] and _round_robin([[], []]) == []


def test_fair_card_order_alternates_roles():
    order = _fair_card_order({"A": [["a1", "a2"]], "B": [["b1", "b2"]]})
    assert order == [("A", "a1"), ("B", "b1"), ("A", "a2"), ("B", "b2")]


def test_fair_select_respects_the_limit_and_age_order():
    aged = [("A", "a-old", 3), ("A", "a-new", 0), ("B", "b-new", 0), ("B", "b-old", 5)]
    assert _fair_select(aged, 3) == [("A", "a-new"), ("B", "b-new"), ("A", "a-old")]
    assert _fair_select([], 5) == []


# --- grouping by resume: Data Scientist vs the AI/ML-type searches -------------------------------------------------

from naukri_agent.orchestration.discovery import _role_groups_from_registry  # noqa: E402

REGISTRY = """
resumes:
  - id: data_scientist
    file: resumes/data_scientist.pdf
    roles: ["Data Scientist", "Data Science"]
  - id: ai_ml_engineer
    file: resumes/ai_ml_engineer.pdf
    roles: ["Machine Learning Engineer", "AI Engineer", "AI/ML Engineer"]
"""
SEARCHES = ["Data Scientist", "Machine Learning Engineer", "AI Engineer", "AI/ML Engineer"]


def _grouped_run(tmp_path, *, explicit=None):
    reg = tmp_path / "resumes.yaml"
    reg.write_text(REGISTRY, encoding="utf-8")
    queries, results = [], []
    for role in SEARCHES:
        for city in ("Pune", "Mumbai"):
            queries.append(f"{role}@{city}")
            results.append(cards(role, 20, city=city))
    client = _FakeClient(results)
    cfg = _cfg(tmp_path, discovery_queries=queries, discovery_max_total_jobs=200,
               discovery_fresh_job_limit=60, resume_registry_path=reg)
    factory = in_memory_factory()
    with session_scope(factory) as s:
        # role_groups=None -> built from the registry file, as in a real run
        discover_and_store(s, client, profile=None, settings=cfg, run_id=1, seq_start=0, role_groups=explicit)
        groups = dict(s.query(RunEvent).filter_by(stage="discover_roles").one().detail)["groups"]
    return client, groups


def test_data_scientist_gets_half_and_the_three_ai_ml_searches_share_the_other_half(tmp_path):
    client, groups = _grouped_run(tmp_path)
    ds = [u for u in client.fetched if u.startswith("Data Scientist/")]
    assert len(ds) == 30 and len(client.fetched) == 60
    assert groups["data_scientist"]["selected"] == 30 and groups["ai_ml_engineer"]["selected"] == 30
    # inside the AI/ML half, the three searches take equal turns (10 each), not first-search-wins
    for role in ("Machine Learning Engineer", "AI Engineer", "AI_ML Engineer"):
        assert sum(1 for u in client.fetched if u.startswith(role + "/")) == 10


def test_without_grouping_the_four_searches_would_each_get_a_quarter(tmp_path):
    client, groups = _grouped_run(tmp_path, explicit={})
    assert len([u for u in client.fetched if u.startswith("Data Scientist/")]) == 15
    assert set(groups) == set(SEARCHES)


def test_groups_come_from_the_resume_registry_roles(tmp_path):
    reg = tmp_path / "resumes.yaml"
    reg.write_text(REGISTRY, encoding="utf-8")
    cfg = _cfg(tmp_path, resume_registry_path=reg)
    g = _role_groups_from_registry(cfg)
    assert g["data scientist"] == "data_scientist" and g["ai/ml engineer"] == "ai_ml_engineer"
    assert g["machine learning engineer"] == "ai_ml_engineer" and "data analyst" not in g


def test_a_missing_registry_just_means_no_grouping(tmp_path):
    assert _role_groups_from_registry(_cfg(tmp_path, resume_registry_path=tmp_path / "nope.yaml")) == {}


def test_a_search_role_no_resume_lists_keeps_its_own_group(tmp_path):
    reg = tmp_path / "resumes.yaml"
    reg.write_text(REGISTRY, encoding="utf-8")
    queries = ["Data Scientist@Pune", "Quant Researcher@Pune"]
    client = _FakeClient([cards("Data Scientist", 20), cards("Quant", 20)])
    cfg = _cfg(tmp_path, discovery_queries=queries, discovery_fresh_job_limit=10, resume_registry_path=reg)
    factory = in_memory_factory()
    with session_scope(factory) as s:
        discover_and_store(s, client, profile=None, settings=cfg, run_id=1, seq_start=0)
        groups = dict(s.query(RunEvent).filter_by(stage="discover_roles").one().detail)["groups"]
    assert set(groups) == {"data_scientist", "Quant Researcher"}
    assert groups["data_scientist"]["selected"] == 5 and groups["Quant Researcher"]["selected"] == 5
