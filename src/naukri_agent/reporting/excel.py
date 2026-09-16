"""
export_workbook: regenerate the job-search-history .xlsx entirely from
the database. Deterministic; idempotent for unchanged DB state; never
writes any credential/secret. Sheets: Jobs, Applications, Daily Runs.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

from openpyxl import Workbook
from pydantic import BaseModel
from sqlalchemy.orm import Session

from naukri_agent.database.models import (
    ApplicationHistory,
    DailyRun,
    Job,
    JobExtraction,
    JobMatch,
    JobRecommendation,
    ResumeSelection,
)
from naukri_agent.database.repositories import canonical_job_id

logger = logging.getLogger(__name__)

_JOBS_HEADERS = [
    "Job ID", "Naukri Job ID", "Job Title", "Company", "Location", "Experience",
    "Salary", "Job URL", "First Seen", "Last Seen", "Match Score", "Match Decision",
    "Recommended Resume", "Recommendation Status", "First Recommended",
    "Last Recommended", "Application Status", "Applied Date", "Resume Used", "Notes",
]
_APPLICATIONS_HEADERS = [
    "Job ID", "Naukri Job ID", "Job Title", "Company", "Applied Date",
    "Application Status", "Resume Used", "Notes",
]
_RUNS_HEADERS = [
    "Date", "Jobs Discovered", "Jobs Evaluated", "Recommendations",
    "Email Status", "Run Status",
]


class ExcelExportResult(BaseModel):
    path: str
    jobs_rows: int
    applications_rows: int
    daily_runs_rows: int


def _dt(v: datetime.datetime | None) -> str:
    return v.strftime("%Y-%m-%d %H:%M") if v is not None else ""


def _date(v: datetime.datetime | None) -> str:
    return v.strftime("%Y-%m-%d") if v is not None else ""


def _experience(job: Job, extr: JobExtraction | None) -> str:
    if job.experience_text:
        return job.experience_text
    if extr and (extr.experience_min is not None or extr.experience_max is not None):
        return f"{extr.experience_min or '?'}-{extr.experience_max or '?'} yrs"
    return ""


def _salary(job: Job, extr: JobExtraction | None) -> str:
    if job.salary_text:
        return job.salary_text
    if extr and (extr.salary_min is not None or extr.salary_max is not None):
        return f"{extr.salary_currency or ''} {extr.salary_min or '?'}-{extr.salary_max or '?'}".strip()
    return ""


def export_workbook(session: Session, path: "str | Path", settings=None) -> ExcelExportResult:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    jobs_ws = wb.active
    jobs_ws.title = "Jobs"
    apps_ws = wb.create_sheet("Applications")
    runs_ws = wb.create_sheet("Daily Runs")
    jobs_ws.append(_JOBS_HEADERS)
    apps_ws.append(_APPLICATIONS_HEADERS)
    runs_ws.append(_RUNS_HEADERS)

    jobs = session.query(Job).order_by(Job.id.asc()).all()
    jobs_rows = 0
    for job in jobs:
        canon = canonical_job_id(session, job.id)
        if canon != job.id:
            continue  # fold reposts into the canonical row
        extr = (
            session.query(JobExtraction)
            .filter_by(job_id=job.id, is_current=True)
            .one_or_none()
        )
        match = (
            session.query(JobMatch)
            .filter_by(job_id=job.id)
            .order_by(JobMatch.matched_at.desc())
            .first()
        )
        sel = (
            session.query(ResumeSelection)
            .filter_by(job_id=job.id)
            .order_by(ResumeSelection.selected_at.desc())
            .first()
        )
        recs = (
            session.query(JobRecommendation)
            .filter_by(job_id=job.id)
            .order_by(JobRecommendation.recommended_at.asc())
            .all()
        )
        app = session.query(ApplicationHistory).filter_by(job_id=job.id).one_or_none()

        if app is not None:
            rec_status = "Applied"
        elif recs:
            rec_status = f"Recommended ({_date(recs[-1].recommended_at)})"
        else:
            rec_status = "Never"

        jobs_ws.append([
            job.id,
            job.external_id or "",
            job.title,
            job.company,
            job.location or "",
            _experience(job, extr),
            _salary(job, extr),
            job.url,
            _dt(job.first_seen_at),
            _dt(job.last_seen_at),
            round(match.overall_score, 1) if match else "",
            match.decision.value if match else "",
            (sel.resume_id or "") if sel else "",
            rec_status,
            _date(recs[0].recommended_at) if recs else "",
            _date(recs[-1].recommended_at) if recs else "",
            app.status.value if app else "Not applied",
            _date(app.applied_at) if app else "",
            (app.resume_id or "") if app else "",
            (app.notes or "") if app else "",
        ])
        jobs_rows += 1

    apps = (
        session.query(ApplicationHistory)
        .order_by(ApplicationHistory.updated_at.asc())
        .all()
    )
    for app in apps:
        apps_ws.append([
            app.job_id,
            app.external_job_id or "",
            app.job_title,
            app.company,
            _date(app.applied_at),
            app.status.value,
            app.resume_id or "",
            app.notes or "",
        ])

    runs = session.query(DailyRun).order_by(DailyRun.started_at.asc()).all()
    for run in runs:
        rec_count = (
            session.query(JobRecommendation).filter_by(daily_run_id=run.id).count()
        )
        emailed = (
            session.query(JobRecommendation)
            .filter_by(daily_run_id=run.id)
            .order_by(JobRecommendation.id.desc())
            .first()
        )
        runs_ws.append([
            _date(run.started_at),
            run.jobs_discovered,
            run.jobs_evaluated,
            rec_count,
            (emailed.email_status if emailed else ""),
            run.status.value,
        ])

    wb.save(path)
    logger.info("Excel workbook regenerated from DB -> %s", path)
    return ExcelExportResult(
        path=str(path),
        jobs_rows=jobs_rows,
        applications_rows=len(apps),
        daily_runs_rows=len(runs),
    )
