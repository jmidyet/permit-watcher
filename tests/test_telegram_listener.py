from src.telegram_listener import _format_summary


def test_format_summary_uses_telegram_html():
    message = _format_summary([
        {
            "permit_name": "Rae Lakes & Friends",
            "permit_id": "445857",
            "segment": "Woods < Creek",
            "dates": ["2026-07-01", "2026-07-02"],
            "people": 2,
        }
    ])

    assert "<b>Current openings</b>" in message
    assert "Rae Lakes &amp; Friends" in message
    assert "Woods &lt; Creek" in message
    assert "<code>Wed 7/1/26</code>" in message
    assert '<a href="https://www.recreation.gov/permits/445857">' in message


def test_format_summary_empty_state_is_html():
    assert _format_summary([]) == (
        "<b>No openings right now</b>\nNo watched permit has availability."
    )
