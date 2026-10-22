"""The researcher stops starting new jobs when the day's model allowance is nearly used up."""

from __future__ import annotations

import datetime
import json

from naukri_agent.config import Settings
from naukri_agent.database.base import session_scope
from naukri_agent.database.repositories import add_job_research, research_tokens_since
from naukri_agent.research_agent.runner import run_research

from .digest_fakes import in_memory_factory
from .test_research_agent import CAREERS, FakeSearch, ScriptedClient, call, page, submit, turn
from .test_research_runner import NOW, FakeReader, cfg, seed


def heavy_job(tokens_per_turn=9000):
    """A job that costs 3 turns x tokens_per_turn."""
    return [turn(call("get_company_page"), pt=tokens_per_turn, ct=0),
            turn(call("fetch_page", url=CAREERS), pt=tokens_per_turn, ct=0),
            turn(submit(), pt=tokens_per_turn, ct=0)]


def go(tmp_path, factory, script, *, email=None, **over):
    client = ScriptedClient(script)
    client.model = "fake-model"
    result = run_research(
        cfg(tmp_path, **over), session_factory=factory, chat_client=client, search=FakeSearch(), fetch=lambda u: page(u),
        reader=FakeReader(), email=email, now=NOW, fetch_raw_fn=lambda u: (u, "<p>x</p>"),
    )
    return result, client


def test_the_defaults_fit_a_full_day_inside_groqs_free_allowance():
    s = Settings(_env_file=None)
    assert s.research_max_jobs == 10 and s.research_daily_token_budget == 180_000
    assert s.research_max_jobs * s.research_tokens_per_job_estimate <= 200_000  # 10 jobs at the estimate fit the allowance


def test_a_job_is_not_started_if_it_would_probably_go_over_the_budget(tmp_path):
    f = in_memory_factory()
    seed(f, [("a", "040926200101", "company_site"), ("b", "040926200102", "company_site"), ("c", "040926200103", "company_site")])
    sent = []
    result, client = go(tmp_path, f, heavy_job() * 3, email=lambda s, b: sent.append((s, b)),
                        research_daily_token_budget=40_000, research_tokens_per_job_estimate=16_000)
    # job 1: 0 + 16k <= 40k, costs 27k.  job 2: 27k + max(16k, 27k) = 54k > 40k, so b and c wait.
    assert result.researched == 1 and len(client.requests) == 3
    assert any("daily model budget reached" in n and "2 job(s) wait" in n for n in result.notes)
    assert "Left for the next run" in sent[0][1] and "b role - b Co" in sent[0][1] and "c role - c Co" in sent[0][1]


def test_earlier_usage_in_the_last_24_hours_counts_even_from_a_previous_run(tmp_path):
    f = in_memory_factory()
    ids = seed(f, [("a", "040926200201", "company_site")])
    with session_scope(f) as s:
        add_job_research(s, job_id=ids["a"], status="failed", model="m", prompt_tokens=150_000, completion_tokens=25_000,
                         created_at=NOW - datetime.timedelta(hours=3))
    result, client = go(tmp_path, f, heavy_job())
    assert client.requests == [] and result.researched == 0 and any("daily model budget reached" in n for n in result.notes)


def test_usage_older_than_a_day_does_not_count(tmp_path):
    f = in_memory_factory()
    ids = seed(f, [("a", "040926200301", "company_site")])
    with session_scope(f) as s:
        add_job_research(s, job_id=ids["a"], status="failed", model="m", prompt_tokens=190_000, completion_tokens=0,
                         created_at=NOW - datetime.timedelta(hours=30))
        assert research_tokens_since(s, NOW - datetime.timedelta(hours=24)) == 0
    result, client = go(tmp_path, f, heavy_job())
    assert result.researched == 1 and len(client.requests) == 3


def test_a_dry_run_reads_the_budget_but_does_not_spend_or_save_any_of_it(tmp_path):
    f = in_memory_factory()
    seed(f, [("a", "040926200401", "company_site")])
    client = ScriptedClient(heavy_job())
    client.model = "fake-model"
    result = run_research(cfg(tmp_path), session_factory=f, chat_client=client, search=FakeSearch(), fetch=lambda u: page(u),
                          reader=FakeReader(), now=NOW, dry_run=True, fetch_raw_fn=lambda u: (u, "<p>x</p>"))
    assert result.researched == 1
    with session_scope(f) as s:
        assert research_tokens_since(s, NOW - datetime.timedelta(days=1)) == 0  # nothing saved


def test_a_normal_day_with_room_to_spare_runs_every_job(tmp_path):
    f = in_memory_factory()
    seed(f, [(f"j{i}", f"04092620050{i}", "company_site") for i in range(4)])
    result, client = go(tmp_path, f, heavy_job(2000) * 4)  # 6k a job, 24k in all
    assert result.researched == 4 and not any("budget" in n for n in result.notes)
    with session_scope(f) as s:
        assert research_tokens_since(s, NOW - datetime.timedelta(hours=1)) == 24_000
    assert json.loads("{}") == {}
