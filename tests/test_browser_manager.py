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


def test_close_swallows_dead_connection_errors_and_stays_idempotent():
    """
    A dead Playwright driver connection (browser window closed by the
    user mid-CAPTCHA) makes context.close()/playwright.stop() raise
    "Connection closed while reading from the driver". close() must
    swallow that, clear its state, and not call the driver again on a
    second invocation.
    """
    manager = BrowserManager(_settings())

    class DeadDriver:
        def __init__(self) -> None:
            self.close_calls = 0
            self.stop_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("Connection closed while reading from the driver")

        def stop(self) -> None:
            self.stop_calls += 1
            raise RuntimeError("Connection closed while reading from the driver")

    driver = DeadDriver()
    manager._context = driver
    manager._playwright = driver
    manager.context = driver
    manager.page = object()

    manager.close()  # must NOT raise despite the dead driver

    assert manager._context is None
    assert manager._playwright is None
    assert manager.context is None
    assert manager.page is None
    assert driver.close_calls == 1
    assert driver.stop_calls == 1

    manager.close()  # idempotent: no raise, driver not touched again
    assert driver.close_calls == 1
    assert driver.stop_calls == 1


def test_exit_does_not_mask_original_exception_when_cleanup_fails(monkeypatch):
    """
    close() runs from __exit__ while another exception is propagating
    (a CAPTCHA the user abandoned by closing the window). A secondary
    "Connection closed" from cleanup must NOT replace that original
    exception.
    """
    manager = BrowserManager(_settings())

    class DeadDriver:
        def close(self) -> None:
            raise RuntimeError("Connection closed while reading from the driver")

        def stop(self) -> None:
            raise RuntimeError("secondary cleanup boom")

    def fake_launch() -> None:
        d = DeadDriver()
        manager._context = d
        manager._playwright = d
        manager.context = d
        manager.page = object()

    monkeypatch.setattr(manager, "launch", fake_launch)

    class CaptchaAbandoned(Exception):
        pass

    with pytest.raises(CaptchaAbandoned):
        with manager:
            raise CaptchaAbandoned("user closed the browser during manual CAPTCHA")

    # cleanup still ran (state cleared) despite raising internally
    assert manager._context is None
    assert manager._playwright is None
    assert manager.page is None


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
