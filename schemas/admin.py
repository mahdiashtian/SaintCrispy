"""Typed administration views and durable Telegram effects."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class AdminUser:
    user_id: int
    username: str | None
    first_name: str | None
    is_started: bool
    is_admin: bool
    is_banned: bool
    is_blocked: bool
    account_kind: str
    last_seen_at: datetime


@dataclass(frozen=True)
class AdminJob:
    id: int
    kind: str
    status: str
    requested_by: int
    total_count: int
    success_count: int
    failure_count: int
    skipped_count: int
    created_at: datetime


@dataclass(frozen=True)
class PendingEffect:
    id: int
    kind: str
    target_id: int
    payload: dict = field(repr=False)
    random_id: int
    job_id: int | None
    attempt_count: int


class RetryEffect(Exception):
    def __init__(self, seconds, code="temporary"):
        self.seconds, self.code = seconds, code


@dataclass(frozen=True)
class SettingSnapshot:
    key: str
    value: bool


@dataclass(frozen=True)
class AdminDataFile:
    filename: str
    data: bytes = field(repr=False)
    next_cursor: int | None


class PermanentEffect(Exception):
    def __init__(self, code):
        self.code = code
