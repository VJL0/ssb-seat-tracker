import os

import pytest

from ssb_seat_tracker.client import TERM, SsbClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("SSB_LIVE_TEST") != "1",
        reason="set SSB_LIVE_TEST=1 to call Temple's public SSB service",
    ),
]


async def test_live_ssb_enrollment_lookup() -> None:
    async with SsbClient() as client:
        info = await client.get_enrollment(term=TERM, crn="53150")

    assert isinstance(info.enrollment, int)
    assert isinstance(info.capacity, int)
    assert isinstance(info.seats_available, int)
