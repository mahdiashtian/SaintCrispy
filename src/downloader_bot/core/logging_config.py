"""Couplyo-style system, Telegram and activity streams with bounded queues."""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from downloader_bot.core.diagnostics import error_fields

from .log_writer import JsonLogWriter
from .request_context import request_chat, request_id, request_user


class SafeQueueHandler(logging.Handler):
    def __init__(self, writers):
        super().__init__()
        self.writers = writers

    def emit(self, record):
        # Library messages and formatted exception strings may contain secrets.
        payload = dict(record.msg) if isinstance(record.msg, dict) else {"event": "library_log"}
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
        stream = "activity" if record.name == "activity" else "system"
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
