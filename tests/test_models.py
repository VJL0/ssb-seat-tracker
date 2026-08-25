from collections.abc import Callable
from datetime import datetime

import pytest
from pydantic import ValidationError

from ssb_seat_tracker.models import Availability, Section, Watch


@pytest.mark.parametrize(
    ("remaining", "wait_count", "expected"),
    [
        pytest.param(0, 0, 0, id="empty"),
        pytest.param(1, 0, 1, id="one-seat-no-waitlist"),
        pytest.param(1, 1, 0, id="one-seat-one-waitlisted"),
        pytest.param(2, 1, 1, id="two-seats-one-waitlisted"),
        pytest.param(1, 4, 0, id="never-negative"),
    ],
)
def test_effective_seats_follow_temple_waitlist_rule(
    section_factory: Callable[..., Section],
    remaining: int,
    wait_count: int,
    expected: int,
) -> None:
    section = section_factory(seatsAvailable=remaining, waitCount=wait_count, openSection=True)
    assert section.effective_seats == expected


def test_closed_banner_section_is_not_available_even_with_seats(
    section_factory: Callable[..., Section],
) -> None:
    section = section_factory(seatsAvailable=3, waitCount=0, openSection=False)
    assert section.effective_seats == 3
    assert section.is_available is False


def test_open_section_requires_effective_seat(
    section_factory: Callable[..., Section],
) -> None:
    assert section_factory(seatsAvailable=2, waitCount=1, openSection=True).is_available is True
    assert section_factory(seatsAvailable=1, waitCount=1, openSection=True).is_available is False


def test_required_banner_fields_are_validated() -> None:
    with pytest.raises(ValidationError):
        Section.model_validate({"courseReferenceNumber": "31752"})


def test_negative_external_counts_are_rejected(section_payload_factory) -> None:
    with pytest.raises(ValidationError):
        Section.model_validate(section_payload_factory(waitCount=-1))


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(
            lambda: Availability(
                crn="31752",
                available=True,
                effectiveSeats=1,
                checkedAt=datetime(2026, 8, 24, 13, 0),
            ),
            id="availability",
        ),
        pytest.param(
            lambda: Watch(
                crn="31752",
                term="2026 Fall",
                subject="CIS",
                course_number="4526",
                last_checked_at=datetime(2026, 8, 24, 13, 0),
            ),
            id="watch",
        ),
    ],
)
def test_persisted_timestamps_must_be_timezone_aware(model) -> None:
    with pytest.raises(ValidationError):
        model()
