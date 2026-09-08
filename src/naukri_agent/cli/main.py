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

    all_ok = True
    for name, ok, detail in checks:
        symbol = "OK" if ok else "FAIL"
        click.echo(f"[{symbol}] {name} — {detail}")
        all_ok = all_ok and ok

    if not all_ok:
        sys.exit(1)


@cli.command("run-now")
def run_now() -> None:
    """Run the full daily pipeline immediately (Phase 11)."""
    raise NotImplementedError("run-now lands in Phase 11 (scheduler + pipeline).")


@cli.command("discover")
def discover() -> None:
    """Run job discovery only (Phase 3/7)."""
    raise NotImplementedError("discover lands in Phase 3/7 (job discovery).")


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


@cli.command("match")
def match() -> None:
    """Run job matching only (Phase 4)."""
    raise NotImplementedError("match lands in Phase 4 (matching engine).")


@cli.command("prepare")
def prepare() -> None:
    """Prepare applications for review (Phase 8)."""
    raise NotImplementedError("prepare lands in Phase 8 (application preparation).")


@cli.command("apply")
def apply_() -> None:
    """Submit approved applications (Phase 9)."""
    raise NotImplementedError("apply lands in Phase 9 (human approval interface).")


@cli.command("report")
def report() -> None:
    """Generate/send the daily report (Phase 10)."""
    raise NotImplementedError("report lands in Phase 10 (email notifications).")


@cli.command("scheduler")
def scheduler() -> None:
    """Start the daily scheduler (Phase 11)."""
    raise NotImplementedError("scheduler lands in Phase 11 (daily scheduler).")


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
