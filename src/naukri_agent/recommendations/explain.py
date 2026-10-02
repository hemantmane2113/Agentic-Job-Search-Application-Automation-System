"""
Match explanation.

DETERMINISTIC first: `reasons` = MatchResult.positive_factors,
`gaps` = MatchResult.negative_factors — always shown in the email.

OPTIONAL LLM narrative: a short prose summary OVER those same factors.
It is additive colour, never authoritative. Guardrails:
  * the LLM is given only the job title/company + the deterministic
    factor strings + the score/decision + the pre-computed factual
    labels (application status, freshness). It is NOT given the raw JD
    or MasterResume.
  * if the LLM narrative contradicts a factual label (claims an
    application/no-application, or a different freshness), the narrative
    is DROPPED and a reason recorded. The label always wins.
  * the LLM never returns the score, status, freshness, or URL — those
    are computed elsewhere and the email prints the deterministic value.
"""

from __future__ import annotations

import logging

from naukri_agent.llm.base import LLMProvider
from naukri_agent.llm.exceptions import LLMError
from naukri_agent.matching.models import MatchResult
from naukri_agent.recommendations.models import MatchExplanation

logger = logging.getLogger(__name__)

_NARRATIVE_MAX = 400

_CONTRADICTION_TERMS = (
    "you applied",
    "already applied",
    "you have applied",
    "has applied",
    "you did not apply",
    "have not applied",
    "not yet applied",
    "never applied",
    "you were rejected",
    "you interviewed",
)

SYSTEM_PROMPT = (
    "You write a 1-2 sentence plain summary of why a job matches a candidate, "
    "using ONLY the provided match factors. Do NOT invent skills, requirements, "
    "numbers, dates, or URLs. Do NOT state whether the candidate applied to the "
    "job or comment on application status. Return only the summary text."
)


def explain_match(
    match_result: MatchResult,
    *,
    job_title: str,
    company: str,
    application_status_label: str,
    freshness_label: str,
    provider: LLMProvider | None = None,
) -> MatchExplanation:
    reasons = list(match_result.positive_factors)
    gaps = list(match_result.negative_factors)

    if provider is None:
        return MatchExplanation(reasons=reasons, gaps=gaps, source="deterministic")

    user_prompt = (
        f"Job: {job_title} at {company}\n"
        f"Overall match score: {match_result.overall_score} / 100 "
        f"({match_result.decision.value})\n"
        f"Positive factors:\n- " + "\n- ".join(reasons or ["(none)"]) + "\n"
        f"Gaps:\n- " + "\n- ".join(gaps or ["(none)"]) + "\n\n"
        "Write the 1-2 sentence summary."
    )

    try:
        raw = provider.complete(SYSTEM_PROMPT, user_prompt, json_mode=False)
    except LLMError as exc:
        logger.warning("explain_match: LLM failed (%s); using deterministic factors", exc)
        return MatchExplanation(
            reasons=reasons, gaps=gaps, source="deterministic",
            narrative_dropped_reason=f"llm_error: {type(exc).__name__}",
        )

    narrative = (raw or "").strip()[:_NARRATIVE_MAX]
    lowered = narrative.lower()
    if any(term in lowered for term in _CONTRADICTION_TERMS):
        return MatchExplanation(
            reasons=reasons, gaps=gaps, source="deterministic", narrative=None,
            narrative_dropped_reason="narrative_touched_application_status",
        )
    if not narrative:
        return MatchExplanation(reasons=reasons, gaps=gaps, source="deterministic")

    return MatchExplanation(
        reasons=reasons, gaps=gaps, narrative=narrative, source="llm"
    )
