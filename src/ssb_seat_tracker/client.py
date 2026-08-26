"""Direct client for Temple's fixed enrollment-info fragment."""

from __future__ import annotations

import asyncio
import re

import httpx

from .errors import SSBError
from .models import EnrollmentInfo

BASE_URL = "https://prd-xereg.temple.edu/StudentRegistrationSsb/ssb"
TERM = "202636"
ENROLLMENT_PATH = "/searchResults/getEnrollmentInfo"
MAX_CONCURRENCY = 5
MAX_ATTEMPTS = 3

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_EXPECTED_LABELS = {
    "Enrollment Actual",
    "Enrollment Maximum",
    "Enrollment Seats Available",
    "Waitlist Capacity",
    "Waitlist Actual",
    "Waitlist Seats Available",
}
_ENROLLMENT_PATTERN = re.compile(
    r'<span class="status-bold">\s*'
    r"(Enrollment Actual|Enrollment Maximum|Enrollment Seats Available|"
    r"Waitlist Capacity|Waitlist Actual|Waitlist Seats Available):\s*"
    r"</span>\s*"
    r"<span[^>]*>\s*(-?\d+)\s*</span>"
)


def parse_enrollment(html: str) -> EnrollmentInfo:
    values = {label: int(value) for label, value in _ENROLLMENT_PATTERN.findall(html)}
    if values.keys() != _EXPECTED_LABELS:
        missing = sorted(_EXPECTED_LABELS - values.keys())
        raise SSBError(f"unexpected Temple enrollment response; missing {missing}")
    return EnrollmentInfo(
        enrollment=values["Enrollment Actual"],
        capacity=values["Enrollment Maximum"],
        seats_available=values["Enrollment Seats Available"],
        waitlist_capacity=values["Waitlist Capacity"],
        waitlist_count=values["Waitlist Actual"],
        waitlist_available=values["Waitlist Seats Available"],
    )


class SsbClient:
    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
            headers={"User-Agent": "ssb-seat-tracker/0.1 (read-only; responsible polling)"},
        )

    async def __aenter__(self) -> SsbClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def get_enrollment(self, *, term: str, crn: str) -> EnrollmentInfo:
        if not crn.isdigit():
            raise SSBError("CRN must contain only digits")
        response = await self._request_with_retry(term=term, crn=crn)
        return parse_enrollment(response.text)

    async def _request_with_retry(self, *, term: str, crn: str) -> httpx.Response:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.post(
                    ENROLLMENT_PATH,
                    data={"term": term, "courseReferenceNumber": crn},
                )
                if response.status_code in _RETRYABLE_STATUS and attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                response.raise_for_status()
                return response
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise SSBError("Temple enrollment request failed") from exc
                await asyncio.sleep(0.5 * (2**attempt))
            except httpx.HTTPStatusError as exc:
                raise SSBError(f"Temple SSB returned HTTP {exc.response.status_code}") from exc
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def fetch_all(
    client: SsbClient,
    *,
    term: str,
    crns: list[str],
) -> dict[str, EnrollmentInfo | BaseException]:
    """Fetch every CRN with at most five Temple requests in flight."""

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def fetch(crn: str) -> EnrollmentInfo:
        async with semaphore:
            return await client.get_enrollment(term=term, crn=crn)

    results = await asyncio.gather(*(fetch(crn) for crn in crns), return_exceptions=True)
    return dict(zip(crns, results, strict=True))
