"""
Digest delivery.

Stage A: file / console senders only — NO real SMTP. The recommendation
engine depends on the EmailSender abstraction, never on smtplib.
"""
