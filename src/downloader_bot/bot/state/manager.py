"""Conversation orchestration follows Couplyo's explicit state manager."""

import logging

from downloader_bot.services.observability import error_fields

from .states import ConversationState

log = logging.getLogger("activity")


class ConversationManager:
    def __init__(self, repository):
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
        except (ValueError, TypeError):
            await self.set(user_id, chat_id, ConversationState.MAIN)
            return ConversationState.MAIN, {}

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
        await self.repository.finish_inspection(user_id, chat_id, request_id)
