## What and why

<!-- What does this change, and what problem does it solve? Link the issue. -->

## How it was tested

- [ ] `pytest` passes (how many: ___)
- [ ] I added or updated a test (for a bug seen on the real site, one built from the real shape that failed)
- [ ] Anything that depends on Naukri's real page and has not been run against it is marked UNVERIFIED

## Checklist

- [ ] A person still approves every application; nothing new can apply on its own
- [ ] No CAPTCHA, MFA, rate-limit or anti-bot bypass
- [ ] Selectors are only in `browser/selectors.py`
- [ ] No secrets, personal data, resumes, databases, logs or captured pages are included
- [ ] Docs updated if behaviour changed
