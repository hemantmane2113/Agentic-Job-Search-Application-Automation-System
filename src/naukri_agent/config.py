"""
Central configuration for naukri-agent.

All runtime configuration is loaded from environment variables (and a
.env file, for local development) into a single validated Settings
object. Nothing else in the codebase should call os.environ directly —
import get_settings() instead, so every value is validated once, in
one place, with one schema.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProviderName(str, Enum):
    """
    WHO serves the model. This is deliberately separate from which
    model is requested (see Settings.llm_model) — a provider is an
    inference platform/runtime, never a model.
    """

    OPENAI = "openai"
    GROQ = "groq"
    OLLAMA = "ollama"


class Settings(BaseSettings):
    """
    Validated application settings, populated from environment
    variables / a .env file. Field names map to env vars of the same
    name in upper case (e.g. `database_url` <- DATABASE_URL).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- General ---
    app_env: str = "development"
    timezone: str = "Asia/Kolkata"
    dry_run: bool = True
    # Phase 14: one of two independent gates `apply` requires (the other
    # is dry_run=False) before it will even start — see cli/main.py's
    # `apply` command. Defaults false so nothing changes for anyone not
    # explicitly opting in.
    auto_apply: bool = False
    # Third gate, on top of auto_apply and dry_run, for the UNATTENDED
    # `auto-apply` command only. Off by default: building the capability
    # applies to nothing until this is switched on deliberately.
    auto_apply_unattended: bool = False
    auto_apply_daily_cap: int = 4  # rolling 24h, counts applied + unconfirmed. The Telegram share of daily_job_total
    auto_apply_decisions: list[str] = Field(default_factory=lambda: ["ACCEPT"])
    auto_apply_max_job_age_days: int = 7
    # While this file exists, auto-apply refuses to run. A manual kill switch.
    auto_apply_pause_file: Path = Path("./data/PAUSE_AUTO_APPLY")
    # Daily resume rotation on the Naukri profile (`profile-refresh --execute`). OFF unless set.
    profile_refresh_enabled: bool = False
    profile_refresh_pause_file: Path = Path("./data/PAUSE_PROFILE_REFRESH")

    # --- Telegram: the human-in-the-loop channel for `telegram-apply` ---
    # Create a bot with @BotFather, put its token here (never in chat or code),
    # message the bot once, then run `naukri-agent telegram-setup` to get the
    # chat id. Only messages from that one chat are ever acted on.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # After the daily run, tell the phone how many jobs are ready for `telegram-apply` (only when
    # Telegram is set up and there is at least one). Never applies anything by itself.
    telegram_ping_apply_ready: bool = True
    # Morning watchdog (`naukri-agent watchdog`, run by a scheduled task at 10:30 and 15:00).
    watchdog_daily_task_name: str = "Naukri Agent - Daily Jobs"
    watchdog_run_start_by: str = "10:30"  # local time after which "the run has not started" counts as a problem
    watchdog_digest_by: str = "15:00"  # local time after which "no finished digest yet" counts as a problem
    watchdog_max_run_hours: float = 5.0  # a run still going after this long is reported as possibly stuck
    watchdog_send_ok: bool = False  # also send a one-line "all good" once the digest is done
    watchdog_state_file: Path = Path("./data/watchdog_state.json")  # what it already said today
    # Parsing a job description with the local LLM sometimes fails for a reason that a second
    # attempt fixes (a timeout, a malformed reply). Retry once, at most this many times per run
    # in total, so a bad day cannot add hours. An oversized description is never retried.
    parse_retries_per_run: int = 6
    # Start applying from the phone: `naukri-agent telegram-listen` waits on the PC for "/apply" from YOUR chat and
    # then starts `telegram-apply` (which still needs your Yes on every job). OFF unless set to true.
    telegram_remote_start: bool = False
    telegram_listener_max_command_age_seconds: int = 600  # an older "/apply" (sent while the PC was off) is ignored
    telegram_listener_run_timeout_minutes: int = 180  # the apply run it starts is stopped after this long
    telegram_apply_lock_file: Path = Path("./data/telegram_apply.lock")  # held while a telegram-apply run is going
    # "Did you apply?" for company-website jobs, asked on Telegram by the phone listener: when you send /applied, and as
    # a reminder every day at followup_reminder_time (only when a job is still waiting for an answer).
    followup_reminder_enabled: bool = True
    followup_reminder_time: str = "20:00"  # 24-hour HH:MM, in `timezone`
    followup_lookback_days: int = 7  # jobs from digests older than this are not asked about
    followup_max_jobs: int = 12  # at most this many questions in one go
    followup_ignore_after_later: int = 4  # the 4th "Later" in a row turns the job into "ignored"
    followup_answer_timeout_minutes: int = 15  # no tap in this time ends the questions; nothing is changed
    followup_state_file: Path = Path("./data/followup_state.json")  # the day the reminder last went out
    telegram_approval_timeout_minutes: int = 20  # waiting for the job's Yes/No
    telegram_answer_timeout_minutes: int = 15  # waiting for each answer / final Yes

    # --- Database ---
    database_url: str = "sqlite:///./data/naukri_agent.db"

    # --- Logging ---
    log_dir: Path = Path("./logs")
    log_level: str = "INFO"

    # --- Candidate profile / master resume (Phase 2) ---
    # Canonical content lives in these YAML files, not in the
    # database or in code — see candidate/models.py and
    # resume/models.py for the loaders and validation.
    candidate_profile_path: Path = Path("./config/candidate_profile.yaml")
    master_resume_path: Path = Path("./config/master_resume.yaml")

    # --- Job matching (Phase 4) ---
    # Category weights need not sum to 100 — JobScorer normalizes by
    # their total, so retuning one doesn't require rebalancing all of
    # them. Defaults reproduce the master spec's own example (Section 9).
    weight_skills: float = 35
    weight_experience: float = 20
    weight_role: float = 15
    weight_salary: float = 11
    weight_location: float = 7
    weight_education: float = 5
    # Posted recency: 7 marks if posted today, down to 1 for a job posted a week ago (see matching/recency_matcher.py).
    # Salary and location gave up 4 and 3 marks for it: salary barely separated jobs (everyone got 10+ of 15) and
    # location matches every job now that 22 cities are listed. The seven weights add up to 100.
    weight_recency: float = 7

    # Overall-score thresholds (0-100) for the ACCEPT/REVIEW/REJECT
    # decision. Anything >= threshold_accept is ACCEPT; between
    # threshold_review and threshold_accept is REVIEW; below is REJECT.
    threshold_accept: float = 80
    threshold_review: float = 70

    # Within the Skills category, how much of the score comes from
    # required-skill coverage vs preferred-skill coverage. 0.8 means a
    # missing required skill costs 4x what a missing preferred skill
    # costs the same way, by construction.
    required_skills_weight_ratio: float = 0.8

    # Missing-information handling (Section 5) — never a silent
    # rejection, always a configurable partial credit plus an
    # explanatory negative factor.
    salary_unknown_credit_ratio: float = 0.67
    experience_unknown_credit_ratio: float = 0.67

    # A job's max salary within this fraction of the candidate's
    # minimum ask still earns partial (not zero) credit.
    salary_tolerance_ratio: float = 0.9

    # --- LLM request behavior (Phase 5) ---
    llm_timeout_seconds: float = 60.0

    # --- Resume registry (Phase 6, revised) ---
    # The candidate's existing, manually-created resume files are
    # catalogued here — this system never generates or edits resume
    # content. See resume/registry.py.
    resume_registry_path: Path = Path("./config/resumes.yaml")

    # --- Resume role-classification LLM override (Phase 6) ---
    # Optional: used only to assist classifying an ambiguous job into
    # one of the resume registry's EXISTING categories (never to
    # generate content). Falls back to LLM_PROVIDER/LLM_MODEL if unset.
    resume_llm_provider: LLMProviderName | None = None
    resume_llm_model: str | None = None

    # --- Apply-answer drafting LLM override (Phase 14) ---
    # Optional: used only to draft a screening-question answer for the
    # human to approve/edit (agents/apply_answer_agent.py) — grounded
    # strictly in CandidateProfile/MasterResume facts. Falls back to
    # LLM_PROVIDER/LLM_MODEL if unset, same shape as resume_llm_provider
    # above.
    apply_llm_provider: LLMProviderName | None = None
    apply_llm_model: str | None = None

    # --- LLM: provider and model are configured independently. ---
    # llm_provider says WHO to call (ollama / groq / openai).
    # llm_model says WHICH model to ask that provider for. The two are
    # validated separately; there is intentionally no hard-coded
    # mapping from provider -> allowed model names, since providers
    # add/remove models over time and we don't want a code change for
    # every new model release.
    llm_provider: LLMProviderName = LLMProviderName.OLLAMA
    llm_model: str = "llama3.1"
    ollama_host: str = "http://localhost:11434"
    groq_api_key: str = ""
    openai_api_key: str = ""

    # --- Company-site job researcher (`research-jobs`) ---
    # A tool-using agent: a hosted Groq model (it reuses GROQ_API_KEY) chooses read-only tools to
    # find the company's own careers page for the jobs the digest says to apply for yourself.
    # It never applies, clicks or logs in anywhere, and sees only public job text.
    research_llm_model: str = "openai/gpt-oss-120b"  # a Groq model that supports tool calling (llama-3.3-70b was retired)
    research_llm_timeout_seconds: float = 60.0
    # Web search for the careers page. Pick whichever you have a key for; with none, the agent still
    # works in a weaker mode (the Naukri company page, plus pages it can guess such as /careers).
    serper_api_key: str = ""  # https://serper.dev  (has a free starter allowance)
    brave_api_key: str = ""  # https://api.search.brave.com  (now a paid plan)
    research_search_provider: str = "auto"  # auto | serper | brave | none
    # Keep only full-time, permanent jobs (Naukri's own Employment Type field). A job with no such field is
    # kept and marked 'not confirmed'. Off = no filtering.
    employment_filter_enabled: bool = True
    research_email: bool = True  # send the research as one email (to NOTIFY_EMAIL_TO) when research-jobs runs
    research_wait_hours: float = 5.0  # `--wait-for-digest` gives up after this long
    research_max_jobs: int = 10  # jobs researched per command (the digest itself holds at most 10)
    # Groq's free plan allows 200,000 tokens a day. Stop starting new jobs once this many were used in the
    # last 24 hours (a job takes roughly 12-16k), so the last job is never begun just to hit the wall.
    research_daily_token_budget: int = 180_000
    research_tokens_per_job_estimate: int = 16_000
    research_max_steps: int = 8  # model turns per job, then it must submit what it has
    research_max_fetches: int = 5  # web pages read per job
    research_max_searches: int = 3  # web searches per job
    research_page_max_bytes: int = 300_000  # larger pages are cut off
    research_compact_history: bool = False  # shorten OLDER tool results each turn; tested: no consistent gain, so OFF
    # Put the employer's direct job address in the research email. It is read from the job data Naukri's own page
    # loads (browser/company_link.py); nothing is pressed (pressing "Apply on company site" would mark the job
    # Applied on your account) and no request is sent beyond opening the job page.
    research_read_direct_link: bool = True
    research_skip_days: int = 7  # a job researched within this many days is not redone

    # --- Naukri credentials (Phase 7) ---
    naukri_email: str = ""
    naukri_password: str = ""

    # --- Browser automation (Phase 7) ---
    # headless=False by default so a human can see and manually solve
    # CAPTCHA/MFA challenges during interactive runs (e.g. `inspect`).
    naukri_headless: bool = False
    browser_profile_dir: Path = Path("./data/browser_profile")
    inspection_output_dir: Path = Path("./inspection_output")

    # --- Email notifications (Phase 10) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    notify_email_to: str = ""

    # --- Daily recommendation digest (scope change: read-only match digest) ---
    # DB is the source of truth; Excel is a regenerated mirror; the LLM
    # never determines score / application status / freshness / URLs.
    daily_recommendation_limit: int = 10  # hard ceiling for the 'apply yourself' list
    # ONE daily budget across both kinds of job: up to auto_apply_daily_cap through Telegram (Naukri Apply)
    # and the rest to apply for yourself on the company's site. Unused Telegram slots go to the other list.
    daily_job_total: int = 10
    # Random pauses (seconds) between page loads in discovery, and before an apply click. 0 turns them off.
    browse_pause_min_seconds: float = 3.0
    browse_pause_max_seconds: float = 8.0
    apply_pause_min_seconds: float = 2.0
    apply_pause_max_seconds: float = 5.0
    # Minimum overall_score (0-100) for a match to be eligible for the
    # email. None -> fall back to threshold_accept (80): only ACCEPT-level jobs are listed.
    recommendation_min_score: float | None = None
    # Order of the digest: True = newest bucket first, then score (the old behaviour); False (default) =
    # highest score first, so the 'top 10' really are the 10 best-scoring jobs.
    recommendation_rank_freshness_first: bool = False
    # A previously-recommended-but-NOT-APPLIED job is eligible again:
    #   0   -> next run (no cooldown)
    #   > 0 -> only after this many days since the last recommendation
    recommendation_cooldown_days: int = 30
    # ApplicationHistory statuses that exclude a job from ALL future
    # recommendations, regardless of cooldown.
    recommendation_exclude_if_status: list[str] = Field(
        default_factory=lambda: [
            "APPLIED", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN",
            "NOT_APPLYING", "IGNORED",  # your answers to the Telegram "did you apply?" question: never suggested again
        ]
    )
    # MatchDecisions eligible for the digest.
    recommendation_decisions: list[str] = Field(
        default_factory=lambda: ["ACCEPT"]  # REVIEW (70-79) jobs are left out; fewer than 10 a day is fine
    )
    # A job first seen within this many days, never recommended, is
    # labelled "Newly discovered".
    freshness_new_days: int = 3

    # --- Discovery ---
    # Optional explicit "role @ location" query overrides; None -> derive
    # from CandidateProfile.preferred_roles x preferred_locations.
    discovery_queries: list[str] | None = None
    discovery_max_jobs_per_query: int = 40
    # False (default): find the day's jobs with as few searches as it takes. The role-only searches (all of India)
    # run first, then city searches are added one at a time, and the moment discovery_fresh_job_limit fresh jobs
    # are found, with every resume group holding an equal share, it stops searching. True: the old behaviour,
    # every role in every preferred city. An explicit discovery_queries list is always run as given.
    discovery_search_every_city: bool = False
    discovery_max_total_jobs: int = 200

    # Freshness-first daily feed (Phase F1). After dedup and the
    # discovery_max_total_jobs ceiling, discovery keeps only jobs whose
    # Naukri search-card posted-date label parses to <= this many days,
    # sorts them newest-first, and sends at most discovery_fresh_job_limit
    # of them on to JD fetch + LLM parsing. Cards with an absent or
    # unparseable posted label are excluded (never assumed fresh) and
    # counted in the discover_freshness RunEvent. discovery_fresh_job_limit
    # is the LLM/JD candidate-processing bound; it is deliberately larger
    # than daily_recommendation_limit (the final digest cap) so the top-10
    # matches can be found without the V1 175-200-job / multi-hour parse.
    discovery_freshness_days: int = 7
    discovery_fresh_job_limit: int = 50

    # --- Manual application recording ---
    mark_applied_default_status: str = "APPLIED"
    # When `mark-applied` is run without --resume, use the job's stored
    # ResumeSelection.resume_id.
    mark_applied_default_resume_from_selection: bool = True

    # --- Digest email delivery ---
    # "file"    -> write the rendered digest under email_output_dir (default)
    # "console" -> print it to stdout
    # "smtp"    -> send real email via SmtpEmailSender (notifications/email.py);
    #              requires smtp_host/smtp_username/smtp_password/notify_email_to
    #              all set, or the run fails loudly (EmailConfigError) rather
    #              than silently falling back to "file"
    email_sender: str = "file"
    email_output_dir: Path = Path("./out/emails")
    email_subject_prefix: str = "[naukri-agent]"

    # --- Stage B: in-process daily scheduler ---
    # "HH:MM" (24-hour) in `timezone`, above. Only read by
    # `naukri-agent scheduler` (scheduler/daemon.py) — run-daily / discover /
    # recommend are unaffected and still run immediately when invoked.
    daily_run_time: str = "10:00"

    @field_validator("daily_run_time")
    @classmethod
    def _validate_daily_run_time(cls, v: str) -> str:
        parts = v.strip().split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"daily_run_time must be 'HH:MM' (24-hour), got {v!r}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"daily_run_time must be 'HH:MM' (24-hour), got {v!r}")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("followup_reminder_time")
    @classmethod
    def _validate_followup_reminder_time(cls, v: str) -> str:
        parts = v.strip().split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts) or not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59):
            raise ValueError(f"followup_reminder_time must be 'HH:MM' (24-hour), got {v!r}")
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"

    # --- Match explanation ---
    # Optional NL polish over the deterministic reasons/gaps. The
    # deterministic factors are always shown; the LLM never sets the
    # score, application status, freshness label, or URL.
    explanation_use_llm: bool = False

    # --- Weekly report (`naukri-agent weekly-report`, run by a scheduled task every Sunday at 22:00) ---
    # One Excel file with every job given during the Monday-Sunday week and where each one stands, saved here and
    # emailed to NOTIFY_EMAIL_TO as an attachment.
    weekly_report_dir: Path = Path("./out/weekly")
    weekly_report_email: bool = True

    # --- Excel reporting mirror (regenerated from the DB every run) ---
    excel_export_enabled: bool = True
    excel_path: Path = Path("./out/job_search_history.xlsx")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v_upper = v.upper()
        if v_upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got {v!r}")
        return v_upper

    def llm_provider_is_configured(self) -> bool:
        """
        Whether the currently selected LLM provider has the
        credentials/host it needs to actually be called. Ollama only
        needs a reachable host (checked separately, at call time,
        since that's a network concern not a config concern); cloud
        providers need an API key.
        """
        if self.llm_provider == LLMProviderName.OLLAMA:
            return bool(self.ollama_host)
        if self.llm_provider == LLMProviderName.GROQ:
            return bool(self.groq_api_key)
        if self.llm_provider == LLMProviderName.OPENAI:
            return bool(self.openai_api_key)
        return False


@lru_cache
def get_settings() -> Settings:
    """
    Return the process-wide Settings singleton. Cached so validation
    only runs once; tests that need different settings should
    construct Settings(...) directly rather than mutating this.
    """
    return Settings()
