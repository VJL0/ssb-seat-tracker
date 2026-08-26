import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from ssb_seat_tracker.client import (
    BASE_URL,
    ENROLLMENT_PATH,
    MAX_CONCURRENCY,
    TERM,
    SsbClient,
    fetch_all,
)
from ssb_seat_tracker.errors import SSBError
from ssb_seat_tracker.models import EnrollmentInfo

HTML_HEADERS = {"content-type": "text/html;charset=UTF-8"}


def enrollment_html(*, actual: int = 44, maximum: int = 45, seats: int = 1) -> str:
    return f"""
    <section aria-labelledby="enrollmentInfo">
      <span class="status-bold">Enrollment Actual:</span> <span>{actual}</span><br/>
      <span class="status-bold">Enrollment Maximum:</span> <span>{maximum}</span><br/>
      <span class="status-bold">Enrollment Seats Available:</span> <span>{seats}</span><br/>
      <hr/>
      <span class="status-bold">Waitlist Capacity:</span> <span>0</span><br/>
      <span class="status-bold">Waitlist Actual:</span> <span>0</span><br/>
      <span class="status-bold">Waitlist Seats Available:</span> <span>0</span><br/>
    </section>
    """


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=True)
async def test_client_posts_identifiers_and_parses_six_counts(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(f"{BASE_URL}{ENROLLMENT_PATH}").mock(
        return_value=httpx.Response(
            200,
            text=enrollment_html(actual=47, maximum=45, seats=-2),
            headers=HTML_HEADERS,
        )
    )

    async with SsbClient() as client:
        info = await client.get_enrollment(term=TERM, crn="53150")

    assert info == EnrollmentInfo(47, 45, -2, 0, 0, 0)
    assert info.is_open is False
    assert route.calls.last.request.content == b"term=202636&courseReferenceNumber=53150"


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=True)
async def test_client_retries_only_transient_failures(
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    route = respx_mock.post(f"{BASE_URL}{ENROLLMENT_PATH}").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(429),
            httpx.Response(200, text=enrollment_html(), headers=HTML_HEADERS),
        ]
    )

    async with SsbClient() as client:
        info = await client.get_enrollment(term=TERM, crn="53150")

    assert info.is_open is True
    assert route.call_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(404), id="non-retryable-http-error"),
        pytest.param(
            httpx.Response(200, text="<section></section>", headers=HTML_HEADERS),
            id="malformed-html",
        ),
    ],
)
@pytest.mark.respx(assert_all_mocked=True, assert_all_called=True)
async def test_client_fails_without_retrying_permanent_errors(
    respx_mock: respx.MockRouter,
    response: httpx.Response,
) -> None:
    route = respx_mock.post(f"{BASE_URL}{ENROLLMENT_PATH}").mock(return_value=response)

    async with SsbClient() as client:
        with pytest.raises(SSBError):
            await client.get_enrollment(term=TERM, crn="53150")

    assert route.call_count == 1


async def test_fetch_all_limits_concurrency_to_five(enrollment_factory) -> None:
    active = maximum_active = 0
    release = asyncio.Event()

    class FakeClient:
        async def get_enrollment(self, *, term: str, crn: str) -> EnrollmentInfo:
            nonlocal active, maximum_active
            assert term == TERM
            active += 1
            maximum_active = max(maximum_active, active)
            if maximum_active == MAX_CONCURRENCY:
                release.set()
            await release.wait()
            active -= 1
            return enrollment_factory()

    crns = [str(53000 + index) for index in range(12)]
    results = await fetch_all(FakeClient(), term=TERM, crns=crns)  # type: ignore[arg-type]

    assert list(results) == crns
    assert maximum_active == MAX_CONCURRENCY
