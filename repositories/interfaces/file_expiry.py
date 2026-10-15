"""Durable cleanup and delivery ports, independent of the Telegram framework."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol


class ExpiryRepository(Protocol):
    async def add(
        self, chat_id: int, message_id: int, peer: bytes, delete_at: datetime
    ) -> None: ...

    async def due(self, limit: int = 100) -> Sequence[Mapping[str, Any]]: ...

    async def finish(self, chat_id: int, message_id: int) -> None: ...

    async def defer(self, chat_id: int, message_id: int, seconds: float) -> None: ...


class OriginInvalidation(Protocol):
    async def forget_origin(self, peer: bytes, message_id: int) -> None: ...


class MessageDeletion(Protocol):
    async def delete(self, peer: bytes, message_ids: list[int]) -> None: ...
