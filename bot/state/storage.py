"""FSM storage adapters; SQL remains durable and memory is used only in tests."""

from copy import deepcopy

from repositories.interfaces.workflow import StateStorage as StateStorage


class PostgresStateStorage:
    def __init__(self, repository: StateStorage):
        self.repository = repository

    async def get_state(self, user_id, chat_id):
        return await self.repository.get_state(user_id, chat_id)

    async def set_state(self, user_id, chat_id, state, data):
        await self.repository.set_state(user_id, chat_id, state, data)

    async def update_data(self, user_id, chat_id, data):
        await self.repository.update_data(user_id, chat_id, data)

    async def finish_transfer(self, user_id, chat_id, transfer_id, state):
        await self.repository.finish_transfer(user_id, chat_id, transfer_id, state)

    async def finish_inspection(self, user_id, chat_id, request_id, **options):
        await self.repository.finish_inspection(user_id, chat_id, request_id, **options)


class MemoryStateStorage:
    def __init__(self):
        self._states = {}

    async def get_state(self, user_id, chat_id):
        return deepcopy(self._states.get((user_id, chat_id)))

    async def set_state(self, user_id, chat_id, state, data):
        key = user_id, chat_id
        self._states[key] = {
            "state": state,
            "data": deepcopy(data),
            "revision": self._states.get(key, {}).get("revision", 0) + 1,
        }

    async def update_data(self, user_id, chat_id, data):
        row = self._states.get((user_id, chat_id), {"state": "MAIN", "data": {}})
        await self.set_state(user_id, chat_id, row["state"], {**row["data"], **data})

    async def finish_transfer(self, user_id, chat_id, transfer_id, state):
        row = self._states.get((user_id, chat_id))
        if row and row["data"].get("transfer_id") == transfer_id:
            await self.set_state(user_id, chat_id, state, {})

    async def finish_inspection(self, user_id, chat_id, request_id, **options):
        row = self._states.get((user_id, chat_id))
        if row and row["state"] == "INSPECTING" and row["data"].get("request_id") == request_id:
            await self.set_state(user_id, chat_id, "FAILED", {})
