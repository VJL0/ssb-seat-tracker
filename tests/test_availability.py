from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ssb_seat_tracker.main as main_module
from ssb_seat_tracker.main import build_notifier, check_once, load_state, save_state, should_notify
from ssb_seat_tracker.models import Availability, Section
from ssb_seat_tracker.notifier import ConsoleNotifier, NotificationError, NtfyNotifier

NOW = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)


class FakeSectionLookup:
    def __init__(self, section: Section) -> None:
        self.section = section
        self.calls: list[dict[str, str]] = []

    async def get_section(
        self, *, term: str, subject: str, course_number: str, crn: str
    ) -> Section:
        self.calls.append(
            {"term": term, "subject": subject, "course_number": course_number, "crn": crn}
        )
        return self.section


class RecordingNotifier:
    def __init__(self, *, error: NotificationError | None = None) -> None:
        self.error = error
        self.sent: list[tuple[Section, datetime]] = []

    async def send_opening(self, section: Section, *, checked_at: datetime) -> None:
        if self.error:
            raise self.error
        self.sent.append((section, checked_at))

    async def close(self) -> None:
        return None


def availability(*, available: bool, seats: int, crn: str = "31752") -> Availability:
    return Availability(
        crn=crn,
        available=available,
        effectiveSeats=seats,
        checkedAt=NOW,
    )


def test_initial_open_and_closed_to_open_notify() -> None:
    opened = availability(available=True, seats=1)
    assert should_notify(None, opened) is True
    assert should_notify(availability(available=False, seats=0), opened) is True


def test_identical_open_state_does_not_notify() -> None:
    current = availability(available=True, seats=1)
    assert should_notify(availability(available=True, seats=1), current) is False


def test_more_effective_seats_notifies_but_fewer_does_not() -> None:
    previous = availability(available=True, seats=2)
    assert should_notify(previous, availability(available=True, seats=3)) is True
    assert should_notify(previous, availability(available=True, seats=1)) is False


def test_closed_state_never_notifies() -> None:
    previous = availability(available=True, seats=1)
    current = availability(available=False, seats=0)
    assert should_notify(previous, current) is False


def test_state_round_trip_uses_atomic_json_file(tmp_path) -> None:
    path = tmp_path / "state.json"
    expected = availability(available=True, seats=2)
    save_state(path, expected)
    assert load_state(path) == expected
    assert list(tmp_path.iterdir()) == [path]


def test_state_timestamp_can_advance_without_notification() -> None:
    previous = availability(available=True, seats=2)
    current = previous.model_copy(update={"checked_at": NOW + timedelta(minutes=1)})
    assert should_notify(previous, current) is False


async def test_check_once_uses_ports_and_notifies_transition(section_factory) -> None:
    lookup = FakeSectionLookup(section_factory(seatsAvailable=2, waitCount=1, openSection=True))
    notifier = RecordingNotifier()
    previous = availability(available=False, seats=0)

    current = await check_once(
        client=lookup,
        notifier=notifier,
        term_code="202636",
        subject="CIS",
        course_number="4526",
        crn="31752",
        previous=previous,
        checked_at=NOW,
    )

    assert current.available is True
    assert current.effective_seats == 1
    assert lookup.calls == [
        {"term": "202636", "subject": "CIS", "course_number": "4526", "crn": "31752"}
    ]
    assert notifier.sent == [(lookup.section, NOW)]


async def test_check_once_does_not_notify_unchanged_state(section_factory) -> None:
    lookup = FakeSectionLookup(section_factory(seatsAvailable=1, waitCount=0, openSection=True))
    notifier = RecordingNotifier()

    current = await check_once(
        client=lookup,
        notifier=notifier,
        term_code="202636",
        subject="CIS",
        course_number="4526",
        crn="31752",
        previous=availability(available=True, seats=1),
        checked_at=NOW,
    )

    assert current.available is True
    assert notifier.sent == []


async def test_check_once_propagates_delivery_failure_before_state_can_advance(
    section_factory,
) -> None:
    lookup = FakeSectionLookup(section_factory(seatsAvailable=1, waitCount=0, openSection=True))
    notifier = RecordingNotifier(error=NotificationError("delivery failed"))
    previous = availability(available=False, seats=0)

    with pytest.raises(NotificationError, match="delivery failed"):
        await check_once(
            client=lookup,
            notifier=notifier,
            term_code="202636",
            subject="CIS",
            course_number="4526",
            crn="31752",
            previous=previous,
            checked_at=NOW,
        )

    assert previous == availability(available=False, seats=0)


async def test_notifier_configuration_prefers_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(main_module, "DEFAULT_NTFY_TOPIC_FILE", tmp_path / "missing-topic")
    monkeypatch.setenv("NTFY_TOPIC", "environment-topic")
    notifier = build_notifier()
    try:
        assert isinstance(notifier, NtfyNotifier)
    finally:
        await notifier.close()


async def test_notifier_configuration_uses_local_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    topic_file = tmp_path / ".ntfy-topic"
    topic_file.write_text("file-topic\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "DEFAULT_NTFY_TOPIC_FILE", topic_file)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    notifier = build_notifier()
    try:
        assert isinstance(notifier, NtfyNotifier)
    finally:
        await notifier.close()


async def test_notifier_configuration_falls_back_to_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(main_module, "DEFAULT_NTFY_TOPIC_FILE", tmp_path / "missing-topic")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    notifier = build_notifier()
    try:
        assert isinstance(notifier, ConsoleNotifier)
    finally:
        await notifier.close()
