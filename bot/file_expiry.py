"""One managed, durable cleanup worker for confirmed user-facing file sends."""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from telethon import errors, utils
from telethon.extensions import BinaryReader

from bot.texts.downloads import FILE_DELETE_SECONDS
from core.diagnostics import error_fields

log = logging.getLogger("activity")


class FileExpiry:
    def __init__(self, client, repository, files, *, clock=None):
        self.client, self.repository, self.files = client, repository, files
        self.clock = clock or (lambda: datetime.now(UTC))
        self._pending = {}
        self._deleted = set()
        self._slots = asyncio.Semaphore(4)
        self._retry_at = 0.0
        self._wake = asyncio.Event()
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(self._run(), name="file-expiry")
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        await self._flush()

    async def schedule(self, peer, message):
        # Only confirmed message IDs are eligible; never infer/delete the link ID.
        message_id = getattr(message, "id", None)
        if type(message_id) is not int or message_id <= 0:
            return
        now = self.clock()
        row = {
            "chat_id": utils.get_peer_id(peer),
            "message_id": message_id,
            "peer": bytes(peer),
            "delete_at": now + timedelta(seconds=FILE_DELETE_SECONDS),
            "next_attempt_at": now + timedelta(seconds=FILE_DELETE_SECONDS),
            "attempts": 0,
        }
        row = self._pending.setdefault((row["chat_id"], message_id), row)
        self._wake.set()
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

    async def _flush(self):
        try:
            async with asyncio.timeout(3):
                for row in list(self._pending.values())[:100]:
                    await self._persist(row)
        except TimeoutError:
            pass

    async def _run(self):
        while True:
            try:
                self._wake.clear()
                await self.run_once()
                try:
                    await asyncio.wait_for(self._wake.wait(), 0.5)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning({"event": "file_expiry_worker_failed", **error_fields(error)})
                await asyncio.sleep(1)

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
        await self._flush()

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
                    with BinaryReader(peer) as reader:
                        target = reader.tgread_object()
                    async with asyncio.timeout(5):
                        await self.client.delete_messages(target, remaining, revoke=True)
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
                if isinstance(error, errors.FloodWaitError):
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
