"""
`research-jobs`: run the company-site job researcher over the jobs the latest digest told you to
apply for yourself, save each report, and send it to you on Telegram.

Read-only end to end. The only browser use is reading Naukri's company page (through the one
function the agent is allowed, with no address of its own to give). Nothing is clicked, applied or
submitted, and your profile, resume and contact details are never sent to the model.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Callable

from pydantic import BaseModel, Field

from naukri_agent.config import Settings
from naukri_agent.research_agent.loop import run_agent
from naukri_agent.research_agent.models import ResearchReport
from naukri_agent.research_agent.tools import Limits, ToolBox
from naukri_agent.research_agent.web import BraveSearch, SerperSearch, fetch_page

logger = logging.getLogger(__name__)


class ResearchConfigError(RuntimeError):
    """A needed setting is missing; the message says which."""


class ResearchOutcome(BaseModel):
    job_id: int
    title: str
    company: str
    status: str  # ok | failed
    error: str | None = None
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    message: str | None = None


class ResearchRunResult(BaseModel):
    considered: int = 0
    researched: int = 0
    failed: int = 0
    outcomes: list[ResearchOutcome] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def build_search(settings: Settings) -> Any:
    """The search client for the configured provider, or None when there is no key to use."""
    choice = (settings.research_search_provider or "auto").lower()
    if choice == "none":
        return None
    if choice in ("auto", "serper") and settings.serper_api_key:
        return SerperSearch(settings.serper_api_key)
    if choice in ("auto", "brave") and settings.brave_api_key:
        return BraveSearch(settings.brave_api_key)
    return None


def select_jobs(session: Any, settings: Settings, now: datetime.datetime, *, job_id: int | None, limit: int) -> list[dict]:
    """The latest digest's company-site jobs (best rank first), skipping any researched recently."""
    from naukri_agent.database.models import DailyRun, DailyRunStatus, Job, JobRecommendation
    from naukri_agent.database.repositories import job_ids_researched_since

    from naukri_agent.database.repositories import current_job_extraction

    def as_dict(job: Job) -> dict:
        extraction = current_job_extraction(session, job.id)
        kind = getattr(getattr(extraction, "job_type", None), "value", None)
        return {
            "employment_type": kind if kind and kind != "unknown" else None,
            "job_id": job.id, "title": job.title, "company": job.company, "location": job.location,
            "experience_text": job.experience_text, "salary_text": job.salary_text,
            "description": job.description, "url": job.url,
        }

    if job_id is not None:  # an explicit request is always honoured, even for a job done recently
        job = session.get(Job, job_id)
        return [as_dict(job)] if job is not None else []

    run = (
        session.query(DailyRun).filter(DailyRun.status == DailyRunStatus.COMPLETED).order_by(DailyRun.id.desc()).first()
    )
    if run is None:
        return []
    done = job_ids_researched_since(session, now - datetime.timedelta(days=settings.research_skip_days))
    rows = (
        session.query(JobRecommendation, Job)
        .join(Job, Job.id == JobRecommendation.job_id)
        .filter(JobRecommendation.daily_run_id == run.id, Job.apply_type == "company_site")
        .order_by(JobRecommendation.rank)
        .all()
    )
    picked = []
    for _rec, job in rows:
        if job.id in done:
            continue
        picked.append(as_dict(job))
        if len(picked) >= limit:
            break
    return picked


def format_report(job: dict, report: ResearchReport) -> str:
    lines = [f"Research: {job['title']} - {job['company']}", "", report.company_summary, ""]
    lines.append(f"Careers page: {report.careers_url or 'not found'}")
    if report.apply_candidates:
        lines.append("Possible apply pages:")
        for i, c in enumerate(report.apply_candidates, 1):
            lines.append(f"  {i}. {c.url} ({c.confidence})" + (f" - {c.why}" if c.why else ""))
    lines.append(f"Lists this role: {report.lists_this_role}")
    if report.differences:
        lines.append("Differs from the Naukri post: " + "; ".join(report.differences))
    if report.red_flags:
        lines.append("Flags: " + "; ".join(report.red_flags))
    lines.append(f"Pages read: {len(report.sources)}")
    for note in report.notes:
        lines.append(f"Note: {note}")
    lines += ["", f"Naukri: {job['url']}"]
    return "\n".join(lines)[:3500]


class NaukriReader:
    """Opens the browser the first time the agent asks for the company page, closes it at the end."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._browser: Any = None
        self._client: Any = None

    def read(self, job_url: str) -> dict:
        if self._client is None:
            from naukri_agent.browser.browser_manager import BrowserManager
            from naukri_agent.browser.naukri_client import NaukriClient

            self._browser = BrowserManager(self._settings)
            self._browser.launch()
            self._client = NaukriClient(self._browser.page, self._settings)
            self._client.login()
        return self._client.read_company_page(job_url)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = self._client = None


def run_research(
    settings: Settings,
    *,
    session_factory: Any = None,
    chat_client: Any = None,
    search: BraveSearch | None | str = "auto",
    fetch: Callable[[str], Any] | None = None,
    reader: Any = None,
    notify: Callable[[str], None] | None = None,
    now: datetime.datetime | None = None,
    max_jobs: int | None = None,
    job_id: int | None = None,
    dry_run: bool = False,
) -> ResearchRunResult:
    from naukri_agent.database.base import init_db, session_scope
    from naukri_agent.database.repositories import add_job_research

    now = now or datetime.datetime.now(datetime.UTC)
    result = ResearchRunResult()

    if chat_client is None:
        if not settings.groq_api_key:
            raise ResearchConfigError("GROQ_API_KEY is not set in .env, so the researcher has no model to use")
        from naukri_agent.research_agent.client import GroqChatClient

        chat_client = GroqChatClient(settings.groq_api_key, settings.research_llm_model, timeout=settings.research_llm_timeout_seconds)
    if search == "auto":
        search = build_search(settings)
    if search is None:
        result.notes.append(
            "no search key is set (SERPER_API_KEY or BRAVE_API_KEY): the agent cannot search the web; "
            "it can only use Naukri's company page and pages it guesses, so expect weaker results"
        )
    fetch_fn = fetch or (lambda url: fetch_page(url, max_bytes=settings.research_page_max_bytes))

    factory = session_factory or init_db(settings)
    with session_scope(factory) as s:
        jobs = select_jobs(s, settings, now, job_id=job_id, limit=max_jobs or settings.research_max_jobs)
    result.considered = len(jobs)
    if not jobs:
        result.notes.append("no company-site jobs to research (none in the latest digest, or all done recently)")
        return result

    own_reader = reader is None
    reader = reader or NaukriReader(settings)
    limits = Limits(max_fetches=settings.research_max_fetches, max_searches=settings.research_max_searches)
    try:
        for job in jobs:
            box = ToolBox(
                job=job, limits=limits, search=search, fetch=fetch_fn,
                read_company=(lambda j=job: reader.read(j["url"])),
            )
            brief = f"{job['title']} at {job['company']}, {job['location']}."
            res = run_agent(chat_client, box, brief, max_steps=settings.research_max_steps, compact=settings.research_compact_history)
            ok = res.report is not None
            outcome = ResearchOutcome(
                job_id=job["job_id"], title=job["title"], company=job["company"],
                status="ok" if ok else "failed", error=res.error, steps=res.steps,
                prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens,
                message=format_report(job, res.report) if ok else None,
            )
            result.outcomes.append(outcome)
            result.researched += ok
            result.failed += not ok
            if not dry_run:
                with session_scope(factory) as s:
                    add_job_research(
                        s, job_id=job["job_id"], status=outcome.status, model=getattr(chat_client, "model", "unknown"),
                        report_json=res.report.model_dump_json() if ok else None, error=res.error,
                        steps=res.steps, prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens,
                        created_at=now,
                    )
                if ok and notify is not None:
                    try:
                        notify(outcome.message)
                    except Exception as exc:  # noqa: BLE001 - the report is saved; sending is a courtesy
                        result.notes.append(f"could not send the report for job {job['job_id']} ({type(exc).__name__})")
    finally:
        if own_reader:
            reader.close()
    return result
