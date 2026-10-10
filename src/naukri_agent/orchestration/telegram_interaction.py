"""
The runner's four questions to the human, asked over Telegram:

  approve_job   -> "Apply to <job>?"            Yes / No / (silence = None)
  ask_question  -> "Question 2/3: ... reply"    text / (silence or /stop = None)
  confirm_submit-> "Submit these answers?"      Yes / No / (silence = None)
  notify        -> one-way status message

Silence is never treated as consent: every wait that times out returns None,
and the runner treats None as "do not proceed".
"""

from __future__ import annotations

import logging
from typing import Any

from naukri_agent.config import Settings
from naukri_agent.recommendations.employment import describe as describe_employment
from naukri_agent.notifications.telegram import TelegramChannel, TelegramError

logger = logging.getLogger(__name__)

ACCEPT_SUGGESTION = "."
STOP_WORD = "/stop"


_MAX_SKILLS_SHOWN = 12


def _years(value: float) -> str:
    return f"{value:g}"


def _skill_line(label: str, items: list[str]) -> str | None:
    if not items:
        return None
    shown = ", ".join(items[:_MAX_SKILLS_SHOWN])
    more = f" +{len(items) - _MAX_SKILLS_SHOWN} more" if len(items) > _MAX_SKILLS_SHOWN else ""
    return f"{label} ({len(items)}): {shown}{more}"


def format_job_card(job: dict) -> str:
    """What is sent to the phone before applying: the job, the score, the
    experience asked for, and which skills matched or are missing."""
    exp = job.get("experience_text")
    if not exp:
        lo, hi = job.get("experience_min"), job.get("experience_max")
        if lo is not None or hi is not None:
            exp = f"{_years(lo) if lo is not None else '?'} - {_years(hi) if hi is not None else '?'} years"
    mine = job.get("your_years")
    exp_line = f"Experience required: {exp or 'not stated'}" + (f"  (you have {_years(mine)} yrs)" if mine is not None else "")

    lines = [
        "Apply to this job?",
        "",
        job["title"],
        job["company"],
        f"Match score: {job['score']:.1f}",
        exp_line,
        "Resume: " + (job["resume_id"] if job.get("resume_id") else "none matched - the one already on your profile"),
        "Job type: " + describe_employment(job.get("employment_type_text")),
        "",
    ]
    skill_lines = [
        _skill_line("Matched - required", job.get("matched_required", [])),
        _skill_line("Matched - preferred", job.get("matched_preferred", [])),
        _skill_line("MISSING - required", job.get("missing_required", [])),
        _skill_line("Missing - preferred", job.get("missing_preferred", [])),
    ]
    lines += [x for x in skill_lines if x]
    if not any(skill_lines):
        lines.append("(no skill breakdown stored for this job)")
    lines += ["", job["url"]]
    return "\n".join(lines)


class TelegramInteraction:
    def __init__(self, channel: TelegramChannel, settings: Settings) -> None:
        self._ch = channel
        self._approve_s = settings.telegram_approval_timeout_minutes * 60
        self._answer_s = settings.telegram_answer_timeout_minutes * 60

    def send_approval(self, job: dict) -> None:
        """Put the job on the phone NOW; the answer is read later by wait_approval()."""
        self._ch.send_yes_no(format_job_card(job))

    def wait_approval(self) -> bool | None:
        return self._ch.wait_yes_no(self._approve_s)

    def approve_job(self, job: dict) -> bool | None:
        self.send_approval(job)
        return self.wait_approval()

    def ask_question(
        self, index: int, total: int, text: str, suggestion: str | None, options: list[str] | None = None
    ) -> str | None:
        if options:  # a choice question: tap one of the offered answers
            hint = f"\n\nFrom your profile I'd answer: {suggestion}" if suggestion in options else ""
            reply = self._ch.ask_choice(
                f"Question {index}/{total}:\n{text}{hint}\n\nTap an answer - it is submitted straight away, there is no second confirmation. (Send {STOP_WORD} to give up on this job.)",
                list(options),
                self._answer_s,
            )
            return None if reply is None or reply == STOP_WORD else reply
        hint = (
            f"\n\nFrom your profile I'd answer: {suggestion}\nReply {ACCEPT_SUGGESTION} to use that, or type your own answer."
            if suggestion
            else "\n\nI have no answer for this one - type yours."
        )
        reply = self._ch.ask_text(
            f"Question {index}/{total}:\n{text}{hint}\n\n(Send {STOP_WORD} to give up on this job.)",
            self._answer_s,
        )
        if reply is None or reply.lower() == STOP_WORD:
            return None
        if reply == ACCEPT_SUGGESTION and suggestion:
            return suggestion
        if reply == ACCEPT_SUGGESTION:
            # "." only means "use the suggestion", and there is none: never send a full stop to a recruiter as an answer.
            reply = self._ch.ask_text(
                f"There is no suggestion for this question, so \"{ACCEPT_SUGGESTION}\" cannot be used. Type your answer "
                f"(or send {STOP_WORD} to give up on this job).",
                self._answer_s,
            )
            if reply is None or reply.lower() == STOP_WORD or reply == ACCEPT_SUGGESTION:
                return None
        return reply

    def confirm_submit(self, job: dict, questions: list[str], answers: list[str]) -> bool | None:
        lines = [f"Ready to submit: {job['title']} - {job['company']}", ""]
        for q, a in zip(questions, answers):
            lines += [f"Q: {q}", f"A: {a}", ""]
        lines.append("Submit with these answers?")
        return self._ch.ask_yes_no("\n".join(lines), self._answer_s)

    def notify(self, text: str) -> None:
        try:
            self._ch.send(text)
        except TelegramError:
            logger.warning("telegram notify failed")


def build_telegram_interaction(settings: Settings, channel: Any = None) -> TelegramInteraction:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set in .env")
    return TelegramInteraction(
        channel or TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id), settings
    )
