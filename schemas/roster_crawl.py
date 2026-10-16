"""Immutable roster progress; convergence is distinct from proven coverage."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RosterUser:
    user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


@dataclass(frozen=True)
class RosterCrawl:
    chat_id: int
    run_id: str
    locked_by: str | None
    status: str
    pass_number: int
    filter_index: int
    offset_count: int
    pass_new_count: int
    claimed_total: int | None
    seen_unique: int
    stored_new: int
    skipped_accounts: int
    requests_made: int
    flood_waits: int
    ambiguous_visits: int
    converged: bool
    stopped_because: str | None
    last_error: str | None
    available_at: datetime
    updated_at: datetime

    @property
    def coverage_ratio(self):
        return min(1.0, self.seen_unique / self.claimed_total) if self.claimed_total else None

    @property
    def is_complete(self):
        return self.claimed_total is not None and self.seen_unique >= self.claimed_total

    @property
    def accounted_for(self):
        return self.seen_unique + self.skipped_accounts
