#!/usr/bin/env python3
"""Print your Telegram chat id so you can set TELEGRAM_CHAT_ID.

Usage:
    1. Create a bot via @BotFather and note its token.
    2. Send your bot any message in Telegram (e.g. "hi").
    3. Run:  TELEGRAM_BOT_TOKEN=123:ABC... python utils/get_telegram_chat_id.py
       (or, if you already put the token in .env, this script reads that too)
"""

import os
import sys
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or token.startswith("123456789:AA"):
        sys.exit("Set TELEGRAM_BOT_TOKEN (env var or .env) to your real bot token first.")

    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    resp.raise_for_status()
    updates = resp.json().get("result", [])
    if not updates:
        sys.exit("No messages found. Send your bot a message in Telegram, then run this again.")

    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat", {})
        if chat.get("id") is not None:
            label = chat.get("username") or chat.get("title") or chat.get("first_name", "")
            chats[chat["id"]] = label

    print("Found chat id(s):")
    for cid, label in chats.items():
        print(f"  {cid}  ({label})")
    print("\nPut the one that's you into .env as TELEGRAM_CHAT_ID.")


if __name__ == "__main__":
    main()
