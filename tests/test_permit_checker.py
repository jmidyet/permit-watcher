import json
from datetime import date

from src.permit_checker import PermitChecker, format_permit_date


class StubNotifier:
    def __init__(self):
        self.messages = []

    def send_notification(self, subject, message, **kwargs):
        self.messages.append((subject, message, kwargs))


def _checker(tmp_path, state=None):
    state_path = tmp_path / "state.json"
    if state is not None:
        state_path.write_text(json.dumps(state))
    notifier = StubNotifier()
    checker = PermitChecker(
        {"app": {"randomize_user_agent": False}},
        {},
        notifier,
        state_path=state_path,
    )
    return checker, notifier, state_path


def test_format_permit_date_includes_weekday():
    assert format_permit_date(date(2026, 8, 13)) == "Thursday, 8/13/26"


def test_notification_marks_new_dates_and_formats_all_open_dates(tmp_path):
    checker, notifier, state_path = _checker(
        tmp_path,
        {
            "250014_371": {
                "current_dates": ["2026-07-25"],
                "alerted_dates": ["2026-07-25"],
            }
        },
    )
    permit = {
        "id": "250014",
        "name": "Dinosaur - Yampa River",
        "people": 6,
        "division_names": {"371": "Deerlodge Park - Yampa River"},
    }

    checker._maybe_notify_segment(
        permit,
        "371",
        [date(2026, 6, 13), date(2026, 7, 25)],
    )

    assert len(notifier.messages) == 1
    message = notifier.messages[0][1]
    telegram_kwargs = notifier.messages[0][2]
    assert "New date(s) [1]:\n- Saturday, 6/13/26" in message
    assert "- Saturday, 6/13/26 (NEW)" in message
    assert "- Saturday, 7/25/26" in message
    assert telegram_kwargs["telegram_parse_mode"] == "HTML"
    assert "<b>New dates [1]</b>" in telegram_kwargs["telegram_message"]
    assert "inline_keyboard" in telegram_kwargs["telegram_reply_markup"]

    state = json.loads(state_path.read_text())
    assert state["250014_371"]["current_dates"] == ["2026-06-13", "2026-07-25"]
    assert state["250014_371"]["alerted_dates"] == ["2026-06-13", "2026-07-25"]


def test_closed_segment_keeps_alert_history_to_prevent_flicker_duplicates(tmp_path):
    checker, notifier, state_path = _checker(
        tmp_path,
        {
            "250014_380": {
                "current_dates": ["2026-06-13"],
                "alerted_dates": ["2026-06-13"],
            }
        },
    )
    permit = {
        "id": "250014",
        "name": "Dinosaur - Gates of Lodore",
        "division_names": {"380": "Gates of Lodore - Green River"},
    }

    checker._maybe_notify_segment(permit, "380", [])
    checker._maybe_notify_segment(permit, "380", [date(2026, 6, 13)])

    assert notifier.messages == []
    state = json.loads(state_path.read_text())
    assert state["250014_380"]["current_dates"] == ["2026-06-13"]
    assert state["250014_380"]["alerted_dates"] == ["2026-06-13"]
