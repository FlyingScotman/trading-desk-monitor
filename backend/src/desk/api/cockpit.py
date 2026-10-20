"""Control center data (R20): ``GET /api/v1/cockpit``, what the control center page (``prototypes/control-center.html``)
shows beside window.DESK and what window.DESK does not carry. Read-only, from the store and the cached bundles of the
run's own snapshots; it never asks the hub.

* ``run``: the run's slice, when it was published and when its market snapshot was known (data freshness).
* ``log``: the run log's warnings and errors (counts and the first lines), for the data quality panel.
* ``history_runs``: the default run of each of the earlier dates window.DESK's ``history`` lists, so a past day's PnL
  opens its positions (``/api/v1/runs/{run_id}/pnl``).
* ``quotes``: of every bond the run holds, its MOEX close on the latest market date of the history (``md_bond_eod``)
  up to the run's date and on the date before (price, yield, G- and Z-spread, duration): the day's price moves.
* ``universe``: the RUB bonds quoted on that date with a G-spread and a duration, the market the relative value chart
  sets the book's bonds against.
* ``rates``: from the run's market snapshot, the CBR meetings ahead and the last decisions, and the latest fixings of
  the key rate, RUONIA and RUSFAR (the last ``FIXING_DAYS`` of each).
* ``calendar``: from the run's static snapshot, the cash flows of the held bonds from the run's date to
  ``HORIZON_DAYS`` after it (coupons, amortizations, maturities, puts and calls), per bond, and ``holdings`` (position
  -> secid, number of bonds), so the page multiplies them per position and keeps the desk filter. A coupon without a
  published amount (a floater's future period) is estimated from the position's current coupon and marked so.

What the snapshots do not hold stays out, said in ``gaps``: issuer limits, call dates of perpetuals that the hub does
not publish as offers, the market universe of earlier dates. A missing bundle (pruned from the cache) leaves its part
empty with the reason, never an error."""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..marketdata.bundle import SnapshotError, read_bundle
from ..store import DEFAULT_RUNS, slice_label

log = logging.getLogger("desk.api.cockpit")

HORIZON_DAYS = 31
FIXING_DAYS = 40
HISTORY_DAYS = 22  # compat.HISTORY_DAYS: the dates window.DESK's history lists
FIXINGS = ("KEY_RATE", "RUONIA", "RUSFAR", "RUSFAR3M", "RUSFARCNY")
LOG_LINES = 60
RUB_SETTLEMENT = ("SUR", "RUB")
_CACHE: OrderedDict[tuple[str, str], Any] = OrderedDict()
_CACHE_SIZE = 6


def _memo(key: tuple[str, str], build):
    """Bundles are content-addressed: what is read from one never changes. A few are kept per process."""
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    value = build()
    _CACHE[key] = value
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return value


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return None


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _bundle(snapshots: Path | None, snapshot_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(doc, None) or (None, why) for a cached bundle."""
    if not snapshot_id:
        return None, "у прогона нет среза"
    if snapshots is None:
        return None, "папка кэша снимков не задана"
    path = Path(snapshots) / f"{snapshot_id}.json.gz"
    if not path.is_file():
        return None, f"снимок {snapshot_id[:12]} не найден в кэше (удалён при очистке?)"
    try:
        return read_bundle(path).doc, None
    except (SnapshotError, OSError, ValueError) as e:
        return None, f"снимок {snapshot_id[:12]} не читается: {e}"


# ---- market snapshot: rates, CBR meetings ------------------------------------------------------------------------

def market_part(doc: dict[str, Any], business_date: date) -> dict[str, Any]:
    data = doc.get("data") or {}
    bd = business_date.isoformat()
    fixings: dict[str, list[list[Any]]] = {}
    for code in FIXINGS:
        rows = sorted((f["fixing_date"], _num(f.get("value"))) for f in data.get("fixings") or []
                       if f.get("index_code") == code and f.get("fixing_date") and f["fixing_date"] <= bd
                       and _num(f.get("value")) is not None)
        if rows:
            fixings[code] = [[d, v] for d, v in rows[-FIXING_DAYS:]]
    meetings = sorted(data.get("cbr_meetings") or [], key=lambda m: m.get("meeting_date") or "")
    past = [m for m in meetings if (m.get("meeting_date") or "") <= bd and m.get("decision")]
    ahead = [m for m in meetings if (m.get("meeting_date") or "") > bd]

    def meeting(m: dict[str, Any]) -> dict[str, Any]:
        return {"date": m.get("meeting_date"), "effective": m.get("effective_date"), "decision": m.get("decision"),
                "rate": _num(m.get("decision_rate_pct")), "prev_rate": _num(m.get("prev_rate_pct")),
                "estimated": bool(m.get("is_estimated")), "extraordinary": bool(m.get("is_extraordinary"))}

    return {"known_at": doc.get("known_at"), "cut_ts": doc.get("cut_ts"), "cut_type": doc.get("cut_type"),
            "fixings": fixings, "cbr_past": [meeting(m) for m in past[-4:]],
            "cbr_ahead": [meeting(m) for m in ahead[:8]]}


# ---- static snapshot: cash flows ---------------------------------------------------------------------------------

def _events_by_secid(doc: dict[str, Any], start: date, end: date) -> dict[str, list[dict[str, Any]]]:
    """The cash flow events of every bond of a static snapshot dated in [start, end]."""
    data = doc.get("data") or {}
    s, e = start.isoformat(), end.isoformat()
    inside = lambda d: bool(d) and s <= d <= e
    out: dict[str, list[dict[str, Any]]] = {}
    for c in data.get("coupon_periods") or []:
        if inside(c.get("pay_date")):
            out.setdefault(c["secid"], []).append({
                "kind": "coupon", "date": c["pay_date"], "record_date": c.get("record_date"),
                "start_date": c.get("start_date"), "rate_pct": _num(c.get("rate_pct")),
                "per_bond": _num(c.get("amount_per_bond")), "per_bond_rub": _num(c.get("amount_rub_per_bond")),
                "unit": c.get("face_unit") or "RUB", "face": _num(c.get("face_value")), "status": c.get("status")})
    for r in data.get("redemptions") or []:
        if inside(r.get("date")):
            out.setdefault(r["secid"], []).append({
                "kind": "maturity" if r.get("kind") == "maturity" else "amortization", "date": r["date"],
                "per_bond": _num(r.get("value_per_bond")), "per_bond_rub": _num(r.get("value_rub_per_bond")),
                "unit": r.get("face_unit") or "RUB", "face": _num(r.get("face_value")), "status": None})
    for o in data.get("offers") or []:
        if inside(o.get("offer_date")) and o.get("status") != "past":
            face, price = _num(o.get("face_value")), _num(o.get("price_pct"))
            per = _num(o.get("value_per_bond")) or (face * price / 100 if face is not None and price is not None
                                                    else face)
            out.setdefault(o["secid"], []).append({
                "kind": "call" if o.get("type") == "call" else "put", "date": o["offer_date"],
                "window_start": o.get("window_start"), "window_end": o.get("window_end"), "price_pct": price,
                "per_bond": per, "per_bond_rub": None, "unit": o.get("face_unit") or "RUB", "face": face,
                "status": o.get("status")})
    return out


def calendar_part(doc: dict[str, Any], static_id: str, business_date: date, holdings: dict[str, dict[str, Any]],
                  fx: dict[str, float], coupons: dict[str, float]) -> dict[str, Any]:
    """Events of the held bonds in [business_date, +HORIZON_DAYS], per bond in RUB. ``coupons``: secid -> the
    position's current coupon, %, for a coupon without an amount; ``fx``: RUB per unit of a foreign face."""
    end = business_date + timedelta(days=HORIZON_DAYS)
    every = _memo(("calendar", f"{static_id}:{business_date}"), lambda: _events_by_secid(doc, business_date, end))
    held = {h["secid"] for h in holdings.values()}
    events = []
    for secid in sorted(held):
        for ev in every.get(secid, []):
            ev = dict(ev, secid=secid, estimated=False, note=None)
            rate = fx.get(ev["unit"], 1.0 if ev["unit"] in ("RUB", "SUR") else None)
            if ev["per_bond_rub"] is None and ev["per_bond"] is not None and rate is not None:
                ev["per_bond_rub"] = ev["per_bond"] * rate
            if ev["kind"] == "coupon" and ev["per_bond_rub"] is None:
                cpn = ev["rate_pct"] if ev["rate_pct"] is not None else coupons.get(secid)
                days = ((date.fromisoformat(ev["date"]) - date.fromisoformat(ev["start_date"])).days
                        if ev.get("start_date") else None)
                if cpn is not None and days and ev["face"] is not None and rate is not None:
                    ev["per_bond_rub"] = ev["face"] * rate * cpn / 100 * days / 365
                    ev["estimated"] = True
                    ev["note"] = f"оценка: номинал × {cpn:.2f}% × {days} дн./365 (сумма купона не опубликована)"
                else:
                    ev["note"] = "сумма купона не опубликована"
            if ev["kind"] in ("put", "call"):
                ev["note"] = ("оферта: возможное погашение по цене оферты" if ev["kind"] == "put"
                              else "колл: возможное погашение эмитентом")
            events.append(ev)
    events.sort(key=lambda x: (x["date"], x["secid"], x["kind"]))
    return {"from": business_date.isoformat(), "to": end.isoformat(), "events": events}


# ---- store: quotes, universe, history runs -----------------------------------------------------------------------

QUOTE_SQL = ("SELECT business_date, secid, clean_pct, yield_pct, gspread_bp, zspread_bp, duration_days, value,"
             " currency_id, yield_date FROM md_bond_eod WHERE business_date = %s")


def quote_dates(con, business_date: date) -> tuple[date | None, date | None]:
    """The latest market date of the history up to ``business_date`` and the one before it."""
    rows = con.execute("SELECT business_date FROM md_kbd WHERE business_date <= %s ORDER BY 1 DESC LIMIT 2",
                       (business_date,)).fetchall()
    days = [r["business_date"] for r in rows]
    return (days[0] if days else None, days[1] if len(days) > 1 else None)


def _quote(r: dict[str, Any]) -> dict[str, Any]:
    return {"date": _iso(r["business_date"]), "price": r["clean_pct"], "ytm": r["yield_pct"], "g": r["gspread_bp"],
            "z": r["zspread_bp"], "dur_d": r["duration_days"], "value": r["value"], "ccy": r["currency_id"]}


def quotes_part(con, business_date: date, secids: list[str]) -> dict[str, Any]:
    d1, d0 = quote_dates(con, business_date)
    out: dict[str, Any] = {"date": _iso(d1), "prev_date": _iso(d0), "bonds": {}, "universe": None}
    if d1 is None:
        return out
    today = {r["secid"]: r for r in con.execute(QUOTE_SQL + " AND secid = ANY(%s)", (d1, secids))}
    prev = {r["secid"]: r for r in con.execute(QUOTE_SQL + " AND secid = ANY(%s)", (d0, secids))} if d0 else {}
    for s in secids:
        if s in today:
            out["bonds"][s] = {**_quote(today[s]), "prev": _quote(prev[s]) if s in prev else None}
    rows = con.execute(
        "SELECT q.secid, coalesce(i.short_name, i.name, q.secid) AS name, i.bond_class, q.yield_pct, q.gspread_bp,"
        " q.duration_days, q.value FROM md_bond_eod q LEFT JOIN md_instrument i USING (secid)"
        " WHERE q.business_date = %s AND q.gspread_bp IS NOT NULL AND q.duration_days > 0"
        " AND q.currency_id = ANY(%s) ORDER BY q.secid", (d1, list(RUB_SETTLEMENT))).fetchall()
    out["universe"] = {"date": _iso(d1), "cols": ["secid", "name", "class", "ytm", "g", "dur_y", "value"],
                       "rows": [[r["secid"], r["name"], r["bond_class"], r["yield_pct"], r["gspread_bp"],
                                 round(r["duration_days"] / 365.25, 4), r["value"]] for r in rows]}
    return out


def history_runs(con, business_date: date) -> list[dict[str, Any]]:
    rows = con.execute(f"SELECT run_id, business_date FROM run WHERE run_id IN ({DEFAULT_RUNS})"
                       " AND business_date < %s ORDER BY business_date DESC LIMIT %s",
                       (business_date, HISTORY_DAYS - 1)).fetchall()[::-1]
    return [{"date": r["business_date"].isoformat(), "run_id": r["run_id"]} for r in rows]


def build(con, run: dict[str, Any], snapshots: Path | None) -> dict[str, Any]:
    """The cockpit payload of a complete run (``con`` with dict rows)."""
    rid, bd = run["run_id"], run["business_date"]
    gaps: list[str] = []
    pos = con.execute("SELECT position_id, secid, quantity, fact->>'instr_type' AS itype,"
                      " (fact->>'coupon')::float8 AS coupon, fact->>'ccy' AS ccy FROM run_position"
                      " WHERE run_id = %s ORDER BY ord", (rid,)).fetchall()
    holdings = {p["position_id"]: {"secid": p["secid"], "quantity": p["quantity"]}
                for p in pos if p["secid"] and p["quantity"] and p["itype"] in ("BOND", None)}
    coupons = {p["secid"]: p["coupon"] for p in pos if p["secid"] and p["coupon"] is not None}
    secids = sorted({p["secid"] for p in pos if p["secid"]})
    fx = {r["ccy"]: r["rate"] for r in (con.execute(
        "SELECT data FROM run_reference WHERE run_id = %s AND kind = 'fx'", (rid,)).fetchone() or {"data": []})["data"]
        if r.get("rate")}

    lines = con.execute("SELECT level, message FROM run_log WHERE run_id = %s AND level <> 'INFO' ORDER BY seq",
                        (rid,)).fetchall()
    counts = {lvl: sum(r["level"] == lvl for r in lines) for lvl in ("WARN", "ERROR")}

    market, why = _bundle(snapshots, run["market_snapshot_id"])
    rates = (_memo(("market", f"{run['market_snapshot_id']}:{bd}"), lambda: market_part(market, bd))
             if market else None)
    if why:
        gaps.append(f"ставки и заседания ЦБ: {why}")
    static, why = _bundle(snapshots, run["static_snapshot_id"])
    calendar = calendar_part(static, run["static_snapshot_id"], bd, holdings, fx, coupons) if static else None
    if why:
        gaps.append(f"календарь выплат: {why}")
    quotes = quotes_part(con, bd, secids)
    if quotes["date"] is None:
        gaps.append("котировки облигаций: истории рынка (md_bond_eod) на дату прогона нет")
    gaps.append("лимиты по эмитентам и группам: данных о лимитах в монитор не приходит")
    gaps.append("колл-опционы бессрочных выпусков: хаб публикует только оферты с датой")

    cut_ts, done = run.get("cut_ts"), run.get("completed_at")
    basis = (run.get("lineage") or {}).get("pnl_basis") or {}
    return {
        "run": {"run_id": rid, "run_type": run["run_type"], "business_date": bd.isoformat(),
                "prev_date": _iso(run["prev_date"]), "cut_type": run["cut_type"], "cut_ts": _iso(cut_ts),
                "prev_cut_type": run.get("prev_cut_type"), "pnl_basis": basis.get("label"),
                "label": slice_label(bd, run["cut_type"], cut_ts, run["run_type"]),
                "started_at": _iso(run.get("started_at")), "completed_at": _iso(done),
                "market_snapshot_id": run["market_snapshot_id"], "static_snapshot_id": run["static_snapshot_id"],
                "market_known_at": rates["known_at"] if rates else None, "core_version": run.get("core_version")},
        "log": {"counts": counts, "lines": [{"level": r["level"], "message": r["message"]} for r in lines[:LOG_LINES]]},
        "history_runs": history_runs(con, bd),
        "quotes": {k: v for k, v in quotes.items() if k != "universe"},
        "universe": quotes["universe"],
        "rates": rates,
        "calendar": calendar,
        "holdings": holdings,
        "gaps": gaps,
    }
