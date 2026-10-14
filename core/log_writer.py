"""Bounded JSON logging with all serialization and disk I/O in one worker thread."""

import json
import logging
import queue
import sys
import threading
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RaisingFileHandler(RotatingFileHandler):
    def handleError(self, record):
        # Let the worker account for disk failures without printing sensitive records.
        raise


class DailyFileHandler(RaisingFileHandler):
    """Rotate on UTC day changes and on size; retain dated, unique archives."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.day = datetime.now(UTC).date()
        if self.stream and self.stream.tell():
            self.day = datetime.fromtimestamp(Path(self.baseFilename).stat().st_mtime, UTC).date()

    def shouldRollover(self, record):
        return self.day != datetime.now(UTC).date() or super().shouldRollover(record)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        path = Path(self.baseFilename)
        if path.exists() and path.stat().st_size:
            stamp = datetime.now(UTC).strftime("%H%M%S%f")
            path.replace(path.with_name(f"{path.name}.{self.day.isoformat()}.{stamp}"))
        self.day = datetime.now(UTC).date()
        archives = sorted(path.parent.glob(path.name + ".????-??-??.*"))
        for archive in archives[: -self.backupCount]:
            archive.unlink()
        if not self.delay:
            self.stream = self._open()


def readable_record(record: dict) -> str:
    source = str(record.get("logger", "system"))
    for prefix in ("downloader_bot.", "bot.handlers.", "downloaders."):
        source = source.removeprefix(prefix)
    source = source if len(source) <= 18 else "…" + source[-17:]
    stamp = str(record.get("timestamp", ""))[:23].replace("T", " ")
    fields = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in record.items()
        if key not in {"timestamp", "level", "logger", "event"}
    )
    return (
        f"{stamp} | {record.get('level', 'INFO'):<7} | {source:<18} | "
        f"{record.get('event', 'event')} {fields}"
    ).rstrip()


class JsonLogWriter:
    def __init__(
        self,
        path: str | None,
        max_bytes: int = 20 * 1024 * 1024,
        backups: int = 10,
        capacity: int = 10000,
        stdout: bool = True,
        *,
        readable: bool = False,
        daily: bool = False,
    ):
        if max_bytes < 1 or backups < 1 or capacity < 1:
            raise ValueError("Log rotation and queue limits must be positive")
        self.path = Path(path) if path else None
        self.max_bytes = max_bytes
        self.backups = backups
        self.stdout = stdout
        self.readable = readable
        self.daily = daily
        self.queue = queue.Queue(maxsize=capacity)
        self.dropped = 0
        self.write_errors = 0
        self._closed = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None or self._closed.is_set():
            raise RuntimeError("Log writer can only be started once")
        self._thread = threading.Thread(target=self._run, name="json-log-writer", daemon=True)
        self._thread.start()

    def emit(self, record: dict) -> None:
        if self._closed.is_set():
            self.dropped += 1
            return
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            # Never block a transfer because its log destination cannot keep up.
            self.dropped += 1

    def close(self):
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self):
        handler = None
        retry_at = 0.0
        try:
            while not self._closed.is_set() or not self.queue.empty():
                try:
                    record = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                missing = False
                try:
                    line = (
                        readable_record(record)
                        if self.readable
                        else json.dumps(record, ensure_ascii=True, separators=(",", ":"))
                    )
                    if self.path is not None:
                        try:
                            if handler is None and time.monotonic() >= retry_at:
                                self.path.parent.mkdir(parents=True, exist_ok=True)
                                handler_type = (
                                    DailyFileHandler if self.daily else RaisingFileHandler
                                )
                                handler = handler_type(
                                    self.path,
                                    maxBytes=self.max_bytes,
                                    backupCount=self.backups,
                                    encoding="utf-8",
                                )
                                handler.setFormatter(logging.Formatter("%(message)s"))
                            if handler is not None:
                                handler.handle(logging.makeLogRecord({"msg": line}))
                            else:
                                missing = True
                        except (OSError, UnicodeError):
                            self.write_errors += 1
                            missing = True
                            retry_at = time.monotonic() + 5
                            if handler is not None:
                                try:
                                    handler.close()
                                except OSError:
                                    pass
                                handler = None
                    if self.stdout:
                        try:
                            print(line, file=sys.stdout, flush=True)
                        except (OSError, UnicodeError):
                            self.write_errors += 1
                            missing = True
                except (ValueError, TypeError):
                    self.write_errors += 1
                    missing = True
                finally:
                    self.dropped += int(missing)
                    self.queue.task_done()
        finally:
            if handler is not None:
                try:
                    handler.close()
                except OSError:
                    self.write_errors += 1
