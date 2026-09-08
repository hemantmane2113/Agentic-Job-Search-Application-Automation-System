# Manual / real-integration tests

Everything in this directory requires:

- A real Naukri account (`NAUKRI_EMAIL` / `NAUKRI_PASSWORD` set)
- Real network access to naukri.com
- Playwright's browser binaries installed locally (`playwright install chromium`)
- A human present to solve a CAPTCHA/MFA challenge if one appears

None of this is available in the sandbox this project was built in,
and none of it should ever run in CI. Every test here is marked
`@pytest.mark.manual`, which `pyproject.toml` excludes by default
(`addopts = "-m 'not manual'"`).

To run these yourself:

```bash
playwright install chromium
cp .env.example .env   # then fill in NAUKRI_EMAIL / NAUKRI_PASSWORD
pytest -m manual
```

These are the only tests that actually exercise `browser/selectors.py`
against the real site. Everything in `tests/test_browser_*.py`
instead validates the *orchestration logic* (login-state
classification, listing parsing, the read-only guarantees) against
fake Page objects, and needs no real access at all.
