"""
Company-site job researcher: a tool-using AI agent, kept deliberately small and read-only.

For a job whose Naukri page says "Apply on company site" (the button has no link behind it, and
the agent must never click), the agent searches for the company's own careers page. It may only use
the four tools in tools.py, for a bounded number of steps, and everything it reads from the web is
untrusted text. It never applies, logs in, or sees your profile or resume.

This package is NOT imported by the daily pipeline, discovery or the scheduler (a test enforces it):
the daily run stays unable to do anything agentic.
"""
