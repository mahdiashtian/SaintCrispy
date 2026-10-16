"""Framework-independent group lifecycle evidence."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum


class Standing(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SavedGroupPeer:
    chat_id: int
    raw_id: int
    chat_kind: str
    access_hash: int | None = field(repr=False)


@dataclass(frozen=True)
class GroupStanding:
    standing: Standing
    can_send: bool | None = None
    reason: str | None = None

    @property
    def active(self) -> bool | None:
        if self.standing is Standing.UNKNOWN:
            return None
        return self.standing is not Standing.GONE

    @property
    def admin(self) -> bool | None:
        if self.standing is Standing.UNKNOWN:
            return None
        return self.standing is Standing.ADMIN


@dataclass(frozen=True)
class GroupSnapshot:
    """Immutable repository output; database records never escape into handlers."""

    chat_id: int
    chat_kind: str
    title: str | None
    username: str | None
    is_active: bool | None
    is_admin: bool | None
    can_send: bool | None
    added_by_user_id: int | None
    added_at: datetime | None
    readded_at: datetime | None
    removed_at: datetime | None
    admin_since: datetime | None
    last_seen_at: datetime
    last_event_at: datetime
    migrated_to: int | None
    owner_id: int | None = None
    admin_ids: tuple[int, ...] = ()
    member_count: int | None = None
    link: str | None = None
    last_activity_at: datetime | None = None
    links_per_minute: int | None = None
    revision: int = 0


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
        if type(self.chat_id) is not int or not -(2**63) < self.chat_id < 0:
            raise ValueError("A group must have a negative Telegram chat ID")
        if self.chat_kind not in {"group", "supergroup"}:
            raise ValueError("Broadcast channels are not groups")
        if self.occurred_at.tzinfo is None or not self.event_key:
            raise ValueError("Lifecycle evidence needs a timezone and stable key")
        if (
            not isinstance(self.event_key, str)
            or len(self.event_key) > 256
            or not isinstance(self.kind, str)
            or len(self.kind) > 80
        ):
            raise ValueError("Invalid lifecycle identity")
        if any(
            value is not None and type(value) is not bool
            for value in (self.active, self.admin, self.can_send)
        ):
            raise ValueError("Invalid lifecycle flags")
        if any(
            value is not None and (not isinstance(value, str) or len(value) > 512)
            for value in (self.title, self.username)
        ):
            raise ValueError("Invalid lifecycle metadata")
        if (
            type(self.sequence) is not int
            or not 0 <= self.sequence < 2**63
            or type(self.priority) is not int
            or not 0 <= self.priority <= 10
        ):
            raise ValueError("Invalid lifecycle ordering")
        if not isinstance(self.stream, str) or len(self.stream) > 80:
            raise ValueError("Invalid lifecycle source")
        for value in (self.title, self.username, self.event_key, self.stream):
            if value is not None:
                value.encode("utf-8")
        if self.actor_id is not None and (type(self.actor_id) is not int or self.actor_id <= 0):
            raise ValueError("An actor must be a human user ID")
        if self.migrated_to is not None and (
            type(self.migrated_to) is not int
            or self.migrated_to >= 0
            or self.migrated_to == self.chat_id
        ):
            raise ValueError("A migration needs a different negative group ID")

    def dump(self):
        result = asdict(self)
        result["occurred_at"] = self.occurred_at.isoformat()
        return result

    @classmethod
    def load(cls, data):
        data = dict(data)
        data["occurred_at"] = datetime.fromisoformat(data["occurred_at"])
        return cls(**data)
