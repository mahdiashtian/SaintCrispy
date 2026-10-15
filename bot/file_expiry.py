"""Telegram deletion adapter and the managed polling lifecycle."""

import asyncio
import logging

from telethon import errors, utils
from telethon.extensions import BinaryReader

from core.diagnostics import error_fields
from schemas.expiry import DeletionRetryAfter
from services.file_expiry import FileExpiryService

log = logging.getLogger("activity")


class TelegramMessageDeletion:
    def __init__(self, client):
        self.client = client

    async def delete(self, peer: bytes, message_ids: list[int]) -> None:
        with BinaryReader(peer) as reader:
            target = reader.tgread_object()
        try:
            await self.client.delete_messages(target, message_ids, revoke=True)
        except errors.FloodWaitError as error:
            raise DeletionRetryAfter(error.seconds) from None


class FileExpiry(FileExpiryService):
    def __init__(self, client, repository, files, *, clock=None):
        super().__init__(repository, files, TelegramMessageDeletion(client), clock=clock)
        self._wake = asyncio.Event()
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(self._run(), name="file-expiry")
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        await self.flush()

    async def schedule(self, peer, message):
        # Only confirmed message IDs are eligible; never infer/delete the link ID.
        message_id = getattr(message, "id", None)
        if type(message_id) is not int or message_id <= 0:
            return
        self._wake.set()
        await super().schedule(utils.get_peer_id(peer), message_id, bytes(peer))

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
