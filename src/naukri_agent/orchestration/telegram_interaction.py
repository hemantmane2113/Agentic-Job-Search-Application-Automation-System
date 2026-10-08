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
from naukri_agent.notifications.telegram import TelegramChannel, TelegramError

logger = logging.getLogger(__name__)

ACCEPT_SUGGESTION = "."
STOP_WORD = "/stop"


class TelegramInteraction:
    def __init__(self, channel: TelegramChannel, settings: Settings) -> None:
        self._ch = channel
        self._approve_s = settings.telegram_approval_timeout_minutes * 60
        self._answer_s = settings.telegram_answer_timeout_minutes * 60

    def approve_job(self, job: dict) -> bool | None:
        return self._ch.ask_yes_no(
            f"Apply to this job?\n\n{job['title']}\n{job['company']}\nMatch score: {job['score']:.1f}\n{job['url']}",
            self._approve_s,
        )

    def ask_question(self, index: int, total: int, text: str, suggestion: str | None) -> str | None:
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
