"""
A one-line description of an exception that is safe to write to the local log file.

The project rule is that errors report only the exception TYPE (messages can carry page text or typed values).
That is right for the database, the digest email and Telegram, and it stays that way. But a bare "Error" in the log
(a Playwright launch failure on 2026-10-21) left no way to tell what went wrong. So the LOG FILE alone also gets the
full class name (module and name) and the first line of the message, cut short, with every secret value held in the
settings (passwords, keys, tokens, the account e-mail) replaced by "***". The log file is git-ignored and local.
"""

from __future__ import annotations

from typing import Any

_SECRET_WORDS = ("password", "api_key", "token", "secret", "email")
_MIN_SECRET_LEN = 4


def _secret_values(settings: Any) -> list[str]:
    values: list[str] = []
    fields = getattr(type(settings), "model_fields", None) or {}
    for name in fields:
        if not any(word in name.lower() for word in _SECRET_WORDS):
            continue
        value = getattr(settings, name, None)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def safe_error_text(exc: BaseException, settings: Any = None, limit: int = 200) -> str:
    cls = type(exc)
    name = f"{cls.__module__}.{cls.__qualname__}"
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    text = lines[0] if lines else ""
    for secret in _secret_values(settings) if settings is not None else []:
        text = text.replace(secret, "***")
    return f"{name}: {text[:limit]}" if text else name
