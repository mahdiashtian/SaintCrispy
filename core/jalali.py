"""Source-compatible Jalali dates and Persian digits, with no extra dependency."""

from datetime import datetime, timedelta, timezone

TEHRAN = timezone(timedelta(hours=3, minutes=30))

_jdatetime = None

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

_G_DAYS_IN_MONTH = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]


def _gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + _G_DAYS_IN_MONTH[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return (jy, jm, jd)


def to_persian_digits(text: str) -> str:
    return str(text).translate(_PERSIAN_DIGITS)


def _to_tehran(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TEHRAN)


def to_shamsi(dt: datetime) -> str:
    local = _to_tehran(dt)
    if _jdatetime is not None:
        jd = _jdatetime.datetime.fromgregorian(datetime=local.replace(tzinfo=None))
        formatted = f"{jd.year:04d}/{jd.month:02d}/{jd.day:02d} {jd.hour:02d}:{jd.minute:02d}"
    else:
        jy, jm, jd_ = _gregorian_to_jalali(local.year, local.month, local.day)
        formatted = f"{jy:04d}/{jm:02d}/{jd_:02d} {local.hour:02d}:{local.minute:02d}"
    return to_persian_digits(formatted)


def to_shamsi_date(dt: datetime) -> str:
    return to_shamsi(dt).split(" ")[0]
