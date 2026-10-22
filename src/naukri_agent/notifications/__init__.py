"""
Digest delivery.

FileEmailSender / ConsoleEmailSender (default) and SmtpEmailSender
(opt-in via EMAIL_SENDER=smtp + all four SMTP_* settings) — see
email.py. The recommendation engine depends on the EmailSender
abstraction, never on smtplib directly.
"""
