"""CLI orchestration for the Temple SSB seat tracker."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import boto3

from .client import RateLimitError, SSBClient, SSBError
from .models import Availability, Section, Term, Watch
from .notifier import ConsoleNotifier, NotificationError, Notifier, NtfyNotifier

LOGGER = logging.getLogger("ssb_seat_tracker")
LOGGER.setLevel(logging.INFO)
DEFAULT_STATE_FILE = Path(".ssb-seat-tracker-state.json")
DEFAULT_NTFY_TOPIC_FILE = Path(".ntfy-topic")
WATCHES_TABLE_ENV = "WATCHES_TABLE_NAME"
NTFY_TOPIC_PARAMETER_ENV = "NTFY_TOPIC_PARAMETER_NAME"


class SectionLookup(Protocol):
    """Application port for looking up one exact Banner section."""

    async def get_section(
        self, *, term: str, subject: str, course_number: str, crn: str
    ) -> Section: ...


class CourseSearch(Protocol):
    """Application port for one shared Banner search session."""

    async def get_terms(self) -> list[Term]: ...

    async def search_sections(
        self, *, term: str, subject: str, course_number: str
    ) -> list[Section]: ...


class WatchRepository(Protocol):
    """Persistence port for watch configuration and successful observations."""

    def list_enabled(self) -> list[Watch]: ...

    def save_observation(self, watch: Watch, availability: Availability) -> None: ...


@dataclass(frozen=True)
class CycleResult:
    watches: int = 0
    checked: int = 0
    notified: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "watches": self.watches,
            "checked": self.checked,
            "notified": self.notified,
            "failed": self.failed,
        }


class WatchCycleError(RuntimeError):
    """One or more watches failed and must be retried."""

    def __init__(self, result: CycleResult) -> None:
        super().__init__(f"{result.failed} of {result.watches} watches failed")
        self.result = result


class DynamoDBWatchRepository:
    """Small-table DynamoDB adapter using a resource Table supplied at composition time."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def list_enabled(self) -> list[Watch]:
        request: dict[str, Any] = {
            "FilterExpression": "#enabled = :enabled",
            "ExpressionAttributeNames": {"#enabled": "enabled"},
            "ExpressionAttributeValues": {":enabled": True},
        }
        watches: list[Watch] = []
        while True:
            response = self._table.scan(**request)
            watches.extend(Watch.model_validate(item) for item in response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return watches
            request["ExclusiveStartKey"] = last_key

    def save_observation(self, watch: Watch, availability: Availability) -> None:
        self._table.update_item(
            Key={"term": watch.term, "crn": watch.crn},
            UpdateExpression=(
                "SET #available = :available, effective_seats = :seats, "
                "last_checked_at = :checked_at"
            ),
            ConditionExpression="attribute_exists(term) AND attribute_exists(crn)",
            ExpressionAttributeNames={"#available": "available"},
            ExpressionAttributeValues={
                ":available": availability.available,
                ":seats": availability.effective_seats,
                ":checked_at": availability.checked_at.isoformat(),
            },
        )


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Write one machine-queryable CloudWatch log record."""

    LOGGER.log(level, json.dumps({"event": event, **fields}, default=str, sort_keys=True))


async def run_watch_cycle(
    *,
    client: CourseSearch,
    notifier: Notifier,
    repository: WatchRepository,
    checked_at: datetime | None = None,
) -> CycleResult:
    """Check all enabled watches, batching watches that share one course search."""

    watches = repository.list_enabled()
    if not watches:
        result = CycleResult()
        log_event("cycle_complete", **result.as_dict())
        return result

    checked_at = checked_at or datetime.now(UTC)
    terms_by_description: dict[str, list[Term]] = defaultdict(list)
    for term in await client.get_terms():
        terms_by_description[term.description].append(term)

    groups: dict[tuple[str, str, str], list[Watch]] = defaultdict(list)
    for watch in watches:
        groups[(watch.term, watch.subject, watch.course_number)].append(watch)

    checked = notified = failed = 0
    for (term_description, subject, course_number), group in groups.items():
        matches = terms_by_description.get(term_description, [])
        if len(matches) != 1:
            failed += len(group)
            log_event(
                "watch_group_failed",
                level=logging.ERROR,
                term=term_description,
                subject=subject,
                course_number=course_number,
                watches=len(group),
                reason="term_not_unique",
            )
            continue

        try:
            sections = await client.search_sections(
                term=matches[0].code,
                subject=subject,
                course_number=course_number,
            )
        except Exception as exc:
            failed += len(group)
            log_event(
                "watch_group_failed",
                level=logging.ERROR,
                term=term_description,
                subject=subject,
                course_number=course_number,
                watches=len(group),
                reason=type(exc).__name__,
            )
            continue

        sections_by_crn: dict[str, list[Section]] = defaultdict(list)
        for section in sections:
            sections_by_crn[section.course_reference_number].append(section)

        for watch in group:
            section_matches = sections_by_crn.get(watch.crn, [])
            if len(section_matches) != 1:
                failed += 1
                log_event(
                    "watch_failed",
                    level=logging.ERROR,
                    crn=watch.crn,
                    reason="section_not_unique",
                )
                continue

            section = section_matches[0]
            availability = Availability.from_section(section, checked_at=checked_at)
            try:
                sent = should_notify(watch.previous_availability(), availability)
                if sent:
                    await notifier.send_opening(section, checked_at=checked_at)
                repository.save_observation(watch, availability)
            except Exception as exc:
                failed += 1
                log_event(
                    "watch_failed",
                    level=logging.ERROR,
                    crn=watch.crn,
                    reason=type(exc).__name__,
                )
                continue

            checked += 1
            notified += int(sent)
            log_event(
                "watch_checked",
                crn=watch.crn,
                available=availability.available,
                effective_seats=availability.effective_seats,
                notified=sent,
            )

    result = CycleResult(watches=len(watches), checked=checked, notified=notified, failed=failed)
    log_event("cycle_complete", **result.as_dict())
    if failed:
        raise WatchCycleError(result)
    return result


@cache
def _dynamodb_resource() -> Any:
    return boto3.resource("dynamodb")


@cache
def _ssm_client() -> Any:
    return boto3.client("ssm")


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable {name} is not configured")
    return value


async def run_lambda_cycle() -> CycleResult:
    """Compose AWS adapters for one bounded scheduler invocation."""

    table_name = _required_environment(WATCHES_TABLE_ENV)
    parameter_name = _required_environment(NTFY_TOPIC_PARAMETER_ENV)
    response = _ssm_client().get_parameter(Name=parameter_name, WithDecryption=True)
    topic = response["Parameter"]["Value"].strip()
    repository = DynamoDBWatchRepository(_dynamodb_resource().Table(table_name))
    notifier = NtfyNotifier(
        topic=topic,
        server_url=os.getenv("NTFY_BASE_URL", "https://ntfy.sh"),
    )
    try:
        async with SSBClient() as client:
            return await run_watch_cycle(
                client=client,
                notifier=notifier,
                repository=repository,
            )
    finally:
        await notifier.close()


def lambda_handler(_event: dict[str, Any], _context: Any) -> dict[str, int]:
    """AWS Lambda entry point for EventBridge Scheduler."""

    return asyncio.run(run_lambda_cycle()).as_dict()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Temple's public SSB class search for a seat opening."
    )
    parser.add_argument("--list-terms", action="store_true", help="list public search terms")
    parser.add_argument("--term", help='human-readable term, such as "2026 Fall"')
    parser.add_argument("--subject", help="course subject, such as CIS")
    parser.add_argument("--course", help="course number, such as 4526")
    parser.add_argument("--crn", help="exact course reference number")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="polling interval in seconds (minimum: 60; default: 60)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="local JSON state file used to suppress duplicate alerts",
    )
    parser.add_argument("--verbose", action="store_true", help="enable diagnostic logging")
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.list_terms:
        return
    missing = [name for name in ("term", "subject", "course", "crn") if not getattr(args, name)]
    if missing:
        names = ", ".join(f"--{name}" for name in missing)
        parser.error("the following arguments are required unless --list-terms is used: " + names)
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    args.subject = args.subject.strip().upper()
    args.course = args.course.strip()
    args.crn = args.crn.strip()


def load_state(path: Path) -> Availability | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Availability.model_validate(payload)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        LOGGER.warning("ignoring unreadable state file %s: %s", path, type(exc).__name__)
        return None


def save_state(path: Path, availability: Availability) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = availability.model_dump(mode="json", by_alias=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def should_notify(previous: Availability | None, current: Availability) -> bool:
    if not current.available:
        return False
    if previous is None or previous.crn != current.crn:
        return True
    return (not previous.available) or current.effective_seats > previous.effective_seats


def build_notifier() -> Notifier:
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        try:
            topic = DEFAULT_NTFY_TOPIC_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            topic = None
        except OSError as exc:
            raise ValueError("could not read the local ntfy topic file") from exc
    if topic:
        return NtfyNotifier(topic=topic)
    return ConsoleNotifier()


def log_section(section: Section) -> None:
    LOGGER.info(
        "%s %s CRN %s enrollment=%d capacity=%d seats=%d waitlist=%d effective=%d "
        "openSection=%s available=%s crossList=%s crossListCapacity=%s crossListCount=%s",
        section.subject,
        section.course_number,
        section.course_reference_number,
        section.enrollment,
        section.maximum_enrollment,
        section.seats_available,
        section.wait_count,
        section.effective_seats,
        section.open_section,
        section.is_available,
        section.cross_list,
        section.cross_list_capacity,
        section.cross_list_count,
    )


async def check_once(
    *,
    client: SectionLookup,
    notifier: Notifier,
    term_code: str,
    subject: str,
    course_number: str,
    crn: str,
    previous: Availability | None,
    checked_at: datetime | None = None,
) -> Availability:
    """Run one application-level check through injected input/output ports."""

    section = await client.get_section(
        term=term_code,
        subject=subject,
        course_number=course_number,
        crn=crn,
    )
    checked_at = checked_at or datetime.now(UTC)
    log_section(section)
    current = Availability.from_section(section, checked_at=checked_at)
    if should_notify(previous, current):
        await notifier.send_opening(section, checked_at=checked_at)
        LOGGER.info("sent opening notification for CRN %s", crn)
    return current


async def run(args: argparse.Namespace) -> int:
    async with SSBClient() as client:
        if args.list_terms:
            for term in await client.get_terms():
                print(f"{term.code}\t{term.description}")
            return 0

        term = await client.resolve_term(args.term)
        LOGGER.info("resolved %s to Banner term %s", term.description, term.code)
        notifier = build_notifier()
        previous = load_state(args.state_file)
        try:
            while True:
                retry_delay = float(args.interval)
                try:
                    LOGGER.info("checking %s %s CRN %s", args.subject, args.course, args.crn)
                    current = await check_once(
                        client=client,
                        notifier=notifier,
                        term_code=term.code,
                        subject=args.subject,
                        course_number=args.course,
                        crn=args.crn,
                        previous=previous,
                    )
                    save_state(args.state_file, current)
                    previous = current
                except NotificationError as exc:
                    LOGGER.error("notification failed; state was preserved: %s", exc)
                    if args.once:
                        raise
                except RateLimitError as exc:
                    retry_delay = max(float(args.interval), exc.retry_after or args.interval * 2)
                    LOGGER.error(
                        "Temple rate limited the check; retrying in %.0f seconds", retry_delay
                    )
                    if args.once:
                        raise
                except SSBError:
                    LOGGER.exception("seat availability is unknown because the check failed")
                    if args.once:
                        raise
                    retry_delay = max(float(args.interval), 120.0)

                if args.once:
                    return 0
                await asyncio.sleep(retry_delay)
        finally:
            await notifier.close()


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LOGGER.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    # HTTPX logs full request URLs at INFO, including Banner's logical session ID.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        return await run(args)
    except (NotificationError, SSBError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
