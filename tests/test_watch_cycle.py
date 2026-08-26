from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from ssb_seat_tracker.errors import WatchCycleError
from ssb_seat_tracker.main import CycleResult, run_watch_cycle
from ssb_seat_tracker.models import Section, Term, Watch

NOW = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)


def watch_with_previous_seats(seats: int) -> Watch:
    return Watch(
        crn="31752",
        term="2026 Fall",
        subject="CIS",
        course_number="4526",
        available=seats > 0,
        effective_seats=seats,
        last_checked_at=NOW,
    )


def cycle_ports(
    *,
    previous_seats: int,
    current_seats: int,
    section_factory: Callable[..., Section],
) -> tuple[Mock, Mock, Mock]:
    client = Mock()
    client.get_terms = AsyncMock(return_value=[Term(code="202636", description="2026 Fall")])
    client.search_sections = AsyncMock(
        return_value=[
            section_factory(
                seatsAvailable=current_seats,
                enrollment=40 - current_seats,
                openSection=current_seats > 0,
            )
        ]
    )
    notifier = Mock()
    notifier.send_opening = AsyncMock()
    repository = Mock()
    repository.list_enabled.return_value = [watch_with_previous_seats(previous_seats)]
    return client, notifier, repository


async def test_watch_cycle_notifies_on_closed_to_open(section_factory) -> None:
    client, notifier, repository = cycle_ports(
        previous_seats=0,
        current_seats=1,
        section_factory=section_factory,
    )

    result = await run_watch_cycle(
        client=client,
        notifier=notifier,
        repository=repository,
        checked_at=NOW,
    )

    assert result == CycleResult(watches=1, checked=1, notified=1, failed=0)
    notifier.send_opening.assert_awaited_once()
    saved_watch, saved_availability = repository.save_observation.call_args.args
    assert saved_watch.crn == "31752"
    assert saved_availability.available is True
    assert saved_availability.effective_seats == 1


@pytest.mark.parametrize(
    ("previous_seats", "current_seats"),
    [(0, 0), (1, 1), (1, 0)],
    ids=["closed-to-closed", "open-to-open", "open-to-closed"],
)
async def test_watch_cycle_does_not_notify_without_opening(
    previous_seats: int,
    current_seats: int,
    section_factory,
) -> None:
    client, notifier, repository = cycle_ports(
        previous_seats=previous_seats,
        current_seats=current_seats,
        section_factory=section_factory,
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
    section_factory,
) -> None:
    client, notifier, repository = cycle_ports(
        previous_seats=0,
        current_seats=1,
        section_factory=section_factory,
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
