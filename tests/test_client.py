from collections.abc import Callable

import httpx
import pytest
import respx

from ssb_seat_tracker.client import BASE_URL, SSBClient
from ssb_seat_tracker.errors import SSBError

JSON_HEADERS = {"content-type": "application/json"}


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=True)
async def test_client_searches_course_with_required_ssb_flow(
    respx_mock: respx.MockRouter,
    section_payload_factory: Callable[..., dict[str, object]],
) -> None:
    select_term = respx_mock.post(f"{BASE_URL}/term/search").mock(
        return_value=httpx.Response(
            200,
            json={"fwdURL": "/StudentRegistrationSsb/ssb/classSearch/classSearch"},
            headers=JSON_HEADERS,
        )
    )
    search = respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "totalCount": 1,
                "data": [section_payload_factory()],
            },
            headers=JSON_HEADERS,
        )
    )

    async with SSBClient() as client:
        sections = await client.search_sections(term="202636", subject="cis", course_number="4526")

    assert len(sections) == 1
    assert sections[0].course_reference_number == "31752"
    assert sections[0].effective_seats == 1
    assert select_term.calls.last.request.content == b"term=202636"
    assert dict(search.calls.last.request.url.params) == {
        "txt_term": "202636",
        "txt_subject": "CIS",
        "txt_courseNumber": "4526",
        "pageMaxSize": "50",
    }


@pytest.mark.parametrize(
    "search_response",
    [
        pytest.param(httpx.Response(500), id="http-error"),
        pytest.param(
            httpx.Response(
                200,
                json={"success": False, "data": []},
                headers=JSON_HEADERS,
            ),
            id="unsuccessful-payload",
        ),
    ],
)
@pytest.mark.respx(assert_all_mocked=True, assert_all_called=True)
async def test_client_handles_ssb_failure(
    respx_mock: respx.MockRouter,
    search_response: httpx.Response,
) -> None:
    respx_mock.post(f"{BASE_URL}/term/search").mock(
        return_value=httpx.Response(
            200,
            json={"fwdURL": "/classSearch"},
            headers=JSON_HEADERS,
        )
    )
    respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(return_value=search_response)

    async with SSBClient() as client:
        with pytest.raises(SSBError):
            await client.search_sections(term="202636", subject="CIS", course_number="4526")


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=True)
async def test_client_handles_empty_results(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE_URL}/term/search").mock(
        return_value=httpx.Response(
            200,
            json={"fwdURL": "/classSearch"},
            headers=JSON_HEADERS,
        )
    )
    respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "totalCount": 0, "data": None},
            headers=JSON_HEADERS,
        )
    )

    async with SSBClient() as client:
        sections = await client.search_sections(term="202636", subject="CIS", course_number="4526")

    assert sections == []
