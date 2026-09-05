"""Allowlisted WD API diagnostics without raw exception data."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass


@dataclass(frozen=True)
class APIFailure:
    """A safe API failure containing only fixed vocabulary."""

    error_type: str
    detail: str

    @property
    def summary(self) -> str:
        """Return a safe value for entity attributes and logs."""
        return f"{self.error_type}; stage=snapshot; {self.detail}"


def describe_api_error(error: BaseException) -> APIFailure:
    """Classify an error without copying messages, headers, or response bodies."""
    status = getattr(error, "status", None)
    if status not in (401, 403):
        status = next(
            (
                value
                for value in getattr(error, "args", ())
                if type(value) is int and value in (401, 403)
            ),
            None,
        )
    if status in (401, 403):
        return APIFailure("authentication_failed", f"http_status={status}")
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return APIFailure("timeout", "request_timeout")
    if isinstance(error, (ConnectionError, OSError)):
        return APIFailure("connection_failed", "transport_error")
    if isinstance(error, (TypeError, ValueError, KeyError)):
        return APIFailure("invalid_response", "response_validation_failed")
    return APIFailure("api_error", "details_withheld")


class SafeAPILogError(Exception):
    """Sanitized exception used instead of a raw API traceback."""


def log_api_failure(logger: logging.Logger, failure: APIFailure) -> None:
    """Log a sanitized exception and traceback exactly when requested by caller."""
    safe = SafeAPILogError(failure.summary)
    safe.__suppress_context__ = True
    try:
        raise safe
    except SafeAPILogError as sanitized:
        sanitized.__context__ = None
        logger.warning(
            "WD API snapshot failed: %s; cached values retained",
            failure.summary,
            exc_info=True,
        )
