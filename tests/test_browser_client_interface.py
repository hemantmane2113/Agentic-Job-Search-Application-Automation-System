"""
JobBoardClient: the generic multi-platform contract. NaukriClient is
the only implementation today, but any future platform client must
conform to the same ABC — these tests are a regression guard for that,
not behavioral coverage of NaukriClient itself (see
test_browser_naukri_client.py for that).
"""

from __future__ import annotations

import pytest

from naukri_agent.browser.client_interface import JobBoardClient
from naukri_agent.browser.naukri_client import NaukriClient
from naukri_agent.config import Settings

from .browser_fakes import FakePage


def _settings() -> Settings:
    return Settings(_env_file=None, naukri_email="a@b.com", naukri_password="pw")


def test_job_board_client_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        JobBoardClient(FakePage(), _settings())


def test_naukri_client_is_a_job_board_client_subclass():
    assert issubclass(NaukriClient, JobBoardClient)


def test_naukri_client_instantiates_cleanly():
    client = NaukriClient(FakePage(), _settings())
    assert isinstance(client, JobBoardClient)
