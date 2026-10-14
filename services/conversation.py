"""Conversation orchestration follows Couplyo's explicit state manager."""

import logging

from core.enums import ConversationState
from core.request_context import current_inspection_work
from repositories.interfaces.workflow import StateStorage
from services.observability import error_fields

log = logging.getLogger("activity")


class ConversationService:
    def __init__(self, repository: StateStorage):
        self.repository = repository

    async def get(self, user_id: int, chat_id: int) -> tuple[ConversationState, dict]:
        import json

        stored = await self.repository.get_state(user_id, chat_id)
        if stored is None:
            return ConversationState.MAIN, {}
        try:
            data = json.loads(stored["data"]) if isinstance(stored["data"], str) else stored["data"]
            if not isinstance(data, dict):
                raise ValueError("Invalid conversation data")
            return ConversationState(stored["state"]), data
        except (ValueError, TypeError, KeyError):
            # Keep malformed state for diagnosis; a read must not erase an operation.
            log.warning({"event": "conversation_payload_invalid"})
            return ConversationState.MAIN, {}

    async def get_state(self, user_id: int, chat_id: int) -> ConversationState:
        state, _ = await self.get(user_id, chat_id)
        return state

    async def get_data(self, user_id: int, chat_id: int) -> dict:
        _, data = await self.get(user_id, chat_id)
        return data

    async def set_state(self, user_id: int, chat_id: int, state: ConversationState, **data):
        await self.set(user_id, chat_id, state, **data)

    async def update_data(self, user_id: int, chat_id: int, **data) -> None:
        await self.repository.update_data(user_id, chat_id, data)

    async def finish(self, user_id: int, chat_id: int) -> None:
        await self.set(user_id, chat_id, ConversationState.MAIN)

    async def reset(self, user_id: int, chat_id: int) -> None:
        await self.finish(user_id, chat_id)

    async def set(self, user_id: int, chat_id: int, state: ConversationState, **data) -> None:
        await self.repository.set_state(user_id, chat_id, state.value, data)
        log.info({"event": "conversation_changed", "state": state.value})

    async def finish_transfer(self, user_id, chat_id, transfer_id, state) -> None:
        try:
            await self.repository.finish_transfer(user_id, chat_id, transfer_id, state.value)
            log.info(
                {
                    "event": "conversation_transfer_finished",
                    "state": state.value,
                    "transfer_id": transfer_id,
                }
            )
        except Exception as error:
            # The already-delivered file stays successful even when an audit write fails.
            log.error({"event": "conversation_write_failed", **error_fields(error)})

    async def finish_inspection(self, user_id, chat_id, request_id) -> None:
        work = current_inspection_work.get()
        if work is not None:
            await self.repository.finish_inspection(
                user_id, chat_id, request_id, link_identity=work.link_identity
            )
        else:
            await self.repository.finish_inspection(user_id, chat_id, request_id)
