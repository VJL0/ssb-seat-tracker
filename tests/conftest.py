from collections.abc import Callable

import httpx
import pytest
import respx

from ssb_seat_tracker.client import BASE_URL
from ssb_seat_tracker.models import Section

type SectionPayloadFactory = Callable[..., dict[str, object]]
type SectionFactory = Callable[..., Section]

TERM_HTML = """<!doctype html><html><head>
<meta name="synchronizerToken" content="token-123">
</head><body>Select a Term for Class Search</body></html>"""
JSON_HEADERS = {"content-type": "application/json;charset=UTF-8"}
HTML_HEADERS = {"content-type": "text/html;charset=UTF-8"}


@pytest.fixture
def section_payload_factory() -> SectionPayloadFactory:
    base: dict[str, object] = {
        "courseReferenceNumber": "31752",
        "subject": "CIS",
        "courseNumber": "4526",
        "sequenceNumber": "001",
        "courseTitle": "Foundations of Machine Learning",
        "maximumEnrollment": 40,
        "enrollment": 39,
        "seatsAvailable": 1,
        "waitCapacity": 10,
        "waitCount": 0,
        "waitAvailable": 10,
        "openSection": True,
        "crossList": None,
        "crossListCapacity": None,
        "crossListCount": None,
        "unrelatedFutureBannerField": "ignored",
    }

    def make(**overrides: object) -> dict[str, object]:
        return base | overrides

    return make


@pytest.fixture
def section_factory(section_payload_factory: SectionPayloadFactory) -> SectionFactory:
    def make(**overrides: object) -> Section:
        return Section.model_validate(section_payload_factory(**overrides))

    return make


@pytest.fixture
def initialized_banner(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(f"{BASE_URL}/term/termSelection").mock(
        return_value=httpx.Response(200, text=TERM_HTML, headers=HTML_HEADERS)
    )


@pytest.fixture
def selected_banner(respx_mock: respx.MockRouter, initialized_banner: respx.Route) -> respx.Route:
    return respx_mock.post(f"{BASE_URL}/term/search").mock(
        return_value=httpx.Response(
            200,
            json={"fwdURL": "/StudentRegistrationSsb/ssb/classSearch/classSearch"},
            headers=JSON_HEADERS,
        )
    )
