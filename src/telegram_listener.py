"""
On-demand manual run: poll Telegram for a command and reply with the current
availability summary. Designed to be invoked once per cron tick (short poll),
so there is no long-running daemon.

You message the bot (e.g. "/run") and it replies with every currently-open
segment, ignoring the notification dedup state entirely.
"""

import json
import logging
import os
import time

import requests

from src.notifier import Notifier
from src.permit_checker import PermitChecker

logger = logging.getLogger(__name__)

# Recognized commands (the leading "/" is optional; "@botname" suffix is stripped).
TRIGGER_WORDS = {"run", "check", "status", "/run", "/check", "/status"}

# Ignore commands older than this so a redeploy/backlog can't replay stale ones.
MAX_AGE_SECONDS = 600


def poll_once(app_config, permits_config, offset_path):
    """Check Telegram once for a manual-run command; reply if one is found."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        logger.error("Telegram poll: missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
        return

    offset = _load_offset(offset_path)
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=20
        )
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except requests.RequestException as e:
        logger.error(f"Telegram getUpdates failed: {e}")
        return

    triggered = False
    last_offset = offset
    now = time.time()
    for update in updates:
        last_offset = update["update_id"] + 1
        message = update.get("message") or {}
        # Only respond to the configured owner chat.
        if str((message.get("chat") or {}).get("id")) != str(chat_id):
            continue
        text = (message.get("text") or "").strip().lower()
        command = text.split()[0].split("@")[0] if text else ""
        if command in TRIGGER_WORDS and (now - message.get("date", 0)) <= MAX_AGE_SECONDS:
            triggered = True

    # Acknowledge consumed updates so they aren't returned again.
    if last_offset is not None:
        _save_offset(offset_path, last_offset)

    if not triggered:
        return

    logger.info("Manual run requested via Telegram.")
    # On-demand runs should feel snappy: shrink the polite inter-request delay.
    app_config.setdefault("request", {})["delay"] = {"min": 1, "max": 2}
    app_config["request"]["jitter"] = 0

    notifier = Notifier(app_config)
    notifier.send_telegram_raw("On it — checking all permits now…")

    # No state_path: a manual run ignores dedup and reports everything currently open.
    checker = PermitChecker(app_config, permits_config, notifier)
    notifier.send_telegram_raw(_format_summary(checker.collect_open_segments()))


def _format_summary(open_segments):
    if not open_segments:
        return "No openings right now for any watched permit. \U0001F937"
    lines = ["Current openings:"]
    for name, segment, dates in open_segments:
        lines.append(f"\n• {name} — {segment}\n  {', '.join(dates)}")
    return "\n".join(lines)


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
