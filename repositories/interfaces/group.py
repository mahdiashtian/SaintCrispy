from typing import Protocol

from schemas.group import GroupChange, GroupSnapshot, SavedGroupPeer


class GroupRepository(Protocol):
    async def get(self, chat_id: int) -> GroupSnapshot | None: ...

    async def page(
        self, cursor: int | None = None, limit: int = 20
    ) -> tuple[GroupSnapshot, ...]: ...

    async def peer(self, chat_id: int) -> SavedGroupPeer | None: ...

    async def remember_peer(
        self, chat_id: int, raw_id: int, chat_kind: str, access_hash: int | None
    ) -> None: ...

    async def metadata(
        self,
        chat_id: int,
        owner_id: int | None,
        admin_ids: list[int],
        member_count: int | None,
        link: str | None,
    ) -> None: ...

    async def record(self, change: GroupChange): ...

    async def claim_link(self, user_id, chat_id, request_id, interval, limit): ...

    async def migrate(self, old_id, new_id, occurred_at): ...
