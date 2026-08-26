from collections.abc import Callable

import pytest

from ssb_seat_tracker.models import EnrollmentInfo

type EnrollmentFactory = Callable[..., EnrollmentInfo]


@pytest.fixture
def enrollment_factory() -> EnrollmentFactory:
    def make(**overrides: int) -> EnrollmentInfo:
        values = {
            "enrollment": 44,
            "capacity": 45,
            "seats_available": 1,
            "waitlist_capacity": 0,
            "waitlist_count": 0,
            "waitlist_available": 0,
        }
        return EnrollmentInfo(**(values | overrides))

    return make
