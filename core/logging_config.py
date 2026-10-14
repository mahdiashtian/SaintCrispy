"""Couplyo-style system, Telegram and activity streams with bounded queues."""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from core.diagnostics import error_fields

from .log_writer import JsonLogWriter
from .request_context import request_chat, request_id, request_user

SENSITIVE_FIELDS = {
    "cookie",
    "cookies",
    "authorization",
    "password",
    "token",
    "api_hash",
    "bot_token",
    "headers",
    "url",
    "proxy",
    "message",
    "stderr",
    "credential",
    "credentials",
}


def safe_payload(value, depth=0):
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if str(key).lower() in SENSITIVE_FIELDS
            else safe_payload(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_payload(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return "[redacted]" if "://" in value else value[:2048]
    return value if value is None or isinstance(value, (int, float, bool)) else type(value).__name__


class SafeQueueHandler(logging.Handler):
    def __init__(self, writers):
        super().__init__()
        self.writers = writers

    def emit(self, record):
        # Library messages and formatted exception strings may contain secrets.
        payload = (
            safe_payload(record.msg) if isinstance(record.msg, dict) else {"event": "library_log"}
        )
        payload.update(
            timestamp=datetime.fromtimestamp(record.created, UTC).isoformat(),
            level=record.levelname,
            logger=record.name,
        )
        if record.exc_info and record.exc_info[1]:
            payload.update(error_fields(record.exc_info[1]))
        if request_id.get():
            payload["request_id"] = request_id.get()
        if request_user.get() is not None:
            payload["user_id"] = request_user.get()
        if request_chat.get() is not None:
            payload["chat_id"] = request_chat.get()
        stream = "activity" if record.name.split(".", 1)[0] == "activity" else "system"
        self.writers[stream].emit(payload)
        if record.name.startswith(("telethon", "telegram")):
            self.writers["telegram"].emit(payload)


class ApplicationLogging:
    def __init__(self, settings):
        directory = Path(settings.log_file).parent if settings.log_file else Path("logs")
        self.writers = {
            name: JsonLogWriter(
                str(directory / f"{name}.log"),
                settings.log_max_bytes,
                settings.log_backups,
                settings.log_queue_size,
                stdout=name == "system" and settings.log_stdout,
                readable=True,
                daily=True,
            )
            for name in ("system", "telegram", "activity")
        }
        self.handler = SafeQueueHandler(self.writers)
        self.root = logging.getLogger()
        self.previous_handlers = None
        self.previous_level = None
        self.telethon = logging.getLogger("telethon")
        self.previous_telegram_level = None

    async def __aenter__(self):
        self.previous_handlers = self.root.handlers[:]
        self.previous_level = self.root.level
        self.previous_telegram_level = self.telethon.level
        for writer in self.writers.values():
            writer.start()
        self.root.handlers = [self.handler]
        self.root.setLevel(logging.INFO)
        self.telethon.setLevel(logging.WARNING)
        return self

    async def __aexit__(self, *args):
        self.root.handlers = self.previous_handlers
        self.root.setLevel(self.previous_level)
        self.telethon.setLevel(self.previous_telegram_level)
        await asyncio.gather(*(asyncio.to_thread(writer.close) for writer in self.writers.values()))
