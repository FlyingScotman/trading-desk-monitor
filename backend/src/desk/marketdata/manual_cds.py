"""Manually entered CDS curves (TDM#652): storage (``manual_cds_rev``, migration 0028), the curve in force on a date
(``curve_on``) for the calculation, and the changes the owner makes (``put_curve``, ``copy_curves``).

The hub has no CDS quotes and no paid source is connected, so the owner types the curves on the monitor «Ручные
рыночные данные» (C22). Later part of them may come from internal systems; they would be stored the same way.

Quotation convention. The owner's decision of 19.11.2026 (an assumption that can be changed, so it is named here, on
the screen and in ``docs/architecture/05_FRONTEND_INTEGRATION.md``) is:

* the quote is the **par coupon spread**, in basis points per year (the coupon that makes the CDS worth zero at
  inception), not an upfront with a fixed coupon;
* tenors ``TENORS``: 3M, 6M, 1Y, 2Y, 3Y, 5Y, 7Y, 10Y (a tenor may be empty: not every name has all of them);
* the recovery rate is stored with the curve, a fraction (0.25 = 25 %, strictly below 1), 25 % unless the owner sets
  another per name.

Not the owner's decision, the developer's assumption: the quote currency is stored with the curve, USD unless the
owner sets another per name.

Identity of a reference name: ``name_key(name)``. A CDS row of the positions report has no issuer id to join on (the
trade terms of the contract have no CDS at all); the adapter gives the reference name as the report's ``x_issuer``
(``position_input.x``, as for an unmapped bond), the row's ``source_instrument_name`` being the name of the deal. The
key is the text of the reference name, normalised (NFKC, case folded, whitespace collapsed). The CDS valuation
(TDM#614) maps the reference name of a CDS row (``x_issuer``, later the terms' field) to the curve with the same
``name_key``. Known limitation: names that differ in spelling (an abbreviation, a legal form, Cyrillic and Latin
look-alike letters, a hyphen for a space) are different names, there is no alias table; the screen lists the names of
the date's CDS rows beside the entered ones so that the owner types the spelling the report has.

A revision holds the whole curve of a name from ``valid_from`` on and only ever adds (append-only, who and when;
``changed_at`` is the time of the INSERT, not of the commit, so a calculation should compare the ``rev_id`` it used, not
timestamps). The curve in force on a date is the name's latest revision of the latest ``valid_from`` on or before it; a withdrawal
(``cleared``) ends it until a later curve is entered. ``curve_on(con, date, name)`` is what the calculation calls.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

TENORS = ("3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y")
"""The tenors of a curve, in order: the owner's decision of 19.11.2026."""
TENOR_YEARS = {"3M": 0.25, "6M": 0.5, "1Y": 1.0, "2Y": 2.0, "3Y": 3.0, "5Y": 5.0, "7Y": 7.0, "10Y": 10.0}
COLUMN = {t: "spread_" + t.lower() for t in TENORS}
DEFAULT_RECOVERY = 0.25
"""The recovery rate of a curve entered without one: 25 % (TDM#652)."""
DEFAULT_CCY = "USD"
"""The currency of the quotes of a curve entered without one: CDS on emerging-market names are quoted in USD."""
MAX_SPREAD_BP = 10000.0
"""A par spread of 100 % a year is the upper bound of what is stored (a typing slip, not a market)."""
MAX_NAME = 120
MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)
"""The dates a curve may start on (the table's check)."""
_FIELDS = ("rev_id", "name_key", "name", "valid_from", "cleared", *COLUMN.values(), "recovery", "ccy", "changed_by",
           "changed_at")
_SELECT = ", ".join(_FIELDS)


class CurveError(ValueError):
    """The input is not a curve (a tenor, a spread, a recovery rate, a name or a date the store refuses)."""


def strip_format(name: str) -> str:
    """``name`` without its invisible formatting characters (category Cf: soft hyphen, zero-width, text direction)."""
    return "".join(c for c in name if unicodedata.category(c) != "Cf")


def name_key(name: str) -> str:
    """The identity of a reference name: NFKC, invisible formatting characters (Cf) dropped, case folded, whitespace
    runs one space, stripped. Idempotent. A name from a report with a soft hyphen has the key of the same name without."""
    return " ".join(strip_format(unicodedata.normalize("NFKC", name)).casefold().split())


def clean_name(raw: str) -> str:
    """The name as shown: whitespace runs one space; control characters and an empty or over-long name are refused."""
    if any(unicodedata.category(c) == "Cc" and c not in "\t\n\r" for c in raw):
        raise CurveError("the name has control characters")
    if any(unicodedata.category(c) == "Cf" for c in raw):
        raise CurveError("the name has invisible formatting characters (zero-width, text direction)")
    name = " ".join(raw.split())
    if not name:
        raise CurveError("the name is empty")
    if len(name) > MAX_NAME or len(name_key(name)) > MAX_NAME:
        raise CurveError(f"the name is longer than {MAX_NAME} characters")
    return name


@dataclass(frozen=True)
class Curve:
    """The curve of a reference name as entered on ``valid_from``."""
    rev_id: int
    name_key: str
    name: str
    valid_from: date
    spreads_bp: dict[str, float | None]
    """Par spread in basis points by tenor (every tenor of ``TENORS``; None: not given)."""
    recovery: float
    ccy: str
    changed_by: str
    changed_at: datetime
    cleared: bool = field(default=False)

    def points(self) -> list[tuple[str, float, float]]:
        """(tenor, years, par spread in bp) of the tenors given, shortest first."""
        return [(t, TENOR_YEARS[t], s) for t in TENORS if (s := self.spreads_bp.get(t)) is not None]


def _curve(r: dict[str, Any]) -> Curve:
    return Curve(rev_id=r["rev_id"], name_key=r["name_key"], name=r["name"], valid_from=r["valid_from"],
                 spreads_bp={t: (float(r[COLUMN[t]]) if r[COLUMN[t]] is not None else None) for t in TENORS},
                 recovery=float(r["recovery"]) if r["recovery"] is not None else DEFAULT_RECOVERY,
                 ccy=r["ccy"] or DEFAULT_CCY, changed_by=r["changed_by"], changed_at=r["changed_at"],
                 cleared=r["cleared"])


def _rows(con: psycopg.Connection, sql: str, args: tuple | dict) -> list[dict[str, Any]]:
    with con.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, args).fetchall()


def curve_on(con: psycopg.Connection, on: date, name: str, *, known_at: datetime | None = None) -> Curve | None:
    """The curve in force on ``on`` for a reference name (any spelling that has its ``name_key``): the name's latest
    revision of the latest ``valid_from`` <= ``on``; None when there is none or the owner withdrew it. ``known_at``:
    only revisions recorded by then (what a run that read its inputs at that time saw)."""
    rows = _rows(con, f"SELECT {_SELECT} FROM manual_cds_rev WHERE name_key = %(k)s AND valid_from <= %(d)s"
                      " AND (%(t)s::timestamptz IS NULL OR changed_at <= %(t)s) ORDER BY valid_from DESC, rev_id DESC"
                      " LIMIT 1", {"k": name_key(name), "d": on, "t": known_at})
    c = _curve(rows[0]) if rows else None
    return None if c is None or c.cleared else c


def curves_on(con: psycopg.Connection, on: date, *, known_at: datetime | None = None) -> list[Curve]:
    """The curves in force on ``on``, one per name, by name."""
    rows = _rows(con, f"SELECT {_SELECT} FROM (SELECT DISTINCT ON (name_key) * FROM manual_cds_rev"
                      " WHERE valid_from <= %(d)s AND (%(t)s::timestamptz IS NULL OR changed_at <= %(t)s)"
                      " ORDER BY name_key, valid_from DESC, rev_id DESC) c WHERE NOT cleared ORDER BY name_key",
                 {"d": on, "t": known_at})
    return [_curve(r) for r in rows]


def curves_changed_since(con: psycopg.Connection, on: date, known_at: datetime | None) -> bool:
    """True when the curves in force on ``on`` now differ from those a run that read its inputs at ``known_at`` saw
    (a curve entered, corrected, withdrawn or carried to ``on`` after it). The calculation of a CDS (TDM#614) asks
    this for the dates whose run holds CDS positions, as ``store.late_inputs`` does for the OCP report and balances.
    ``changed_at`` is the time of the INSERT (``clock_timestamp()``), not of the commit: a revision committed after
    ``known_at`` may carry an earlier time. A run that records the ``rev_id`` of every curve it used (TDM#614) compares
    those with ``curves_on(con, on)`` and needs no timestamps; this function is the approximation until then."""
    if known_at is None:
        return False
    return ({(c.name_key, c.rev_id) for c in curves_on(con, on)}
            != {(c.name_key, c.rev_id) for c in curves_on(con, on, known_at=known_at)})


def history(con: psycopg.Connection, name: str, limit: int = 200) -> list[Curve]:
    """Every revision of a name, newest first (a withdrawal is a revision with ``cleared``)."""
    return [_curve(r) for r in _rows(con, f"SELECT {_SELECT} FROM manual_cds_rev WHERE name_key = %s"
                                          " ORDER BY rev_id DESC LIMIT %s", (name_key(name), limit))]


def entry_on(con: psycopg.Connection, on: date, key: str) -> Curve | None:
    """The name's own entry of exactly ``on`` (its latest revision, a withdrawal included), else None."""
    rows = _rows(con, f"SELECT {_SELECT} FROM manual_cds_rev WHERE name_key = %s AND valid_from = %s"
                      " ORDER BY rev_id DESC LIMIT 1", (key, on))
    return _curve(rows[0]) if rows else None


def all_names(con: psycopg.Connection) -> list[tuple[str, str]]:
    """(name_key, the name as last written) of every name that ever had a revision, by key."""
    return [(r["name_key"], r["name"]) for r in _rows(
        con, "SELECT DISTINCT ON (name_key) name_key, name FROM manual_cds_rev ORDER BY name_key, rev_id DESC", ())]


def check_spreads(spreads_bp: dict[str, Any]) -> dict[str, float | None]:
    """Every tenor of ``TENORS`` -> a par spread in bp (finite, 0..``MAX_SPREAD_BP``) or None; a tenor outside the list
    or a value that is no number is refused."""
    unknown = sorted(set(spreads_bp) - set(TENORS))
    if unknown:
        raise CurveError(f"unknown tenor {', '.join(map(repr, unknown))}: the tenors are {', '.join(TENORS)}")
    out: dict[str, float | None] = {}
    for t in TENORS:
        v = spreads_bp.get(t)
        if v is None or (isinstance(v, str) and not v.strip()):
            out[t] = None
            continue
        if isinstance(v, bool):
            raise CurveError(f"the spread at {t} is no number")
        try:
            x = float(v.replace(",", ".")) if isinstance(v, str) else float(v)
        except (TypeError, ValueError):
            raise CurveError(f"the spread at {t} is no number") from None
        if not math.isfinite(x):
            raise CurveError(f"the spread at {t} is no number")
        if not 0 <= x <= MAX_SPREAD_BP:
            raise CurveError(f"the spread at {t} is outside 0..{MAX_SPREAD_BP:g} bp")
        out[t] = round(x, 4)
    return out


def check_recovery(v: Any) -> float:
    if v is None:
        return DEFAULT_RECOVERY
    if isinstance(v, bool):
        raise CurveError("the recovery rate is no number")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise CurveError("the recovery rate is no number") from None
    if not math.isfinite(x) or not 0 <= round(x, 4) < 1:
        raise CurveError("the recovery rate must be from 0 up to (not including) 1 (a fraction: 0.25 is 25 %)")
    return round(x, 4)


def check_date(d: date) -> date:
    if not MIN_DATE <= d <= MAX_DATE:
        raise CurveError(f"the date is outside {MIN_DATE.isoformat()}..{MAX_DATE.isoformat()}")
    return d


def check_ccy(v: str | None) -> str:
    if v is None or not str(v).strip():
        return DEFAULT_CCY
    ccy = str(v).strip().upper()
    if len(ccy) != 3 or not ccy.isascii() or not ccy.isalpha():
        raise CurveError(f"not a currency code: {v!r} (three letters)")
    return ccy


def _same(c: Curve, name: str, spreads: dict[str, float | None], recovery: float, ccy: str) -> bool:
    return (c.name == name and c.spreads_bp == spreads and abs(c.recovery - recovery) < 1e-9 and c.ccy == ccy)


def _insert(con: psycopg.Connection, key: str, name: str, valid_from: date, spreads: dict[str, float | None] | None,
            recovery: float | None, ccy: str | None, who: str) -> Curve:
    """One revision; ``spreads`` None: a withdrawal."""
    cols = [COLUMN[t] for t in TENORS]
    vals = [None if spreads is None else spreads[t] for t in TENORS]
    with con.cursor(row_factory=dict_row) as cur:
        r = cur.execute(
            f"INSERT INTO manual_cds_rev (name_key, name, valid_from, cleared, {', '.join(cols)}, recovery, ccy,"
            f" changed_by) VALUES (%s, %s, %s, %s, {', '.join(['%s'] * len(cols))}, %s, %s, %s)"
            f" RETURNING {_SELECT}", (key, name, valid_from, spreads is None, *vals, recovery, ccy, who)).fetchone()
    return _curve(r)


def _lock(con: psycopg.Connection, key: str) -> None:
    con.execute("SELECT pg_advisory_xact_lock(hashtext('manual_cds:' || %s))", (key,))


def put_curve(con: psycopg.Connection, on: date, name: str, spreads_bp: dict[str, Any] | None, *,
              recovery: Any = None, ccy: str | None = None, who: str = "local") -> tuple[Curve | None, bool]:
    """Set the curve of ``name`` from ``on`` (``spreads_bp`` None: withdraw the name from ``on``). Returns (the curve in
    force on ``on`` afterwards, changed). A request that sets what is already in force on ``on`` adds no revision.
    The caller opens the transaction (and audits the change)."""
    check_date(on)
    name = clean_name(name)
    key = name_key(name)
    if spreads_bp is None:
        with con.transaction():
            _lock(con, key)
            if curve_on(con, on, key) is None:
                return None, False
            _insert(con, key, name, on, None, None, None, who)
        return None, True
    spreads = check_spreads(spreads_bp)
    if all(v is None for v in spreads.values()):
        raise CurveError("a curve needs at least one spread (to remove the name, withdraw it)")
    rec, cur_ccy = check_recovery(recovery), check_ccy(ccy)
    with con.transaction():
        _lock(con, key)
        now = curve_on(con, on, key)
        if now is not None and _same(now, name, spreads, rec, cur_ccy):
            return now, False
        return _insert(con, key, name, on, spreads, rec, cur_ccy, who), True


@dataclass(frozen=True)
class CopyResult:
    to_date: date
    copied: list[Curve]
    """The new entries of ``to_date``."""
    skipped: list[tuple[str, str, str]]
    """(name_key, name, why) of the names asked for that were not copied."""


def copy_curves(con: psycopg.Connection, to_date: date, *, from_date: date | None = None,
                names: list[str] | None = None, who: str = "local") -> CopyResult:
    """Enter on ``to_date`` the curves in force before it (``from_date`` None) or on ``from_date``: for ``names``, else for
    every name that has one. A name that already has an entry of its own on ``to_date`` is left as it is; a withdrawal
    on ``to_date`` is undone only for a name asked for (``names``), never by a copy of everything. All or none."""
    check_date(to_date)
    if from_date is not None:
        check_date(from_date)
        if from_date >= to_date:
            raise CurveError("the date to copy from must be earlier than the date to copy to")
    keys = None if names is None else list(dict.fromkeys(name_key(clean_name(n)) for n in names))
    copied: list[Curve] = []
    skipped: list[tuple[str, str, str]] = []

    def source() -> dict[str, Curve]:
        return {r["name_key"]: _curve(r) for r in _rows(
            con, f"SELECT {_SELECT} FROM (SELECT DISTINCT ON (name_key) * FROM manual_cds_rev WHERE "
                 + ("valid_from <= %(f)s" if from_date else "valid_from < %(t)s")
                 + " ORDER BY name_key, valid_from DESC, rev_id DESC) c WHERE NOT cleared ORDER BY name_key",
            {"f": from_date, "t": to_date})}

    with con.transaction():
        # the locks of the names concerned first (sorted: put_curve takes one, so no cycle), then read what they guard:
        # the names asked for, else those the source holds (a first read to know them, read again under the locks)
        for key in sorted(set(keys) if keys is not None else source()):
            _lock(con, key)
        known = dict(all_names(con))
        src = source()
        for key in (keys if keys is not None else list(src)):
            c = src.get(key)
            if c is None:
                skipped.append((key, known.get(key, key), "нет кривой на дату, с которой копируем"))
                continue
            own = entry_on(con, to_date, key)
            if own is not None and (keys is None or not own.cleared):
                skipped.append((key, c.name, "на эту дату имя снято" if own.cleared else "на эту дату кривая уже введена"))
                continue
            copied.append(_insert(con, key, c.name, to_date, c.spreads_bp, c.recovery, c.ccy, who))
    return CopyResult(to_date, copied, skipped)
