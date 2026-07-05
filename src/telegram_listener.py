"""
On-demand manual run: long-poll Telegram for a command and reply with the
current availability summary. There is no long-running daemon — cron fires this
once a minute and it long-polls for ~a minute, so a command gets answered within
seconds (Telegram holds the connection open until a message arrives) rather than
waiting for the next cron tick.

You message the bot (e.g. "/run") and it replies with every currently-open
segment, ignoring the notification dedup state entirely.
"""

import json
import logging
import os
import time
from datetime import datetime
from html import escape

import requests

from src.notifier import Notifier
from src.permit_checker import PermitChecker, recreation_permit_url

logger = logging.getLogger(__name__)

# Recognized commands (the leading "/" is optional; "@botname" suffix is stripped).
TRIGGER_WORDS = {"run", "check", "status", "/run", "/check", "/status"}

# Ignore commands older than this so a redeploy/backlog can't replay stale ones.
MAX_AGE_SECONDS = 600

# How long Telegram holds an idle getUpdates open before returning empty.
LONG_POLL_TIMEOUT = 50
# Keep long-polling for ~this long per invocation, then exit; cron restarts us
# the next minute for near-continuous coverage.
RUN_WINDOW_SECONDS = 55


def poll_once(app_config, permits_config, offset_path):
    """Long-poll Telegram for ~a minute, replying to each manual-run command."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        logger.error("Telegram poll: missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
        return

    deadline = time.monotonic() + RUN_WINDOW_SECONDS
    offset = _load_offset(offset_path)

    while True:
        # Cap the long poll to whatever time is left so we exit near the deadline
        # (flock keeps the next cron tick from overlapping while we finish).
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        poll_timeout = max(1, min(LONG_POLL_TIMEOUT, int(remaining)))

        params = {"timeout": poll_timeout}
        if offset is not None:
            params["offset"] = offset

        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params=params,
                timeout=poll_timeout + 10,
            )
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except requests.RequestException as e:
            logger.error(f"Telegram getUpdates failed: {e}")
            return

        if not updates:
            continue  # long poll timed out with no message; loop until the deadline

        triggered = False
        now = time.time()
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            # Only respond to the configured owner chat.
            if str((message.get("chat") or {}).get("id")) != str(chat_id):
                continue
            text = (message.get("text") or "").strip().lower()
            command = text.split()[0].split("@")[0] if text else ""
            if command in TRIGGER_WORDS and (now - message.get("date", 0)) <= MAX_AGE_SECONDS:
                triggered = True

        # Acknowledge consumed updates so they aren't returned again.
        _save_offset(offset_path, offset)

        if triggered:
            _run_and_reply(app_config, permits_config)


def _run_and_reply(app_config, permits_config):
    """Run a full check ignoring dedup state and reply with everything open."""
    logger.info("Manual run requested via Telegram.")
    # On-demand runs should feel snappy: shrink the polite inter-request delay.
    app_config.setdefault("request", {})["delay"] = {"min": 1, "max": 2}
    app_config["request"]["jitter"] = 0

    notifier = Notifier(app_config)
    notifier.send_telegram_raw("On it, checking all permits now...")

    # No state_path: a manual run ignores dedup and reports everything currently open.
    checker = PermitChecker(app_config, permits_config, notifier)
    notifier.send_telegram_raw(
        _format_summary(checker.collect_open_segments()),
        parse_mode="HTML",
    )


def _format_summary(open_segments):
    if not open_segments:
        return "<b>No openings right now</b>\nNo watched permit has availability."

    lines = [
        "<b>Current openings</b>",
        f"{len(open_segments)} segment(s) with availability",
    ]
    for segment in open_segments:
        permit_name = segment["permit_name"]
        segment_name = segment["segment"]
        dates = segment["dates"]
        remaining = segment.get("remaining", {})
        people = segment.get("people", 1)
        permit_id = segment.get("permit_id")
        lines.extend([
            "",
            f"<b>{escape(permit_name)}</b>",
            f"<i>{escape(segment_name)}</i>",
            f"Party size: {escape(str(people))}",
            _format_date_chips(dates, remaining),
        ])
        if permit_id:
            url = escape(recreation_permit_url(permit_id), quote=True)
            lines.append(f'<a href="{url}">Open recreation.gov</a>')
    return "\n".join(lines)


def _format_date_chips(dates, remaining=None):
    remaining = remaining or {}
    chips = []
    for d in dates:
        label = _compact_date(d)
        rem = remaining.get(d)
        if rem is not None:
            label = f"{label} ({rem})"
        chips.append(f"<code>{escape(label)}</code>")
    return " ".join(chips)


def _compact_date(value):
    d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    return f"{d.strftime('%a')} {d.month}/{d.day}/{d.strftime('%y')}"


def _load_offset(path):
    try:
        return json.loads(path.read_text()).get("offset")
    except (OSError, ValueError):
        return None


def _save_offset(path, offset):
    try:
        path.write_text(json.dumps({"offset": offset}))
    except OSError as e:
        logger.warning(f"Could not save Telegram offset to {path}: {e}")
