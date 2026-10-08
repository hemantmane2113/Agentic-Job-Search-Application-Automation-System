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


@cli.command("profile-refresh")
@click.option("--execute", is_flag=True, default=False,
              help="Actually upload the next resume (needs PROFILE_REFRESH_ENABLED=true). Without it: dry run.")
def profile_refresh(execute: bool) -> None:
    """
    Refresh the Naukri profile by uploading the next of your resumes in rotation (one per
    day, registry order). Dry run by default: shows which resume is next, changes nothing.
    """
    settings = get_settings()
    from naukri_agent.orchestration.profile_refresh import run_profile_refresh

    notify = None
    if execute and settings.telegram_bot_token and settings.telegram_chat_id:
        from naukri_agent.orchestration.telegram_interaction import build_telegram_interaction

        notify = build_telegram_interaction(settings).notify
    result = _explain_busy_database(lambda: run_profile_refresh(settings, execute=execute, notify=notify))
    click.echo(result.model_dump_json(indent=2))
    if result.outcome in {"failed", "needs_human", "unconfirmed"}:
        sys.exit(1)


@cli.command("linkedin-inspect")
@click.option("--query", default="data scientist", show_default=True, help="Job search keywords.")
@click.option("--location", default="India", show_default=True, help="Job search location.")
def linkedin_inspect(query: str, location: str) -> None:
    """
    LinkedIn L1: READ-ONLY inspection. Opens LinkedIn in its own browser profile,
    waits for YOU to log in (credentials are never typed by the tool), then loads
    one search page and one job page and saves their HTML + a JSON report to the
    inspection output dir. Nothing is clicked, filled or submitted; any LinkedIn
    checkpoint/CAPTCHA is left for you to complete. Run in an interactive terminal.
    """
    settings = get_settings()
    from naukri_agent.browser.linkedin_inspection import run_linkedin_inspection

    report = run_linkedin_inspection(settings, query=query, location=location)
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


@cli.command("inspect-profile-edit")
@click.option(
    "--reuse-session", is_flag=True, default=False,
    help="Reuse the persistent browser profile instead of an isolated, disposable one.",
)
@click.option(
    "--manual-open/--no-manual-open", default=True, show_default=True,
    help="Pause so YOU open the edit panel (click the pencil next to Resume headline); it is then captured.",
)
def inspect_profile_edit(reuse_session: bool, manual_open: bool) -> None:
    """
    READ-ONLY inspection of Naukri's profile-EDIT page DOM (currently
    completely unknown to this codebase). No save/submit, no upload, no
    field edits — same safety posture as `inspect`/`inspect-apply`. This
    is a PREREQUISITE for, not an implementation of, the future daily
    resume/profile "touch to refresh last-updated" action, which remains
    unbuilt and blocked on a human reviewing this output. Requires
    NAUKRI_EMAIL/NAUKRI_PASSWORD and real network access — run locally,
    against your own account.
    """
    settings = get_settings()
    from naukri_agent.browser.profile_inspection import run_profile_edit_inspection

    report = run_profile_edit_inspection(
        settings, isolated_profile=not reuse_session, capture_after_manual_open=manual_open
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
@click.argument("job")
@click.option(
    "--reuse-session", is_flag=True, default=False,
    help="Reuse the persistent browser profile instead of an isolated, disposable one.",
)
def apply_(job: str, reuse_session: bool) -> None:
    """
    Phase 14: open Naukri's real Apply flow for JOB (id / external id /
    URL) and, with your approval of every drafted answer and the final
    submission, apply. Gated by BOTH AUTO_APPLY=true AND DRY_RUN=false —
    neither alone is enough — on top of this command's own interactive
    batch-answer review and final y/n confirm. Always interactive; never
    reachable from run-daily/discover/scheduler.
    """
    settings = get_settings()
    if not (settings.auto_apply and not settings.dry_run):
        raise click.ClickException(
            "`apply` is disabled. Set AUTO_APPLY=true and DRY_RUN=false to enable it "
            "(both are required, on top of this command's own per-step human approval)."
        )
    from naukri_agent.orchestration.apply_runner import run_apply_workflow

    result = run_apply_workflow(settings, job, isolated_profile=not reuse_session)
    click.echo(json.dumps(result.model_dump(), indent=2, default=str))
    if not result.submitted:
        sys.exit(1)


def _explain_busy_database(fn):
    """Run fn(); turn SQLite's "database is locked" into a plain message. The daily
    run holds the database for its whole duration, so apply commands started while
    it is still going fail at their first query, before touching Naukri."""
    from sqlalchemy.exc import OperationalError

    try:
        return fn()
    except OperationalError as exc:
        if "database is locked" in str(exc).lower():
            raise click.ClickException(
                "The database is busy - the daily run is probably still going. Wait until it "
                "finishes, then run this again. Nothing was opened and nothing was applied."
            )
        raise


@cli.command("auto-apply")
def auto_apply_cmd() -> None:
    """
    UNATTENDED: apply to eligible jobs that have a plain Naukri Apply button
    (never the "Apply on company site" ones - those come to you by email).
    Needs ALL of AUTO_APPLY=true, DRY_RUN=false and AUTO_APPLY_UNATTENDED=true,
    stops while the pause file exists, applies only to ACCEPT-level matches (up
    to AUTO_APPLY_DAILY_CAP per 24h), answers screening questions only from your
    profile when certain, and otherwise submits nothing and emails the job to
    you. Never reachable from run-daily/discover/scheduler.
    """
    from naukri_agent.orchestration.auto_apply_runner import run_auto_apply

    result = _explain_busy_database(lambda: run_auto_apply(get_settings()))
    click.echo(json.dumps(result.model_dump(), indent=2, default=str))
    if result.blocked_reason:
        sys.exit(2)
    if any(o.outcome in ("failed", "unconfirmed") for o in result.outcomes) or (
        result.stopped_reason and result.stopped_reason.startswith("could not run")
    ):
        sys.exit(1)


@cli.command("telegram-setup")
@click.option("--wait", default=120, show_default=True, help="Seconds to wait for you to message the bot.")
def telegram_setup(wait: int) -> None:
    """
    One-time Telegram setup. Create a bot with @BotFather, put its token in .env as
    TELEGRAM_BOT_TOKEN, open the bot on your phone and send it any message, then run
    this to find your chat id (and get a hello back to prove it works).
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise click.ClickException("Set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather).")
    from naukri_agent.notifications.telegram import TelegramChannel, TelegramError

    click.echo(f"Open your bot in Telegram and send it any message (waiting up to {wait}s)...")
    try:
        chat_id = TelegramChannel(settings.telegram_bot_token, "0").discover_chat_id(wait)
    except TelegramError as exc:
        raise click.ClickException(
            f"Could not talk to Telegram ({exc}). HTTP 401/404 means the token is wrong - check "
            "TELEGRAM_BOT_TOKEN; URLError means a network or certificate problem on this PC."
        )
    if chat_id is None:
        raise click.ClickException("No message reached the bot in time. Send it a message and run this again.")
    click.echo(f"Found your chat id. Add this line to .env:\n\nTELEGRAM_CHAT_ID={chat_id}")
    try:
        TelegramChannel(settings.telegram_bot_token, chat_id).send(
            "naukri-agent is connected. After TELEGRAM_CHAT_ID is in .env, jobs will be sent here for your Yes/No."
        )
    except TelegramError:
        click.echo("(Could not send the hello message, but the chat id above is still right.)")


@cli.command("telegram-apply")
@click.option("--max-jobs", type=click.IntRange(min=1), default=None,
              help="Offer at most this many jobs in this run (handy for testing).")
def telegram_apply(max_jobs: int | None) -> None:
    """
    Apply to eligible jobs that have a plain Naukri Apply button, with YOU approving
    on Telegram: each job is sent to your phone and nothing is clicked until you tap
    Yes; every screening question is sent for you to answer; if there were questions
    you confirm once more before it submits. Silence always means no. Needs
    AUTO_APPLY=true and DRY_RUN=false plus TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
    Only ACCEPT-level matches, at most AUTO_APPLY_DAILY_CAP per 24h. Never reachable
    from run-daily/discover/scheduler.
    """
    settings = get_settings()
    from naukri_agent.orchestration.auto_apply_runner import run_auto_apply
    from naukri_agent.orchestration.telegram_interaction import build_telegram_interaction

    try:
        interaction = build_telegram_interaction(settings)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    result = _explain_busy_database(lambda: run_auto_apply(settings, interaction=interaction, max_attempts=max_jobs))
    if not result.blocked_reason:
        interaction.notify(
            f"Run finished: {result.applied} applied, {len(result.outcomes)} job(s) handled."
            + (f" Stopped: {result.stopped_reason}" if result.stopped_reason else "")
        )
    click.echo(json.dumps(result.model_dump(), indent=2, default=str))
    if result.blocked_reason:
        sys.exit(2)
    if any(o.outcome in ("failed", "unconfirmed") for o in result.outcomes):
        sys.exit(1)


@cli.command("email-outreach")
@click.argument("job")
def email_outreach(job: str) -> None:
    """
    Phase 15: draft and, with your approval, send an email for JOB
    (id / external id / URL) -- an application-by-email if its JD
    explicitly asked for one, or a cold-outreach note (NOT an
    application) if it merely mentions a contact email. The mode is
    resolved automatically from the job's own extraction, never chosen
    here. Gated by BOTH AUTO_EMAIL_OUTREACH=true AND DRY_RUN=false —
    neither alone is enough — on top of this command's own interactive
    review and final y/n confirm. Always interactive; never reachable
    from run-daily/discover/scheduler.
    """
    settings = get_settings()
    if not (settings.auto_email_outreach and not settings.dry_run):
        raise click.ClickException(
            "`email-outreach` is disabled. Set AUTO_EMAIL_OUTREACH=true and DRY_RUN=false "
            "to enable it (both are required, on top of this command's own human approval)."
        )
    from naukri_agent.orchestration.email_outreach_runner import run_email_outreach_workflow

    result = run_email_outreach_workflow(settings, job)
    click.echo(json.dumps(result.model_dump(), indent=2, default=str))
    if not result.sent:
        sys.exit(1)


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
    """
    Console-script entry point. get_settings() is called here, before
    setup_logging() and before any Click command runs, specifically so
    a bad .env value (an invalid DAILY_RUN_TIME, an unknown
    LLM_PROVIDER, etc.) never shows the user a raw Pydantic
    ValidationError traceback — logging isn't even configured yet at
    that point, so there'd be nowhere for a "nice" version of it to go
    either way. `doctor` already does this per-check, more precisely;
    this is the same idea for every other command's startup.
    """
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any startup config failure lands here
        click.echo(f"naukri-agent: configuration error — {exc}", err=True)
        click.echo(
            "Run `naukri-agent doctor` for a detailed per-check diagnosis, "
            "or check your .env file.",
            err=True,
        )
        sys.exit(1)

    setup_logging(settings)
    cli()


# The console-script entry point in pyproject.toml points at `main`,
# not `cli`, so every invocation of `naukri-agent ...` goes through
# setup_logging() first. `cli` is exported too (for direct testing /
# invoking the Click group without touching logging).
if __name__ == "__main__":
    main()
