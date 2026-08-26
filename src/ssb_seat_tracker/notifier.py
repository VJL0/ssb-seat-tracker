"""Notification delivery for CRN seat openings."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from .errors import ConfigurationError, NotificationError
from .models import EnrollmentInfo

EASTERN = ZoneInfo("America/New_York")
NTFY_TOPIC_PATTERN = re.compile(r"[-_A-Za-z0-9]{1,64}")


class Notifier(Protocol):
    async def send_opening(
        self, crn: str, info: EnrollmentInfo, *, checked_at: datetime
    ) -> None: ...

    async def close(self) -> None: ...


def opening_message(crn: str, info: EnrollmentInfo, *, checked_at: datetime) -> str:
    local_time = checked_at.astimezone(EASTERN)
    checked = f"{local_time:%b %d, %Y} {local_time:%I:%M %p}".replace(" 0", " ") + " ET"
    return (
        f"🚨 CRN {crn} has an opening\n\n"
        f"Seats available: {info.seats_available}\n"
        f"Enrollment: {info.enrollment}/{info.capacity}\n"
        f"Waitlist actual: {info.waitlist_count}\n\n"
        f"Checked: {checked}"
    )


class ConsoleNotifier:
    async def send_opening(self, crn: str, info: EnrollmentInfo, *, checked_at: datetime) -> None:
        print(opening_message(crn, info, checked_at=checked_at), flush=True)

    async def close(self) -> None:
        return None


class NtfyNotifier:
    def __init__(
        self,
        *,
        topic: str,
        server_url: str = "https://ntfy.sh",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not NTFY_TOPIC_PATTERN.fullmatch(topic):
            raise ConfigurationError(
                "ntfy topic must contain only letters, numbers, dashes, or underscores"
            )
        self._topic = topic
        self._server_url = server_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def send_opening(self, crn: str, info: EnrollmentInfo, *, checked_at: datetime) -> None:
        try:
            response = await self._client.post(
                f"{self._server_url}/{self._topic}",
                content=opening_message(crn, info, checked_at=checked_at),
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Title": f"CRN {crn} seat opening",
                    "Priority": "high",
                    "Tags": "rotating_light",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotificationError("ntfy notification failed") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
