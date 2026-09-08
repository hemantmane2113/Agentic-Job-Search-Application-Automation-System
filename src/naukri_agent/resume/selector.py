"""
Resume selection: choose which of the candidate's EXISTING resume
files best fits a job. Never generates, edits, or rewords anything —
selection only ever picks among files that already exist.

    Job + JobExtraction -> select_resume() -> ResumeSelectionOutcome

Matching order:
1. DETERMINISTIC: exact/substring match between the job's role and
   each registry entry's `roles` list. Preferred whenever it resolves
   to exactly one entry.
2. LLM-ASSISTED (optional, only if a provider is given, and only when
   deterministic matching is ambiguous or empty): the LLM classifies
   the job into one of the registry's EXISTING ids — never a new one.
   Same structural anti-fabrication pattern as the rest of this
   project: the schema has no field for inventing a category, and any
   id outside the known set is treated as "could not classify".
3. Otherwise: REVIEW. A missing resume file always forces REVIEW too,
   regardless of how confident the role match was — this system will
   not silently skip resume attachment.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ValidationError

from naukri_agent.database.models import Job, JobExtraction
from naukri_agent.llm.base import LLMProvider
from naukri_agent.llm.exceptions import LLMError
from naukri_agent.llm.response_parsing import extract_json_text
from naukri_agent.resume.registry import (
    ResumeFileStatus,
    ResumeMatchVia,
    ResumeRegistry,
    ResumeSelectionDecision,
    check_registry_files,
)

logger = logging.getLogger(__name__)


class ResumeSelectionOutcome(BaseModel):
    decision: ResumeSelectionDecision
    resume_id: str | None = None
    file: str | None = None
    file_hash: str | None = None
    matched_via: ResumeMatchVia | None = None
    reason: str


def _target_role(job: Job, extraction: JobExtraction | None) -> str:
    if extraction is not None and extraction.normalized_title:
        return extraction.normalized_title
    return job.title


def match_deterministic(target_role: str, registry: ResumeRegistry) -> list[str]:
    """
    Return the ids of every registry entry whose `roles` list contains
    something matching target_role (case-insensitive, exact or
    substring in either direction).
    """
    target_lower = target_role.strip().lower()
    matches = []
    for entry in registry.resumes:
        for role in entry.roles:
            role_lower = role.strip().lower()
            if role_lower == target_lower or role_lower in target_lower or target_lower in role_lower:
                matches.append(entry.id)
                break
    return matches


class _LLMRoleClassification(BaseModel):
    """
    The EXACT (and only) thing asked of the LLM: which known resume
    id fits, or null. There is no field here for a new category name
    — the LLM can select from the given ids or say it doesn't know.
    """

    resume_id: str | None = None


def classify_role_with_llm(
    provider: LLMProvider,
    target_role: str,
    extraction: JobExtraction | None,
    registry: ResumeRegistry,
) -> str | None:
    """
    Ask the LLM to classify target_role into one of registry's
    EXISTING ids. Returns that id only if it's genuinely one of the
    known ids; returns None on any failure, invalid response, or an
    id the LLM invented that isn't in the registry — never guesses on
    the caller's behalf.
    """
    known_ids = [e.id for e in registry.resumes]
    categories = {e.id: e.roles for e in registry.resumes}

    system_prompt = f"""You classify a job into one of a fixed set of resume categories. You do not write or modify resume content.

Known categories (id: associated roles):
{json.dumps(categories, indent=2)}

Return ONLY a JSON object: {{"resume_id": one of {known_ids} or null}}.
Return null if the job does not clearly fit exactly one of these categories. Never return an id that is not in the list above — inventing a new category is not allowed."""

    required = extraction.required_skills if extraction and extraction.required_skills else []
    preferred = extraction.preferred_skills if extraction and extraction.preferred_skills else []
    user_prompt = (
        f"Job title: {target_role}\nRequired skills: {required}\nPreferred skills: {preferred}\n\n"
        "Return only the JSON object described in the system instructions."
    )

    try:
        raw = provider.complete(system_prompt, user_prompt, json_mode=True)
        data = json.loads(extract_json_text(raw))
        parsed = _LLMRoleClassification.model_validate(data)
    except (LLMError, json.JSONDecodeError, ValidationError) as exc:
        logger.warning("LLM role classification failed for %r: %s", target_role, exc)
        return None

    if parsed.resume_id in known_ids:
        return parsed.resume_id

    if parsed.resume_id is not None:
        logger.warning(
            "LLM returned an unknown resume id %r for %r; treating as unclassified",
            parsed.resume_id,
            target_role,
        )
    return None


def _status_for(resume_id: str, statuses: list[ResumeFileStatus]) -> ResumeFileStatus | None:
    return next((s for s in statuses if s.id == resume_id), None)


def select_resume(
    job: Job,
    extraction: JobExtraction | None,
    registry: ResumeRegistry,
    provider: LLMProvider | None = None,
) -> ResumeSelectionOutcome:
    target_role = _target_role(job, extraction)
    statuses = check_registry_files(registry)

    def _outcome_for(resume_id: str, matched_via: ResumeMatchVia, reason: str) -> ResumeSelectionOutcome:
        status = _status_for(resume_id, statuses)
        if status is None or not status.exists:
            return ResumeSelectionOutcome(
                decision=ResumeSelectionDecision.REVIEW,
                resume_id=resume_id,
                matched_via=matched_via,
                reason=f"Matched resume {resume_id!r} but its file was not found on disk.",
            )
        return ResumeSelectionOutcome(
            decision=ResumeSelectionDecision.SELECTED,
            resume_id=resume_id,
            file=str(status.file),
            file_hash=status.file_hash,
            matched_via=matched_via,
            reason=reason,
        )

    deterministic_matches = match_deterministic(target_role, registry)

    if len(deterministic_matches) == 1:
        return _outcome_for(
            deterministic_matches[0],
            ResumeMatchVia.DETERMINISTIC,
            f"Role {target_role!r} matched resume {deterministic_matches[0]!r} by role list.",
        )

    if provider is not None:
        llm_resume_id = classify_role_with_llm(provider, target_role, extraction, registry)
        if llm_resume_id is not None:
            return _outcome_for(
                llm_resume_id,
                ResumeMatchVia.LLM,
                f"Role {target_role!r} classified as {llm_resume_id!r} by LLM (deterministic match was ambiguous).",
            )

    if not deterministic_matches:
        reason = f"No resume in the registry lists a matching role for {target_role!r}."
    else:
        reason = (
            f"Role {target_role!r} matched multiple resumes "
            f"({', '.join(deterministic_matches)}) and could not be resolved confidently."
        )
    return ResumeSelectionOutcome(decision=ResumeSelectionDecision.REVIEW, reason=reason)
