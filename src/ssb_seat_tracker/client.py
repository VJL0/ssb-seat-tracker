"""Asynchronous client for Temple's public Ellucian SSB class search."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, TypeVar
from uuid import uuid4

import httpx
from pydantic import ValidationError

from .models import Section, Term

BASE_URL = "https://prd-xereg.temple.edu/StudentRegistrationSsb/ssb"
AJAX_ACCEPT = "application/json, text/javascript, */*; q=0.01"
SEARCH_PAGE_SIZE = 50
MAX_SEARCH_PAGES = 20


class SSBError(RuntimeError):
    """Base error for safe, read-only SSB operations."""


class SSBRequestError(SSBError):
    """A network or HTTP failure whose result must be treated as unknown."""


class SSBResponseError(SSBError):
    """Banner returned data that could not be safely interpreted."""


class SessionExpiredError(SSBRequestError):
    """The anonymous Banner session or synchronizer token is stale."""


class RateLimitError(SSBRequestError):
    """Temple requested that the client reduce request pressure."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Temple SSB rate limited the request")
        self.retry_after = retry_after


class TermResolutionError(SSBError):
    """A human-readable term description did not resolve uniquely."""


class SectionNotFoundError(SSBError):
    """The requested CRN was not present in the narrow course search."""


class _SynchronizerTokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"meta", "input"} or self.token is not None:
            return
        values = {key.lower(): value for key, value in attrs}
        if values.get("name", "").lower() != "synchronizertoken":
            return
        token = values.get("content") if tag == "meta" else values.get("value")
        if token:
            self.token = token


def _extract_synchronizer_token(html: str) -> str | None:
    parser = _SynchronizerTokenParser()
    parser.feed(html)
    if parser.token:
        return parser.token

    # Banner versions sometimes assign the token in an inline JavaScript object.
    match = re.search(
        r"(?:synchronizerToken|synchronizer_token)\s*[:=]\s*['\"]([^'\"]+)['\"]",
        html,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _new_unique_session_id() -> str:
    """Create one browser-like logical identifier per initialized search session."""

    return f"{uuid4().hex[:5]}{int(time.time() * 1000)}"


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


T = TypeVar("T")


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
        self._synchronizer_token: str | None = None
        self._unique_session_id: str | None = None
        self._selected_term: str | None = None
        self._initialized = False

    async def __aenter__(self) -> SSBClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def unique_session_id(self) -> str | None:
        return self._unique_session_id

    def _ajax_headers(self) -> dict[str, str]:
        headers = {"Accept": AJAX_ACCEPT, "X-Requested-With": "XMLHttpRequest"}
        if self._synchronizer_token:
            headers["X-Synchronizer-Token"] = self._synchronizer_token
        return headers

    async def initialize(self, *, force: bool = False) -> None:
        if self._initialized and not force:
            return
        if force:
            self._client.cookies.clear()
        self._initialized = False
        self._selected_term = None
        self._synchronizer_token = None
        self._unique_session_id = _new_unique_session_id()

        response = await self._send(
            "GET", "/term/termSelection", params={"mode": "search"}, expect_json=False
        )
        if "text/html" not in response.headers.get("content-type", "").lower():
            raise SSBResponseError("Temple term-selection page was not HTML")
        self._synchronizer_token = _extract_synchronizer_token(response.text)
        self._initialized = True

    async def get_terms(self) -> list[Term]:
        await self.initialize()

        async def operation() -> list[Term]:
            response = await self._send(
                "GET",
                "/classSearch/getTerms",
                params={"searchTerm": "", "offset": 1, "max": 100},
                headers=self._ajax_headers(),
            )
            payload = self._json(response)
            if not isinstance(payload, list):
                raise SSBResponseError("Temple term response was not a JSON list")
            try:
                return [Term.model_validate(item) for item in payload]
            except ValidationError as exc:
                raise SSBResponseError("Temple term response failed validation") from exc

        return await self._with_session_refresh(operation)

    async def resolve_term(self, description: str) -> Term:
        wanted = description.strip()
        matches = [term for term in await self.get_terms() if term.description == wanted]
        if len(matches) != 1:
            raise TermResolutionError(
                f"expected exactly one term named {wanted!r}; found {len(matches)}"
            )
        return matches[0]

    async def select_term(self, term: str) -> None:
        await self.initialize()

        async def operation() -> None:
            assert self._unique_session_id is not None
            response = await self._send(
                "POST",
                "/term/search",
                params={"mode": "search"},
                data={
                    "term": term,
                    "studyPath": "",
                    "studyPathText": "",
                    "student": "",
                    "altPin": "",
                    "stu_pin": "",
                    "holdPassword": "",
                    "startDatepicker": "",
                    "endDatepicker": "",
                    "uniqueSessionId": self._unique_session_id,
                },
                headers=self._ajax_headers(),
            )
            payload = self._json(response)
            if not isinstance(payload, dict) or not payload.get("fwdURL"):
                raise SSBResponseError("Temple did not establish the class-search term")
            self._selected_term = term

        await self._with_session_refresh(operation)

    async def search_sections(
        self, *, term: str, subject: str, course_number: str
    ) -> list[Section]:
        if self._selected_term != term:
            await self.select_term(term)

        async def operation() -> list[Section]:
            assert self._unique_session_id is not None
            await self._reset_class_search_form()
            sections: list[Section] = []
            expected_total: int | None = None
            offset = 0

            for _page in range(MAX_SEARCH_PAGES):
                response = await self._send(
                    "GET",
                    "/searchResults/searchResults",
                    params={
                        "txt_term": term,
                        "txt_subjectcoursecombo": "",
                        "txt_subject": subject.upper(),
                        "txt_courseNumber": course_number,
                        "txt_college": "",
                        "txt_division": "",
                        "txt_attribute": "",
                        "startDatepicker": "",
                        "endDatepicker": "",
                        "uniqueSessionId": self._unique_session_id,
                        "pageOffset": offset,
                        "pageMaxSize": SEARCH_PAGE_SIZE,
                        "sortColumn": "subjectDescription",
                        "sortDirection": "asc",
                    },
                    headers=self._ajax_headers(),
                )
                payload = self._json(response)
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise SSBResponseError("Temple section response lacked a JSON data list")
                if payload.get("success") is not True:
                    raise SSBResponseError("Temple reported an unsuccessful section search")

                total = payload.get("totalCount")
                if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                    raise SSBResponseError("Temple section response had an invalid total count")
                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    raise SSBResponseError("Temple section count changed during pagination")

                page_data = payload["data"]
                try:
                    sections.extend(Section.model_validate(item) for item in page_data)
                except ValidationError as exc:
                    raise SSBResponseError("Temple section response failed validation") from exc

                if len(sections) >= expected_total:
                    if len(sections) != expected_total:
                        raise SSBResponseError("Temple returned more sections than it reported")
                    return sections
                if not page_data:
                    raise SSBResponseError("Temple section pagination stopped before completion")
                offset += len(page_data)

            raise SSBResponseError("Temple section response exceeded the pagination safety limit")

        try:
            return await operation()
        except SessionExpiredError:
            await self.initialize(force=True)
            await self.select_term(term)
            return await operation()

    async def _reset_class_search_form(self) -> None:
        """Clear Banner's session-scoped criteria before one logical course search."""

        response = await self._send(
            "POST",
            "/classSearch/resetDataForm",
            data={"resetCourses": "true", "resetSections": "true"},
            headers=self._ajax_headers(),
        )
        payload = self._json(response)
        if not isinstance(payload, dict) or payload.get("reset") is not True:
            raise SSBResponseError("Temple did not reset the class-search form")

    async def get_section(
        self, *, term: str, subject: str, course_number: str, crn: str
    ) -> Section:
        sections = await self.search_sections(
            term=term, subject=subject, course_number=course_number
        )
        matches = [section for section in sections if section.course_reference_number == crn]
        if len(matches) != 1:
            raise SectionNotFoundError(
                f"CRN {crn} was not found uniquely in {subject.upper()} {course_number}"
            )
        return matches[0]

    async def _with_session_refresh(self, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            return await operation()
        except SessionExpiredError:
            await self.initialize(force=True)
            return await operation()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        expect_json: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, f"{self.base_url}{path}", **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SSBRequestError("Temple SSB request failed") from exc

        if response.status_code in {401, 403}:
            raise SessionExpiredError("Temple SSB anonymous session expired")
        if response.status_code == 429:
            raise RateLimitError(_retry_after(response))
        if response.status_code >= 400:
            raise SSBRequestError(f"Temple SSB returned HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "").lower()
        if expect_json and "json" not in content_type:
            if "html" in content_type:
                raise SessionExpiredError("Temple returned HTML instead of anonymous search data")
            raise SSBResponseError("Temple response was not JSON")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise SSBResponseError("Temple returned malformed JSON") from exc
