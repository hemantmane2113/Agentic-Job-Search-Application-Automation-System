"""
Job domain models.

Two shapes, deliberately kept separate:

- JobCreate: RAW data as scraped from Naukri. Every field is
  something a human could read directly off the listing page —
  nothing here is inferred.
- JobExtractionCreate: STRUCTURED/derived data as produced by an LLM
  parser (Phase 5). Every field here is inferred or normalized from a
  Job's raw description. Phase 3 only defines this shape and its
  storage — nothing populates it with real LLM output yet.

See database/repositories.py for how these get persisted (upsert_job,
add_job_extraction) and database/models.py for the ORM tables.
"""

from __future__ import annotations

import enum
import hashlib
import re

from pydantic import BaseModel, Field


class JobType(str, enum.Enum):
    """
    A DERIVED classification — not something you can read directly off
    a raw listing without interpretation, so it belongs to the
    extraction layer, not JobCreate.
    """

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    UNKNOWN = "unknown"


class JobCreate(BaseModel):
    """RAW job data, as scraped, before any LLM processing."""

    title: str
    company: str
    location: str
    salary_text: str | None = None
    experience_text: str | None = None
    description: str
    url: str
    posted_date_text: str | None = None
    source: str = "naukri"


class JobExtractionCreate(BaseModel):
    """
    STRUCTURED/derived job data, as an LLM parser (Phase 5) would
    produce it. Distinct from JobCreate so the two can never be
    confused for one another or accidentally merged into one table.
    """

    llm_provider: str | None = None
    llm_model: str | None = None

    normalized_title: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    experience_min: float | None = None
    experience_max: float | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    education_requirements: list[str] = Field(default_factory=list)
    job_type: JobType = JobType.UNKNOWN

    # The LLM's actual output, verbatim, before it was parsed into the
    # typed fields above — this is what makes the extraction step
    # auditable rather than a black box.
    raw_llm_response: str | None = None


class LLMJobExtractionPayload(BaseModel):
    """
    The EXACT JSON shape asked of the LLM — deliberately narrower than
    JobExtractionCreate. llm_provider/llm_model/raw_llm_response are
    audit metadata filled in by JobParser itself after the call
    returns; the model is never asked for them and any attempt by the
    LLM to include them (or any other field) is silently dropped by
    Pydantic's default extra="ignore" behavior. This is the structural
    half of this system's prompt-injection defense: even a
    successfully-manipulated response can't smuggle data past this
    schema boundary.
    """

    normalized_title: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    experience_min: float | None = None
    experience_max: float | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    education_requirements: list[str] = Field(default_factory=list)
    job_type: JobType = JobType.UNKNOWN


def extract_external_id(url: str) -> str | None:
    """
    Best-effort extraction of Naukri's own job identifier from a job
    URL. Naukri job URLs end in a numeric ID, e.g.
    ".../job-listings-python-developer-pune-020124500123" ->
    "020124500123". Returns None if the URL doesn't match the
    expected pattern, in which case dedup falls back to exact URL
    matching — see database.repositories.upsert_job.
    """
    match = re.search(r"-(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else None


def compute_content_fingerprint(title: str, company: str, description: str) -> str:
    """
    A hash of the normalized (lowercased, whitespace-collapsed) title,
    company, and description. Two listings with the same fingerprint
    but different URLs/external_ids are treated as a likely repost of
    the same underlying job — see database.repositories.upsert_job.
    """

    def normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower())

    canonical = f"{normalize(title)}|{normalize(company)}|{normalize(description)}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
