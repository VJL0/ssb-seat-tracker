"""CLI and Lambda orchestration for fixed-term CRN watches."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .client import TERM, SsbClient, fetch_all
from .errors import ConfigurationError, SeatTrackerError, WatchCycleError
from .models import TrackedSection
from .notifier import ConsoleNotifier, Notifier, NtfyNotifier

LOGGER = logging.getLogger("ssb_seat_tracker")
LOGGER.setLevel(logging.INFO)
DEFAULT_STATE_FILE = Path(".ssb-seat-tracker-state.json")
DEFAULT_NTFY_TOPIC_FILE = Path(".ntfy-topic")
WATCHES_TABLE_ENV = "WATCHES_TABLE_NAME"
NTFY_TOPIC_PARAMETER_ENV = "NTFY_TOPIC_PARAMETER_NAME"
DEFAULT_STACK_NAME = "ssb-seat-tracker"
WATCHES_TABLE_OUTPUT = "WatchesTableName"


class WatchRepository(Protocol):
    def list_enabled(self) -> list[TrackedSection]: ...

    def save_observation(
        self, watch: TrackedSection, *, seats_available: int, updated_at: datetime
    ) -> None: ...


@dataclass(frozen=True, slots=True)
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


class DynamoDBWatchRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    def list_enabled(self) -> list[TrackedSection]:
        return self._scan(
            {
                "FilterExpression": "#enabled = :enabled",
                "ExpressionAttributeNames": {"#enabled": "enabled"},
                "ExpressionAttributeValues": {":enabled": True},
            }
        )

    def list_all(self) -> list[TrackedSection]:
        return self._scan({})

    def _scan(self, request: dict[str, Any]) -> list[TrackedSection]:
        watches: list[TrackedSection] = []
        while True:
            response = self._table.scan(**request)
            for item in response.get("Items", []):
                updated_at = item.get("updated_at")
                watches.append(
                    TrackedSection(
                        crn=item["crn"],
                        enabled=item.get("enabled", True),
                        seats_available=item.get("seats_available"),
                        updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
                    )
                )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return watches
            request["ExclusiveStartKey"] = last_key

    def add(self, crn: str) -> bool:
        try:
            self._table.put_item(
                Item={"crn": crn, "enabled": True},
                ConditionExpression="attribute_not_exists(crn)",
            )
        except ClientError as exc:
            if _is_conditional_check_failure(exc):
                return False
            raise
        return True

    def set_enabled(self, crn: str, *, enabled: bool) -> bool:
        try:
            self._table.update_item(
                Key={"crn": crn},
                UpdateExpression="SET #enabled = :enabled",
                ConditionExpression="attribute_exists(crn)",
                ExpressionAttributeNames={"#enabled": "enabled"},
                ExpressionAttributeValues={":enabled": enabled},
            )
        except ClientError as exc:
            if _is_conditional_check_failure(exc):
                return False
            raise
        return True

    def remove(self, crn: str) -> bool:
        try:
            self._table.delete_item(
                Key={"crn": crn},
                ConditionExpression="attribute_exists(crn)",
            )
        except ClientError as exc:
            if _is_conditional_check_failure(exc):
                return False
            raise
        return True

    def save_observation(
        self, watch: TrackedSection, *, seats_available: int, updated_at: datetime
    ) -> None:
        self._table.update_item(
            Key={"crn": watch.crn},
            UpdateExpression="SET seats_available = :seats, updated_at = :updated_at",
            ConditionExpression="attribute_exists(crn)",
            ExpressionAttributeValues={
                ":seats": seats_available,
                ":updated_at": updated_at.isoformat(),
            },
        )


class LocalWatchRepository:
    def __init__(self, path: Path, crns: list[str]) -> None:
        self._path = path
        self._crns = crns
        self._state = load_state(path)

    def list_enabled(self) -> list[TrackedSection]:
        return [self._state.get(crn, TrackedSection(crn=crn)) for crn in self._crns]

    def save_observation(
        self, watch: TrackedSection, *, seats_available: int, updated_at: datetime
    ) -> None:
        self._state[watch.crn] = TrackedSection(
            crn=watch.crn,
            seats_available=seats_available,
            updated_at=updated_at,
        )
        save_state(self._path, self._state)


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    LOGGER.log(level, json.dumps({"event": event, **fields}, default=str, sort_keys=True))


def should_notify(previous_seats: int | None, current_seats: int) -> bool:
    return previous_seats is not None and previous_seats <= 0 < current_seats


async def run_watch_cycle(
    *,
    client: SsbClient,
    notifier: Notifier,
    repository: WatchRepository,
    checked_at: datetime | None = None,
) -> CycleResult:
    watches = repository.list_enabled()
    checked_at = checked_at or datetime.now(UTC)
    fetched = await fetch_all(client, term=TERM, crns=[watch.crn for watch in watches])
    checked = notified = failed = 0

    for watch in watches:
        info = fetched[watch.crn]
        if isinstance(info, BaseException):
            failed += 1
            log_event(
                "watch_failed",
                level=logging.ERROR,
                crn=watch.crn,
                reason=type(info).__name__,
            )
            continue
        try:
            sent = should_notify(watch.seats_available, info.seats_available)
            if sent:
                await notifier.send_opening(watch.crn, info, checked_at=checked_at)
            repository.save_observation(
                watch,
                seats_available=info.seats_available,
                updated_at=checked_at,
            )
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
            seats_available=info.seats_available,
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
        raise ConfigurationError(f"required environment variable {name} is not configured")
    return value


def _is_conditional_check_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _resolve_watches_table_name(*, table_name: str | None, stack_name: str) -> str:
    if table_name and table_name.strip():
        return table_name.strip()
    if configured := os.getenv(WATCHES_TABLE_ENV, "").strip():
        return configured

    response = boto3.client("cloudformation").describe_stacks(StackName=stack_name)
    stacks = response.get("Stacks", [])
    outputs = stacks[0].get("Outputs", []) if stacks else []
    for output in outputs:
        if output.get("OutputKey") == WATCHES_TABLE_OUTPUT:
            value = output.get("OutputValue", "").strip()
            if value:
                return value
    raise ConfigurationError(f"stack {stack_name!r} does not have a {WATCHES_TABLE_OUTPUT} output")


async def run_lambda_cycle() -> CycleResult:
    table_name = _required_environment(WATCHES_TABLE_ENV)
    parameter_name = _required_environment(NTFY_TOPIC_PARAMETER_ENV)
    response = _ssm_client().get_parameter(Name=parameter_name, WithDecryption=True)
    topic = response["Parameter"]["Value"].strip()
    repository = DynamoDBWatchRepository(_dynamodb_resource().Table(table_name))
    notifier = NtfyNotifier(topic=topic, server_url=os.getenv("NTFY_BASE_URL", "https://ntfy.sh"))
    try:
        async with SsbClient() as client:
            return await run_watch_cycle(client=client, notifier=notifier, repository=repository)
    finally:
        await notifier.close()


def lambda_handler(_event: dict[str, object], _context: object) -> dict[str, int]:
    return asyncio.run(run_lambda_cycle()).as_dict()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Monitor Temple term {TERM} for one or more CRN seat openings."
    )
    parser.add_argument("--crn", nargs="+", help="one or more CRNs")
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
    commands = parser.add_subparsers(dest="command")
    watch = commands.add_parser("watch", help="manage production watches in DynamoDB")
    watch.add_argument(
        "--table-name",
        help=f"DynamoDB table name (defaults to ${WATCHES_TABLE_ENV} or a stack output)",
    )
    watch.add_argument(
        "--stack-name",
        default=DEFAULT_STACK_NAME,
        help=f"CloudFormation stack used to discover the table (default: {DEFAULT_STACK_NAME})",
    )
    watch_commands = watch.add_subparsers(dest="watch_command", required=True)
    for action in ("add", "enable", "disable", "remove"):
        action_parser = watch_commands.add_parser(action)
        action_parser.add_argument("crn", help="Temple course reference number")
    watch_commands.add_parser("list")
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command == "watch":
        crn = getattr(args, "crn", None)
        if crn is not None and not crn.isdigit():
            parser.error("CRN must contain only digits")
        return
    if not args.crn:
        parser.error("--crn is required unless using the watch command")
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    crns = list(dict.fromkeys(crn.strip() for crn in args.crn))
    if any(not crn.isdigit() for crn in crns):
        parser.error("every --crn value must contain only digits")
    args.crn = crns


def _print_watches(watches: list[TrackedSection]) -> None:
    rows = [
        (
            watch.crn,
            "yes" if watch.enabled else "no",
            "—" if watch.seats_available is None else str(watch.seats_available),
            "—" if watch.updated_at is None else watch.updated_at.isoformat(),
        )
        for watch in sorted(watches, key=lambda item: (int(item.crn), item.crn))
    ]
    headers = ("CRN", "Enabled", "Seats", "Last checked")
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def run_watch_command(args: argparse.Namespace) -> int:
    try:
        table_name = _resolve_watches_table_name(
            table_name=args.table_name,
            stack_name=args.stack_name,
        )
        repository = DynamoDBWatchRepository(_dynamodb_resource().Table(table_name))
        action = args.watch_command
        if action == "list":
            _print_watches(repository.list_all())
            return 0
        if action == "add":
            if repository.add(args.crn):
                print(f"Added CRN {args.crn}")
                return 0
            print(f"CRN {args.crn} is already being tracked")
            return 1
        if action in {"enable", "disable"}:
            enabled = action == "enable"
            if repository.set_enabled(args.crn, enabled=enabled):
                print(f"{'Enabled' if enabled else 'Disabled'} CRN {args.crn}")
                return 0
            print(f"CRN {args.crn} is not being tracked")
            return 1
        if repository.remove(args.crn):
            print(f"Removed CRN {args.crn}")
            return 0
        print(f"CRN {args.crn} is not being tracked")
        return 1
    except (BotoCoreError, ClientError) as exc:
        raise ConfigurationError(
            "AWS could not manage the watch; check your credentials, region, and table access"
        ) from exc


def load_state(path: Path) -> dict[str, TrackedSection]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        return {
            crn: TrackedSection(
                crn=crn,
                seats_available=value["seats_available"],
                updated_at=datetime.fromisoformat(value["updated_at"]),
            )
            for crn, value in payload.items()
        }
    except FileNotFoundError:
        return {}
    except (KeyError, OSError, TypeError, ValueError) as exc:
        LOGGER.warning("ignoring unreadable state file %s: %s", path, type(exc).__name__)
        return {}


def save_state(path: Path, state: dict[str, TrackedSection]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        crn: {
            "seats_available": watch.seats_available,
            "updated_at": watch.updated_at.isoformat() if watch.updated_at else None,
        }
        for crn, watch in state.items()
    }
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def build_notifier() -> Notifier:
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        try:
            topic = DEFAULT_NTFY_TOPIC_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            topic = None
        except OSError as exc:
            raise ConfigurationError("could not read the local ntfy topic file") from exc
    return NtfyNotifier(topic=topic) if topic else ConsoleNotifier()


async def run(args: argparse.Namespace) -> int:
    notifier = build_notifier()
    repository = LocalWatchRepository(args.state_file, args.crn)
    try:
        async with SsbClient() as client:
            while True:
                try:
                    await run_watch_cycle(client=client, notifier=notifier, repository=repository)
                except WatchCycleError:
                    if args.once:
                        raise
                    LOGGER.exception("one or more CRN checks failed; retrying next cycle")
                if args.once:
                    return 0
                await asyncio.sleep(args.interval)
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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.command == "watch":
            return run_watch_command(args)
        return await run(args)
    except SeatTrackerError as exc:
        LOGGER.error("%s", exc)
        return 1


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
