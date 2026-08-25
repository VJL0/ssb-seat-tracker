import os

import pytest

from ssb_seat_tracker.client import SSBClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("SSB_LIVE_TEST") != "1",
        reason="set SSB_LIVE_TEST=1 to call Temple's public SSB service",
    ),
]


async def test_live_public_term_discovery() -> None:
    async with SSBClient() as client:
        terms = await client.get_terms()

    assert terms
    assert all(term.code and term.description for term in terms)
