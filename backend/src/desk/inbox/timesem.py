"""Time semantics of an inbox file (08 §6.5, §6.6): business date, plausibility, revision order. Pure functions."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from desk.inbox.static_index import CalendarUnavailable

MSK = timezone(timedelta(hours=3), "MSK")  # Moscow has no DST since 2014; a fixed offset needs no tzdata


class Calendar(Protocol):
    def prev_business_day(self, d: date) -> date: ...


def _date(d: date | str) -> date:
    return d if isinstance(d, date) else date.fromisoformat(d)


def parse_ts(value: str | None) -> datetime | None:
    """ISO 8601 timestamp as an aware datetime; one without an offset is Europe/Moscow (08 §3)."""
    if value is None:
        return None
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=MSK)


def valid_date(report_date: date | str, time_basis: str, calendar: Calendar | None) -> date:
    """The business date: the previous business day for ``start_of_day``; ``report_date`` unchanged for
    ``end_of_day`` and ``intraday``, even on a non-business day (never shifted or dropped). ``start_of_day``
    without a calendar raises ``CalendarUnavailable``: the date is never guessed."""
    d = _date(report_date)
    if time_basis == "start_of_day":
        if calendar is None:
            raise CalendarUnavailable(f"no calendar for {d}")
        return calendar.prev_business_day(d)
    if time_basis in ("end_of_day", "intraday"):
        return d
    raise ValueError(f"unknown time_basis {time_basis!r}")


def report_date_suspicious(report_date: date | str, time_basis: str, generated_at: str | None) -> bool:
    """True when ``meta.generated_at`` (in Moscow time) contradicts the report date: for ``start_of_day`` it
    should fall on ``report_date``, for ``end_of_day`` not before it (an evening EOD file of the same day is
    plausible). No timestamp, or ``intraday``: nothing to check."""
    ts = parse_ts(generated_at)
    if ts is None:
        return False
    local, d = ts.astimezone(MSK).date(), _date(report_date)
    if time_basis == "start_of_day":
        return local != d
    if time_basis == "end_of_day":
        return local < d
    return False


def revision_key(report_date: date | str, report_ts: str | None, seq: int | None, generated_at: str | None,
                 file_mtime: float | None) -> tuple:
    """Sort key of revisions within one unit (08 §6.6): the greatest is current. A missing part sorts below
    any present one, so a corrected file with ``seq`` beats the original without."""
    def part(v):
        return (0,) if v is None else (1, v)
    return (_date(report_date), part(parse_ts(report_ts)), part(seq), part(parse_ts(generated_at)), part(file_mtime))
