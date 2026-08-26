from collections.abc import Callable

import httpx
import pytest
import respx

from ssb_seat_tracker.client import BASE_URL
from ssb_seat_tracker.models import Section

type SectionPayloadFactory = Callable[..., dict[str, object]]
type SectionFactory = Callable[..., Section]

JSON_HEADERS = {"content-type": "application/json;charset=UTF-8"}


@pytest.fixture
def section_payload_factory() -> SectionPayloadFactory:
    base: dict[str, object] = {
        "courseReferenceNumber": "31752",
        "subject": "CIS",
        "courseNumber": "4526",
        "sequenceNumber": "001",
        "courseTitle": "Foundations of Machine Learning",
        "campusDescription": "Main",
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
def selected_banner(
    respx_mock: respx.MockRouter,
) -> respx.Route:
    return respx_mock.post(f"{BASE_URL}/term/search").mock(
        return_value=httpx.Response(
            200,
            json={"fwdURL": "/StudentRegistrationSsb/ssb/classSearch/classSearch"},
            headers=JSON_HEADERS,
        )
    )
