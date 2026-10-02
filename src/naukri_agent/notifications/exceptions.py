"""Email delivery exceptions (mirrors llm/exceptions.py)."""

from __future__ import annotations


class EmailError(Exception):
    """Base class for digest-delivery errors."""


class EmailSendError(EmailError):
    """A sender failed to deliver / write the digest."""


class EmailConfigError(EmailSendError):
    """email_sender=smtp was selected but required SMTP settings are
    missing. A subclass of EmailSendError so it is caught by the same
    handler that already converts a send failure into a clean FAILED
    DailyRun -- a misconfigured schedule fails the run visibly instead
    of silently writing a file nobody is watching."""
