"""Tehran calendar windows used by manual and scheduled group reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.jalali import TEHRAN

_SATURDAY = 5  # datetime.weekday(): Monday=0 ... Saturday=5


@dataclass(frozen=True, slots=True)
class ReportWindow:
    start: datetime
    end: datetime
    previous_start: datetime
    previous_end: datetime
    key: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _window(
    start_local: datetime,
    end_local: datetime,
    *,
    comparison_days: int,
    key: str,
) -> ReportWindow:
    shift = timedelta(days=comparison_days)
    return ReportWindow(
        start=_as_utc(start_local),
        end=_as_utc(end_local),
        previous_start=_as_utc(start_local - shift),
        previous_end=_as_utc(end_local - shift),
        key=key,
    )


def current_tehran_day(now: datetime | None = None) -> ReportWindow:
    """Today from Tehran midnight through ``now``, compared with yesterday."""

    end_local = (now or datetime.now(timezone.utc)).astimezone(TEHRAN)
    start_local = end_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return _window(
        start_local,
        end_local,
        comparison_days=1,
        key=start_local.date().isoformat(),
    )


def current_tehran_week(now: datetime | None = None) -> ReportWindow:
    """Current Iranian week: Saturday 00:00 Tehran through ``now``."""

    end_local = (now or datetime.now(timezone.utc)).astimezone(TEHRAN)
    days_since_saturday = (end_local.weekday() - _SATURDAY) % 7
    start_local = (end_local - timedelta(days=days_since_saturday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return _window(
        start_local,
        end_local,
        comparison_days=7,
        key=start_local.date().isoformat(),
    )


def latest_completed_tehran_day(now: datetime | None = None) -> ReportWindow:
    """The full Tehran day immediately preceding today's midnight."""

    local = (now or datetime.now(timezone.utc)).astimezone(TEHRAN)
    end_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=1)
    return _window(
        start_local,
        end_local,
        comparison_days=1,
        key=start_local.date().isoformat(),
    )


def latest_weekly_trigger(
    now: datetime,
    *,
    weekday: int = _SATURDAY,
    hour: int = 12,
) -> tuple[datetime, str]:
    """Most recent configured weekly trigger in Tehran, plus its date key."""

    local = now.astimezone(TEHRAN)
    target_weekday = max(0, min(6, weekday))
    days_back = (local.weekday() - target_weekday) % 7
    trigger = (local - timedelta(days=days_back)).replace(
        hour=max(0, min(23, hour)), minute=0, second=0, microsecond=0
    )
    if trigger > local:
        trigger -= timedelta(days=7)
    return _as_utc(trigger), trigger.date().isoformat()


def completed_tehran_week_for_trigger(trigger: datetime) -> ReportWindow:
    """Full Saturday-Friday week ending at the trigger date's 00:00."""

    trigger_local = trigger.astimezone(TEHRAN)
    end_local = trigger_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=7)
    return _window(
        start_local,
        end_local,
        comparison_days=7,
        key=end_local.date().isoformat(),
    )
