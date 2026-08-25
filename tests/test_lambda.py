from datetime import UTC, datetime
from typing import Any

import pytest

from ssb_seat_tracker.main import (
    CycleResult,
    DynamoDBWatchRepository,
    WatchCycleError,
    run_watch_cycle,
)
from ssb_seat_tracker.models import Availability, Section, Term, Watch

NOW = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)


class FakeCourseSearch:
    def __init__(self, sections: list[Section], *, error: Exception | None = None) -> None:
        self.sections = sections
        self.error = error
        self.term_calls = 0
        self.search_calls: list[dict[str, str]] = []

    async def get_terms(self) -> list[Term]:
        self.term_calls += 1
        return [Term(code="202636", description="2026 Fall")]

    async def search_sections(
        self, *, term: str, subject: str, course_number: str
    ) -> list[Section]:
        self.search_calls.append({"term": term, "subject": subject, "course_number": course_number})
        if self.error:
            raise self.error
        return self.sections


class RecordingRepository:
    def __init__(self, watches: list[Watch]) -> None:
        self.watches = watches
        self.saved: list[tuple[Watch, Availability]] = []

    def list_enabled(self) -> list[Watch]:
        return self.watches

    def save_observation(self, watch: Watch, availability: Availability) -> None:
        self.saved.append((watch, availability))


class RecordingNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[Section, datetime]] = []

    async def send_opening(self, section: Section, *, checked_at: datetime) -> None:
        if self.error:
            raise self.error
        self.sent.append((section, checked_at))

    async def close(self) -> None:
        return None


def watch(**overrides: Any) -> Watch:
    return Watch.model_validate(
        {
            "crn": "31752",
            "term": "2026 Fall",
            "subject": "CIS",
            "course_number": "4526",
            **overrides,
        }
    )


async def test_cycle_batches_watches_for_the_same_course(section_factory) -> None:
    sections = [
        section_factory(courseReferenceNumber="31752"),
        section_factory(courseReferenceNumber="31753", sequenceNumber="002"),
    ]
    client = FakeCourseSearch(sections)
    repository = RecordingRepository(
        [
            watch(available=False, effective_seats=0, last_checked_at=NOW),
            watch(crn="31753", available=True, effective_seats=1, last_checked_at=NOW),
        ]
    )
    notifier = RecordingNotifier()

    result = await run_watch_cycle(
        client=client,
        notifier=notifier,
        repository=repository,
        checked_at=NOW,
    )

    assert result == CycleResult(watches=2, checked=2, notified=1, failed=0)
    assert client.term_calls == 1
    assert client.search_calls == [{"term": "202636", "subject": "CIS", "course_number": "4526"}]
    assert [item[0].crn for item in repository.saved] == ["31752", "31753"]
    assert [item[0].course_reference_number for item in notifier.sent] == ["31752"]


async def test_cycle_persists_closed_observation_without_notifying(section_factory) -> None:
    client = FakeCourseSearch([section_factory(openSection=False, seatsAvailable=0, enrollment=40)])
    repository = RecordingRepository([watch()])
    notifier = RecordingNotifier()

    result = await run_watch_cycle(
        client=client,
        notifier=notifier,
        repository=repository,
        checked_at=NOW,
    )

    assert result.notified == 0
    assert repository.saved[0][1].available is False
    assert notifier.sent == []


async def test_delivery_failure_preserves_previous_state(section_factory) -> None:
    repository = RecordingRepository(
        [watch(available=False, effective_seats=0, last_checked_at=NOW)]
    )

    with pytest.raises(WatchCycleError) as raised:
        await run_watch_cycle(
            client=FakeCourseSearch([section_factory()]),
            notifier=RecordingNotifier(RuntimeError("ntfy unavailable")),
            repository=repository,
            checked_at=NOW,
        )

    assert raised.value.result == CycleResult(watches=1, checked=0, notified=0, failed=1)
    assert repository.saved == []


async def test_course_failure_is_reported_and_not_persisted(section_factory) -> None:
    repository = RecordingRepository([watch(), watch(crn="31753")])

    with pytest.raises(WatchCycleError) as raised:
        await run_watch_cycle(
            client=FakeCourseSearch([section_factory()], error=RuntimeError("upstream failed")),
            notifier=RecordingNotifier(),
            repository=repository,
            checked_at=NOW,
        )

    assert raised.value.result.failed == 2
    assert repository.saved == []


async def test_unknown_term_fails_without_searching(section_factory) -> None:
    client = FakeCourseSearch([section_factory()])
    repository = RecordingRepository([watch(term="not a published term")])

    with pytest.raises(WatchCycleError):
        await run_watch_cycle(
            client=client,
            notifier=RecordingNotifier(),
            repository=repository,
            checked_at=NOW,
        )

    assert client.search_calls == []


class FakeTable:
    def __init__(self) -> None:
        self.scan_requests: list[dict[str, Any]] = []
        self.update_requests: list[dict[str, Any]] = []

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.scan_requests.append(kwargs)
        if len(self.scan_requests) == 1:
            return {
                "Items": [watch().model_dump()],
                "LastEvaluatedKey": {"term": "2026 Fall", "crn": "31752"},
            }
        return {"Items": [watch(crn="31753").model_dump()]}

    def update_item(self, **kwargs: Any) -> None:
        self.update_requests.append(kwargs)


def test_dynamodb_repository_paginates_and_conditionally_updates() -> None:
    table = FakeTable()
    repository = DynamoDBWatchRepository(table)

    watches = repository.list_enabled()
    repository.save_observation(
        watches[0],
        Availability(crn="31752", available=True, effectiveSeats=2, checkedAt=NOW),
    )

    assert [item.crn for item in watches] == ["31752", "31753"]
    assert table.scan_requests[1]["ExclusiveStartKey"] == {
        "term": "2026 Fall",
        "crn": "31752",
    }
    update = table.update_requests[0]
    assert update["Key"] == {"term": "2026 Fall", "crn": "31752"}
    assert update["ConditionExpression"] == "attribute_exists(term) AND attribute_exists(crn)"
    assert update["ExpressionAttributeValues"] == {
        ":available": True,
        ":seats": 2,
        ":checked_at": NOW.isoformat(),
    }
