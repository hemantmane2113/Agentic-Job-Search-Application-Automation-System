"""Researcher improvements: report disagreements with the Naukri post, and shorter history per model turn."""

from __future__ import annotations

import json

from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent.config import Settings
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import JobExtraction
from naukri_agent.jobs.models import JobType
from naukri_agent.research_agent import runner as runner_mod
from naukri_agent.research_agent.loop import SYSTEM_PROMPT, compact_for_model, run_agent
from naukri_agent.research_agent.models import ResearchReport
from naukri_agent.research_agent.runner import ResearchOutcome, ResearchRunResult, format_report, select_jobs
from naukri_agent.research_agent.tools import ToolBox

from .digest_fakes import in_memory_factory
from .test_research_agent import BRIEF, CAREERS, JOB, ROLE, ScriptedClient, call, make_box, page, submit, turn
from .test_research_runner import NOW, cfg, seed

# --- differences from the Naukri post ---------------------------------------------------------------------------------


def test_the_agent_is_told_to_compare_the_company_page_with_the_naukri_post():
    assert "differences" in SYSTEM_PROMPT and "contract vs full-time" in SYSTEM_PROMPT


def test_differences_survive_validation_cleaning_and_the_run():
    client = ScriptedClient([turn(call("fetch_page", url=CAREERS)), turn(submit(
        differences=["Company page says Contract; Naukri post does not say", "  Remote   only  "] + [f"d{i}" for i in range(9)],
    ))])
    r = run_agent(client, make_box(), BRIEF).report
    assert r.differences[0].startswith("Company page says Contract") and r.differences[1] == "Remote only" and len(r.differences) == 5


def test_the_report_message_shows_differences_before_flags():
    rep = ResearchReport(company_summary="A firm.", differences=["Contract, not full-time"], red_flags=["vague employer"])
    text = format_report({"title": "DS", "company": "Acme", "url": "https://www.naukri.com/j"}, rep)
    assert "Differs from the Naukri post: Contract, not full-time" in text
    assert text.index("Differs from") < text.index("Flags:")
    assert "Differs from" not in format_report({"title": "t", "company": "c", "url": "u"}, ResearchReport(company_summary="x"))


def test_get_job_shows_the_known_employment_type_and_the_select_step_supplies_it(tmp_path):
    f = in_memory_factory()
    ids = seed(f, [("withtype", "040926002001", "company_site"), ("unknown", "040926002002", "company_site"), ("none", "040926002003", "company_site")])
    with session_scope(f) as s:
        s.add(JobExtraction(job_id=ids["withtype"], extraction_version=1, is_current=True, job_type=JobType.CONTRACT))
        s.add(JobExtraction(job_id=ids["unknown"], extraction_version=1, is_current=True, job_type=JobType.UNKNOWN))
        s.flush()
        picked = {j["job_id"]: j for j in select_jobs(s, cfg(tmp_path), NOW, job_id=None, limit=5)}
    assert picked[ids["withtype"]]["employment_type"] == "contract"
    assert picked[ids["unknown"]]["employment_type"] is None and picked[ids["none"]]["employment_type"] is None
    box = ToolBox(job={**JOB, "employment_type": "contract"}, fetch=lambda u: page(u))
    assert json.loads(box.call("get_job", {})[0])["employment_type"] == "contract"


# --- shorter history ----------------------------------------------------------------------------------------------------------


def tool_msg(i, payload):
    return {"role": "tool", "tool_call_id": f"t{i}", "content": json.dumps(payload)}


def convo():
    big_page = {"untrusted_data": True, "url": CAREERS, "title": "Acme Careers", "text": "p" * 5000,
                "career_links": [{"text": f"l{i}", "url": f"https://acme.example/j{i}"} for i in range(12)], "truncated": False}
    search = {"untrusted_data": True, "results": [{"title": f"r{i}", "url": f"https://acme.example/r{i}", "snippet": "s" * 400} for i in range(8)]}
    return [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        tool_msg(1, search),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t2"}]},
        tool_msg(2, big_page),
    ]


def test_older_tool_results_are_shortened_but_keep_their_facts():
    msgs = convo()
    out = compact_for_model(msgs)
    old_search = json.loads(out[3]["content"])
    assert len(old_search["results"]) == 5 and all(len(r["snippet"]) == 120 for r in old_search["results"])
    assert old_search["results"][0]["url"] == "https://acme.example/r0"  # URLs survive: the agent can still cite them
    assert len(out[3]["content"]) < len(msgs[3]["content"])


def test_the_latest_results_are_kept_whole_and_the_original_history_is_untouched():
    msgs = convo()
    before = json.dumps(msgs)
    out = compact_for_model(msgs)
    latest = json.loads(out[5]["content"])
    assert len(latest["text"]) == 5000 and len(latest["career_links"]) == 12  # not yet shortened
    assert json.dumps(msgs) == before


def test_an_old_page_keeps_its_url_title_and_first_links_but_not_its_long_text():
    msgs = convo() + [{"role": "assistant", "content": None, "tool_calls": [{"id": "t3"}]}, tool_msg(3, {"untrusted_data": True, "error": "x"})]
    old = json.loads(compact_for_model(msgs)[5]["content"])
    assert old["url"] == CAREERS and old["title"] == "Acme Careers"
    assert len(old["text"]) < 800 and old["text"].endswith("[shortened]") and len(old["career_links"]) == 6


def test_non_json_tool_content_is_cut_not_crashed_on():
    msgs = [{"role": "assistant", "content": None}, {"role": "tool", "tool_call_id": "x", "content": "n" * 5000},
            {"role": "assistant", "content": None}]
    assert len(compact_for_model(msgs)[1]["content"]) == 700


def test_the_model_really_receives_the_shorter_history_and_can_be_switched_off():
    def script():
        return [turn(call("fetch_page", url=CAREERS)), turn(call("web_search", query="x")), turn(submit())]

    long_page = lambda url: page(url, text="w" * 6000)
    compact = ScriptedClient(script())
    run_agent(compact, make_box(fetch=long_page), BRIEF, compact=True)
    full = ScriptedClient(script())
    run_agent(full, make_box(fetch=long_page), BRIEF, compact=False)

    def size(client):
        return len(json.dumps(client.requests[-1]["messages"]))

    assert size(compact) < size(full) - 2500  # the first page was shortened by the final turn
    first_fetch = [m for m in compact.requests[-1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "[shortened]" in first_fetch


# --- tokens reported -----------------------------------------------------------------------------------------------------------


def test_the_command_reports_the_tokens_the_model_used(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None))
    res = ResearchRunResult(considered=1, researched=1, outcomes=[
        ResearchOutcome(job_id=1, title="t", company="c", status="ok", prompt_tokens=9000, completion_tokens=400, message="R")])
    monkeypatch.setattr(runner_mod, "run_research", lambda s, **kw: res)
    out = CliRunner().invoke(cli_main.cli, ["research-jobs", "--dry-run"]).output
    assert "Model tokens used: 9400" in out


def test_compaction_is_off_by_default_because_testing_showed_no_consistent_gain():
    assert Settings(_env_file=None).research_compact_history is False
