"""Application exception hierarchy for the seat tracker."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import CycleResult

__all__ = (
    "ConfigurationError",
    "NotificationError",
    "RateLimitError",
    "SSBError",
    "SeatTrackerError",
    "WatchCycleError",
)


class SeatTrackerError(Exception):
    """Base class for expected operational failures."""


class ConfigurationError(SeatTrackerError):
    """Required application configuration is missing or invalid."""


class SSBError(SeatTrackerError):
    """Temple SSB could not provide a trustworthy result."""


class _SessionExpiredError(SSBError):
    """The anonymous Banner session is stale."""


class RateLimitError(SSBError):
    """Temple requested that the client reduce request pressure."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Temple SSB rate limited the request")
        self.retry_after = retry_after


class NotificationError(SeatTrackerError):
    """A notification failed without exposing delivery secrets."""


class WatchCycleError(SeatTrackerError):
    """One or more watches failed and must be retried."""

    def __init__(self, result: CycleResult) -> None:
        super().__init__(f"{result.failed} of {result.watches} watches failed")
        self.result = result
