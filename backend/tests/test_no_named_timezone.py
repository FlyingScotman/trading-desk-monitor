"""FlyingScotman/trading-desk-monitor#155: the embedded Postgres of the Windows bundle ships without time zone data,
so ``AT TIME ZONE 'Europe/Moscow'`` failed every run (``time zone "Europe/Moscow" not recognized``) once the store
held a second book revision without ``valid_from``. Moscow has had no DST since 2014: the Moscow day is UTC+03:00, a
fixed offset that needs no time zone data. No named zone in the backend's SQL or Python; the books and reference rows
in force are the same revisions as with the named zone, on both sides of Moscow midnight, whatever the session's
TimeZone. Synthetic data only."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from desk import store

SRC = Path(__file__).resolve().parents[1] / "src"

# A time zone looked up by name: in SQL a string zone after AT TIME ZONE, SET TIME ZONE, timezone('...', ...),
# a zone name literal; in Python zoneinfo / pytz / dateutil.tz.gettz. A fixed offset (INTERVAL '+03:00',
# datetime.timezone(timedelta(hours=3))) needs no time zone data.
NAMED = [
    re.compile(r"AT\s+TIME\s+ZONE\s+'", re.IGNORECASE),
    re.compile(r"SET\s+(SESSION\s+|LOCAL\s+)?TIME\s*ZONE", re.IGNORECASE),
    re.compile(r"\btimezone\s*\(\s*'", re.IGNORECASE),
    re.compile(r"'(Europe|Asia|America|Africa|Australia|Etc)/\w+'"),
    re.compile(r"\bzoneinfo\b|\bZoneInfo\b|\bpytz\b|\bgettz\b"),
]


def test_no_named_time_zone_in_the_backend_source():
    hits = []
    for f in sorted([*SRC.rglob("*.py"), *SRC.rglob("*.sql")]):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if any(p.search(line) for p in NAMED):
                hits.append(f"{f.relative_to(SRC)}:{n}: {line.strip()}")
    assert not hits, "a named time zone needs tz data the Windows bundle does not have:\n" + "\n".join(hits)


def test_the_fixed_offset_is_the_moscow_day(store_url):
    """``AT TIME ZONE INTERVAL '+03:00'`` (east of UTC, unlike a POSIX string offset) gives the same Moscow day as the
    named zone (the test Postgres has tz data), every half hour over two years, for any session TimeZone."""
    with store.connect(store_url) as con:
        for tz in ("UTC", "America/New_York", "Asia/Tokyo"):
            con.execute(f"SET TIME ZONE '{tz}'")
            bad = con.execute(
                "SELECT count(*), count(*) FILTER (WHERE (t AT TIME ZONE INTERVAL '+03:00')::date"
                " <> (t AT TIME ZONE 'Europe/Moscow')::date) FROM generate_series("
                "'2025-01-01T00:00:00Z'::timestamptz, '2026-12-31T23:30:00Z', '30 minutes') t").fetchone()
            assert bad[0] > 35000 and bad[1] == 0, tz


BEFORE = datetime(2026, 10, 22, 20, 59, 59, tzinfo=UTC)  # 22.10 23:59:59 MSK
MIDNIGHT = datetime(2026, 10, 22, 21, 0, tzinfo=UTC)     # 23.10 00:00 MSK
LATER = datetime(2030, 1, 1, tzinfo=UTC)                 # known_at after every revision


def _book(con, book_type: str, at: datetime) -> None:
    con.execute("INSERT INTO book_rev (book_id, name, desk, book_type, entity, active, recorded_at,"
                " adapter_fingerprint, content_sha256) VALUES ('B1', 'n', 'Rates', %s, 'E', true, %s, 'f', %s)",
                (book_type, at, book_type))


def _rule(con, rate: str, at: datetime) -> None:
    con.execute("INSERT INTO reference_rev (kind, ref_key, active, content, recorded_at, adapter_fingerprint,"
                " content_sha256) VALUES ('funding_rules', 'r1', true, %s, %s, 'f', %s)",
                (Jsonb({"rule_id": "r1", "fixed_rate_pct": rate}), at, rate))


@pytest.mark.parametrize("session_tz", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_revisions_without_valid_from_take_effect_on_the_moscow_day_they_were_recorded(store_url, session_tz):
    """Three revisions without ``valid_from``: the first in force always, the second recorded a second before Moscow
    midnight (22.10 MSK), the third at Moscow midnight (23.10 MSK)."""
    with store.connect(store_url) as con:
        _book(con, "FIRST", BEFORE - timedelta(days=5))
        _book(con, "D22", BEFORE)
        _book(con, "D23", MIDNIGHT)
        _rule(con, "1", BEFORE - timedelta(days=5))
        _rule(con, "2", BEFORE)
        _rule(con, "3", MIDNIGHT)
        con.execute(f"SET TIME ZONE '{session_tz}'")

        def book(on: str, known_at: datetime = LATER) -> str:
            return store.current_books(con, date.fromisoformat(on), known_at)[0]["book_type"]

        def rate(on: str, known_at: datetime = LATER) -> str:
            return store.current_reference(con, "funding_rules", date.fromisoformat(on), known_at)[0]["fixed_rate_pct"]

        assert [book(d) for d in ("2020-01-01", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24")] == [
            "FIRST", "FIRST", "D22", "D23", "D23"]
        assert [rate(d) for d in ("2020-01-01", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24")] == [
            "1", "1", "2", "3", "3"]
        assert book("2026-10-23", MIDNIGHT - timedelta(seconds=1)) == "D22"  # as known before the third
        assert rate("2026-10-23", MIDNIGHT - timedelta(seconds=1)) == "2"
