"""Market history for the bond charts (R7): the market snapshots the desk sees, as daily rows of the desk store.

A market snapshot of cut ``eod_final`` gives ``md_kbd`` (the date's KBD parameters) and ``md_bond_eod`` (one quote per
bond: of its boards the one with a price, then the most trades), a static snapshot ``md_instrument`` (the terms the
history needs). ``md_snapshot_load`` lists every snapshot id handled, so a pass over a cache with nothing new costs a
folder listing and one query. Per business date the newest snapshot (latest ``known_at``) wins: a correction replaces
the date's rows, an older snapshot read later changes nothing. The newest static snapshot gives a bond's terms.

Filled by ``load_cache`` (every autopilot sync, ``desk sync``, ``desk import-drop``; the whole cache the first time),
by ``desk eod`` for the snapshots its run reads (``load_snapshot``) and, for dates the cache does not hold, by
``fetch_hub`` (the autopilot's history step, ``history_days`` deep). Those bundles are not kept: for the whole universe
a market bundle is ~0.5 MB and its static one ~4 MB (the fixtures' bytes per bond: ~140 per quote, ~1 200 per bond's
terms, times ~3 000 bonds), a year of them ~1 GB, while a year of rows is ~100 MB.

With a hub that has the history API (hub 0.2.0) and a token with scope ``read``, the autopilot's history step reads
the history from it instead (``desk.marketdata.hubhistory``: years in a few calls, the KBD, curves, fixings and FX
too, tables ``md_hub_*``) and ``fetch_hub`` is not used; without it (a hub before 0.2.0 answers 404, a token
without ``read`` 403) the step falls back to ``fetch_hub``. The readers take a day's row from the views
``md_bond_day`` / ``md_kbd_day``: the history API's row when it has one, else the snapshot's. Contract 1.2
quotes add MOEX's linker analytics (``icpi``, ``ir``, ``bei``, ``cbr``) to ``md_bond_eod``.

``bond_history`` reads one bond's series for ``GET /api/v1/bonds/history``: every quoted day, the desk's default runs
merged in (Z-spread, face held), and the KBD yield at the bond's term (to the offer while it is ahead, else to
maturity: DATA_CONTRACT §6) with the spread to it, for RUB fixed-coupon bonds.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb
from rub_fixed_income import ZcycParameters, kbd_effective_yield

from ..store import DEFAULT_RUNS
from ..valuation.bonds import PRICE_BASIS_ORDER
from .adapter import BondQuote, MarketView, StaticView, zcyc_parameters
from .bundle import Snapshot, SnapshotError, parse_bundle, read_bundle
from .hub import HubClient

log = logging.getLogger("desk.marketdata.history")

CUT_TYPE = "eod_final"
KBD_CURVE = "RUB OFZ"
"""The runs' name of the KBD (``publish.tdm_run.CURVE_ID``): the one curve the history has rates of."""
CURVE_COUPONS = ("fixed", "zero", "variable_known")
"""Coupon types whose yield is compared with the KBD: known cash flows. Floaters and OFZ-IN are not."""
RUB_SETTLEMENT = ("SUR", "RUB")
_LOCK = 0x6D646864  # "mdhd": one loader at a time per store
BAD_BUNDLE = (SnapshotError, KeyError, TypeError, ValueError, ArithmeticError)
"""What a snapshot that cannot be read raises (a malformed row, a missing field, a bad number)."""

EOD_COLS = ("business_date", "secid", "board", "clean_pct", "price_basis", "yield_pct", "yield_date", "duration_days",
            "gspread_bp", "zspread_bp", "accrued_per_bond", "face_value", "volume_bonds", "value", "currency_id",
            "num_trades", "icpi", "ir", "bei", "cbr")
INSTRUMENT_COLS = ("secid", "isin", "name", "short_name", "bond_class", "coupon_type", "face_unit", "maturity_date",
                   "is_perpetual", "business_date", "snapshot_id")


def _f(v: Any) -> float | None:
    return None if v is None else float(Decimal(v))


def _price(q: BondQuote) -> tuple[str, float] | None:
    """The run's choice of price basis (``valuation.bonds.select_price``), None without one."""
    return next(((b, float(q.prices_pct[b])) for b in PRICE_BASIS_ORDER if b in q.prices_pct), None)


def market_rows(snap: Snapshot) -> tuple[dict[str, Any], list[tuple]]:
    """The ``md_kbd`` fields and the ``md_bond_eod`` rows of a market snapshot, without the store. Raises one of
    ``BAD_BUNDLE`` when the snapshot cannot be read."""
    view = MarketView(snap)
    d = view.business_date
    params = view.zcyc.get("params")
    try:
        zcyc_parameters(view.zcyc["trade_date"], params or {}, snap.id)
    except SnapshotError:
        params = None  # the quotes still count; the date has no curve rate
    head = {"business_date": d, "snapshot_id": snap.id, "known_at": datetime.fromisoformat(snap.doc["known_at"]),
            "trade_date": view.zcyc.get("trade_date"), "params": params}
    boards: dict[str, list[BondQuote]] = defaultdict(list)
    for q in view.quotes.values():
        boards[q.secid].append(q)
    rows = []
    for secid, qs in sorted(boards.items()):
        q = max(qs, key=lambda q: (_price(q) is not None, q.num_trades or 0, q.board))
        px, sa = _price(q), q.source_analytics
        y = _f(sa.get("effective_yield_pct"))
        if px is None and y is None:
            continue  # no price and no yield: not a quoted day of the bond
        rows.append((d, secid, q.board, px and px[1], px and px[0], y,
                     date.fromisoformat(sa["yield_date"]) if sa.get("yield_date") else None,
                     _f(sa.get("duration_days")), _f(sa.get("gspread_bp")), _f(sa.get("zspread_bp")),
                     _f(q.accrued_per_bond), _f(q.face_value), _f(q.volume_bonds), _f(q.value), q.currency_id,
                     q.num_trades, *(_f(sa.get(k)) for k in ("icpi", "ir", "bei", "cbr"))))
    return head, rows


def instrument_rows(snap: Snapshot) -> list[tuple]:
    """The ``md_instrument`` rows of a static snapshot."""
    view = StaticView(snap)
    d = date.fromisoformat(snap.business_date)
    return [(t.secid, t.isin, t.name, t.short_name, t.bond_class, t.coupon_type, t.face_unit, t.maturity_date,
             t.is_perpetual, d, snap.id) for t in view.bonds.values()]


def loaded_ids(con: psycopg.Connection) -> set[str]:
    return {sid for (sid,) in con.execute("SELECT snapshot_id FROM md_snapshot_load")}


def load_snapshot(con: psycopg.Connection, snap: Snapshot) -> str:
    """One snapshot into the tables, idempotent: its status in ``md_snapshot_load`` (loaded, older, skipped, error).
    A snapshot that cannot be read is recorded as ``error`` with the reason (a bundle never changes)."""
    if row := con.execute("SELECT status FROM md_snapshot_load WHERE snapshot_id = %s", (snap.id,)).fetchone():
        return row[0]
    status, n, note = "skipped", 0, None
    head: dict[str, Any] | None = None
    rows: list[tuple] = []
    try:  # read first: nothing is written for a snapshot that cannot be read
        if snap.kind == "market" and snap.cut_type == CUT_TYPE:
            head, rows = market_rows(snap)
        elif snap.kind == "static":
            rows = instrument_rows(snap)
    except BAD_BUNDLE as e:
        status, note, rows = "error", f"{type(e).__name__}: {e}", []
        log.warning("history: snapshot %s not loaded: %s", snap.id[:12], note)
    with con.transaction():
        con.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
        if row := con.execute("SELECT status FROM md_snapshot_load WHERE snapshot_id = %s", (snap.id,)).fetchone():
            return row[0]  # another process loaded it meanwhile
        if head is not None:
            status, n = _write_market(con, head, rows)
        elif status != "error" and snap.kind == "static":
            status, n = "loaded", _write_instruments(con, rows)
        con.execute("INSERT INTO md_snapshot_load (snapshot_id, kind, cut_type, business_date, status, n_rows, note)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (snap.id, snap.kind, snap.cut_type, snap.business_date, status, n, note))
    return status


def _write_market(con: psycopg.Connection, head: dict[str, Any], rows: list[tuple]) -> tuple[str, int]:
    cur = con.execute("SELECT snapshot_id, known_at FROM md_kbd WHERE business_date = %s",
                      (head["business_date"],)).fetchone()
    if cur is not None and (cur[1], cur[0]) >= (head["known_at"], head["snapshot_id"]):
        return "older", 0
    con.execute("DELETE FROM md_kbd WHERE business_date = %s", (head["business_date"],))  # cascades to md_bond_eod
    con.execute("INSERT INTO md_kbd (business_date, snapshot_id, known_at, trade_date, params)"
                " VALUES (%s, %s, %s, %s, %s)",
                (head["business_date"], head["snapshot_id"], head["known_at"], head["trade_date"],
                 Jsonb(head["params"]) if head["params"] is not None else None))
    with con.cursor() as c, c.copy(f"COPY md_bond_eod ({', '.join(EOD_COLS)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
    return "loaded", len(rows)


def _write_instruments(con: psycopg.Connection, rows: list[tuple]) -> int:
    cols = ", ".join(INSTRUMENT_COLS)
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in INSTRUMENT_COLS[1:])
    with con.cursor() as c:
        c.executemany(f"INSERT INTO md_instrument ({cols}) VALUES ({', '.join(['%s'] * len(INSTRUMENT_COLS))})"
                      f" ON CONFLICT (secid) DO UPDATE SET {upd}"
                      " WHERE md_instrument.business_date <= EXCLUDED.business_date", rows)
    return len(rows)


@dataclass
class Loaded:
    """What a pass loaded: business dates whose rows were written, static snapshots, and the rest."""

    dates: list[str] = field(default_factory=list)
    statics: int = 0
    other: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, snap: Snapshot, status: str) -> None:
        if status == "loaded" and snap.kind == "market":
            self.dates.append(snap.business_date)
        elif status == "loaded":
            self.statics += 1
        elif status == "error":
            self.errors.append(f"{snap.id[:12]} unreadable")
        else:
            self.other += 1

    def text(self) -> str:
        """One line for the cycle's detail and the CLI; empty when nothing was new."""
        parts = []
        if self.dates:
            ds = sorted(self.dates)
            parts.append(f"date {ds[0]}" if len(ds) == 1 else f"{len(ds)} dates {ds[0]}..{ds[-1]}")
        if self.statics:
            parts.append(f"static {self.statics}")
        if self.other:
            parts.append(f"older or other cuts {self.other}")
        parts += self.errors
        return "history tables: " + ", ".join(parts) if parts else ""


def load_cache(con: psycopg.Connection, cache: Path, progress: Callable[[int, int, str], None] | None = None) -> Loaded:
    """Every bundle of the cache folder not handled yet (``md_snapshot_load``) into the tables; a cache with nothing
    new costs a listing and one query."""
    out = Loaded()
    folder = Path(cache)
    if not folder.is_dir():
        return out
    files = {p.name.removesuffix(".json.gz"): p for p in folder.glob("*.json.gz")}
    todo = sorted(set(files) - loaded_ids(con))
    for i, sid in enumerate(todo):
        if progress is not None:
            progress(i, len(todo), sid[:12])
        try:
            snap = read_bundle(files[sid])
        except OSError as e:  # unreadable now (a lock on Windows): tried again next pass
            out.errors.append(f"{sid[:12]}: {e}")
            continue
        except BAD_BUNDLE as e:  # not a bundle: never tried again
            con.execute("INSERT INTO md_snapshot_load (snapshot_id, status, note) VALUES (%s, 'error', %s)"
                        " ON CONFLICT DO NOTHING", (sid, f"{type(e).__name__}: {e}"))
            out.errors.append(f"{sid[:12]} unreadable")
            continue
        out.add(snap, load_snapshot(con, snap))
    return out


def fetch_hub(con: psycopg.Connection, client: HubClient, business_date: str) -> str | None:
    """The date's latest market snapshot from the hub straight into the tables, not cached (a date older than the
    cache keeps). Returns its load status; None when the hub has no snapshot for the date."""
    rows = list(client.list_snapshots(kind="market", cut_type=CUT_TYPE, business_date=business_date, latest=True))
    if not rows:
        return None
    sid = rows[0]["snapshot_id"]
    if row := con.execute("SELECT status FROM md_snapshot_load WHERE snapshot_id = %s", (sid,)).fetchone():
        return row[0]
    return load_snapshot(con, parse_bundle(client.bundle(sid), sid))


def loaded_dates(con: psycopg.Connection) -> set[str]:
    """The business dates with a KBD row: of a snapshot (``md_kbd``) or of the hub's history API (``md_kbd_day``)."""
    return {d.isoformat() for (d,) in con.execute("SELECT business_date FROM md_kbd_day")}


def latest_market_day(con: psycopg.Connection, on_or_before: date) -> date | None:
    """The latest day with bond quotes on or before a date, of the history API (``md_hub_bond``) or of the snapshots
    (``md_bond_eod``); two index lookups, not a scan of the view."""
    cur = con.cursor(row_factory=tuple_row)
    days = [r[0] for r in (cur.execute("SELECT max(trade_date) FROM md_hub_bond WHERE trade_date <= %s",
                                       (on_or_before,)).fetchone(),
                           cur.execute("SELECT max(business_date) FROM md_bond_eod WHERE business_date <= %s",
                                       (on_or_before,)).fetchone()) if r[0] is not None]
    return max(days) if days else None


def superseded_dates(con: psycopg.Connection, latest: list[tuple[str, str]]) -> list[str]:
    """The dates of ``latest`` ((date, the hub's current snapshot id), ``cache.hub_latest``) whose rows come from
    another snapshot while the hub's current one was never handled: a correction published after the date was loaded
    (the rows would otherwise stay on the superseded snapshot for good)."""
    rows = {d.isoformat(): sid for d, sid in con.execute("SELECT business_date, snapshot_id FROM md_kbd")}
    handled = loaded_ids(con)
    return [d for d, sid in latest if d in rows and rows[d] != sid and sid not in handled]


# ---- reading


def kbd(trade_date: date | None, params: dict[str, Any] | None) -> ZcycParameters | None:
    if not params or trade_date is None:
        return None
    try:
        return zcyc_parameters(trade_date, params)
    except BAD_BUNDLE:
        return None


def curve_rate(params: ZcycParameters | None, tenor_y: float | None) -> float | None:
    """The KBD's effective annual yield in % at ``tenor_y`` years (MOEX publishes its yields so), None without
    parameters or a term ahead."""
    if params is None or tenor_y is None or not tenor_y > 0:
        return None
    return kbd_effective_yield(params, tenor_y) * 100


def _years(d: date, until: date | None) -> float | None:
    return (until - d).days / 365.25 if until is not None and until > d else None


def bond_history(con: psycopg.Connection, *, instrument_id: str | None, isin: str | None, curve: str | None = None,
                 from_: date | None = None, to: date | None = None) -> dict[str, Any]:
    """The bond's series (the ``BondHistory`` shape): every day of ``md_bond_day`` (the history API's rows, else the
    snapshots') in the range and every default run that holds it, by date. Market fields (price, MOEX yield and G-spread, accrued, volume) come from the quote,
    the run fills those the quote lacks and adds the Z-spread and the desk's face (all books); a day without a run
    has ``face_amount`` null. The term (``tenor_y``) runs to the MOEX yield date (the offer while it is ahead, else
    maturity), else to maturity. ``curve_rate``: the KBD of the date at that term, for a RUB bond with known coupons,
    when ``curve`` is the KBD (``KBD_CURVE``; unset, ``own`` and ``base`` mean it too: the runs' only curve);
    ``spread_to_curve_bp`` = ytm - curve_rate. ``moex_zspread_bp`` is MOEX's Z-spread to the KBD; ``icpi``, ``ir``,
    ``bei``: MOEX's linker analytics as published (contract 1.2); ``origin``: ``hub`` (history API) or ``snapshot``."""
    on_kbd = curve in (None, "", "own", "base", KBD_CURVE)
    cur = con.cursor(row_factory=dict_row)
    facts = cur.execute(
        f"SELECT r.business_date, p.secid, p.fact FROM run_position p JOIN run r ON r.run_id = p.run_id"
        f" WHERE p.run_id IN ({DEFAULT_RUNS}) AND p.fact IS NOT NULL AND p.fact->>'instr_type' = 'BOND'"
        " AND (p.fact->>'instrument_id' = %(id)s OR p.fact->>'isin' = %(isin)s)"
        " AND (%(from)s::date IS NULL OR r.business_date >= %(from)s::date)"
        " AND (%(to)s::date IS NULL OR r.business_date <= %(to)s::date)"
        " ORDER BY r.business_date, p.ord", {"id": instrument_id, "isin": isin, "from": from_, "to": to}).fetchall()
    # the bond's secid: the instrument id, the runs' secid, the ISIN's bond in the static snapshots, the ISIN itself
    # (a corporate's secid is its ISIN)
    secids = [s for s in (instrument_id, *(f["secid"] for f in facts)) if s]
    if isin:
        secids += [r["secid"] for r in cur.execute("SELECT secid FROM md_instrument WHERE isin = %s ORDER BY"
                                                   " business_date DESC, secid", (isin,))] + [isin]
    secids = list(dict.fromkeys(secids))
    inst = cur.execute("SELECT * FROM md_instrument WHERE secid = ANY(%s)", (secids,)).fetchall()
    inst = min(inst, key=lambda r: secids.index(r["secid"])) if inst else None
    quotes = cur.execute(
        "SELECT DISTINCT ON (b.business_date) b.*, k.trade_date AS kbd_date, k.params FROM md_bond_day b"
        " LEFT JOIN md_kbd_day k USING (business_date) WHERE b.secid = ANY(%(s)s)"
        " AND (%(from)s::date IS NULL OR b.business_date >= %(from)s::date)"
        " AND (%(to)s::date IS NULL OR b.business_date <= %(to)s::date)"
        " ORDER BY b.business_date, array_position(%(s)s, b.secid)", {"s": secids, "from": from_, "to": to}).fetchall()
    curves = {r["business_date"]: kbd(r["trade_date"], r["params"]) for r in cur.execute(
        "SELECT business_date, trade_date, params FROM md_kbd_day WHERE business_date = ANY(%s)",
        (sorted({f["business_date"] for f in facts}),))}
    points: dict[date, dict[str, Any]] = {}
    for q in quotes:
        d = q["business_date"]
        curves[d] = kbd(q["kbd_date"], q["params"])
        rub = q["currency_id"] in RUB_SETTLEMENT
        points[d] = {"date": d, "clean_price": q["clean_pct"], "ytm": q["yield_pct"], "g_spread_bp": q["gspread_bp"],
                     "accrued": q["accrued_per_bond"], "volume_rub": q["value"] if rub else None,
                     "source": "market", "face_amount": None, "yield_date": q["yield_date"],
                     "price_basis": q["price_basis"], "moex_zspread_bp": q["zspread_bp"],
                     **{k: q[k] for k in ("icpi", "ir", "bei")}, "origin": q["origin"]}
    for r in facts:
        f, d = r["fact"], r["business_date"]
        p = points.setdefault(d, {"date": d, "source": "desk run"})
        if p.get("face_amount") is None:
            p.update({"face_amount": 0.0, "z_spread_bp": f.get("spread_bp"), "run_tenor_y": f.get("tenor_y")})
            if p["source"] == "market":
                p["source"] = "market + desk run"
            for k, v in (("clean_price", f.get("price")), ("ytm", f.get("ytm")), ("accrued", f.get("accrued"))):
                if p.get(k) is None:
                    p[k] = v
        p["face_amount"] += f.get("notional") or 0.0
    last = facts[-1]["fact"] if facts else {}
    maturity = inst["maturity_date"] if inst else (date.fromisoformat(last["maturity"]) if last.get("maturity")
                                                   else None)
    known = on_kbd and inst is not None and inst["face_unit"] == "RUB" and inst["coupon_type"] in CURVE_COUPONS
    series = []
    for d in sorted(points):
        p = points[d]
        run_t = p.pop("run_tenor_y", None)
        p["tenor_y"] = _years(d, p.get("yield_date")) or _years(d, maturity) or run_t
        rate = curve_rate(curves.get(d), p["tenor_y"]) if known else None
        p["curve_rate"] = rate
        p["spread_to_curve_bp"] = (p["ytm"] - rate) * 100 if rate is not None and p.get("ytm") is not None else None
        series.append(p)
    if not series:
        note = None
    elif not on_kbd:
        note = f"кривой {curve} в истории нет, есть только {KBD_CURVE} (КБД)"
    elif known:
        note = ("ставка кривой — КБД (кривая ОФЗ) даты на сроке до оферты или погашения; спред = доходность MOEX"
                " минус она")
    elif inst is None:
        note = "условий выпуска нет в снимках: ставка кривой не считается"
    else:
        note = (f"выпуск {'не в рублях' if inst['face_unit'] != 'RUB' else 'без известных купонов'}"
                f" ({inst['face_unit']}, купон {inst['coupon_type']}): спред к КБД не считается")
    return {"instrument_id": last.get("instrument_id") or (inst and inst["secid"]) or instrument_id,
            "isin": last.get("isin") or (inst and inst["isin"]) or isin,
            "name": last.get("name") or (inst and (inst["short_name"] or inst["name"])),
            "ccy": last.get("ccy") or (inst and inst["face_unit"]), "curve": KBD_CURVE if on_kbd else curve,
            "spread_to_curve_note": note, "series": series}
