"""Stage A: run_daily_recommendations end-to-end (fake discovery, no LLM,
no browser, no email). DB is authoritative; digest + Excel are produced
under DRY_RUN without sending anything.
"""

from __future__ import annotations

import datetime
import inspect
import shutil

from openpyxl import load_workbook

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import (
    ApplicationStatus,
    DailyRun,
    DailyRunStatus,
    JobRecommendation,
    RunEvent,
)
from naukri_agent.database.repositories import upsert_application_history, upsert_job
from naukri_agent.jobs.models import JobCreate
from naukri_agent.orchestration.discovery import DiscoveryResult
from naukri_agent.orchestration.pipeline import run_daily_recommendations

from .digest_fakes import in_memory_factory, settings

REPO_CONFIG = None  # resolved in _prep


def _prep(tmp_path):
    """Copy the shipped example YAMLs into the settings paths."""
    import naukri_agent

    root = __import__("pathlib").Path(naukri_agent.__file__).resolve().parents[2]
    cfg_dir = root / "config"
    shutil.copy(cfg_dir / "candidate_profile.example.yaml", tmp_path / "cand.yaml")
    shutil.copy(cfg_dir / "master_resume.example.yaml", tmp_path / "resume.yaml")
    shutil.copy(cfg_dir / "resumes.example.yaml", tmp_path / "resumes.yaml")


def _fake_discover(job_specs):
    def discover_fn(session, profile, settings, run_id, seq):
        ids = []
        for slug, ext, sc in job_specs:
            job, created = upsert_job(
                session,
                JobCreate(
                    title=f"{slug} role", company="Acme", location="Pune",
                    description=f"JD for {slug}",
                    url=f"https://www.naukri.com/job-listings-{slug}-{ext}",
                    salary_text="10-15 LPA",
                    # None (ambiguous/absent structured field): the fake
                    # LLM responses in this file uniformly use
                    # experience_min/max=None, and a real "2-5 yrs" here
                    # would make the Run 17 fix 2 reconciliation fire on
                    # every job, polluting the exact parse-RunEvent
                    # detail assertions below with an unrelated note.
                    experience_text=None,
                ),
                run_id=run_id,
            )
            ids.append(job.id)
        return (
            DiscoveryResult(
                queries_run=1, queries_failed=0, jobs_new=len(ids), jobs_reseen=0,
                details_failed=0, job_ids=ids, total_failure=False,
            ),
            seq + 1,
        )

    return discover_fn


_VALID_EXTRACTION = __import__("json").dumps({
    "normalized_title": "Data Scientist",
    "required_skills": ["Python"], "preferred_skills": [],
    "experience_min": None, "experience_max": None,
    "salary_min": None, "salary_max": None, "salary_currency": None,
    "education_requirements": [],
    "job_type": "unknown",
})

# job_type carries the job title -> LLMJobExtractionPayload rejects it and
# JobParser returns success=False (the Run 14 Job 2 shape). Not in the
# job_type normalization allowlist, so it is a genuine attempted-and-failed
# parse, not a normalization case.
_SCHEMA_FAIL_EXTRACTION = __import__("json").dumps({
    "normalized_title": "Data Scientist",
    "required_skills": [], "preferred_skills": [],
    "experience_min": None, "experience_max": None,
    "salary_min": None, "salary_max": None, "salary_currency": None,
    "education_requirements": [],
    "job_type": "Staff Data Scientist Bengaluru",
})


class _PerJobLLM:
    """Fake LLM returning a different canned response per job, keyed on a
    substring of the user prompt (the JD text). An empty mapping just
    returns `default` for every job."""

    provider_name = "fake"
    model = "fake-1"

    def __init__(self, mapping: dict[str, str], default: str = "{}") -> None:
        self._mapping = mapping
        self._default = default

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        for key, resp in self._mapping.items():
            if key in prompt:
                return resp
        return self._default


def test_happy_path_completes_writes_digest_and_excel_without_email(tmp_path):
    _prep(tmp_path)
    cfg = settings(tmp_path, dry_run=True, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()

    result = run_daily_recommendations(
        cfg,
        now=datetime.datetime(2026, 9, 9, tzinfo=datetime.UTC),
        discover_fn=_fake_discover([("ds", "040926000001", 0), ("mle", "040926000002", 0)]),
        # deterministic valid extraction: keeps the test off the real LLM
        # (ollama_host defaults non-empty, so extraction_provider=None would
        # auto-build one) without depending on a live model
        extraction_provider=_PerJobLLM({}, _VALID_EXTRACTION),
        session_factory=factory,
    )

    assert result.status == "COMPLETED"
    assert result.jobs_discovered == 2 and result.jobs_evaluated == 2
    assert result.recommendations >= 1
    # digest file written, NO smtp
    assert result.email_path and __import__("pathlib").Path(result.email_path).exists()
    assert result.email_status == "dry_run"
    # excel regenerated from DB
    assert result.excel_path and __import__("pathlib").Path(result.excel_path).exists()
    # Regression: export_workbook() used to run BEFORE run.status was
    # finalized to COMPLETED, so the "Daily Runs" sheet showed every
    # successful run as "STARTED" forever, even though result.status
    # (asserted above) was already correct.
    runs_ws = load_workbook(result.excel_path)["Daily Runs"]
    header = [c.value for c in runs_ws[1]]
    status_col = header.index("Run Status")
    assert runs_ws[2][status_col].value == "COMPLETED"

    with session_scope(factory) as s:
        run = s.query(DailyRun).one()
        assert run.status == DailyRunStatus.COMPLETED
        stages = {e.stage for e in s.query(RunEvent).filter_by(daily_run_id=run.id)}
        assert {"score", "build_digest", "send_email", "excel_export"} <= stages
        assert s.query(JobRecommendation).filter_by(daily_run_id=run.id).count() == result.recommendations


def test_misconfigured_smtp_fails_the_run_cleanly_not_uncaught(tmp_path):
    """email_sender=smtp with missing SMTP_PASSWORD must raise
    EmailConfigError inside build_email_sender(), which pipeline.py
    now calls INSIDE the same try/except that already handles a
    send() failure -- so a scheduled run with an incomplete .env ends
    as a clean FAILED DailyRun, never an exception escaping
    run_daily_recommendations()."""
    _prep(tmp_path)
    cfg = settings(
        tmp_path, threshold_review=0, threshold_accept=100,
        email_sender="smtp", smtp_host="smtp.gmail.com", smtp_username="me@gmail.com",
        smtp_password="", notify_email_to="me@gmail.com",  # password missing
    )
    factory = in_memory_factory()

    result = run_daily_recommendations(
        cfg,
        now=datetime.datetime(2026, 9, 18, tzinfo=datetime.UTC),
        discover_fn=_fake_discover([("smtpfail", "040926000401", 0)]),
        extraction_provider=_PerJobLLM({}, _VALID_EXTRACTION),
        session_factory=factory,
    )

    assert result.status == "FAILED"
    assert "EmailConfigError" in (result.failure_reason or "")
    with session_scope(factory) as s:
        run = s.query(DailyRun).one()
        assert run.status == DailyRunStatus.FAILED
        from naukri_agent.database.models import RunEventStatus

        send_email_events = [e for e in s.query(RunEvent).filter_by(daily_run_id=run.id) if e.stage == "send_email"]
        assert len(send_email_events) == 1 and send_email_events[0].status == RunEventStatus.FAILED


def test_discovery_total_failure_marks_run_failed(tmp_path):
    _prep(tmp_path)
    cfg = settings(tmp_path)
    factory = in_memory_factory()

    def failing_discover(session, profile, settings, run_id, seq):
        return (
            DiscoveryResult(
                queries_run=3, queries_failed=3, jobs_new=0, jobs_reseen=0,
                details_failed=0, job_ids=[], total_failure=True,
            ),
            seq + 1,
        )

    result = run_daily_recommendations(
        cfg, discover_fn=failing_discover, extraction_provider=None, session_factory=factory
    )
    assert result.status == "FAILED"
    assert result.recommendations == 0
    with session_scope(factory) as s:
        assert s.query(DailyRun).one().status == DailyRunStatus.FAILED


def test_mark_applied_between_runs_excludes_job_from_the_next_digest(tmp_path):
    _prep(tmp_path)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100, recommendation_cooldown_days=0)
    factory = in_memory_factory()
    disc = _fake_discover([("keepme", "040926000010", 0), ("applyme", "040926000011", 0)])

    r1 = run_daily_recommendations(
        cfg, now=datetime.datetime(2026, 9, 9, tzinfo=datetime.UTC),
        discover_fn=disc, extraction_provider=_PerJobLLM({}, _VALID_EXTRACTION),
        session_factory=factory,
    )
    assert r1.recommendations == 2

    with session_scope(factory) as s:
        from naukri_agent.database.models import Job

        applyme = s.query(Job).filter(Job.url.like("%applyme%")).one()
        upsert_application_history(s, applyme.id, status=ApplicationStatus.APPLIED)

    r2 = run_daily_recommendations(
        cfg, now=datetime.datetime(2026, 9, 10, tzinfo=datetime.UTC),
        discover_fn=disc, extraction_provider=_PerJobLLM({}, _VALID_EXTRACTION),
        session_factory=factory,
    )
    assert r2.recommendations == 1  # the applied job is gone

    with session_scope(factory) as s:
        from naukri_agent.database.models import Job

        run2 = s.query(DailyRun).order_by(DailyRun.id.desc()).first()
        recs = s.query(JobRecommendation).filter_by(daily_run_id=run2.id).all()
        rec_job_urls = {s.get(Job, r.job_id).url for r in recs}
        assert not any("applyme" in u for u in rec_job_urls)
        assert any("keepme" in u for u in rec_job_urls)


def test_no_application_workflow_code_is_imported_or_invoked():
    """Structural: the digest pipeline must not pull in the frozen
    Apply-workflow modules, and prepare_application stays disabled."""
    from naukri_agent.orchestration import discovery, pipeline
    from naukri_agent.recommendations import builder, explain
    from naukri_agent.notifications import email, render
    from naukri_agent.reporting import excel

    for mod in (pipeline, discovery, builder, explain, email, render, excel):
        src = inspect.getsource(mod).lower()
        for forbidden in (
            "apply_inspection", "mutatingrequestblocker", "run_apply_inspection",
            "prepare_application", "postinitclientobserver", "extract_application_ui",
            ".click(", "questionnaire",
        ):
            assert forbidden not in src, f"{mod.__name__} references {forbidden!r}"

    from unittest.mock import MagicMock
    from naukri_agent.browser.naukri_client import NaukriClient

    client = NaukriClient(MagicMock(), MagicMock())
    try:
        client.prepare_application()
    except NotImplementedError:
        pass
    else:  # pragma: no cover
        raise AssertionError("prepare_application must stay disabled")


def test_parse_run_event_records_job_type_normalization(tmp_path):
    """When JobParser deterministically coerces job_type (e.g.
    'Individual Contributor' -> 'unknown'), the parse RunEvent carries a
    'normalized' entry so the coercion is auditable per run/job."""
    import json as _json

    from naukri_agent.database.models import RunEventStatus
    from .digest_fakes import FakeLLMProvider

    _prep(tmp_path)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()

    llm = FakeLLMProvider(_json.dumps({
        "normalized_title": "Data Scientist",
        "required_skills": [], "preferred_skills": [],
        "experience_min": None, "experience_max": None,
        "salary_min": None, "salary_max": None, "salary_currency": None,
        "education_requirements": [],
        "job_type": "Individual Contributor",
    }))

    run_daily_recommendations(
        cfg,
        now=datetime.datetime(2026, 9, 11, tzinfo=datetime.UTC),
        discover_fn=_fake_discover([("ic", "040926000042", 0)]),
        extraction_provider=llm,
        session_factory=factory,
    )

    with session_scope(factory) as s:
        parse_events = s.query(RunEvent).filter_by(stage="parse").all()
        assert len(parse_events) == 1
        e = parse_events[0]
        assert e.status == RunEventStatus.OK  # normalization made it valid
        assert e.detail == {"normalized": ["job_type: 'Individual Contributor' -> 'unknown'"]}


def test_parse_run_event_records_skill_form_warnings(tmp_path):
    """A prose-style JD makes the model return requirement sentences as
    required_skills. JobParser flags them (non-destructively) and the
    parse RunEvent carries a 'skill_warnings' entry for the audit trail;
    the extracted list is stored unchanged."""
    import json as _json

    from naukri_agent.database.models import JobExtraction, RunEvent as _RE, RunEventStatus
    from .digest_fakes import FakeLLMProvider

    _prep(tmp_path)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()

    sentences = [
        "Proficiency in SQL and Excel",
        "Strong programming skills in Python (3.7+)",
    ]
    llm = FakeLLMProvider(_json.dumps({
        "normalized_title": "Data Scientist",
        "required_skills": sentences, "preferred_skills": [],
        "experience_min": None, "experience_max": None,
        "salary_min": None, "salary_max": None, "salary_currency": None,
        "education_requirements": [],
        "job_type": "unknown",
    }))

    run_daily_recommendations(
        cfg,
        now=datetime.datetime(2026, 9, 12, tzinfo=datetime.UTC),
        discover_fn=_fake_discover([("prose", "040926000099", 0)]),
        extraction_provider=llm,
        session_factory=factory,
    )

    with session_scope(factory) as s:
        e = s.query(_RE).filter_by(stage="parse").one()
        assert e.status == RunEventStatus.OK
        assert set(e.detail.keys()) == {"skill_warnings"}
        assert len(e.detail["skill_warnings"]) == 2
        assert all("looks like a requirement sentence" in w for w in e.detail["skill_warnings"])
        # extraction stored the values verbatim -- matching sees them as-is
        ext = s.query(JobExtraction).filter_by(is_current=True).one()
        assert ext.required_skills == sentences


def test_H_parse_run_event_records_skill_cleanups(tmp_path):
    """Phase 1: deterministic skill-list cleanup (dedupe / required-wins /
    education-overlap) is recorded in the parse RunEvent detail alongside
    'normalized' and 'skill_warnings'."""
    import json as _json

    from naukri_agent.database.models import JobExtraction, RunEvent as _RE, RunEventStatus
    from .digest_fakes import FakeLLMProvider

    _prep(tmp_path)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()

    llm = FakeLLMProvider(_json.dumps({
        "normalized_title": "Data Scientist",
        "required_skills": ["Python", "python", "Statistics", "SQL"],  # dup + edu overlap
        "preferred_skills": ["SQL", "Docker"],                          # dup of required
        "experience_min": None, "experience_max": None,
        "salary_min": None, "salary_max": None, "salary_currency": None,
        "education_requirements": ["Statistics"],
        "job_type": "unknown",
    }))

    run_daily_recommendations(
        cfg,
        now=datetime.datetime(2026, 9, 13, tzinfo=datetime.UTC),
        discover_fn=_fake_discover([("clean", "040926000123", 0)]),
        extraction_provider=llm,
        session_factory=factory,
    )

    with session_scope(factory) as s:
        e = s.query(_RE).filter_by(stage="parse").one()
        assert e.status == RunEventStatus.OK
        assert "skill_cleanups" in e.detail
        assert e.detail["skill_cleanups"] == [
            "deduplicated required skill: 'python'",
            "preferred skill removed because required wins: 'SQL'",
            "required skill removed because it overlaps education_requirements: 'Statistics'",
        ]
        # extraction stored the CLEANED lists; raw response untouched
        ext = s.query(JobExtraction).filter_by(is_current=True).one()
        assert ext.required_skills == ["Python", "SQL"]
        assert ext.preferred_skills == ["Docker"]
        assert '"python"' in ext.raw_llm_response  # verbatim model output kept


# --- Option A: an LLM parse failure this run never yields a recommendation ---


def test_parse_failed_job_excluded_from_recs_but_keeps_diagnostic_jobmatch_and_note(tmp_path):
    from naukri_agent.database.models import Job, JobMatch

    _prep(tmp_path)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()
    llm = _PerJobLLM({
        "JD for okjob": _VALID_EXTRACTION,
        "JD for badjob": _SCHEMA_FAIL_EXTRACTION,
    })

    result = run_daily_recommendations(
        cfg,
        now=datetime.datetime(2026, 9, 14, tzinfo=datetime.UTC),
        discover_fn=_fake_discover([("okjob", "040926000301", 0), ("badjob", "040926000302", 0)]),
        extraction_provider=llm,
        session_factory=factory,
    )

    assert result.status == "COMPLETED"
    assert result.jobs_evaluated == 2
    # the parse-failure note is still surfaced
    assert any("could not be parsed by the LLM" in n for n in result.notes)

    with session_scope(factory) as s:
        run = s.query(DailyRun).one()
        ok_job = s.query(Job).filter(Job.url.like("%-okjob-%")).one()
        bad_job = s.query(Job).filter(Job.url.like("%-badjob-%")).one()

        rec_job_ids = {
            r.job_id for r in s.query(JobRecommendation).filter_by(daily_run_id=run.id)
        }
        assert bad_job.id not in rec_job_ids        # parse-failed job excluded
        assert ok_job.id in rec_job_ids             # cleanly parsed job still eligible

        # diagnostic JobMatch for the failed job is still written, on the raw
        # listing (no extraction link)
        jm = s.query(JobMatch).filter_by(job_id=bad_job.id).one()
        assert jm.job_extraction_id is None
        # no JobExtraction row was persisted for the failed parse
        from naukri_agent.database.models import JobExtraction as _JE
        assert s.query(_JE).filter_by(job_id=bad_job.id).count() == 0


def test_unconfigured_llm_is_not_treated_as_a_parse_failure(tmp_path):
    """No LLM provider configured (ollama_host="" => llm_provider_is_configured
    is False => none is auto-built) means the parse is NEVER attempted. Such
    jobs are scored on the raw listing but are NOT parse failures: they stay
    recommendation-eligible and emit no parse-failure note."""
    _prep(tmp_path)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100, ollama_host="")
    factory = in_memory_factory()

    result = run_daily_recommendations(
        cfg,
        now=datetime.datetime(2026, 9, 15, tzinfo=datetime.UTC),
        discover_fn=_fake_discover([("nollm", "040926000311", 0)]),
        extraction_provider=None,
        session_factory=factory,
    )
    assert result.recommendations == 1
    assert not any("could not be parsed" in n for n in result.notes)


def test_parse_failed_set_is_scoped_to_the_current_run(tmp_path):
    """A job that failed to parse in an earlier run must not stay
    suppressed: the exclusion set is rebuilt from THIS run's attempts."""
    from naukri_agent.database.models import Job

    _prep(tmp_path)
    cfg = settings(
        tmp_path, threshold_review=0, threshold_accept=100, recommendation_cooldown_days=0
    )
    factory = in_memory_factory()
    disc = _fake_discover([("flaky", "040926000321", 0)])

    r1 = run_daily_recommendations(
        cfg, now=datetime.datetime(2026, 9, 16, tzinfo=datetime.UTC),
        discover_fn=disc,
        extraction_provider=_PerJobLLM({"JD for flaky": _SCHEMA_FAIL_EXTRACTION}),
        session_factory=factory,
    )
    assert r1.recommendations == 0  # excluded this run

    r2 = run_daily_recommendations(
        cfg, now=datetime.datetime(2026, 9, 17, tzinfo=datetime.UTC),
        discover_fn=disc,
        extraction_provider=_PerJobLLM({"JD for flaky": _VALID_EXTRACTION}),
        session_factory=factory,
    )
    assert r2.recommendations == 1  # parses fine now -> eligible again

    with session_scope(factory) as s:
        run2 = s.query(DailyRun).order_by(DailyRun.id.desc()).first()
        recs = s.query(JobRecommendation).filter_by(daily_run_id=run2.id).all()
        flaky = s.query(Job).filter(Job.url.like("%-flaky-%")).one()
        assert [r.job_id for r in recs] == [flaky.id]
