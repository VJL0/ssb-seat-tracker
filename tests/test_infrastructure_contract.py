from pathlib import Path

from ssb_seat_tracker.main import NTFY_TOPIC_PARAMETER_ENV, WATCHES_TABLE_ENV

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_lambda_environment_names_match_sam_template() -> None:
    template = (PROJECT_ROOT / "template.yaml").read_text(encoding="utf-8")

    assert f"{WATCHES_TABLE_ENV}:" in template
    assert f"{NTFY_TOPIC_PARAMETER_ENV}:" in template


def test_sam_handler_matches_application_entry_point() -> None:
    template = (PROJECT_ROOT / "template.yaml").read_text(encoding="utf-8")

    assert "Handler: ssb_seat_tracker.main.lambda_handler" in template
