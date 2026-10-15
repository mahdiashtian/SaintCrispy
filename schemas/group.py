"""Framework-independent group lifecycle evidence."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GroupChange:
    chat_id: int
    kind: str
    event_key: str
    occurred_at: datetime
    chat_kind: str
    active: bool | None = None
    admin: bool | None = None
    can_send: bool | None = None
    actor_id: int | None = None
    title: str | None = None
    username: str | None = None
    stream: str = "service"
    sequence: int = 0
    priority: int = 1
    migrated_to: int | None = None

    def __post_init__(self):
        if type(self.chat_id) is not int or self.chat_id >= 0:
            raise ValueError("A group must have a negative Telegram chat ID")
        if self.chat_kind not in {"group", "supergroup"}:
            raise ValueError("Broadcast channels are not groups")
        if self.occurred_at.tzinfo is None or not self.event_key:
            raise ValueError("Lifecycle evidence needs a timezone and stable key")
        if self.actor_id is not None and (type(self.actor_id) is not int or self.actor_id <= 0):
            raise ValueError("An actor must be a human user ID")
