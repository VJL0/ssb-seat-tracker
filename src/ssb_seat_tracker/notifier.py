"""Notification delivery, independent of Banner session behavior."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from .models import Section

EASTERN = ZoneInfo("America/New_York")


class NotificationError(RuntimeError):
    """A notification failed without exposing delivery secrets."""


class Notifier(Protocol):
    async def send_opening(self, section: Section, *, checked_at: datetime) -> None: ...

    async def close(self) -> None: ...


def opening_message(section: Section, *, checked_at: datetime) -> str:
    local_time = checked_at.astimezone(EASTERN)
    checked = f"{local_time:%b %d, %Y} {local_time:%I:%M %p}".replace(" 0", " ") + " ET"
    return (
        f"🚨 {section.subject} {section.course_number} has an opening\n\n"
        f"CRN: {section.course_reference_number}\n"
        f"Effective seats: {section.effective_seats}\n"
        f"Banner seats remaining: {section.seats_available}\n"
        f"Waitlist actual: {section.wait_count}\n\n"
        f"Checked: {checked}"
    )


class ConsoleNotifier:
    async def send_opening(self, section: Section, *, checked_at: datetime) -> None:
        print(opening_message(section, checked_at=checked_at), flush=True)

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
        if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
            raise ValueError(
                "ntfy topic must contain only letters, numbers, dashes, or underscores"
            )
        self._topic = topic
        self._server_url = server_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=10, write=10, pool=5)
        )

    async def send_opening(self, section: Section, *, checked_at: datetime) -> None:
        try:
            response = await self._client.post(
                f"{self._server_url}/{self._topic}",
                content=opening_message(section, checked_at=checked_at).encode("utf-8"),
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Title": f"{section.subject} {section.course_number} seat opening",
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
