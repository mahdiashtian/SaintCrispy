"""Validated human activity; no message contents are persisted."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class MembershipEvent:
    chat_id: int
    user_id: int
    event_key: str
    kind: str
    occurred_at: datetime
    sequence: int = 0
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    stream: str = "message"
    observed_admin: bool | None = None

    def __post_init__(self):
        if self.observed_admin is not None and type(self.observed_admin) is not bool:
            raise ValueError("Invalid activity eligibility")
        if type(self.chat_id) is not int or not -(2**63) < self.chat_id < 0:
            raise ValueError("Membership requires a group")
        if type(self.user_id) is not int or not 0 < self.user_id < 2**63:
            raise ValueError("Membership requires a human user")
        if (
            self.kind not in {"message", "joined", "left"}
            or not isinstance(self.event_key, str)
            or not self.event_key
            or len(self.event_key) > 180
            or "\x00" in self.event_key
        ):
            raise ValueError("Invalid membership event")
        self.event_key.encode("utf-8")
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.tzinfo is None:
            raise ValueError("Membership evidence needs UTC time")
        if (
            type(self.sequence) is not int
            or not 0 <= self.sequence < 2**63
            or self.stream not in {"message", "basic", "participant", "service"}
        ):
            raise ValueError("Invalid membership sequence")
        for value in (self.username, self.first_name, self.last_name):
            if value is not None and (
                not isinstance(value, str) or len(value) > 256 or "\x00" in value
            ):
                raise ValueError("Invalid human metadata")
            if value is not None:
                value.encode("utf-8")

    def dump(self):
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.astimezone(UTC).isoformat()
        return data

    @classmethod
    def load(cls, data):
        data = dict(data)
        data["occurred_at"] = datetime.fromisoformat(data["occurred_at"])
        return cls(**data)
