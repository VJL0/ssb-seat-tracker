from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from botocore.exceptions import ClientError

from ssb_seat_tracker.main import (
    DynamoDBWatchRepository,
    _print_watches,
    build_parser,
    run_watch_command,
    validate_args,
)
from ssb_seat_tracker.models import TrackedSection


def conditional_failure(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "condition failed"}},
        operation,
    )


def test_add_uses_conditional_put_and_does_not_overwrite() -> None:
    table = Mock()
    repository = DynamoDBWatchRepository(table)

    assert repository.add("53150") is True
    table.put_item.assert_called_once_with(
        Item={"crn": "53150", "enabled": True},
        ConditionExpression="attribute_not_exists(crn)",
    )

    table.put_item.side_effect = conditional_failure("PutItem")
    assert repository.add("53150") is False


def test_enable_and_remove_require_an_existing_watch() -> None:
    table = Mock()
    repository = DynamoDBWatchRepository(table)

    assert repository.set_enabled("53150", enabled=False) is True
    table.update_item.assert_called_once_with(
        Key={"crn": "53150"},
        UpdateExpression="SET #enabled = :enabled",
        ConditionExpression="attribute_exists(crn)",
        ExpressionAttributeNames={"#enabled": "enabled"},
        ExpressionAttributeValues={":enabled": False},
    )

    table.delete_item.side_effect = conditional_failure("DeleteItem")
    assert repository.remove("99999") is False


def test_list_all_reads_every_page() -> None:
    table = Mock()
    table.scan.side_effect = [
        {
            "Items": [{"crn": "53150", "enabled": True, "seats_available": 0}],
            "LastEvaluatedKey": {"crn": "53150"},
        },
        {
            "Items": [
                {
                    "crn": "31752",
                    "enabled": False,
                    "seats_available": 2,
                    "updated_at": "2026-08-26T12:00:00+00:00",
                }
            ]
        },
    ]

    watches = DynamoDBWatchRepository(table).list_all()

    assert [watch.crn for watch in watches] == ["53150", "31752"]
    assert watches[1].updated_at == datetime(2026, 8, 26, 12, tzinfo=UTC)
    assert table.scan.call_args_list[0].kwargs == {}
    assert table.scan.call_args_list[1].kwargs == {
        "ExclusiveStartKey": {"crn": "53150"},
    }


def test_list_enabled_filters_disabled_watches() -> None:
    table = Mock()
    table.scan.return_value = {"Items": [{"crn": "53150", "enabled": True}]}

    assert [watch.crn for watch in DynamoDBWatchRepository(table).list_enabled()] == ["53150"]
    table.scan.assert_called_once_with(
        FilterExpression="#enabled = :enabled",
        ExpressionAttributeNames={"#enabled": "enabled"},
        ExpressionAttributeValues={":enabled": True},
    )


def test_watch_parser_and_crn_validation() -> None:
    parser = build_parser()
    args = parser.parse_args(["watch", "add", "53150"])

    validate_args(parser, args)

    assert args.watch_command == "add"
    assert args.crn == "53150"


def test_watch_command_reports_duplicate(monkeypatch, capsys) -> None:
    repository = Mock()
    repository.add.return_value = False
    monkeypatch.setattr(
        "ssb_seat_tracker.main._resolve_watches_table_name", lambda **_kwargs: "table"
    )
    resource = Mock()
    resource.Table.return_value = repository
    monkeypatch.setattr("ssb_seat_tracker.main._dynamodb_resource", lambda: resource)
    monkeypatch.setattr("ssb_seat_tracker.main.DynamoDBWatchRepository", lambda _table: repository)
    args = SimpleNamespace(
        table_name=None,
        stack_name="ssb-seat-tracker",
        watch_command="add",
        crn="53150",
    )

    assert run_watch_command(args) == 1
    assert capsys.readouterr().out == "CRN 53150 is already being tracked\n"


def test_print_watches_sorts_and_formats(capsys) -> None:
    _print_watches(
        [
            TrackedSection(crn="53150", enabled=False),
            TrackedSection(crn="31752", seats_available=2),
        ]
    )

    output = capsys.readouterr().out
    assert output.index("31752") < output.index("53150")
    assert "yes" in output
    assert "no" in output
    assert "2" in output
