"""Phase 15: email attachment support (notifications/email.py).

Covers SmtpEmailSender's multipart/mixed + attachment parts, the
"never log str(exc)" rule extended to attachment file I/O, and the
filenames-only convention for FileEmailSender/ConsoleEmailSender --
plus a regression guard proving the no-attachment digest path is
completely unaffected.
"""

from __future__ import annotations

import email
from pathlib import Path

import pytest

from naukri_agent.notifications.email import (
    ConsoleEmailSender,
    EmailMessage,
    FileEmailSender,
    SmtpEmailSender,
)
from naukri_agent.notifications.exceptions import EmailSendError


class _FakeSmtp:
    def __init__(self, host, port, timeout=30):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, username, password):
        pass

    def send_message(self, mime_msg):
        self.sent_message = mime_msg
        _CAPTURED["message"] = mime_msg


_CAPTURED: dict = {}


def _sender() -> SmtpEmailSender:
    return SmtpEmailSender(host="smtp.gmail.com", port=587, username="me@gmail.com",
                            password="pw", to_addr="me@gmail.com")


def test_smtp_sender_attaches_file_as_multipart_mixed(tmp_path, monkeypatch):
    _CAPTURED.clear()
    monkeypatch.setattr("naukri_agent.notifications.email.smtplib.SMTP", _FakeSmtp)

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake resume bytes")

    result = _sender().send(
        EmailMessage(subject="Application", text_body="Please find my resume attached.",
                     attachments=[str(resume)])
    )

    assert result.sender == "smtp" and result.status == "sent"
    mime_msg = _CAPTURED["message"]
    assert mime_msg.get_content_type() == "multipart/mixed"

    parts = mime_msg.get_payload()
    assert len(parts) == 2  # alternative body + one attachment
    alt_part, attachment_part = parts
    assert alt_part.get_content_type() == "multipart/alternative"
    assert attachment_part.get_filename() == "resume.pdf"
    assert attachment_part.get_content_disposition() == "attachment"
    decoded = attachment_part.get_payload(decode=True)
    assert decoded == b"%PDF-1.4 fake resume bytes"


def test_smtp_sender_missing_attachment_raises_type_only(tmp_path, monkeypatch):
    monkeypatch.setattr("naukri_agent.notifications.email.smtplib.SMTP", _FakeSmtp)

    missing = tmp_path / "does_not_exist.pdf"
    with pytest.raises(EmailSendError) as excinfo:
        _sender().send(
            EmailMessage(subject="Application", text_body="body", attachments=[str(missing)])
        )

    message = str(excinfo.value)
    assert "FileNotFoundError" in message
    assert str(missing) not in message


def test_file_email_sender_records_filename_only_never_copies_content(tmp_path):
    attachment = tmp_path / "secret_resume.docx"
    attachment.write_bytes(b"binary resume content")

    out_dir = tmp_path / "out"
    result = FileEmailSender(out_dir).send(
        EmailMessage(subject="Application", text_body="body", attachments=[str(attachment)])
    )

    written = Path(result.path).read_text(encoding="utf-8")
    assert "Attachments: secret_resume.docx" in written
    assert "binary resume content" not in written
    assert str(attachment) not in written
    # No copy of the attachment itself was written alongside the digest.
    assert not any(p.name == "secret_resume.docx" for p in out_dir.iterdir())


def test_console_email_sender_prints_filename_only(tmp_path, capsys):
    attachment = tmp_path / "resume.pdf"
    attachment.write_bytes(b"pdf bytes")

    ConsoleEmailSender().send(
        EmailMessage(subject="Application", text_body="body", attachments=[str(attachment)])
    )

    out = capsys.readouterr().out
    assert "Attachments: resume.pdf" in out
    assert str(attachment) not in out


def test_smtp_sender_without_attachments_is_unaffected_regression(monkeypatch):
    _CAPTURED.clear()
    monkeypatch.setattr("naukri_agent.notifications.email.smtplib.SMTP", _FakeSmtp)

    result = _sender().send(EmailMessage(subject="Digest", text_body="body"))

    assert result.sender == "smtp" and result.status == "sent"
    mime_msg = _CAPTURED["message"]
    # No attachments => outer is still multipart/mixed containing only
    # the alternative part (structural change is internal; message
    # still parses and carries the exact same text).
    assert mime_msg.get_content_type() == "multipart/mixed"
    parts = mime_msg.get_payload()
    assert len(parts) == 1
    alt_part = parts[0]
    assert alt_part.get_content_type() == "multipart/alternative"
    body_text = alt_part.get_payload()[0].get_payload(decode=True).decode("utf-8")
    assert body_text == "body"


def test_file_email_sender_without_attachments_has_no_attachments_line(tmp_path):
    result = FileEmailSender(tmp_path).send(EmailMessage(subject="Digest", text_body="body"))
    written = Path(result.path).read_text(encoding="utf-8")
    assert "Attachments:" not in written


def test_email_message_round_trips_via_stdlib_parser(tmp_path, monkeypatch):
    _CAPTURED.clear()
    monkeypatch.setattr("naukri_agent.notifications.email.smtplib.SMTP", _FakeSmtp)

    resume = tmp_path / "r.txt"
    resume.write_text("resume text content", encoding="utf-8")

    _sender().send(EmailMessage(subject="Application", text_body="hello",
                                 attachments=[str(resume)]))

    raw = _CAPTURED["message"].as_bytes()
    parsed = email.message_from_bytes(raw)
    assert parsed.is_multipart()
    attachment_payloads = [
        part.get_payload(decode=True) for part in parsed.walk()
        if part.get_content_disposition() == "attachment"
    ]
    assert attachment_payloads == [b"resume text content"]
