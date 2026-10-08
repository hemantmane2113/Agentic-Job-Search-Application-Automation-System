"""Two daily-run improvements: (1) the phone is told how many jobs are ready for telegram-apply,
without the daily run ever being able to apply; (2) a parse that fails for a fixable reason is
retried once, within a per-run budget."""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace

import pytest

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import Job, RunEvent
from naukri_agent.orchestration import pipeline as pipe
from naukri_agent.orchestration.pipeline import _apply_ready_text, _parse_with_retry, run_daily_recommendations

from .digest_fakes import in_memory_factory, settings
from .test_daily_pipeline import _VALID_EXTRACTION, _fake_discover, _PerJobLLM, _prep

NOW = datetime.datetime(2026, 9, 9, tzinfo=datetime.UTC)


def native(specs):
    base = _fake_discover(specs)

    def fn(session, profile, settings_, run_id, seq):
        res, seq = base(session, profile, settings_, run_id, seq)
        for jid in res.job_ids:
            session.get(Job, jid).apply_type = "native"
        return res, seq

    return fn


def run(tmp_path, specs, *, notify=None, llm=None, discover=None, **cfg_over):
    _prep(tmp_path)
    cfg = settings(tmp_path, dry_run=True, threshold_review=0, threshold_accept=0, **cfg_over)
    factory = in_memory_factory()
    result = run_daily_recommendations(
        cfg, now=NOW, discover_fn=discover or native(specs),
        extraction_provider=llm or _PerJobLLM({}, _VALID_EXTRACTION),
        session_factory=factory, telegram_notify=notify,
    )
    return result, factory


def events(factory, stage):
    with session_scope(factory) as s:
        return [
            (e.status.value.upper(), dict(e.detail or {}))
            for e in s.query(RunEvent).filter_by(stage=stage).order_by(RunEvent.seq)
        ]


# --- the ping --------------------------------------------------------------------------------------------------------


def test_the_phone_is_told_which_jobs_are_ready_and_nothing_is_applied(tmp_path):
    sent = []
    result, factory = run(tmp_path, [("ds", "040926000001", 0), ("mle", "040926000002", 0)], notify=sent.append)
    assert result.status == "COMPLETED" and len(sent) == 1
    text = sent[0]
    assert "2 job(s) ready to apply via Telegram" in text
    assert "ds role - Acme" in text and "mle role - Acme" in text and "resume " in text
    assert "up to 3 today" in text and "nothing is applied until you tap Yes" in text
    assert "http" not in text  # no links or credentials in a chat message
    assert events(factory, "telegram_ping") == [("OK", {"jobs": 2})]


def test_a_long_list_is_cut_to_five_with_a_count_of_the_rest(tmp_path):
    sent = []
    specs = [(f"j{i}", f"04092600010{i}", 0) for i in range(7)]
    run(tmp_path, specs, notify=sent.append)
    assert "7 job(s) ready" in sent[0] and "... and 2 more" in sent[0]
    numbered = [ln for ln in sent[0].splitlines() if ln[:2] in {f"{i}." for i in range(1, 10)}]
    assert len(numbered) == 5


def test_no_ready_jobs_means_no_message(tmp_path):
    sent = []
    # a job without a Naukri Apply button is not offered by telegram-apply
    result, factory = run(tmp_path, [], notify=sent.append, discover=_fake_discover([("co", "040926000201", 0)]))
    assert sent == [] and events(factory, "telegram_ping") == []


def test_the_ping_can_be_switched_off(tmp_path):
    sent = []
    run(tmp_path, [("ds", "040926000301", 0)], notify=sent.append, telegram_ping_apply_ready=False)
    assert sent == []


def test_without_telegram_set_up_nothing_is_attempted(tmp_path):
    result, factory = run(tmp_path, [("ds", "040926000401", 0)], notify=None)
    assert result.status == "COMPLETED" and events(factory, "telegram_ping") == []


def test_a_used_up_daily_limit_is_said_plainly(tmp_path):
    sent = []
    run(tmp_path, [("ds", "040926000501", 0)], notify=sent.append, auto_apply_daily_cap=0)
    assert "already used" in sent[0] and "up to" not in sent[0]


def test_a_failing_ping_never_fails_the_daily_run(tmp_path):
    def boom(_text):
        raise RuntimeError("telegram down, token=SECRET")

    result, factory = run(tmp_path, [("ds", "040926000601", 0)], notify=boom)
    assert result.status == "COMPLETED"
    assert events(factory, "telegram_ping")[0][0] == "FAILED"
    assert "SECRET" not in json.dumps(events(factory, "telegram_ping"))


def test_the_message_names_the_resume_or_says_no_role_match():
    jobs = [
        {"title": "A", "company": "X", "score": 91.2, "resume_id": "data_scientist"},
        {"title": "B", "company": "Y", "score": 84.0, "resume_id": None},
    ]
    text = _apply_ready_text(jobs, 3, 3)
    assert "score 91, resume data_scientist" in text and "score 84, resume no role match" in text


# --- parse retry -----------------------------------------------------------------------------------------------------


class Flaky(_PerJobLLM):
    """Fails (not JSON) the first `fail_first` calls for each job text, then answers properly."""

    def __init__(self, fail_first=1):
        super().__init__({}, _VALID_EXTRACTION)
        self.fail_first = fail_first
        self.seen = {}

    def complete(self, system, prompt, *, json_mode=False):
        self.calls += 1
        key = prompt[:200]
        self.seen[key] = self.seen.get(key, 0) + 1
        return "not json at all" if self.seen[key] <= self.fail_first else _VALID_EXTRACTION


def test_a_failed_parse_is_retried_once_and_recovers(tmp_path):
    llm = Flaky(fail_first=1)
    result, factory = run(tmp_path, [("ds", "040926000701", 0)], llm=llm)
    assert llm.calls == 2
    assert events(factory, "parse") == [("OK", {"retried": True})]
    assert not any("could not be parsed" in n for n in result.notes)


def test_only_one_retry_per_job(tmp_path):
    llm = Flaky(fail_first=5)
    result, factory = run(tmp_path, [("ds", "040926000801", 0)], llm=llm)
    assert llm.calls == 2  # the first try and one retry, never a third
    status, detail = events(factory, "parse")[0]
    assert status == "FAILED" and detail["retried"] is True and detail["error"].startswith("invalid_json")
    assert any("could not be parsed" in n for n in result.notes)


def test_the_retry_budget_is_shared_across_the_run(tmp_path):
    llm = Flaky(fail_first=9)
    specs = [("a", "040926000901", 0), ("b", "040926000902", 0), ("c", "040926000903", 0)]
    run(tmp_path, specs, llm=llm, parse_retries_per_run=1)
    assert llm.calls == 4  # three first tries + the single retry the run is allowed


def test_retries_can_be_turned_off(tmp_path):
    llm = Flaky(fail_first=9)
    run(tmp_path, [("a", "040926001001", 0)], llm=llm, parse_retries_per_run=0)
    assert llm.calls == 1


@pytest.mark.parametrize(
    "error, retried",
    [
        ("llm_error: timed out", True),
        ("invalid_json: x", True),
        ("invalid_schema: x", True),
        ("jd_too_long:15671", False),
        ("something_else", False),
    ],
)
def test_only_fixable_failures_are_retried(monkeypatch, error, retried):
    calls = []

    class FakeParser:
        def __init__(self, provider):
            pass

        def parse(self, job):
            calls.append(1)
            return SimpleNamespace(success=len(calls) > 1 and retried, error=error)

    monkeypatch.setattr(pipe, "JobParser", FakeParser)
    budget = [3]
    _pr, did = _parse_with_retry(object(), SimpleNamespace(id=1), budget)
    assert did is retried and len(calls) == (2 if retried else 1)
    assert budget == [2 if retried else 3]


def test_a_success_is_never_retried(monkeypatch):
    calls = []

    class FakeParser:
        def __init__(self, provider):
            pass

        def parse(self, job):
            calls.append(1)
            return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr(pipe, "JobParser", FakeParser)
    _pr, did = _parse_with_retry(object(), SimpleNamespace(id=1), [5])
    assert did is False and len(calls) == 1
