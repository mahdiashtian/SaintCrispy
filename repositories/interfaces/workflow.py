"""Storage contracts for scoped conversation state and user registration."""

from collections.abc import Mapping
from typing import Any, Protocol


class StateStorage(Protocol):
    async def get_state(self, user_id: int, chat_id: int) -> Mapping[str, Any] | None: ...

    async def set_state(self, user_id: int, chat_id: int, state: str, data: dict) -> None: ...

    async def update_data(self, user_id: int, chat_id: int, data: dict) -> None: ...

    async def finish_transfer(
        self, user_id: int, chat_id: int, transfer_id: str, state: str
    ) -> None: ...

    async def finish_inspection(
        self, user_id: int, chat_id: int, request_id: str, *, link_identity: str | None = None
    ) -> None: ...


class UserRepository(Protocol):
    async def touch_user(self, user_id: int, *, started: bool = False) -> None: ...
