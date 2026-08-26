from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from ssb_seat_tracker.errors import WatchCycleError
from ssb_seat_tracker.main import CycleResult, run_watch_cycle
from ssb_seat_tracker.models import TrackedSection

NOW = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)


def cycle_ports(*, previous_seats: int | None, current_seats: int, enrollment_factory):
    client = Mock()
    client.get_enrollment = AsyncMock(
        return_value=enrollment_factory(seats_available=current_seats)
    )
    notifier = Mock()
    notifier.send_opening = AsyncMock()
    repository = Mock()
    repository.list_enabled.return_value = [
        TrackedSection(crn="53150", seats_available=previous_seats, updated_at=NOW)
    ]
    return client, notifier, repository


async def test_watch_cycle_notifies_only_on_closed_to_open(enrollment_factory) -> None:
    client, notifier, repository = cycle_ports(
        previous_seats=0,
        current_seats=1,
        enrollment_factory=enrollment_factory,
    )

    result = await run_watch_cycle(
        client=client,
        notifier=notifier,
        repository=repository,
        checked_at=NOW,
    )

    assert result == CycleResult(watches=1, checked=1, notified=1, failed=0)
    notifier.send_opening.assert_awaited_once_with("53150", enrollment_factory(), checked_at=NOW)
    repository.save_observation.assert_called_once_with(
        repository.list_enabled.return_value[0], seats_available=1, updated_at=NOW
    )


@pytest.mark.parametrize(
    ("previous_seats", "current_seats"),
    [(None, 1), (0, 0), (1, 1), (1, 2), (1, 0), (-2, -1)],
    ids=["first-open", "full", "unchanged-open", "more-seats", "closed", "still-overfull"],
)
async def test_watch_cycle_does_not_notify_without_transition(
    previous_seats: int | None,
    current_seats: int,
    enrollment_factory,
) -> None:
    client, notifier, repository = cycle_ports(
        previous_seats=previous_seats,
        current_seats=current_seats,
        enrollment_factory=enrollment_factory,
    )

    result = await run_watch_cycle(
        client=client,
        notifier=notifier,
        repository=repository,
        checked_at=NOW,
    )

    assert result == CycleResult(watches=1, checked=1, notified=0, failed=0)
    notifier.send_opening.assert_not_awaited()
    repository.save_observation.assert_called_once()


async def test_watch_cycle_does_not_commit_state_when_notification_fails(
    enrollment_factory,
) -> None:
    client, notifier, repository = cycle_ports(
        previous_seats=0,
        current_seats=1,
        enrollment_factory=enrollment_factory,
    )
    notifier.send_opening.side_effect = RuntimeError("delivery failed")

    with pytest.raises(WatchCycleError) as raised:
        await run_watch_cycle(
            client=client,
            notifier=notifier,
            repository=repository,
            checked_at=NOW,
        )

    assert raised.value.result == CycleResult(watches=1, checked=0, notified=0, failed=1)
    repository.save_observation.assert_not_called()
