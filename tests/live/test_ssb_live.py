import os

import pytest

from ssb_seat_tracker.client import SSBClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("SSB_LIVE_TEST") != "1",
        reason="set SSB_LIVE_TEST=1 to call Temple's public SSB service",
    ),
]


async def test_live_ssb_course_search() -> None:
    async with SSBClient() as client:
        sections = await client.search_sections(term="202636", subject="CIS", course_number="4526")

    assert sections
    for section in sections:
        assert section.subject == "CIS"
        assert section.course_number == "4526"
        assert section.course_reference_number
