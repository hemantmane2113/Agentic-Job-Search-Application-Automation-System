"""Discovery reads jobs it has not read before first, and a posting that repeats its own text can still be parsed."""

from __future__ import annotations

from naukri_agent.browser.models import JobDetail, JobListingSummary
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import Job, JobExtraction, RunEvent
from naukri_agent.jobs.parser import (
    MAX_JD_CHARS_FOR_LLM,
    JobParser,
    description_for_llm,
    drop_repeated_lines,
)
from naukri_agent.orchestration.discovery import discover_and_store

from .digest_fakes import in_memory_factory, settings as _settings
from .test_job_parser import VALID_RESPONSE, FakeProvider, _job


def _cfg(tmp_path, **over):
    base = dict(discovery_queries=["Data Scientist @ Pune", "Data Scientist @ Delhi", "Data Scientist @ Noida"],
                discovery_freshness_days=7, discovery_fresh_job_limit=4, discovery_max_total_jobs=200,
                resume_registry_path=tmp_path / "none.yaml")
    base.update(over)
    return _settings(tmp_path, **base)


def _url(n: int) -> str:
    return f"https://www.naukri.com/job-listings-role-{n}-pune-{100000000000 + n}"


def _card(n: int, posted: str = "Just now") -> JobListingSummary:
    return JobListingSummary(title="t", company="c", location="Pune", url=_url(n), posted_text=posted)


class Client:
    def __init__(self, searches: list[list[JobListingSummary]]):
        self._searches, self._n, self.fetched = searches, 0, []

    def search_jobs(self, role, location=""):
        out = self._searches[self._n] if self._n < len(self._searches) else []
        self._n += 1
        return out

    def fetch_job_detail(self, url):
        self.fetched.append(url)
        return JobDetail(url=url, title="T", company="C", location="Pune", description=f"JD {url}")


def _store(session, n: int, *, parsed: bool, description: str = "short description") -> Job:
    job = Job(url=_url(n), external_id=str(100000000000 + n), title="T", company="C", location="Pune",
              description=description, content_fingerprint=f"fp{n}")
    session.add(job)
    session.flush()
    if parsed:
        session.add(JobExtraction(job_id=job.id, extraction_version=1, is_current=True))
        session.flush()
    return job


def _run(client, cfg, *, seed=None):
    with session_scope(in_memory_factory()) as s:
        if seed:
            seed(s)
        result, _ = discover_and_store(s, client, profile=None, settings=cfg, run_id=1, seq_start=0, role_groups={})
        detail = dict(s.query(RunEvent).filter_by(stage="discover_freshness").one().detail)
    return result, detail


# --- which jobs are read -------------------------------------------------------------------------------------------


def test_jobs_not_read_before_come_first_and_known_ones_only_fill_the_rest(tmp_path):
    cards = [_card(n) for n in range(1, 7)]  # 6 fresh cards; 1, 2, 3 were read and parsed on an earlier day
    client = Client([cards])
    cfg = _cfg(tmp_path, discovery_queries=["Data Scientist"], discovery_fresh_job_limit=4)

    def seed(s):
        for n in (1, 2, 3):
            _store(s, n, parsed=True)

    _result, detail = _run(client, cfg, seed=seed)
    assert set(client.fetched[:3]) == {_url(4), _url(5), _url(6)}  # the three unseen ones first
    assert len(client.fetched) == 4 and client.fetched[3] in {_url(1), _url(2), _url(3)}  # one slot left over
    assert (detail["selected_unseen"], detail["selected_already_read"], detail["unseen_in_window"]) == (3, 1, 3)


def test_a_known_job_that_never_parsed_is_read_again_like_an_unseen_one(tmp_path):
    cards = [_card(n) for n in range(1, 5)]
    client = Client([cards])
    cfg = _cfg(tmp_path, discovery_queries=["Data Scientist"], discovery_fresh_job_limit=2)

    def seed(s):
        _store(s, 1, parsed=True)
        _store(s, 2, parsed=True)
        _store(s, 3, parsed=False)  # in the database, but its parse failed (the Jet2 case)

    _run(client, cfg, seed=seed)
    assert _url(3) in client.fetched and _url(4) in client.fetched
    assert _url(1) not in client.fetched and _url(2) not in client.fetched


def test_a_known_job_that_can_never_be_parsed_is_not_read_again_and_again(tmp_path):
    too_long = "\n".join(f"line number {i} says something different" for i in range(400))  # > 8000, no repeats
    assert description_for_llm(too_long) is None
    client = Client([[_card(1), _card(2)]])
    cfg = _cfg(tmp_path, discovery_queries=["Data Scientist"], discovery_fresh_job_limit=1)

    def seed(s):
        _store(s, 1, parsed=False, description=too_long)  # nothing more to learn from it

    _run(client, cfg, seed=seed)
    assert client.fetched == [_url(2)]


def test_with_the_setting_off_the_freshest_jobs_win_whether_or_not_they_were_read(tmp_path):
    cards = [_card(1, "Just now"), _card(2, "Just now"), _card(3, "3 days ago"), _card(4, "3 days ago")]
    client = Client([cards])
    cfg = _cfg(tmp_path, discovery_queries=["Data Scientist"], discovery_fresh_job_limit=2, discovery_prefer_unseen=False)

    def seed(s):
        _store(s, 1, parsed=True)
        _store(s, 2, parsed=True)

    _run(client, cfg, seed=seed)
    assert set(client.fetched) == {_url(1), _url(2)}


# --- how long searching goes on ----------------------------------------------------------------------------------------


def test_searching_goes_on_until_there_are_enough_new_jobs(tmp_path):
    # Search 1 holds only known jobs, search 2 only known ones, search 3 finally has the new jobs. The old rule
    # (any 4 fresh jobs) would have stopped after search 1.
    s1 = [_card(n) for n in range(1, 5)]
    s2 = [_card(n) for n in range(5, 9)]
    s3 = [_card(n) for n in range(9, 13)]
    client = Client([s1, s2, s3])
    cfg = _cfg(tmp_path, discovery_fresh_job_limit=4, discovery_search_every_city=False, discovery_queries=None)

    class Profile:  # a search list of three cities, none of them spelled out in settings
        preferred_roles = ["Data Scientist"]
        preferred_locations = ["Pune", "Delhi"]

    def seed(s):
        for n in range(1, 9):
            _store(s, n, parsed=True)

    with session_scope(in_memory_factory()) as s:
        seed(s)
        result, _ = discover_and_store(s, client, profile=Profile(), settings=cfg, run_id=1, seq_start=0, role_groups={})
        detail = dict(s.query(RunEvent).filter_by(stage="discover_freshness").one().detail)
    assert result.queries_run == 3
    assert detail["selected_unseen"] == 4 and detail["selected_already_read"] == 0 and not detail["search_cap_hit"]
    assert set(client.fetched) == {_url(n) for n in range(9, 13)}


def test_a_quiet_day_stops_at_the_search_cap_instead_of_walking_every_city(tmp_path):
    class Profile:
        preferred_roles = ["Data Scientist"]
        preferred_locations = [f"City{i}" for i in range(10)]

    # every search returns the same already-read jobs, so "enough new jobs" is never reached
    known = [_card(n) for n in range(1, 4)]
    client = Client([known] * 12)
    cfg = _cfg(tmp_path, discovery_fresh_job_limit=4, discovery_queries=None, discovery_max_searches=5)

    with session_scope(in_memory_factory()) as s:
        for n in range(1, 4):
            _store(s, n, parsed=True)
        result, _ = discover_and_store(s, client, profile=Profile(), settings=cfg, run_id=1, seq_start=0, role_groups={})
        detail = dict(s.query(RunEvent).filter_by(stage="discover_freshness").one().detail)
    assert result.queries_run == 5
    assert detail["search_cap_hit"] is True and detail["stopped_early"] is True
    assert detail["selected_already_read"] == 3  # the known jobs still fill the day, as before


# --- a posting that repeats itself ---------------------------------------------------------------------------------------


def _posting() -> str:
    lines = ["About the company: a long paragraph about who we are and what we do every day"] * 1
    lines += [f"Responsibility {i}: build and ship machine learning systems for customers" for i in range(60)]
    lines += ["Skills", "o  Python", "o  SQL"]
    lines += ["Python and SQL are required for this role in the analytics team"]
    return "\n".join(lines)


def test_a_posting_pasted_twice_is_sent_once(tmp_path):
    once = _posting()
    padded_once = once + "\n" + "\n".join(f"Benefit {i}: a perk number {i} that nobody else offers" for i in range(60))
    padded_twice = padded_once + "\n" + padded_once
    assert len(padded_twice) > MAX_JD_CHARS_FOR_LLM >= len(padded_once)
    assert drop_repeated_lines(padded_twice).strip() == padded_once.strip()  # exactly one copy, nothing else lost
    assert description_for_llm(padded_twice) == drop_repeated_lines(padded_twice)


def test_the_parser_calls_the_model_once_with_the_single_copy():
    once = "\n".join(f"Requirement {i}: knowledge of topic number {i} is expected" for i in range(120))
    twice = once + "\n" + once
    assert len(once) <= MAX_JD_CHARS_FOR_LLM < len(twice)
    provider = FakeProvider(model="m", response=VALID_RESPONSE)
    result = JobParser(provider).parse(_job(description=twice))
    assert result.success is True
    assert provider.last_prompt.count("Requirement 7:") == 1


def test_a_long_posting_that_does_not_repeat_is_still_refused_and_never_truncated():
    honest = "\n".join(f"Requirement {i}: a different thing to know about subject {i}" for i in range(400))
    assert len(honest) > MAX_JD_CHARS_FOR_LLM
    provider = FakeProvider(model="m", response=VALID_RESPONSE)
    result = JobParser(provider).parse(_job(description=honest))
    assert result.success is False and result.error.startswith("jd_too_long:")
    assert provider.last_prompt is None


def test_short_lines_that_repeat_in_an_honest_posting_are_never_dropped():
    text = "Skills\nPython\nSQL\nDuties\nBuild models for the team every single day of the week\nSkills\nPython\nSQL"
    assert drop_repeated_lines(text) == text  # a short line repeating is not a second copy of the posting


def test_new_lines_inside_a_second_copy_are_kept():
    first = "A long opening line about the role and the company we are hiring for\nSkills\nPython"
    second = "A long opening line about the role and the company we are hiring for\nSkills\nPython\nA brand new requirement"
    out = drop_repeated_lines(first + "\n" + second)
    assert out.count("A long opening line") == 1 and out.endswith("A brand new requirement")
