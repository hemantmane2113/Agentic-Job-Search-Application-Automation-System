"""
CLI entrypoint for naukri-agent.

Phase 1 implements `doctor` fully (it only needs to check things that
exist in Phase 1: config, database, Python version) and registers
stubs for every other command described in the master spec, so the
command surface is stable from the start even though most commands
raise NotImplementedError until their owning phase is built.
"""

from __future__ import annotations

import json
import logging
import sys

import click

from naukri_agent.config import get_settings
from naukri_agent.database.base import init_db
from naukri_agent.logging_config import setup_logging
from naukri_agent.candidate.models import load_candidate_profile
from naukri_agent.resume.models import load_master_resume
from naukri_agent.resume.registry import check_registry_files, load_resume_registry

logger = logging.getLogger(__name__)


def _dir_writable(path) -> bool:
    from pathlib import Path

    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return True
    except Exception:  # noqa: BLE001
        return False


@click.group()
def cli() -> None:
    """Agentic job-search and application-assistance system for Naukri.com."""


@cli.command("doctor")
def doctor() -> None:
    """Check that the environment is set up correctly."""
    logger.info("Running doctor checks")
    checks: list[tuple[str, bool, str]] = []

    # Python version
    py_ok = sys.version_info >= (3, 12)
    checks.append((
        "Python >= 3.12",
        py_ok,
        f"found {sys.version_info.major}.{sys.version_info.minor}",
    ))

    # Settings load without raising
    try:
        settings = get_settings()
        checks.append(("Configuration loads", True, f"app_env={settings.app_env}"))
    except Exception as exc:  # noqa: BLE001 - doctor reports, doesn't crash
        checks.append(("Configuration loads", False, str(exc)))
        settings = None

    # LLM provider configured
    if settings is not None:
        llm_ok = settings.llm_provider_is_configured()
        detail = f"provider={settings.llm_provider.value} model={settings.llm_model}"
        if not llm_ok:
            detail += " (missing credentials/host)"
        checks.append(("LLM provider configured", llm_ok, detail))

    # Database reachable
    if settings is not None:
        try:
            init_db(settings)
            checks.append(("Database reachable", True, settings.database_url))
        except Exception as exc:  # noqa: BLE001
            checks.append(("Database reachable", False, str(exc)))

    # Candidate profile loads
    if settings is not None:
        try:
            profile = load_candidate_profile(settings.candidate_profile_path)
            checks.append((
                "Candidate profile loads",
                True,
                f"{profile.full_name} ({len(profile.skills)} skills)",
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(("Candidate profile loads", False, str(exc)))

    # Master resume loads
    if settings is not None:
        try:
            resume = load_master_resume(settings.master_resume_path)
            checks.append((
                "Master resume loads",
                True,
                f"{len(resume.work_experience)} roles, "
                f"{resume.total_years_experience()} yrs experience",
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(("Master resume loads", False, str(exc)))

    # Resume registry loads and files exist
    if settings is not None:
        try:
            registry = load_resume_registry(settings.resume_registry_path)
            statuses = check_registry_files(registry)
            missing = [s.id for s in statuses if not s.exists]
            if missing:
                checks.append((
                    "Resume registry files",
                    False,
                    f"{len(statuses)} resume(s) registered, missing on disk: {', '.join(missing)}",
                ))
            else:
                checks.append((
                    "Resume registry files",
                    True,
                    f"{len(statuses)} resume(s) registered, all present",
                ))
        except Exception as exc:  # noqa: BLE001
            checks.append(("Resume registry files", False, str(exc)))

    # Daily-digest configuration (scope change)
    if settings is not None:
        limit = settings.daily_recommendation_limit
        checks.append((
            "Daily recommendation limit",
            isinstance(limit, int) and limit >= 1,
            f"DAILY_RECOMMENDATION_LIMIT={limit}",
        ))
        cd = settings.recommendation_cooldown_days
        checks.append((
            "Recommendation cooldown",
            isinstance(cd, int) and cd >= 0,
            f"{cd} day(s) "
            f"({'no cooldown' if cd == 0 else 'gate previously-recommended-not-applied jobs'})",
        ))
        valid_status = {"NOT_APPLIED", "APPLIED", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN", "UNKNOWN"}
        bad = [s for s in settings.recommendation_exclude_if_status if s.upper() not in valid_status]
        checks.append((
            "recommendation_exclude_if_status",
            not bad,
            f"{settings.recommendation_exclude_if_status}"
            + (f" (unknown: {bad})" if bad else ""),
        ))
        checks.append((
            "Digest email sender (Stage A)",
            settings.email_sender in ("file", "console", "smtp"),
            f"email_sender={settings.email_sender}"
            + (" — smtp falls back to file in Stage A" if settings.email_sender == "smtp" else ""),
        ))
        checks.append((
            "Excel export path writable",
            _dir_writable(settings.excel_path.parent),
            str(settings.excel_path),
        ))

    # New tables present + application-history integrity
    if settings is not None:
        try:
            from naukri_agent.database.base import session_scope
            from naukri_agent.database.models import ApplicationHistory, JobRecommendation, RunEvent

            factory = init_db(settings)
            with session_scope(factory) as session:
                n_apps = session.query(ApplicationHistory).count()
                n_recs = session.query(JobRecommendation).count()
                n_events = session.query(RunEvent).count()
            checks.append((
                "Digest tables reachable",
                True,
                f"application_history={n_apps}, job_recommendations={n_recs}, run_events={n_events}",
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(("Digest tables reachable", False, str(exc)))

    all_ok = True
    for name, ok, detail in checks:
        symbol = "OK" if ok else "FAIL"
        click.echo(f"[{symbol}] {name} — {detail}")
        all_ok = all_ok and ok

    if not all_ok:
        sys.exit(1)


@cli.command("run-now")
@click.pass_context
def run_now(ctx: click.Context) -> None:
    """Alias for `run-daily` — run the daily match digest immediately."""
    ctx.invoke(run_daily)


@cli.command("inspect")
@click.option(
    "--query", default="data scientist", show_default=True,
    help="Job search query to use during Stage 1 inspection.",
)
def inspect_naukri(query: str) -> None:
    """
    Phase 7 Stage 1: read-only inspection of Naukri's login, profile,
    search, and apply workflow. Requires NAUKRI_EMAIL/NAUKRI_PASSWORD
    to be set and real network access to naukri.com — this is meant to
    be run locally, against your own account, not in CI or a sandbox.
    Nothing is modified; nothing is submitted.
    """
    settings = get_settings()
    from naukri_agent.browser.inspection import run_inspection

    report = run_inspection(settings, search_query=query)
    click.echo(json.dumps(report, indent=2))
    if not report.get("completed"):
        sys.exit(1)


_STAGE_1_5_TEST_JOB_URL = (
    "https://www.naukri.com/job-listings-gen-ai-data-scientist-sigma-allied-services-"
    "pune-gurugram-bengaluru-2-to-7-years-040926008523"
)


@cli.command("inspect-apply")
@click.option(
    "--job-url", default=_STAGE_1_5_TEST_JOB_URL, show_default=True,
    help="Job listing URL to open for the controlled post-Apply inspection.",
)
@click.option(
    "--reuse-session", is_flag=True, default=False,
    help="Reuse the persistent browser profile instead of an isolated, disposable one.",
)
def inspect_apply(job_url: str, reuse_session: bool) -> None:
    """
    Phase 7 Stage 1.5: CONTROLLED, human-driven inspection of Naukri's
    dynamically-rendered POST-Apply UI.

    The automation never clicks Apply — you click it, in the visible
    browser window — and a fail-safe default-deny network guard blocks
    every mutating request (POST/PUT/PATCH/DELETE) for the whole
    session, so an accidental click cannot submit an application. The
    tool only READS the resulting UI: HTML, screenshot, and a
    structured list of its inputs/labels/buttons.

    Inspection only. No resume upload/removal, no answering questions,
    no resume selection, no submission, no prepare_application(). This
    is not Stage 2. Requires NAUKRI_EMAIL / NAUKRI_PASSWORD and real
    network access — run locally, against your own account.
    """
    settings = get_settings()
    from naukri_agent.browser.apply_inspection import run_apply_inspection

    report = run_apply_inspection(
        settings, job_url=job_url, isolated_profile=not reuse_session
    )
    click.echo(json.dumps(report, indent=2))
    if not report.get("completed"):
        sys.exit(1)


@cli.command("match")
def match() -> None:
    """Run job matching only (Phase 4)."""
    raise NotImplementedError("match lands in Phase 4 (matching engine).")


@cli.command("prepare")
def prepare() -> None:
    """REMOVED FROM SCOPE — applications are performed manually."""
    raise NotImplementedError(
        "Application submission is out of scope. Applications are manual; use "
        "`mark-applied` to record one."
    )


@cli.command("apply")
def apply_() -> None:
    """REMOVED FROM SCOPE — applications are performed manually."""
    raise NotImplementedError(
        "Application submission is out of scope. Applications are manual; use "
        "`mark-applied` to record one."
    )


# ---------------------------------------------------------------------------
# Scope change: daily read-only match digest + manual application history.
# ---------------------------------------------------------------------------


@cli.command("run-daily")
def run_daily() -> None:
    """Discover -> understand -> match -> rank -> digest (+ Excel). No email sent in Stage A."""
    from naukri_agent.orchestration.pipeline import run_daily_recommendations

    result = run_daily_recommendations(get_settings())
    click.echo(json.dumps(result.model_dump(), indent=2, default=str))
    if result.status != "COMPLETED":
        sys.exit(1)


@cli.command("discover")
@click.option("--query", multiple=True, help='Override query as "role @ location" (repeatable).')
def discover(query: tuple[str, ...]) -> None:
    """Read-only Naukri discovery: search, fetch detail pages, upsert Job rows. No matching/email."""
    from naukri_agent.browser.browser_manager import BrowserManager
    from naukri_agent.browser.naukri_client import NaukriClient
    from naukri_agent.candidate.models import load_candidate_profile as _lcp
    from naukri_agent.database.base import session_scope
    from naukri_agent.database.models import DailyRun, DailyRunStatus
    from naukri_agent.orchestration.discovery import discover_and_store

    settings = get_settings()
    if query:
        settings = settings.model_copy(update={"discovery_queries": list(query)})
    factory = init_db(settings)
    with session_scope(factory) as session:
        profile = _lcp(settings.candidate_profile_path)
        run = DailyRun(status=DailyRunStatus.STARTED)
        session.add(run)
        session.flush()
        with BrowserManager(settings) as browser:
            client = NaukriClient(browser.page, settings)
            client.login()
            result, _seq = discover_and_store(
                session, client, profile, settings, run_id=run.id
            )
        run.status = DailyRunStatus.COMPLETED
        run.jobs_discovered = result.jobs_new
    click.echo(json.dumps(result.model_dump(), indent=2, default=str))


@cli.command("recommend")
@click.option("--dry-run/--no-dry-run", default=True, show_default=True)
def recommend(dry_run: bool) -> None:
    """Run the full daily pipeline. Stage A only writes the digest + Excel (never emails)."""
    settings = get_settings().model_copy(update={"dry_run": dry_run})
    from naukri_agent.orchestration.pipeline import run_daily_recommendations

    result = run_daily_recommendations(settings)
    click.echo(json.dumps(result.model_dump(), indent=2, default=str))
    if result.status != "COMPLETED":
        sys.exit(1)


@cli.command("export-excel")
@click.option("--path", "path", default=None, help="Output .xlsx path (default: config excel_path).")
def export_excel(path: str | None) -> None:
    """Regenerate the job-search-history workbook entirely from the database."""
    from naukri_agent.database.base import session_scope
    from naukri_agent.reporting.excel import export_workbook

    settings = get_settings()
    factory = init_db(settings)
    with session_scope(factory) as session:
        res = export_workbook(session, path or settings.excel_path, settings)
    click.echo(json.dumps(res.model_dump(), indent=2))


@cli.command("mark-applied")
@click.argument("job")
@click.option("--resume", "resume_id", default=None, help="ResumeRegistry id used.")
@click.option("--status", "status", default=None, help="APPLIED (default) / INTERVIEW / OFFER / REJECTED / WITHDRAWN.")
@click.option("--date", "applied_date", default=None, help="Applied date YYYY-MM-DD (default: now).")
@click.option("--note", "note", default=None)
def mark_applied(job: str, resume_id, status, applied_date, note) -> None:
    """Record that YOU manually applied to a job (id | Naukri external id | URL)."""
    import datetime as _dt

    from naukri_agent.database.base import session_scope
    from naukri_agent.database.models import ApplicationStatus
    from naukri_agent.database.repositories import (
        latest_resume_selection,
        resolve_canonical_job,
        upsert_application_history,
    )

    settings = get_settings()
    factory = init_db(settings)
    st = ApplicationStatus((status or settings.mark_applied_default_status).upper())
    when = (
        _dt.datetime.strptime(applied_date, "%Y-%m-%d").replace(tzinfo=_dt.UTC)
        if applied_date
        else None
    )
    with session_scope(factory) as session:
        target = resolve_canonical_job(session, job)
        if target is None:
            raise click.ClickException(f"No job matched {job!r} (by id / external id / URL).")
        rid = resume_id
        if rid is None and settings.mark_applied_default_resume_from_selection:
            sel = latest_resume_selection(session, target.id)
            rid = sel.resume_id if sel else None
        row, created = upsert_application_history(
            session, target.id, status=st, applied_at=when, resume_id=rid,
            source="manual_cli", note=note,
        )
        click.echo(
            f"{'Recorded' if created else 'Updated'} {row.status.value} for job #{target.id} "
            f"'{target.title}' @ {target.company} (resume: {rid or 'none'}, "
            f"applied {row.applied_at or 'n/a'}). Future recommendation runs will "
            f"{'exclude' if st.value in settings.recommendation_exclude_if_status else 'still consider'} it."
        )


@cli.command("mark-status")
@click.argument("job")
@click.argument("status")
@click.option("--note", "note", default=None)
def mark_status(job: str, status: str, note) -> None:
    """Set the application status of a job (NOT_APPLIED / APPLIED / INTERVIEW / OFFER / REJECTED / WITHDRAWN / UNKNOWN)."""
    from naukri_agent.database.base import session_scope
    from naukri_agent.database.models import ApplicationStatus
    from naukri_agent.database.repositories import resolve_canonical_job, set_application_status

    settings = get_settings()
    factory = init_db(settings)
    st = ApplicationStatus(status.upper())
    with session_scope(factory) as session:
        target = resolve_canonical_job(session, job)
        if target is None:
            raise click.ClickException(f"No job matched {job!r}.")
        row = set_application_status(session, target.id, st, source="manual_cli", note=note)
        click.echo(f"job #{target.id} -> {row.status.value}")


@cli.command("applications")
@click.option("--status", "status", default=None)
def applications(status) -> None:
    """List recorded applications (the authoritative application history)."""
    from naukri_agent.database.base import session_scope
    from naukri_agent.database.models import ApplicationStatus
    from naukri_agent.database.repositories import list_applications

    settings = get_settings()
    factory = init_db(settings)
    st = ApplicationStatus(status.upper()) if status else None
    with session_scope(factory) as session:
        rows = list_applications(session, status=st)
        out = [
            {
                "job_id": r.job_id, "external_job_id": r.external_job_id,
                "title": r.job_title, "company": r.company, "status": r.status.value,
                "applied_at": str(r.applied_at) if r.applied_at else None,
                "resume_id": r.resume_id, "url": r.job_url,
            }
            for r in rows
        ]
    click.echo(json.dumps(out, indent=2))


@cli.command("report")
def report() -> None:
    """Re-render the latest recommendation digest to the console (no send)."""
    from naukri_agent.database.base import session_scope
    from naukri_agent.database.models import DailyRun, JobRecommendation

    settings = get_settings()
    factory = init_db(settings)
    with session_scope(factory) as session:
        run = session.query(DailyRun).order_by(DailyRun.started_at.desc()).first()
        if run is None:
            click.echo("No runs recorded yet.")
            return
        recs = (
            session.query(JobRecommendation)
            .filter_by(daily_run_id=run.id)
            .order_by(JobRecommendation.rank.asc())
            .all()
        )
        click.echo(f"Run #{run.id} ({run.status.value}) — {len(recs)} recommendation(s)")
        for r in recs:
            click.echo(
                f"  #{r.rank} job {r.job_id} score {r.score_at_email} "
                f"{r.decision_at_email.value} status {r.application_status_at_email.value}"
            )


@cli.command("scheduler")
def scheduler() -> None:
    """
    Stage B: start the in-process daily scheduler and block forever,
    firing `run-daily`'s pipeline once a day at Settings.daily_run_time
    (default 10:00) in Settings.timezone. An alternative to an OS-level
    cron entry / Task Scheduler task calling `naukri-agent run-daily`
    directly — either is a valid way to run this daily; use whichever
    fits how you deploy it. Ctrl+C stops it cleanly.
    """
    settings = get_settings()
    from naukri_agent.scheduler.daemon import run_scheduler

    run_scheduler(settings)


def main() -> None:
    settings = get_settings()
    setup_logging(settings)
    cli()


# The console-script entry point in pyproject.toml points at `main`,
# not `cli`, so every invocation of `naukri-agent ...` goes through
# setup_logging() first. `cli` is exported too (for direct testing /
# invoking the Click group without touching logging).
if __name__ == "__main__":
    main()
