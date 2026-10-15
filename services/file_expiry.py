"""Deadline, persistence and retry policy for confirmed downloaded-file sends."""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from core.constants import FILE_DELETE_SECONDS
from core.diagnostics import error_fields
from repositories.interfaces.file_expiry import (
    ExpiryRepository,
    MessageDeletion,
    OriginInvalidation,
)
from schemas.expiry import DeletionRetryAfter

log = logging.getLogger("activity")


class FileExpiryService:
    def __init__(
        self,
        repository: ExpiryRepository,
        files: OriginInvalidation,
        deletion: MessageDeletion,
        *,
        clock=None,
    ):
        self.repository, self.files, self.deletion = repository, files, deletion
        self.clock = clock or (lambda: datetime.now(UTC))
        self._pending = {}
        self._deleted = set()
        self._slots = asyncio.Semaphore(4)
        self._retry_at = 0.0

    async def schedule(self, chat_id: int, message_id: int, peer: bytes):
        if (
            type(chat_id) is not int
            or not chat_id
            or type(message_id) is not int
            or message_id <= 0
        ):
            raise ValueError("Confirmed message and chat identities are required")
        now = self.clock()
        row = {
            "chat_id": chat_id,
            "message_id": message_id,
            "peer": peer,
            "delete_at": now + timedelta(seconds=FILE_DELETE_SECONDS),
            "next_attempt_at": now + timedelta(seconds=FILE_DELETE_SECONDS),
            "attempts": 0,
        }
        row = self._pending.setdefault((row["chat_id"], message_id), row)
        await self._persist(row)

    async def _persist(self, row):
        try:
            async with asyncio.timeout(3):
                await self.repository.add(
                    row["chat_id"], row["message_id"], row["peer"], row["delete_at"]
                )
            self._pending.pop((row["chat_id"], row["message_id"]), None)
        except Exception as error:
            # A delivered file must never be sent again because cleanup storage failed.
            log.warning({"event": "file_expiry_persist_failed", **error_fields(error)})

    async def flush(self):
        try:
            async with asyncio.timeout(3):
                for row in list(self._pending.values())[:100]:
                    await self._persist(row)
        except TimeoutError:
            pass

    async def run_once(self):
        now = self.clock()
        rows = {key: row for key, row in self._pending.items() if row["next_attempt_at"] <= now}
        try:
            async with asyncio.timeout(3):
                rows.update(
                    {(r["chat_id"], r["message_id"]): dict(r) for r in await self.repository.due()}
                )
        except Exception as error:
            log.warning({"event": "file_expiry_read_failed", **error_fields(error)})
        groups = defaultdict(list)
        for row in rows.values():
            groups[bytes(row["peer"])].append(row)
        await asyncio.gather(
            *(
                self._delete(peer, group[offset : offset + 100])
                for peer, group in groups.items()
                for offset in range(0, len(group), 100)
            )
        )
        await self.flush()

    async def _delete(self, peer, rows):
        async with self._slots:
            if time.monotonic() < self._retry_at:
                return
            keys = {(row["chat_id"], row["message_id"]) for row in rows}
            completed = set()
            try:
                remaining = [
                    row["message_id"]
                    for row in rows
                    if (row["chat_id"], row["message_id"]) not in self._deleted
                ]
                if remaining:
                    async with asyncio.timeout(5):
                        await self.deletion.delete(peer, remaining)
                    self._deleted.update(keys)
                for row in rows:
                    async with asyncio.timeout(3):
                        await self.files.forget_origin(peer, row["message_id"])
                        await self.repository.finish(row["chat_id"], row["message_id"])
                    self._pending.pop((row["chat_id"], row["message_id"]), None)
                    self._deleted.discard((row["chat_id"], row["message_id"]))
                    completed.add((row["chat_id"], row["message_id"]))
                log.info({"event": "file_messages_expired", "count": len(rows)})
            except Exception as error:
                delay = min(300, 2 ** min(max(row["attempts"] for row in rows) + 1, 8))
                if isinstance(error, DeletionRetryAfter):
                    delay = max(1, error.seconds)
                    self._retry_at = time.monotonic() + delay
                log.warning({"event": "file_expiry_delete_failed", **error_fields(error)})
                for row in rows:
                    if (row["chat_id"], row["message_id"]) in completed:
                        continue
                    row["attempts"] += 1
                    row["next_attempt_at"] = self.clock() + timedelta(seconds=delay)
                    try:
                        async with asyncio.timeout(3):
                            await self.repository.defer(row["chat_id"], row["message_id"], delay)
                    except Exception:
                        self._pending[(row["chat_id"], row["message_id"])] = row
