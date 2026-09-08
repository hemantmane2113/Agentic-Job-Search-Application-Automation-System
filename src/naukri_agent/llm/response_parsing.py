"""
Shared helper for pulling a JSON object out of raw LLM text. Used by
both jobs.parser.JobParser and resume.tailor.ResumeTailor so the
recovery logic (markdown fences, surrounding prose) lives in one
place rather than being duplicated.
"""

from __future__ import annotations

import json
import re

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json_text(raw: str) -> str:
    """
    Best-effort extraction of a JSON object from raw LLM text. Tries
    the text as-is first, then with markdown code fences stripped,
    then falls back to the substring between the first '{' and the
    last '}'. Does not attempt to fix malformed JSON beyond this — if
    none of these produce valid JSON, parsing is meant to fail.
    """
    candidates = [raw, _CODE_FENCE_RE.sub("", raw).strip()]
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidates.append(raw[first_brace : last_brace + 1])

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError as exc:
            last_error = exc
    raise last_error or json.JSONDecodeError("no JSON object found", raw, 0)
