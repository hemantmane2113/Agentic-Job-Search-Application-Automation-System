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
    auto_apply: bool = False

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
    weight_salary: float = 15
    weight_location: float = 10
    weight_education: float = 5

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
