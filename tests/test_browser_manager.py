import pytest

from naukri_agent.browser.browser_manager import BrowserManager
from naukri_agent.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(_env_file=None)
    defaults.update(overrides)
    return Settings(**defaults)


def test_close_without_launch_is_a_safe_noop():
    manager = BrowserManager(_settings())
    manager.close()  # must not raise
    assert manager.page is None


def test_close_is_idempotent():
    manager = BrowserManager(_settings())
    manager.close()
    manager.close()  # calling twice must not raise either


def test_stores_settings():
    settings = _settings()
    manager = BrowserManager(settings)
    assert manager.settings is settings


@pytest.mark.manual
def test_real_launch_and_close(tmp_path):
    """
    Requires real Playwright browser binaries installed locally
    (`playwright install chromium`) -- not available in this sandbox,
    and skipped by default everywhere. Run with `pytest -m manual`
    after installing browsers.
    """
    settings = _settings(browser_profile_dir=tmp_path / "profile")
    with BrowserManager(settings) as browser:
        assert browser.page is not None
