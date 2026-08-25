from collections.abc import Callable

import httpx
import pytest
import respx

from ssb_seat_tracker.client import (
    BASE_URL,
    SEARCH_PAGE_SIZE,
    RateLimitError,
    SectionNotFoundError,
    SSBClient,
    SSBRequestError,
    SSBResponseError,
    TermResolutionError,
    _extract_synchronizer_token,
    _retry_after,
)

TERM_HTML = """<!doctype html><html><head>
<meta name="synchronizerToken" content="token-123">
</head><body>Select a Term for Class Search</body></html>"""
TERM_HTML_INPUT = '<input name="synchronizerToken" value="input-token">'
TERM_HTML_SCRIPT = '<script>window.synchronizerToken = "script-token";</script>'
JSON_HEADERS = {"content-type": "application/json;charset=UTF-8"}
HTML_HEADERS = {"content-type": "text/html;charset=UTF-8"}
TERMS = [{"code": "202636", "description": "2026 Fall"}]


def term_page() -> httpx.Response:
    return httpx.Response(200, text=TERM_HTML, headers=HTML_HEADERS)


def term_selected() -> httpx.Response:
    return httpx.Response(
        200,
        json={"fwdURL": "/StudentRegistrationSsb/ssb/classSearch/classSearch"},
        headers=JSON_HEADERS,
    )


def form_reset() -> httpx.Response:
    return httpx.Response(200, json={"reset": True}, headers=JSON_HEADERS)


def search_result(
    section_payload: dict[str, object], data: list[dict[str, object]] | None = None
) -> httpx.Response:
    sections = [section_payload] if data is None else data
    return httpx.Response(
        200,
        json={"success": True, "totalCount": len(sections), "data": sections},
        headers=JSON_HEADERS,
    )


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(TERM_HTML, "token-123", id="meta"),
        pytest.param(TERM_HTML_INPUT, "input-token", id="input"),
        pytest.param(TERM_HTML_SCRIPT, "script-token", id="inline-script"),
    ],
)
def test_token_extraction(html: str, expected: str) -> None:
    assert _extract_synchronizer_token(html) == expected


def test_retry_after_supports_seconds_and_http_date() -> None:
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "180"})) == 180
    delay = _retry_after(
        httpx.Response(429, headers={"Retry-After": "Mon, 24 Aug 2099 13:00:00 GMT"})
    )
    assert delay is not None and delay > 0


async def test_successful_initialization_preserves_token_and_session_id(
    initialized_banner: respx.Route,
) -> None:
    async with SSBClient() as client:
        await client.initialize()
        first_session_id = client.unique_session_id
        await client.initialize()
    assert initialized_banner.call_count == 1
    assert first_session_id and len(first_session_id) >= 18


@pytest.mark.usefixtures("initialized_banner")
async def test_term_lookup_and_exact_resolution(respx_mock: respx.MockRouter) -> None:
    term_route = respx_mock.get(f"{BASE_URL}/classSearch/getTerms").mock(
        return_value=httpx.Response(200, json=TERMS, headers=JSON_HEADERS)
    )
    async with SSBClient() as client:
        term = await client.resolve_term("2026 Fall")
    assert term.code == "202636"
    request = term_route.calls.last.request
    assert request.url.params["offset"] == "1"
    assert request.headers["X-Synchronizer-Token"] == "token-123"


@pytest.mark.usefixtures("initialized_banner")
async def test_term_resolution_requires_one_exact_match(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE_URL}/classSearch/getTerms").mock(
        return_value=httpx.Response(200, json=TERMS, headers=JSON_HEADERS)
    )
    async with SSBClient() as client:
        with pytest.raises(TermResolutionError):
            await client.resolve_term("Fall 2026")


async def test_section_search_selects_term_and_maps_json(
    respx_mock: respx.MockRouter,
    selected_banner: respx.Route,
    reset_banner: respx.Route,
    section_payload_factory: Callable[..., dict[str, object]],
) -> None:
    search = respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        return_value=search_result(section_payload_factory())
    )
    async with SSBClient() as client:
        section = await client.get_section(
            term="202636", subject="cis", course_number="4526", crn="31752"
        )
    assert section.effective_seats == 1
    assert selected_banner.calls.last.request.url.params["mode"] == "search"
    assert b"term=202636" in selected_banner.calls.last.request.content
    assert reset_banner.call_count == 1
    assert reset_banner.calls.last.request.content == b"resetCourses=true&resetSections=true"
    assert search.calls.last.request.url.params["txt_subject"] == "CIS"
    assert search.calls.last.request.url.params["uniqueSessionId"]


async def test_each_course_search_resets_session_scoped_form(
    respx_mock: respx.MockRouter,
    selected_banner: respx.Route,
    reset_banner: respx.Route,
    section_payload_factory: Callable[..., dict[str, object]],
) -> None:
    search = respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        side_effect=[
            search_result(section_payload_factory()),
            search_result(
                section_payload_factory(
                    courseReferenceNumber="20419",
                    subject="MATH",
                    courseNumber="1041",
                    courseTitle="Calculus I",
                )
            ),
        ]
    )

    async with SSBClient() as client:
        cis = await client.search_sections(term="202636", subject="CIS", course_number="4526")
        math = await client.search_sections(term="202636", subject="MATH", course_number="1041")

    assert [cis[0].subject, math[0].subject] == ["CIS", "MATH"]
    assert selected_banner.call_count == 1
    assert reset_banner.call_count == 2
    assert [call.request.url.params["txt_subject"] for call in search.calls] == ["CIS", "MATH"]


@pytest.mark.usefixtures("selected_banner")
async def test_section_search_reads_every_reported_page(
    respx_mock: respx.MockRouter,
    section_payload_factory: Callable[..., dict[str, object]],
) -> None:
    first_page = [
        section_payload_factory(courseReferenceNumber=str(31000 + index))
        for index in range(SEARCH_PAGE_SIZE)
    ]
    final_section = section_payload_factory(courseReferenceNumber="39999")
    route = respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"success": True, "totalCount": SEARCH_PAGE_SIZE + 1, "data": first_page},
                headers=JSON_HEADERS,
            ),
            httpx.Response(
                200,
                json={
                    "success": True,
                    "totalCount": SEARCH_PAGE_SIZE + 1,
                    "data": [final_section],
                },
                headers=JSON_HEADERS,
            ),
        ]
    )

    async with SSBClient() as client:
        sections = await client.search_sections(term="202636", subject="CIS", course_number="4526")

    assert len(sections) == SEARCH_PAGE_SIZE + 1
    assert route.calls[1].request.url.params["pageOffset"] == str(SEARCH_PAGE_SIZE)


@pytest.mark.usefixtures("selected_banner")
async def test_incomplete_section_pagination_fails_safely(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "totalCount": 1, "data": []},
            headers=JSON_HEADERS,
        )
    )
    async with SSBClient() as client:
        with pytest.raises(SSBResponseError, match="pagination stopped"):
            await client.search_sections(term="202636", subject="CIS", course_number="4526")


@pytest.mark.usefixtures("selected_banner")
async def test_missing_crn_raises_domain_error(
    respx_mock: respx.MockRouter,
    section_payload_factory: Callable[..., dict[str, object]],
) -> None:
    respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        return_value=search_result(section_payload_factory())
    )
    async with SSBClient() as client:
        with pytest.raises(SectionNotFoundError):
            await client.get_section(
                term="202636", subject="CIS", course_number="4526", crn="99999"
            )


@pytest.mark.parametrize(
    "status",
    [pytest.param(401, id="unauthorized"), pytest.param(403, id="forbidden")],
)
async def test_search_refreshes_session_once_after_auth_status(
    respx_mock: respx.MockRouter,
    section_payload_factory: Callable[..., dict[str, object]],
    status: int,
) -> None:
    init = respx_mock.get(f"{BASE_URL}/term/termSelection").mock(
        side_effect=[term_page(), term_page()]
    )
    select = respx_mock.post(f"{BASE_URL}/term/search").mock(
        side_effect=[term_selected(), term_selected()]
    )
    reset = respx_mock.post(f"{BASE_URL}/classSearch/resetDataForm").mock(
        side_effect=[form_reset(), form_reset()]
    )
    search = respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        side_effect=[httpx.Response(status), search_result(section_payload_factory())]
    )
    async with SSBClient() as client:
        sections = await client.search_sections(term="202636", subject="CIS", course_number="4526")
    assert sections[0].course_reference_number == "31752"
    assert init.call_count == select.call_count == reset.call_count == search.call_count == 2


@pytest.mark.usefixtures("selected_banner")
async def test_429_exposes_retry_after_without_retrying(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "180"})
    )
    async with SSBClient() as client:
        with pytest.raises(RateLimitError) as caught:
            await client.search_sections(term="202636", subject="CIS", course_number="4526")
    assert caught.value.retry_after == 180


@pytest.mark.usefixtures("selected_banner")
async def test_server_error_is_unknown_not_closed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(return_value=httpx.Response(500))
    async with SSBClient() as client:
        with pytest.raises(SSBRequestError):
            await client.search_sections(term="202636", subject="CIS", course_number="4526")


async def test_timeout_is_unknown_not_closed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE_URL}/term/termSelection").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    async with SSBClient() as client:
        with pytest.raises(SSBRequestError):
            await client.initialize()


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            httpx.Response(200, text="not-json", headers=JSON_HEADERS), id="malformed-json"
        ),
        pytest.param(
            httpx.Response(200, json={"success": True}, headers=JSON_HEADERS), id="missing-data"
        ),
        pytest.param(
            httpx.Response(
                200,
                json={"success": True, "totalCount": "1", "data": []},
                headers=JSON_HEADERS,
            ),
            id="invalid-total-count",
        ),
        pytest.param(
            httpx.Response(200, json={"success": True, "data": [{}]}, headers=JSON_HEADERS),
            id="invalid-section",
        ),
    ],
)
@pytest.mark.usefixtures("selected_banner")
async def test_invalid_section_payload_fails_safely(
    respx_mock: respx.MockRouter, response: httpx.Response
) -> None:
    respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(return_value=response)
    async with SSBClient() as client:
        with pytest.raises(SSBResponseError):
            await client.search_sections(term="202636", subject="CIS", course_number="4526")


async def test_unexpected_html_refreshes_once_then_fails(
    respx_mock: respx.MockRouter,
) -> None:
    init = respx_mock.get(f"{BASE_URL}/term/termSelection").mock(
        side_effect=[term_page(), term_page()]
    )
    select = respx_mock.post(f"{BASE_URL}/term/search").mock(
        side_effect=[term_selected(), term_selected()]
    )
    reset = respx_mock.post(f"{BASE_URL}/classSearch/resetDataForm").mock(
        side_effect=[form_reset(), form_reset()]
    )
    search = respx_mock.get(f"{BASE_URL}/searchResults/searchResults").mock(
        side_effect=[
            httpx.Response(200, text="<html>expired</html>", headers=HTML_HEADERS),
            httpx.Response(200, text="<html>expired</html>", headers=HTML_HEADERS),
        ]
    )
    async with SSBClient() as client:
        with pytest.raises(SSBRequestError):
            await client.search_sections(term="202636", subject="CIS", course_number="4526")
    assert init.call_count == select.call_count == reset.call_count == search.call_count == 2
