"""Data of the concept pages (``prototypes/concepts/``) beyond ``window.DESK``: ``/concepts-data.js`` sets
``window.DESK_EXTRAS`` for the run the page shows, ``/api/v1/history/positions`` gives the positions' series for the
charts, ``/api/v1/calendar/market`` the market's payment calendar (``lib/real.js`` builds the pages' data from them
and ``window.DESK``).

``extras``: the desk's series over the default runs up to the run (risk by curve, CS01, the OFZ basis, DV01 by book,
market value by instrument type and currency, PnL by factor: each run's own sums, ``publish.summary``), per position
of the run its PnL month and year to date over those runs (``pnlPeriods`` says from which date and whether a business
day of the period has no run: then the sum is partial), the previous run's price and spread and what changed in its
holding (a trade, a new position), its flows of the next 18 months (``extra.cashflows``), the market of the latest
cached close not after the run (every quoted bond, ``universeRub`` and ``universeFx``, with the static snapshot's
issuer, group, sector, outstanding face, coupon and frequency: ``marketdata.static_facts``; the day's movers) and the
CBR meetings (``cbr``: the next one, the dates ahead, the past decisions). Issuer, group, sector and the desk's rating
(``marketdata.ratings``) of the market's bonds come from the newest static of the read model (R25). What the store does
not have is null with a note in ``notes``: ratings when the static has none, months before the first default run.

``positions_history``: per position of the run, aligned on one date axis: MOEX's clean price, yield (none for a
floater), G- and Z-spread and a linker's ICPI / IR / BEI from ``md_bond_day`` (the hub's history API, from
2024-01-03; else the snapshots, ``history_days`` deep), the KBD at the bond's term (a RUB bond with known coupons),
and from the default runs the face, DV01, CS01, OFZ basis, MV and DV01 by curve, carried forward between runs (null
before the first), and a floater's discount margin on the days of a run. A series that is zero or null on every date
is sent as null; the optional ones (``OPTIONAL``) are left out.

``market_calendar`` / ``market_flows``: the aggregates of the static snapshot of the run's date
(``static_facts.calendar``) and, for a drill-down, the flows themselves with their bond.

The classification (``kind_of``) and the curve ids (``CURVE_MAP``) are the pages' own and ``lib/real.js`` mirrors
them: a change here is a change there. The responses are cached by the API (``app``) per run and store state
(``fingerprint``): a new run, market date or static snapshot changes it."""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from psycopg.rows import tuple_row

from ..marketdata import static_facts
from ..marketdata.bundle import read_bundle
from ..marketdata.history import RUB_SETTLEMENT, curve_rate, curve_rates_by_day, kbd, latest_market_day
from ..publish import summary
from ..publish.summary import CURVE_MAP, GOV_CLASSES, PNL, kind_of  # noqa: F401 - the pages' names, re-exported
from ..store import DEFAULT_RUNS

HIST_RUNS = 260
"""At most this many default runs make the desk series (about a year of business days)."""
HISTORY_DAYS = 365
"""Default depth of ``positions_history`` (``from`` asks for more: the card of a position asks for its whole history)."""
FX_CCYS = ("USD", "EUR", "CNY", "CHF", "GBP", "HKD", "AED", "INR", "KZT", "BYN", "TRY", "JPY")
KIND_SETS = {"red": ("amort", "mat", "put", "call"), "redemption": ("amort", "mat"), "offer": ("put", "call")}
FLOW_LIMIT = 500
"""Rows of a calendar drill-down at most (the largest first)."""

Writer = Callable[[list[int]], None] | None
"""Writes the missing run summaries on a connection of its own (the API's connections are read-only)."""

FINGERPRINT_SQL = (
    "SELECT (SELECT max(run_id) FROM run WHERE status = 'complete') AS r,"
    " (SELECT count(*) FROM run WHERE status = 'complete') AS n,"
    " (SELECT max(completed_at) FROM run WHERE status = 'complete') AS c,"
    " (SELECT max(loaded_at) FROM md_kbd) AS k,"
    " (SELECT max(loaded_at) FROM md_static_summary) AS s,"
    " (SELECT max(snapshot_id) FROM md_cbr_meeting) AS m,"
    " (SELECT max(fetch_id) FROM md_hub_job) AS h, (SELECT sum(rows) FROM md_hub_job) AS hr")


def fingerprint(con) -> str:
    """What the cached responses depend on: the complete runs, the market history (the snapshots' and the hub
    history API's, ``md_hub_job``), the static facts, the CBR meetings."""
    r = con.execute(FINGERPRINT_SQL).fetchone()
    return json.dumps(list(r.values()) if isinstance(r, dict) else list(r), default=str)


def _runs(con, run: dict[str, Any]) -> list[dict[str, Any]]:
    """The default runs up to the run's date (the run itself last, also when it is an intraday run), oldest first."""
    rows = con.execute(f"SELECT run_id, business_date FROM run WHERE run_id IN ({DEFAULT_RUNS})"
                       " AND business_date < %s ORDER BY business_date DESC LIMIT %s",
                       (run["business_date"], HIST_RUNS - 1)).fetchall()[::-1]
    return [dict(r) for r in rows] + [{"run_id": run["run_id"], "business_date": run["business_date"]}]


def summaries(con, run_ids: list[int], writer: Writer = None,
              pids: list[str] | None = None) -> dict[int, dict[str, Any]]:
    """The runs' summaries (``publish.summary``): read, the missing ones written by ``writer`` first, any still
    missing (no writer, a read-only store) built in memory. ``pids``: only ``pos`` of these positions is read (a
    year of summaries is megabytes of JSON; one position's history needs a few kilobytes of it)."""
    def read(ids: list[int]) -> dict[int, dict[str, Any]]:
        if pids is not None:
            return {r["run_id"]: {"pos": r["pos"] or {}} for r in con.execute(
                "SELECT run_id, (SELECT jsonb_object_agg(k, data->'pos'->k) FROM unnest(%s::text[]) k"
                " WHERE data->'pos' ? k) AS pos FROM run_summary WHERE run_id = ANY(%s) AND version = %s",
                (pids, ids, summary.VERSION))}
        return {r["run_id"]: r["data"] for r in con.execute(
            "SELECT run_id, data FROM run_summary WHERE run_id = ANY(%s) AND version = %s", (ids, summary.VERSION))}

    have = read(run_ids)
    missing = [i for i in run_ids if i not in have]
    if missing and writer is not None:
        with contextlib.suppress(Exception):  # a store that cannot be written: built in memory below
            writer(missing)
            have.update(read(missing))
    for rid in [i for i in run_ids if i not in have]:
        have[rid] = summary.build(con.execute(summary.ROWS_SQL, (rid,)).fetchall())
    return have


def _periods(con, as_of: date, dates: list[str]) -> tuple[dict[str, Any], list[str]]:
    """``pnlPeriods`` and its notes: for the month and the year to ``as_of``, the first date with a default run and
    whether the sum is partial: the store's positions start after the period's first business day (the market
    history's dates, ``md_kbd``), a business day from the first positions on has no default run, or the market
    history does not reach back to the period's start (its business days are unknown)."""
    first_pos = con.execute("SELECT min(valid_date) AS d FROM position_set").fetchone()["d"]
    oldest = con.execute("SELECT min(business_date) AS d FROM md_kbd").fetchone()["d"]
    have = set(dates)
    out, notes = {}, []
    for key, start, label in (("mtd", as_of.replace(day=1), "с начала месяца"),
                              ("ytd", as_of.replace(day=1, month=1), "с начала года")):
        ref = [r["business_date"].isoformat() for r in con.execute(
            "SELECT business_date FROM md_kbd WHERE business_date >= %s AND business_date <= %s ORDER BY 1",
            (start, as_of))]
        lo = max(start, first_pos).isoformat() if first_pos else start.isoformat()
        missing = [d for d in ref if d >= lo and d not in have]
        runs_in = [d for d in dates if d >= start.isoformat()]
        why = []
        if oldest is None or oldest > start:
            why.append("история рынка начинается позже начала периода")
        elif first_pos is not None and ref and first_pos.isoformat() > ref[0]:
            why.append(f"позиции в базе с {first_pos.isoformat()}")
        if missing:
            why.append(f"нет прогонов за {len(missing)} рабочих дн. (первый {missing[0]})")
        out[key] = {"from": runs_in[0] if runs_in else None, "runs": len(runs_in), "missing": len(missing),
                    "missingDates": missing[:10], "complete": not why and bool(runs_in),
                    "positionsFrom": first_pos.isoformat() if first_pos else None, "why": why}
        if why and runs_in:
            notes.append(f"PnL {label} — частичный: сумма {len(runs_in)} прогонов с {runs_in[0]}; {'; '.join(why)}"
                         + ("; досчитать: desk backfill" if missing else ""))
    return out, notes


def extras(con, run: dict[str, Any], writer: Writer = None) -> dict[str, Any]:
    runs = _runs(con, run)
    dates = [r["business_date"].isoformat() for r in runs]
    n = len(runs)
    S = summaries(con, [r["run_id"] for r in runs], writer)
    sums = [S[r["run_id"]] for r in runs]
    as_of = run["business_date"]

    def series(field: str) -> dict[str, list[float]]:
        keys = list(dict.fromkeys(k for s in sums for k in s[field]))
        return {k: [s[field].get(k, 0.0) for s in sums] for k in keys}

    pnl = {k: [(s["pnl"] or {}).get(k) if s["pnl"] else None for s in sums] for k in (*PNL, "total")}
    # the day's trades, the previous price and the spread change: against the run of the previous close only (the
    # run's prev_date); an older run (a gap in the history of runs, the backfill still running) would show every
    # change since then as a trade of the day
    prev_ok = n > 1 and run.get("prev_date") is not None and runs[-2]["business_date"] == run["prev_date"]
    cur, prev = sums[-1]["pos"], (sums[-2]["pos"] if prev_ok else None)
    acc: dict[str, list[float]] = {pid: [0.0, 0.0] for pid in cur}
    y, m = as_of.year, as_of.month
    for r, s in zip(runs, sums):
        d = r["business_date"]
        if d.year != y:
            continue
        mtd = d.month == m
        for pid, a in acc.items():
            p = s["pos"].get(pid)
            if p is not None and p[5] is not None:
                a[1] += p[5]
                if mtd:
                    a[0] += p[5]
    has_pnl = {pid for s in sums for pid, p in s["pos"].items() if p[5] is not None}
    qty = {(r["run_id"], r["position_id"]): r["quantity"] for r in con.execute(
        "SELECT run_id, position_id, quantity FROM run_position WHERE run_id = ANY(%s) AND quantity IS NOT NULL",
        ([run["run_id"], runs[-2]["run_id"]],))} if prev is not None else {}
    positions: dict[str, dict[str, Any]] = {}
    for pid, p in cur.items():
        out: dict[str, Any] = {}
        if pid in has_pnl:
            out["pnlMtd"], out["pnlYtd"] = acc[pid]
        p0 = prev.get(pid) if prev is not None else None
        if p0 is not None:
            out["prevPrice"] = p0[6]
            if p[7] is not None and p0[7] is not None:
                out["spreadChgBp"] = p[7] - p0[7]
            dq = (p[0] or 0.0) - (p0[0] or 0.0)
            n1, n0 = qty.get((run["run_id"], pid)), qty.get((runs[-2]["run_id"], pid))
            if n1 and n0 is not None:  # the number of bonds: a linker's indexed or an amortizer's face moves by itself
                dq = (n1 - n0) * (p[0] or 0.0) / n1 if abs(n1 - n0) > 1e-6 * max(1.0, abs(n1)) else 0.0
            if abs(dq) > 1e-6 * max(1.0, abs(p[0] or 0.0)):
                out["trade"] = {"qty": dq, "price": p[6]}
        elif prev is not None:
            out["trade"] = {"qty": p[0], "price": p[6], "new": True}
        positions[pid] = out
    periods, notes = _periods(con, as_of, dates)
    first = runs[0]["business_date"]
    if first.replace(day=1) > as_of.replace(day=1, month=1) and not notes:
        notes.append(f"история прогонов с {first.isoformat()}: PnL с начала года — с этой даты")
    flows = []
    held = set()
    for x in con.execute("SELECT position_id, fact->>'ccy' AS ccy, fact->>'instrument_id' AS secid,"
                         " fact->'extra'->'cashflows' AS cf FROM run_position WHERE run_id = %s AND fact IS NOT NULL",
                         (run["run_id"],)):
        held.add(x["secid"])
        for d, kind, amount, est in x["cf"] or []:
            flows.append({"date": d, "pos": x["position_id"], "kind": kind, "ccy": x["ccy"] or "RUB",
                          "amount": amount, "est": bool(est)})
    if not flows and cur:
        notes.append("потоков позиций в прогоне нет: прогон посчитан до версии с потоками (пересчитается последняя дата)")
    if not prev_ok and run.get("prev_date") is not None:
        notes.append(f"сделки дня и изменения к вчера не показаны: нет прогона за {run['prev_date'].isoformat()}"
                     " (досчитать: desk backfill)")
    flows.sort(key=lambda c: c["date"])
    market = universe(con, as_of, held)
    notes += market.pop("notes")
    cb = cbr(con, as_of)
    if cb["next"] is None and not cb["meetings"]:
        notes.append("заседаний ЦБ нет в данных: появятся после прогона на снимке с cbr_meetings")
    return {"runId": run["run_id"], "asOf": as_of.isoformat(), "histDates": dates,
            "hist": {"dv01": series("dv01"), "cs01": [s["cs01"] for s in sums], "basis01": [s["basis"] for s in sums],
                     "dv01Book": series("dv01Book"), "mvType": series("mvType"), "mvCcy": series("mvCcy"), "pnl": pnl},
            "histSource": "runs", "pnlPeriods": periods,
            "positions": positions, "cashflows": flows, **market,
            "cbr": cb, "curveMap": CURVE_MAP, "notes": notes}


def cbr(con, as_of: date) -> dict[str, Any]:
    """The CBR meetings of the newest market snapshot loaded (``md_cbr_meeting``): ``next`` (date, label, whether
    the date is the hub's estimate, the key rate in force), ``meetings`` (the dates after ``as_of``), ``past`` (the
    decisions on or before it, newest first, at most 12). Consensus and the rate the curve prices are not in the data
    (null)."""
    rows = con.execute("SELECT * FROM md_cbr_meeting ORDER BY meeting_date").fetchall()
    d = as_of.isoformat()
    ahead = [r for r in rows if r["meeting_date"].isoformat() > d]
    past = [r for r in rows if r["meeting_date"].isoformat() <= d and r["decision"]]
    key = past[-1]["decision_rate_pct"] if past else None
    nxt = None
    if ahead:
        a = ahead[0]
        nxt = {"date": a["meeting_date"].isoformat(), "effective": a["effective_date"].isoformat() if a["effective_date"]
               else None, "label": "заседание ЦБ по ключевой ставке" + (" (дата ориентировочная)" if a["is_estimated"]
                                                                       else ""),
               "estimated": bool(a["is_estimated"]), "keyRate": key, "consensus": None, "priced": None}
    names = {"hike": "повышение", "cut": "снижение", "hold": "без изменений"}
    return {"next": nxt, "meetings": [r["meeting_date"].isoformat() for r in ahead],
            "estimated": [r["meeting_date"].isoformat() for r in ahead if r["is_estimated"]],
            "past": [{"date": r["meeting_date"].isoformat(), "decision": r["decision"],
                      "decisionText": names.get(r["decision"], r["decision"]), "rate": r["decision_rate_pct"],
                      "prev": r["prev_rate_pct"], "extraordinary": bool(r["is_extraordinary"])} for r in past[::-1][:12]],
            "keyRate": key, "source": rows[0]["business_date"].isoformat() if rows else None}


MARKET_SQL = ("SELECT b.*, i.isin, i.name, i.short_name, i.bond_class, i.coupon_type, i.face_unit, i.maturity_date,"
              " i.is_perpetual, COALESCE(n.data, s.data) AS st FROM md_bond_day b LEFT JOIN md_instrument i USING (secid)"
              " LEFT JOIN md_static_bond n ON n.snapshot_id = %s AND n.secid = b.secid"
              " LEFT JOIN md_static_bond s ON s.snapshot_id = %s AND s.secid = b.secid WHERE b.business_date = %s")
"""A day's market: ``md_bond_day``, the hub's history API rows, else the snapshots' (R24), with the static facts of the
newest static (issuer, group, sector, rating: R25), else of the static of the date (a bond matured since)."""


def _static_for(con, as_of: date) -> dict[str, Any] | None:
    s = static_facts.static_for(con, as_of)
    return s


def universe(con, as_of: date, held: set[str | None]) -> dict[str, Any]:
    """The market of the latest cached close not after ``as_of``: every quoted RUB bond (``universeRub``), every quoted
    bond with a foreign face (``universeFx``) and the 60 RUB bonds with the most turnover with their yield change since
    the close before (``marketMovers``). Issuer, group, sector, outstanding face (``size``, RUB; ``outstanding`` in the
    face currency), coupon and frequency from the static snapshot's facts (``static_facts``; the newest static's for
    the issuer, sector, group and rating, R25)."""
    d = latest_market_day(con, as_of)
    if d is None:
        return {"marketDate": None, "universeRub": [], "universeFx": [], "marketMovers": [],
                "notes": ["рыночной истории (md_bond_eod) нет: карта рынка пуста"]}
    st = _static_for(con, as_of)
    sid = st["snapshot_id"] if st else None
    new = static_facts.newest(con)
    nid = new["snapshot_id"] if new else None
    prev = latest_market_day(con, d - timedelta(days=1))
    old = {r["secid"]: r["yield_pct"] for r in con.execute(
        "SELECT secid, yield_pct FROM md_bond_day WHERE business_date = %s", (prev,))} if prev else {}
    fx_coupons: dict[str, list[str]] = {}
    if sid:
        for r in con.execute("SELECT secid, array_agg(pay_date::text ORDER BY pay_date) AS d FROM md_market_flow"
                             " WHERE snapshot_id = %s AND market = 'fx' AND kind = 'cpn' AND pay_date > %s"
                             " GROUP BY secid", (sid, as_of)):
            fx_coupons[r["secid"]] = r["d"]
    rub, fx = [], []
    with_static = 0
    for r in con.execute(MARKET_SQL, (nid, sid, d)).fetchall():
        mat, yd = r["maturity_date"], r["yield_date"]
        ev = yd if yd and (mat is None or yd < mat) else mat
        tenor = (ev - d).days / 365.25 if ev and ev > d else None
        flt = r["coupon_type"] == "floating"
        y = r["yield_pct"]
        chg = (y - old[r["secid"]]) * 100 if y is not None and old.get(r["secid"]) is not None else None
        f = r["st"] or {}
        with_static += bool(f)
        b = {"id": r["secid"], "isin": r["isin"], "name": r["short_name"] or r["name"] or r["secid"],
             "fullName": r["name"], "latName": f.get("lat_name"),
             "maturity": mat.isoformat() if mat else None,
             "put": yd.isoformat() if yd and mat and yd < mat else None, "tenor": tenor,
             "ytm": None if flt else y, "ytmMoex": y, "dur": (r["duration_days"] or 0) / 365.25 or None,
             "spread": r["gspread_bp"], "zspread": r["zspread_bp"], "chgBp": chg, "value": r["value"],
             "flt": flt, "held": r["secid"] in held, "rating": f.get("rating"),
             "ratingAgency": (f.get("rating_info") or {}).get("agency"),
             "ratingInfo": f.get("rating_info"), "sector": f.get("sector_label"),
             "group": f.get("group"), "issuerName": f.get("issuer"), "issuerFull": f.get("issuer_full"),
             "type": f.get("type"), "cpn": f.get("cpn_pct"), "freq": f.get("freq_letter"),
             "issueSize": f.get("issue_size"),
             **{k: r[k] for k in ("icpi", "ir", "bei") if r[k] is not None}}
        unit = r["face_unit"] or f.get("face_unit") or ("RUB" if r["currency_id"] in RUB_SETTLEMENT else None)
        if unit == "RUB" or unit is None:
            b["isOfz"] = (r["bond_class"] or "").startswith("ofz")
            if b["isOfz"]:  # the sovereign: no agency rating (not «б/р»), the pages' sector «ОФЗ»
                b.update(rating=None, ratingAgency=None, ratingInfo=None, sector="ОФЗ")
            b["size"] = f.get("outstanding")
            b["issuer"] = f.get("issuer") or None
            rub.append(b)
        else:
            if f.get("foreign_issuer"):
                continue
            iss = f.get("issuer") or b["name"].split(" ")[0]
            b.update({"ccy": unit, "origCcy": unit, "issuer": iss, "issuerName": iss,
                      "outstanding": f.get("outstanding"), "coupons": fx_coupons.get(r["secid"], [])})
            fx.append(b)
    movers = sorted((b for b in rub if b["chgBp"] is not None and b["value"]), key=lambda b: -b["value"])[:60]
    notes = []
    if st is None:
        notes.append(f"рынок на {d.isoformat()}: у выпусков нет эмитента, сектора, объёма и купона — снимок статики "
                     "ещё не разобран (появится после прогона)")
    elif st["business_date"] != d:
        notes.append(f"условия выпусков (эмитент, сектор, объём, купон) — по статике {st['business_date']}")
    cov = ((new or st or {}).get("data") or {}).get("calendar", {}).get("coverage", {})
    if not any(b.get("rating") for b in rub):
        notes.append("рейтингов в статике хаба нет (" + ((new or st or {}).get("business_date") or date.min).isoformat()
                     + "): G-спред по рейтингам — одной группой «без рейтинга»")
    else:
        notes.append(f"рейтинги — лучший из АКРА / Эксперт РА / НКР / НРА по национальной шкале (выпуск, иначе эмитент;"
                     f" у {cov.get('with_rating', 0)} из {cov.get('bonds', 0)} выпусков статики"
                     f" {new['business_date'].isoformat() if new else ''})")
    notes.append("объём выпуска — по MOEX (для ОФЗ — объявленный, а не размещённый)")
    return {"marketDate": d.isoformat(), "universeRub": rub, "universeFx": fx,
            "staticDate": st["business_date"].isoformat() if st else None,
            "referenceDate": new["business_date"].isoformat() if new else None,
            "marketMovers": [{"name": b["name"], "rating": b["rating"], "sector": b["sector"], "ytm": b["ytmMoex"],
                              "chgBp": b["chgBp"], "vol": b["value"]} for b in movers],
            "notes": notes}


def market_calendar(con, as_of: date, held: set[str] | None = None) -> dict[str, Any] | None:
    """The market's payment calendar of the static snapshot for ``as_of`` (``static_facts.calendar``), plus the
    snapshot's id and date and the held bonds' secids; None when no static snapshot is in the read model."""
    st = _static_for(con, as_of)
    if st is None:
        return None
    cal = dict(st["data"]["calendar"])
    cal.update({"snapshotId": st["snapshot_id"], "staticDate": st["business_date"].isoformat(),
                "held": sorted(h for h in (held or ()) if h)})
    return cal


def position_flows(con, run: dict[str, Any], pid: str, snapshots: Path) -> dict[str, Any] | None:
    """A bond position's whole schedule after the run's date (``static_facts.schedule`` of the static snapshot the read
    model shows for the run, × the position's bonds): coupons, amortizations, maturity, and the put offers and calls
    on the face outstanding then, in the face currency, signed by the side. The position card shows it; the stored
    ``extra.cashflows`` stop at 18 months and at the offer the bond is priced to. None: not a bond, or the static
    or its bundle is missing (the card then shows the stored flows)."""
    row = con.execute("SELECT fact->>'instrument_id' AS secid, (fact->'extra'->>'quantity_bonds')::float8 AS q,"
                      " fact->'extra'->>'offer_date' AS offer, fact->'extra'->>'offer_kind' AS offer_kind"
                      " FROM run_position WHERE run_id = %s AND position_id = %s AND fact IS NOT NULL",
                      (run["run_id"], pid)).fetchone()
    if row is None or not row["secid"] or row["q"] is None:
        return None
    st = static_facts.static_for(con, run["business_date"])
    if st is None:
        return None
    path = Path(snapshots) / f"{st['snapshot_id']}.json.gz"
    if not path.is_file() and st["snapshot_id"] not in static_facts._SCHEDULES:
        return None
    sch = static_facts.schedule(st["snapshot_id"], lambda: read_bundle(path), row["secid"], run["business_date"])
    if sch is None:
        return None
    q = row["q"]
    return {"staticDate": st["business_date"].isoformat(), "ccy": sch["ccy"], "maturity": sch["maturity"],
            "offer": row["offer"], "offerKind": row["offer_kind"],
            "flows": [[d, kind, None if amt is None else amt * q, est] for d, kind, amt, est in sch["flows"]]}


def held_secids(con, run_id: int) -> set[str]:
    return {r["s"] for r in con.execute("SELECT fact->>'instrument_id' AS s FROM run_position WHERE run_id = %s"
                                        " AND fact IS NOT NULL", (run_id,)) if r["s"]}


def market_flows(con, as_of: date, *, market: str, date_from: date | None = None, date_to: date | None = None,
                 type_: str | None = None, kind: str | None = None, ccy: str | None = None, sector: str | None = None,
                 big: bool = False, issuer: str | None = None, secid: str | None = None,
                 held: set[str] | None = None, limit: int = FLOW_LIMIT) -> dict[str, Any] | None:
    """The flows behind a calendar cell (``md_market_flow``), the largest first, at most ``limit``, with the totals of
    all that match. ``kind``: one kind, or ``KIND_SETS`` (``red``: everything but coupons; ``redemption``: maturities
    and amortizations; ``offer``: puts and calls)."""
    st = _static_for(con, as_of)
    if st is None:
        return None
    where, args = ["f.snapshot_id = %s", "f.market = %s"], [st["snapshot_id"], market]
    for cond, v in (("f.pay_date >= %s", date_from), ("f.pay_date <= %s", date_to), ("f.type = %s", type_),
                    ("f.ccy = %s", ccy), ("f.secid = %s", secid), ("s.data->>'issuer' = %s", issuer)):
        if v is not None:
            where.append(cond)
            args.append(v)
    if kind in KIND_SETS:
        where.append("f.kind = ANY(%s)")
        args.append(list(KIND_SETS[kind]))
    elif kind:
        where.append("f.kind = %s")
        args.append(kind)
    if sector is not None:
        if sector == "ОФЗ":  # the OFZ by type (their sector, by the bond class since R25, is the sovereign's)
            where.append("f.type IN ('PD', 'PK', 'IN')")
        elif sector == "—":
            where.append("s.data->>'sector_label' IS NULL AND f.type NOT IN ('PD', 'PK', 'IN')")
        else:
            where.append("s.data->>'sector_label' = %s AND f.type NOT IN ('PD', 'PK', 'IN')")
            args.append(sector)
    if big:
        where.append("(s.data->>'outstanding')::float8 >= %s")
        args.append(static_facts.BIG_RUB)
    sql = (" FROM md_market_flow f JOIN md_static_bond s ON s.snapshot_id = f.snapshot_id AND s.secid = f.secid"
           " WHERE " + " AND ".join(where))
    tot = con.execute("SELECT count(*) AS n, sum(f.amount) AS amount" + sql, args).fetchone()
    rows = con.execute(
        "SELECT f.pay_date, f.kind, f.secid, f.type, f.ccy, f.amount, f.per_bond, f.est, s.data->>'isin' AS isin,"
        " coalesce(s.data->>'short_name', s.data->>'name') AS name, s.data->>'name' AS full_name,"
        " s.data->>'issuer' AS issuer, s.data->>'group' AS grp, s.data->>'sector_label' AS sector,"
        " (s.data->>'outstanding')::float8 AS outstanding" + sql
        + " ORDER BY f.amount DESC NULLS LAST, f.pay_date LIMIT %s", [*args, limit]).fetchall()
    h = held or set()
    return {"staticDate": st["business_date"].isoformat(), "count": tot["n"], "amount": tot["amount"],
            "rows": [{"date": r["pay_date"].isoformat(), "kind": r["kind"], "secid": r["secid"], "isin": r["isin"],
                      "name": r["name"], "fullName": r["full_name"], "issuer": r["issuer"], "group": r["grp"],
                      "sector": r["sector"], "type": r["type"], "ccy": r["ccy"], "amount": r["amount"],
                      "perBond": r["per_bond"], "est": r["est"], "outstanding": r["outstanding"],
                      "held": r["secid"] in h} for r in rows]}


def _rnd(v: float | None, nd: int) -> float | None:
    return None if v is None or not math.isfinite(v) else round(v, nd)


def _thin(s: list) -> list | None:
    """A series with nothing in it (null or zero on every date) as null: the pages read null as zeros."""
    return s if any(v for v in s) else None


def positions_history(con, run: dict[str, Any], ids: list[str] | None = None, from_: date | None = None,
                      writer: Writer = None) -> dict[str, Any]:
    from_ = from_ or run["business_date"] - timedelta(days=HISTORY_DAYS)
    cur = con.execute("SELECT position_id, secid, fact FROM run_position WHERE run_id = %s AND fact IS NOT NULL"
                      + (" AND position_id = ANY(%s)" if ids else ""),
                      (run["run_id"], ids) if ids else (run["run_id"],)).fetchall()
    pos = {r["position_id"]: r for r in cur}
    if not pos:
        return {"runId": run["run_id"], "dates": [], "series": {}}
    runs = [r for r in _runs(con, run) if r["business_date"] >= from_]
    S = summaries(con, [r["run_id"] for r in runs], writer, list(pos) if ids else None)
    by_run = [(r["business_date"], S[r["run_id"]]["pos"]) for r in runs]
    secid = {pid: (r["fact"].get("instrument_id") or r["secid"]) for pid, r in pos.items()
             if r["fact"].get("instr_type") == "BOND"}
    quotes: dict[str, dict[date, tuple]] = {}
    with con.cursor(row_factory=tuple_row) as cur:  # ~150k rows a year of 600 bonds: tuples, rounded by the store
        for d, sid, *q in cur.execute(
                "SELECT business_date, secid, round(clean_pct::numeric, 4)::float8, round(yield_pct::numeric, 4)::float8,"
                " round(gspread_bp::numeric, 1)::float8, yield_date, round(zspread_bp::numeric, 1)::float8, icpi, ir, bei"
                " FROM md_bond_day"
                " WHERE secid = ANY(%s) AND business_date >= %s AND business_date <= %s",
                (list(set(secid.values())), from_, run["business_date"])):
            quotes.setdefault(sid, {})[d] = q
    dates = sorted({d for s in quotes.values() for d in s} | {d for d, _ in by_run})
    curves = {r["business_date"]: kbd(r["trade_date"], r["params"]) for r in con.execute(
        "SELECT business_date, trade_date, params FROM md_kbd_day WHERE business_date = ANY(%s)", (dates,))}
    run_at = dict(by_run)
    # the KBD at each fixed RUB bond's term on each date, all at once per date (core kbd_effective_yields)
    want: dict[date, set[int]] = {}
    for pid, r in pos.items():
        f = r["fact"]
        if f.get("ccy") in (None, "RUB") and f.get("coupon_type") == "fix":
            mat = date.fromisoformat(f["maturity"]) if f.get("maturity") else None
            q = quotes.get(secid.get(pid) or "", {})
            for d in dates:
                yd = (q[d][3] if d in q and q[d][3] else None) or mat
                if yd and yd > d:
                    want.setdefault(d, set()).add((yd - d).days)
    bench_cache: dict[tuple[date, int], float | None] = curve_rates_by_day(curves, want)
    cpi = _cpi_known(con, run["business_date"]) if any(_linker(r["fact"]) for r in pos.values()) else []

    def bench(d: date, t: float | None) -> float | None:
        if t is None:
            return None
        k = (d, round(t * 365.25))
        if k not in bench_cache:
            bench_cache[k] = curve_rate(curves.get(d), t)
        return bench_cache[k]

    series = {}
    for pid, r in pos.items():
        f = r["fact"]
        flt = f.get("coupon_type") == "float"
        rub_fix = f.get("ccy") in (None, "RUB") and f.get("coupon_type") == "fix"
        mat = date.fromisoformat(f["maturity"]) if f.get("maturity") else None
        q = quotes.get(secid.get(pid) or "", {})
        s: dict[str, list] = {k: [] for k in ("price", "ytm", "spr", "notional", "dv01", "cs01", "basis", "mv", "bench", *OPTIONAL)}
        cv: dict[str, list] = {}
        last: list | None = None
        for i, d in enumerate(dates):
            x = q.get(d)
            ps = run_at.get(d)
            if ps is not None:
                last = ps.get(pid)  # None: not held on that run date
            s["price"].append(x[0] if x else (f.get("price") if d == run["business_date"] else None))
            s["ytm"].append(None if flt or not x else x[1])
            s["spr"].append(x[2] if x else None)
            s["zspr"].append(x[4] if x else None)
            s["dm"].append((last[9] if len(last) > 9 and last[9] is not None else last[7])  # core DM, else the Z
                           if flt and last and ps is not None else None)
            for k, j in (("icpi", 5), ("ir", 6), ("bei", 7)):
                s[k].append(x[j] if x else None)
            s["cpi"].append(_cpi_at(cpi, d) if cpi and _linker(f) else None)
            for k, j in (("notional", 0), ("dv01", 1), ("cs01", 2), ("basis", 3), ("mv", 4)):
                s[k].append(last[j] if last else None)
            for c in last[8] if last else ():
                cv.setdefault(c, [None] * i)
            for c, arr in cv.items():
                arr.append(last[8].get(c, 0.0) if last else None)
            if rub_fix:
                yd = x[3] if x and x[3] else mat
                s["bench"].append(_rnd(bench(d, (yd - d).days / 365.25 if yd and yd > d else None), 4))
            else:
                s["bench"].append(None)
        for k in OPTIONAL:  # only when the bond has them: most series would be nulls
            if all(v is None for v in s[k]):
                del s[k]
        out: dict[str, Any] = {k: (_thin(v) if k in ("cs01", "basis", "bench", "ytm") else v) for k, v in s.items()}
        out["curves"] = {c: v for c, v in cv.items() if any(v)}
        series[pid] = out
    return {"runId": run["run_id"], "dates": [d.isoformat() for d in dates], "series": series,
            "note": "цена, доходность, G- и Z-спред (к КБД), у линкеров ICPI, IR, BEI — MOEX (история закрытий хаба), "
                    "ИПЦ г/г — последний известный на дату (ЦБ, иначе по ИПЦ м/м Росстата, через "
                    f"{CPI_LAG_DAYS} дн. после месяца); номинал, DV01, CS01, базис, MV и дисконт-маржа флоатера — по "
                    "прогонам деска (между прогонами — последний)"}


OPTIONAL = ("zspr", "dm", "icpi", "ir", "bei", "cpi")
"""Series of ``positions_history`` given only when the position has a value: MOEX's Z-spread to the KBD, the desk's
discount margin of a floater (its run spread), MOEX's linker analytics (contract 1.2), and for a linker CPI year on
year as known on the date (R25)."""
CPI_LAG_DAYS = 45
"""CPI of a month is known this many days after the month's first day (Rosstat publishes it mid next month; the hub's
``published_on`` is kept only for the newest months): a date sees the CPI y/y of the months it could have known."""


def _linker(f: dict[str, Any]) -> bool:
    return f.get("asset_class") == "ОФЗ-ИН" or f.get("coupon_type") == "indexed_face"


def _cpi_known(con, d: date) -> list[tuple[date, float]]:
    """CPI y/y (``market_history.cpi_yoy``) by the date it is known on (the month's first day + ``CPI_LAG_DAYS``)."""
    from .market_history import CPI, CPI_YOY, cpi_yoy, fixing_series  # api.market_history imports the app

    yoy, _ = cpi_yoy(fixing_series(con, [CPI, CPI_YOY], d))
    return [(m + timedelta(days=CPI_LAG_DAYS), v) for m, v in yoy]


def _cpi_at(series: list[tuple[date, float]], d: date) -> float | None:
    v = None
    for x, y in series:
        if x > d:
            break
        v = y
    return _rnd(v, 2)


def script(data: dict[str, Any]) -> str:
    return "window.DESK_EXTRAS = " + json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str) + ";\n"
