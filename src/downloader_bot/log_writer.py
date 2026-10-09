"""Bounded JSON logging with all serialization and disk I/O in one worker thread."""

import json
import logging
import queue
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RaisingFileHandler(RotatingFileHandler):
    def handleError(self, record):
        # Let the worker account for disk failures without printing sensitive records.
        raise


class JsonLogWriter:
    def __init__(
        self,
        path: str | None,
        max_bytes: int = 20 * 1024 * 1024,
        backups: int = 10,
        capacity: int = 10000,
        stdout: bool = True,
    ):
        if max_bytes < 1 or backups < 1 or capacity < 1:
            raise ValueError("Log rotation and queue limits must be positive")
        self.path = Path(path) if path else None
        self.max_bytes = max_bytes
        self.backups = backups
        self.stdout = stdout
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
                    line = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
                    if self.path is not None:
                        try:
                            if handler is None and time.monotonic() >= retry_at:
                                self.path.parent.mkdir(parents=True, exist_ok=True)
                                handler = RaisingFileHandler(
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
                        except OSError:
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
                        except OSError:
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
