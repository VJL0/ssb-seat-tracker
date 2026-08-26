from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from ssb_seat_tracker.models import EnrollmentInfo
from ssb_seat_tracker.notifier import NtfyNotifier

NOW = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=True)
async def test_notifier_sends_opening(
    respx_mock: respx.MockRouter,
    enrollment_factory: Callable[..., EnrollmentInfo],
) -> None:
    notification = respx_mock.post("https://ntfy.sh/private-topic").mock(
        return_value=httpx.Response(204)
    )
    info = enrollment_factory()

    async with httpx.AsyncClient() as http_client:
        notifier = NtfyNotifier(topic="private-topic", client=http_client)
        await notifier.send_opening("53150", info, checked_at=NOW)

    request = notification.calls.last.request
    assert request.headers["Title"] == "CRN 53150 seat opening"
    assert request.content.decode() == (
        "🚨 CRN 53150 has an opening\n\n"
        "Seats available: 1\n"
        "Enrollment: 44/45\n"
        "Waitlist actual: 0\n\n"
        "Checked: Aug 24, 2026 9:00 AM ET"
    )
