"""Pure bot identity/deep-link formatting, independent of Telegram events."""

import re
from urllib.parse import urlsplit


def normalize_bot_username(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith(("https://t.me/", "http://t.me/")):
        value = urlsplit(value).path.strip("/")
    value = value.removeprefix("@").removeprefix("t.me/")
    return value if re.fullmatch(r"[A-Za-z0-9_]{5,32}", value) else None


def bot_handle(value: str | None) -> str:
    username = normalize_bot_username(value)
    return f"@{username}" if username else "این ربات"
