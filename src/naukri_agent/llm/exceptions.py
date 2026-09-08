"""
Exception hierarchy for the LLM provider layer. JobParser (and any
future caller) catches LLMError and its subclasses — never a
provider-specific SDK exception type, since that would leak provider
identity into code that's supposed to depend only on LLMProvider.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for all LLM provider/config errors."""


class LLMConfigError(LLMError):
    """A provider is misconfigured — e.g. a required API key is missing."""


class LLMTimeoutError(LLMError):
    """The provider took too long to respond."""


class LLMRequestError(LLMError):
    """
    The provider request failed for any other reason: network error,
    authentication failure, malformed response shape, or a
    provider-side API error.
    """
