from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from ssb_seat_tracker.models import Section
from ssb_seat_tracker.notifier import NotificationError, NtfyNotifier


async def test_ntfy_publishes_opening_with_priority_and_tags(
    respx_mock: respx.MockRouter, section_factory: Callable[..., Section]
) -> None:
    route = respx_mock.post("https://ntfy.sh/private-topic").mock(
        return_value=httpx.Response(200, json={"id": "message-id"})
    )
    async with httpx.AsyncClient() as http_client:
        notifier = NtfyNotifier(topic="private-topic", client=http_client)
        await notifier.send_opening(
            section_factory(),
            checked_at=datetime(2026, 8, 24, 13, 17, tzinfo=UTC),
        )

    request = route.calls.last.request
    assert request.headers["Title"] == "CIS 4526 seat opening"
    assert request.headers["Priority"] == "high"
    assert request.headers["Tags"] == "rotating_light"
    assert "Effective seats: 1" in request.content.decode()


async def test_ntfy_failure_is_safe_and_retryable(
    respx_mock: respx.MockRouter, section_factory: Callable[..., Section]
) -> None:
    respx_mock.post("https://ntfy.sh/private-topic").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http_client:
        notifier = NtfyNotifier(topic="private-topic", client=http_client)
        with pytest.raises(NotificationError, match="ntfy notification failed"):
            await notifier.send_opening(
                section_factory(),
                checked_at=datetime(2026, 8, 24, 13, 17, tzinfo=UTC),
            )
