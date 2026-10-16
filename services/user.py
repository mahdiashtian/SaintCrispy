"""Register a user on any request; /start is only a presentation action."""

from core.enums import ConversationState
from repositories.interfaces.workflow import UserRepository

from .base import TransactionalService


class UserService(TransactionalService):
    def __init__(self, repository: UserRepository, uow_factory=None):
        super().__init__(uow_factory)
        self.repository = repository

    async def touch_user(self, user_id: int, *, started: bool = False) -> None:
        await self.repository.touch_user(user_id, started=started)

    async def start(
        self, user_id: int, chat_id: int, *, source="plain", source_chat_id=None
    ) -> bool:
        if source not in {"plain", "referral", "unknown"} or (
            source_chat_id is not None
            and (type(source_chat_id) is not int or not -(2**63) < source_chat_id < 0)
        ):
            raise ValueError("Invalid first-start attribution")
        if self._uow_factory is None:
            await self.touch_user(user_id, started=True)
            return False
        async with self._uow_factory() as uow:
            await uow.workflow.touch_user(
                user_id, started=True, start_source=source, start_chat_id=source_chat_id
            )
            await uow.workflow.set_state(user_id, chat_id, ConversationState.MAIN.value, {})
        return True
