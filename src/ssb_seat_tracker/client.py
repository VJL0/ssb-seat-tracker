"""Asynchronous client for Temple's public Ellucian SSB class search."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from .errors import RateLimitError, SSBError, _SessionExpiredError
from .models import Section, Term

BASE_URL = "https://prd-xereg.temple.edu/StudentRegistrationSsb/ssb"
SEARCH_PAGE_SIZE = 50
MAX_SEARCH_PAGES = 20
MAIN_CAMPUS_DESCRIPTION = "Main"
TERM_LIST_ADAPTER = TypeAdapter(list[Term])
SECTION_LIST_ADAPTER = TypeAdapter(list[Section])


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)
        except TypeError, ValueError, OverflowError:
            return None


class SSBClient:
    """Persistent anonymous session for Temple's public class-search system."""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=10, write=10, pool=10),
            follow_redirects=True,
            headers={"User-Agent": "ssb-seat-tracker/0.1 (read-only; responsible polling)"},
        )
        self._selected_term: str | None = None

    async def __aenter__(self) -> SSBClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_terms(self) -> list[Term]:
        payload = await self._request_json(
            "GET",
            "/classSearch/getTerms",
            params={"searchTerm": "", "offset": 1, "max": 100},
        )
        try:
            return TERM_LIST_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise SSBError("Temple term response failed validation") from exc

    async def resolve_term(self, description: str) -> Term:
        wanted = description.strip()
        matches = [term for term in await self.get_terms() if term.description == wanted]
        if len(matches) != 1:
            raise SSBError(f"expected exactly one term named {wanted!r}; found {len(matches)}")
        return matches[0]

    async def _select_term(self, term: str) -> None:
        payload = await self._request_json(
            "POST",
            "/term/search",
            data={"term": term},
        )
        if not isinstance(payload, dict) or not payload.get("fwdURL"):
            raise SSBError("Temple did not establish the class-search term")
        self._selected_term = term

    async def search_sections(
        self, *, term: str, subject: str, course_number: str
    ) -> list[Section]:
        term = term.strip()
        subject = subject.strip().upper()
        course_number = course_number.strip()
        try:
            return await self._search_sections(term, subject, course_number)
        except _SessionExpiredError:
            self._client.cookies.clear()
            self._selected_term = None
            return await self._search_sections(term, subject, course_number)

    async def _search_sections(self, term: str, subject: str, course_number: str) -> list[Section]:
        if self._selected_term != term:
            await self._select_term(term)

        sections: list[Section] = []
        total: int | None = None

        for _page in range(MAX_SEARCH_PAGES):
            params: dict[str, str | int] = {
                "txt_term": term,
                "txt_subject": subject,
                "txt_courseNumber": course_number,
                "pageMaxSize": SEARCH_PAGE_SIZE,
            }
            if sections:
                params["pageOffset"] = len(sections)

            payload = await self._request_json(
                "GET",
                "/searchResults/searchResults",
                params=params,
            )
            if not isinstance(payload, dict):
                raise SSBError("Temple section response lacked a JSON data list")
            if payload.get("success") is not True:
                raise SSBError("Temple reported an unsuccessful section search")

            reported_total = payload.get("totalCount")
            if isinstance(reported_total, bool) or not isinstance(reported_total, int):
                raise SSBError("Temple section response had an invalid total count")
            if total is not None and reported_total != total:
                raise SSBError("Temple section count changed during pagination")
            total = reported_total

            raw_data = payload.get("data")
            if raw_data is None and total == 0:
                raw_data = []
            try:
                page = SECTION_LIST_ADAPTER.validate_python(raw_data)
            except ValidationError as exc:
                raise SSBError("Temple section response failed validation") from exc
            if any(
                section.subject != subject or section.course_number != course_number
                for section in page
            ):
                raise SSBError("Temple section response contained results for a different course")
            sections.extend(page)

            if len(sections) == total:
                return [
                    section
                    for section in sections
                    if section.campus_description == MAIN_CAMPUS_DESCRIPTION
                ]
            if not page:
                raise SSBError("Temple section pagination stopped before completion")
            if len(sections) > total:
                raise SSBError("Temple returned an inconsistent section count")

        raise SSBError("Temple section response exceeded the pagination safety limit")

    async def get_section(
        self, *, term: str, subject: str, course_number: str, crn: str
    ) -> Section:
        sections = await self.search_sections(
            term=term, subject=subject, course_number=course_number
        )
        matches = [section for section in sections if section.course_reference_number == crn]
        if len(matches) != 1:
            raise SSBError(f"CRN {crn} was not found uniquely in {subject.upper()} {course_number}")
        return matches[0]

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, f"{self.base_url}{path}", **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SSBError("Temple SSB request failed") from exc

        if response.status_code in {401, 403}:
            raise _SessionExpiredError("Temple SSB anonymous session expired")
        if response.status_code == 429:
            raise RateLimitError(_retry_after(response))
        if response.status_code >= 400:
            raise SSBError(f"Temple SSB returned HTTP {response.status_code}")

        return response

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        content_type = response.headers.get("content-type", "").lower()
        if "html" in content_type:
            raise _SessionExpiredError("Temple returned HTML instead of anonymous search data")
        if "json" not in content_type:
            raise SSBError("Temple response was not JSON")
        try:
            return response.json()
        except ValueError as exc:
            raise SSBError("Temple returned malformed JSON") from exc
