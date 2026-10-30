"""Data of the concept pages (``prototypes/concepts/``) beyond ``window.DESK``: ``/concepts-data.js`` sets
``window.DESK_EXTRAS`` for the run the page shows, ``/api/v1/history/positions`` gives the positions' series for the
charts, ``/api/v1/calendar/market`` the market's payment calendar (``lib/real.js`` builds the pages' data from them
and ``window.DESK``).

``extras``: the desk's series over the default runs up to the run (risk by curve, CS01, the OFZ basis, DV01 by book,
market value by instrument type and currency, PnL by factor: each run's own sums, ``publish.summary``), per position
of the run its PnL month and year to date over those runs (``pnlPeriods`` says from which date and whether a business
day of the period has no run: then the sum is partial), the previous run's price and spread and what changed in its
holding (a trade, a new position), its flows of the next 18 months up to the offer it is priced to
(``extra.cashflows`` cut by ``calendar_flows``: they go to maturity, TDM#166), the market of the latest
cached close not after the run (every quoted bond, ``universeRub`` and ``universeFx``, with the static snapshot's
issuer, group, sector, outstanding face, coupon and frequency: ``marketdata.static_facts``; the day's movers) and the
CBR meetings (``cbr``: the next one, the dates ahead, the past decisions). Issuer, group, sector and the desk's rating
(``marketdata.ratings``) of the market's bonds come from the newest static of the read model (R25). What the store does
not have is null with a note in ``notes``: ratings when the static has none, months before the first default run,
the dates of the history computed by an older program (R44). ``unvalued`` / ``approx``: what the run did not value or
valued without core, by cause and instrument (``api.coverage``, R45).

``positions_history``: per position of the run, aligned on one date axis: MOEX's clean price, yield (none for a
floater), G- and Z-spread and a linker's ICPI / IR / BEI from ``md_bond_day`` (the hub's history API, from
2024-01-03; else the snapshots, ``history_days`` deep), the KBD at the bond's term (a RUB bond with known coupons),
and from the default runs the face, number of bonds (``qty``), DV01, CS01, OFZ basis, MV and DV01 by curve, carried
forward between runs (null before the first), and a floater's discount margin on the days of a run (``dm``, core's) and
on every day of its price history (``dmPx``, estimated from the price: ``dm_from_price``). A series that is zero or null
on every date is sent as null; the optional ones (``OPTIONAL``) are left out. What a point stands on (R44): ``runAt``
per date (null: no run of the date, the value is carried; else the run, ``cut`` when not the final close, ``old`` when
another program computed it) and per position ``pt`` {date index: [kind, text]} for the points not valued by core on
the day's data (``point_flag``), so that a chart shows them apart instead of as risk moves.

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
from ..publish.summary import CURVE_MAP, GOV_CLASSES, PNL, is_minfin, kind_of, product_group  # noqa: F401 - the pages' names
from ..scenario.carry import meetings
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
        if oldest is None or unknown_days(start, oldest):
            why.append("история рынка начинается позже начала периода" if oldest is None else
                       f"история рынка (закрытия хаба) с {oldest.isoformat()}: прогонов раньше быть не может")
        elif first_pos is not None and ref and first_pos.isoformat() > ref[0]:
            why.append(f"позиции в базе с {first_pos.isoformat()}: отчётов раньше нет (папки отчётов, Настройки)")
        if missing:
            why.append(f"нет прогонов за {len(missing)} рабочих дн. (первый {missing[0]})")
        out[key] = {"from": runs_in[0] if runs_in else None, "runs": len(runs_in), "missing": len(missing),
                    "missingDates": missing[:10], "complete": not why and bool(runs_in),
                    "positionsFrom": first_pos.isoformat() if first_pos else None, "why": why}
        if why and runs_in:
            notes.append(f"PnL {label} — частичный: сумма {len(runs_in)} прогонов с {runs_in[0]}; {'; '.join(why)}"
                         + (f"; досчитать: {BACKFILL_LINK} (desk backfill)" if missing else ""))
    return out, notes


BACKFILL_LINK = "Настройки → «История прогонов»"
"""The settings page's history of runs (``settings.html#bf``): a note naming it is a link on the pages (``lib/kit.js``)."""
FIXED_HOLIDAYS = {(1, d) for d in range(1, 9)} | {(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)}
"""Russia's fixed public holidays (month, day): no close on them, so a period starting on one is not cut short."""


def unknown_days(start: date, first: date) -> list[date]:
    """The weekdays from ``start`` up to the market history's ``first`` date that are not fixed public holidays: the
    business days of the period the history cannot tell (2026: the hub's first close is 5 January, the year has no
    earlier business day, so the year to date is not partial because of it)."""
    out, d = [], start
    while d < first:
        if d.weekday() < 5 and (d.month, d.day) not in FIXED_HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def extras(con, run: dict[str, Any], writer: Writer = None) -> dict[str, Any]:
    from . import coverage  # it imports this module

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
                         " fact->'extra'->'cashflows' AS cf, fact->'extra'->>'offer_date' AS offer"
                         " FROM run_position WHERE run_id = %s AND fact IS NOT NULL", (run["run_id"],)):
        held.add(x["secid"])
        for d, kind, amount, est in calendar_flows(x["cf"], as_of, x["offer"]):
            flows.append({"date": d, "pos": x["position_id"], "kind": kind, "ccy": x["ccy"] or "RUB",
                          "amount": amount, "est": bool(est)})
    if not flows and cur:
        notes.append("потоков позиций в прогоне нет: прогон посчитан до версии с потоками (пересчитается последняя дата)")
    if not prev_ok and run.get("prev_date") is not None:
        notes.append(f"сделки дня и изменения к вчера не показаны: нет прогона за {run['prev_date'].isoformat()}"
                     f" (досчитать: {BACKFILL_LINK}, desk backfill)")
    if (old := old_runs(con, runs)):
        notes.append(f"история: {old} из {n} дат посчитаны прошлой версией программы — в графиках истории позиции "
                     f"они пунктиром; пересчитать: {BACKFILL_LINK} (идёт в фоне, от ранних дат)")
    if (bad := [(d, s["riskBad"]) for d, s in zip(dates, sums) if s.get("riskBad")]):  # TDM#170
        notes.append(f"невозможный риск (|DV01| не сходится с MV и номиналом) — не в суммах DV01, CS01 и базиса: "
                     f"{len(bad)} дат(ы), " + "; ".join(f"{d}: " + ", ".join(f"{p} ({w})" for p, w in list(b.items())[:3])
                                                      + (f" и ещё {len(b) - 3}" if len(b) > 3 else "")
                                                      for d, b in bad[-5:])
                     + (f"; ещё {len(bad) - 5} дат раньше" if len(bad) > 5 else ""))
    flows.sort(key=lambda c: c["date"])
    market = universe(con, as_of, held)
    notes += market.pop("notes")
    cb = cbr(con, as_of)
    if cb["next"] is None and not cb["meetings"]:
        notes.append("заседаний ЦБ нет в данных: появятся после прогона на снимке с cbr_meetings")
    return {"runId": run["run_id"], "asOf": as_of.isoformat(), "histDates": dates,
            "hist": {"dv01": series("dv01"), "cs01": [s["cs01"] for s in sums], "basis01": [s["basis"] for s in sums],
                     "ofzSpr01": [s.get("ofzSpr", 0.0) for s in sums], "basisPk01": [s.get("basisPk", 0.0) for s in sums],
                     "basisIn01": [s.get("basisIn", 0.0) for s in sums],
                     "ofzRuo01": [s.get("ofzRuo") for s in sums],
                     "riskBad": [sorted(s.get("riskBad") or ()) for s in sums],
                     "dv01Book": series("dv01Book"), "mvType": series("mvType"), "mvCcy": series("mvCcy"), "pnl": pnl},
            "histSource": "runs", "pnlPeriods": periods,
            "positions": positions, "cashflows": flows, **market,
            "cbr": cb, "curveMap": CURVE_MAP, "notes": notes,
            "unvalued": coverage.unvalued(con, run), "approx": coverage.approx(con, run)}


def old_runs(con, runs: list[dict[str, Any]]) -> int:
    """How many of the runs were computed by another program than the current one (``backfill.same_logic``)."""
    from .. import logic_version
    from ..backfill import same_logic

    now = logic_version()
    return sum(not same_logic(r["v"], now) for r in con.execute(
        "SELECT lineage->>'logic_version' AS v FROM run WHERE run_id = ANY(%s)", ([r["run_id"] for r in runs],)))


def cbr(con, as_of: date) -> dict[str, Any]:
    """The CBR meetings of the newest market snapshot loaded (``md_cbr_meeting``): ``next`` (date, label, whether
    the date is the hub's estimate, the key rate in force), ``meetings`` (the dates after ``as_of``), ``schedule``
    (the meetings that take effect after ``as_of``, in order: ``date`` the decision, ``effective`` from when it applies,
    ``estimated`` the hub's estimated date; ``scenario.carry.meetings``, the key-rate path's, as window.DESK's
    ``CBR_MEETINGS``, TDM#289), ``past`` (the decisions on or before it, newest first, at most 12). Consensus and the
    rate the curve prices are not in the data (null)."""
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
    schedule = [{"date": m.date.isoformat(), "effective": m.effective.isoformat(), "estimated": m.estimated}
                for m in meetings(rows, as_of)]
    return {"next": nxt, "meetings": [r["meeting_date"].isoformat() for r in ahead], "schedule": schedule,
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


def calendar_flows(cf: list[list[Any]] | None, as_of: date, offer: str | None) -> list[list[Any]]:
    """A position's stored flows (``extra.cashflows``, to maturity since TDM#166) as the payment calendars read them:
    the next ``tdm_run.CASHFLOW_DAYS`` up to the offer the bond is priced to (the offer itself in)."""
    from ..publish.tdm_run import CASHFLOW_DAYS

    end = (as_of + timedelta(days=CASHFLOW_DAYS)).isoformat()
    if offer and offer < end:
        end = offer
    return [x for x in cf or [] if x[0] <= end]


def position_flows(con, run: dict[str, Any], pid: str, snapshots: Path) -> dict[str, Any] | None:
    """A bond position's whole schedule after the run's date (``static_facts.schedule`` of the static snapshot the read
    model shows for the run, × the position's bonds): coupons, amortizations, maturity, and the put offers and calls
    on the face outstanding then, in the face currency, signed by the side. The position card shows it. None: no such
    position in the run. No schedule is an answer, not an error (#288): empty ``flows`` and a ``reason`` —
    ``not_valued``, ``not_bond``, ``no_static`` (the static or its bundle is missing) or ``no_schedule``; the card then
    shows the stored flows. TDM#166: without the static's bundle (a cleared cache) or the bond in it, the run's own
    flows when they go to maturity (``extra.cashflows_to``, the runs since then): ``source`` ``run`` and the
    ``reason`` the static was not used; of the offers, only the one the bond is priced to."""
    row = con.execute("SELECT fact IS NOT NULL AS valued, fact->>'instrument_id' AS secid,"
                      " (fact->'extra'->>'quantity_bonds')::float8 AS q,"
                      " fact->'extra'->>'offer_date' AS offer, fact->'extra'->>'offer_kind' AS offer_kind,"
                      " fact->>'ccy' AS ccy, fact->>'maturity' AS maturity,"
                      " fact->'extra'->>'cashflows_to' AS cf_to, fact->'extra'->'cashflows' AS cf"
                      " FROM run_position WHERE run_id = %s AND position_id = %s",
                      (run["run_id"], pid)).fetchone()
    if row is None:
        return None
    if not row["valued"]:
        return {"flows": [], "reason": "not_valued"}
    if not row["secid"] or row["q"] is None:
        return {"flows": [], "reason": "not_bond"}

    def own(reason: str) -> dict[str, Any]:  # the run's flows to maturity, else no schedule and why
        if row.get("cf_to") != "maturity" or not row.get("cf"):
            return {"flows": [], "reason": reason}
        return {"source": "run", "reason": reason, "staticDate": None, "ccy": row["ccy"] or "RUB",
                "maturity": row["maturity"], "offer": row["offer"], "offerKind": row["offer_kind"],
                "flows": [list(x) for x in row["cf"]]}

    st = static_facts.static_for(con, run["business_date"])
    if st is None:
        return own("no_static")
    path = Path(snapshots) / f"{st['snapshot_id']}.json.gz"
    if not path.is_file() and st["snapshot_id"] not in static_facts._SCHEDULES:
        return own("no_static")
    sch = static_facts.schedule(st["snapshot_id"], lambda: read_bundle(path), row["secid"], run["business_date"])
    if sch is None:
        return own("no_schedule")
    q = row["q"]
    return {"source": "static", "staticDate": st["business_date"].isoformat(), "ccy": sch["ccy"],
            "maturity": sch["maturity"], "offer": row["offer"], "offerKind": row["offer_kind"],
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
    meta, held = run_points(con, runs, list(pos))
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
    run_of = {r["business_date"]: r["run_id"] for r in runs}
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
        anchors: list[tuple[int, float, float | None, float | None, float | None]] = []
        qty: list[float | None] = []
        pts: dict[int, list[str]] = {}
        n_bonds: float | None = None
        for i, d in enumerate(dates):
            x = q.get(d)
            ps = run_at.get(d)
            if ps is not None:
                last = ps.get(pid)  # None: not held on that run date
                h = held.get((run_of[d], pid))
                n_bonds = h[0] if h and last is not None else None
                if h and (p := point_flag(h, meta[run_of[d]]["books"].get(h[3]), d, last is not None)):
                    pts[i] = p
            qty.append(n_bonds)
            s["price"].append(x[0] if x else (f.get("price") if d == run["business_date"] else None))
            s["ytm"].append(None if flt or not x else x[1])
            s["spr"].append(x[2] if x else None)
            s["zspr"].append(x[4] if x else None)
            s["dm"].append((last[9] if len(last) > 9 and last[9] is not None else last[7])  # core DM, else the Z
                           if flt and last and ps is not None else None)
            if s["dm"][-1] is not None:  # a run's DM, its price and spread risk per face: an anchor of dmPx
                anchors.append((i, s["dm"][-1], x[0] if x else last[6], last[2] or last[3], last[0]))  # type: ignore[index]
            for k, j in (("icpi", 5), ("ir", 6), ("bei", 7)):
                s[k].append(x[j] if x else None)
            s["cpi"].append(_cpi_at(cpi, d) if cpi and _linker(f) else None)
            for k, j in (("notional", 0), ("dv01", 1), ("cs01", 2), ("basis", 3), ("mv", 4)):
                s[k].append(last[j] if last else None)
            s["ofzSpr"].append(last[10] if last and len(last) > 10 else None)  # an OFZ's spread to the KBD (0.2.6)
            s["ofzRuo"].append(last[11] if last and len(last) > 11 else None)  # its RUONIA risk apart (0.2.7, TDM#213)
            for c in last[8] if last else ():
                cv.setdefault(c, [None] * i)
            for c, arr in cv.items():
                arr.append(last[8].get(c, 0.0) if last else None)
            if rub_fix:
                yd = x[3] if x and x[3] else mat
                s["bench"].append(_rnd(bench(d, (yd - d).days / 365.25 if yd and yd > d else None), 4))
            else:
                s["bench"].append(None)
        dm_note = None
        if flt and anchors and f.get("ccy") in (None, "RUB"):
            hz = f.get("yield_date") or f.get("maturity")
            s["dmPx"] = dm_from_price(dates, s["price"], anchors, date.fromisoformat(hz) if hz else None,
                                      (f.get("coupon") or 0.0) / 100) or []
            if not s["dmPx"]:
                dm_note = "DM по цене не посчитан: нет даты погашения или оферты" if not hz else None
        elif flt:
            dm_note = dm_missing(f)
        for k in OPTIONAL:  # only when the bond has them: most series would be nulls
            if all(v is None for v in s[k]):
                del s[k]
        out: dict[str, Any] = {k: (_thin(v) if k in ("cs01", "basis", "ofzSpr", "bench", "ytm") else v)
                               for k, v in s.items()}
        out["curves"] = {c: v for c, v in cv.items() if any(v)}
        if dm_note:
            out["dmNote"] = dm_note
        out["qty"], out["pt"] = qty, pts
        series[pid] = out
    at = [run_of.get(d) for d in dates]
    return {"runId": run["run_id"], "dates": [d.isoformat() for d in dates], "series": series,
            "runAt": [None if rid is None else {"id": rid, **{k: v for k, v in meta[rid].items() if k != "books"}}
                      for rid in at],
            "note": "цена, доходность, G- и Z-спред (к КБД), у линкеров ICPI, IR, BEI — MOEX (история закрытий хаба), "
                    "ИПЦ г/г — последний известный на дату (ЦБ, иначе по ИПЦ м/м Росстата, через "
                    f"{CPI_LAG_DAYS} дн. после месяца); номинал, DV01, CS01, базис, MV и дисконт-маржа флоатера — по "
                    "прогонам деска (между прогонами — последний; runAt и pt говорят, какой прогон у даты и чем точка "
                    "отличается); DM флоатера по цене (dmPx) — оценка за каждый день "
                    "истории цены: DM ближайшего прогона минус изменение чистой цены MOEX, делённое на спредовую "
                    "дюрацию прогона (CS01 на номинал, приведённую к сроку до оферты или погашения)"}



def dm_missing(fact: dict[str, Any]) -> str:
    """Why a floater has no DM history (TDM#33): the history of DM by price (``dm_from_price``) is anchored on core's DM
    of at least one run, so a floater core never valued (a fallback rung, an unsupported formula) or a foreign one has
    none; the card and C11 say so instead of an empty chart."""
    ex = fact.get("extra") if isinstance(fact.get("extra"), dict) else {}
    if fact.get("ccy") not in (None, "RUB"):
        return "DM по цене считается только у рублёвых флоатеров"
    why = ex.get("not_exact_reason")
    q = QUALITY_TEXT.get(ex.get("quality") or "")
    return ("DM нет: ни в одном прогоне истории флоатер не оценён ядром"
            + (f" ({q}{'; ' + why if why else ''})" if q or why else ""))


QUALITY_TEXT = {"market_value": "оценка по рыночной стоимости (без ядра)",
                "stale_price": "нет котировки дня: старая цена, риск — по дюрации (до пересчёта купона у флоатера)",
                "model_price": "нет котировки: модельная цена, риск — по дюрации",
                "par_proxy": "оценка по номиналу: риска нет (не ноль, а пропуск)",
                "reported_value": "по отчёту банка: риск — отчёта или нет"}
"""A run position's quality (``valuation.fallback.QUALITIES``) whose risk is not core's from the bond's terms and the
day's price: its point is not comparable with core's on the neighbouring dates (``pt`` kind ``method``)."""


def _logic_label(v: str | None) -> str:
    """``0.2.0+v8+core0.32.1`` as ``0.2.0 (оценка v8, ядро 0.32.1)``."""
    if not v:
        return "версия не записана"
    parts = v.split("+build", 1)[0].split("+")
    ver = next((p[1:] for p in parts[1:] if p.startswith("v")), None)
    core = next((p[4:] for p in parts[1:] if p.startswith("core")), None)
    return parts[0] + (f" (оценка v{ver}" + (f", ядро {core}" if core else "") + ")" if ver else "")


UNIT_TEXT = {True: "риск валютных кривых в котировках (прокси USD/EUR; CNY XCCY у FX-сделок), у показанного прогона — "
                   "в нулевых ставках",
             False: "риск валютных кривых в нулевых ставках, у показанного прогона — в котировках (прокси USD/EUR; "
                    "CNY XCCY у FX-сделок)"}
"""``unit`` of a history point (``run_points``): its run's foreign curves' risk is in another unit than the shown run's
(the setting ``quote_curve_risk``, issue #171), by its run's own flag."""


def run_points(con, runs: list[dict[str, Any]], pids: list[str]
               ) -> tuple[dict[int, dict[str, Any]], dict[tuple[int, str], tuple]]:
    """What a history point of a position depends on beyond its numbers (R44): per run its cut when not the final
    close (``cut``: ``eod_preliminary`` when the date has no final one; an intraday cut for the run shown), the program that computed it when that is not the current one
    (``old``: another valuation or core version, ``backfill.same_logic``; the history of runs
    recomputes such a date), the unit of its foreign curves' risk when it is not the shown run's (``unit``: the
    setting ``quote_curve_risk``, run lineage, issue #171; the program is the same, so the history of runs does not
    recompute it) and per book the date of the report its positions are from (``books``); per (run, position) the
    number of bonds, the quality, why it is not exact and the book. ``runs``: the shown run last (``_runs``)."""
    from .. import logic_version
    from ..backfill import same_logic

    now = logic_version()
    meta: dict[int, dict[str, Any]] = {}
    ids = [r["run_id"] for r in runs]
    quoted: dict[int, bool] = {}
    for r in con.execute(
            "SELECT r.run_id, r.cut_type, r.lineage->>'logic_version' AS logic,"
            " coalesce((r.lineage->>'quote_curve_risk')::boolean, false) AS quoted,"
            " (SELECT jsonb_object_agg(s.book_id, s.valid_date) FROM position_set s"
            "  WHERE s.set_id = ANY(r.position_set_ids)) AS books FROM run r WHERE r.run_id = ANY(%s)", (ids,)):
        m: dict[str, Any] = {"books": r["books"] or {}}
        if r["cut_type"] != "eod_final":
            m["cut"] = r["cut_type"]
        if not same_logic(r["logic"], now):
            m["old"] = _logic_label(r["logic"])
        meta[r["run_id"]] = m
        quoted[r["run_id"]] = r["quoted"]
    shown = quoted.get(ids[-1], False) if ids else False
    for rid, q in quoted.items():
        if q != shown:
            meta[rid]["unit"] = UNIT_TEXT[q]
    held: dict[tuple[int, str], tuple] = {}
    with con.cursor(row_factory=tuple_row) as cur:
        for rid, pid, n, quality, why, book in cur.execute(
                "SELECT run_id, position_id, quantity, quality, CASE WHEN quality = 'exact' THEN NULL ELSE reason END,"
                " book_id FROM run_position WHERE run_id = ANY(%s) AND position_id = ANY(%s)", (ids, pids)):
            held[(rid, pid)] = (n, quality, why, book)
    return meta, held


def point_flag(h: tuple, book_date: str | None, d: date, valued: bool) -> list[str] | None:
    """``pt`` of a position's point on a run date (``run_points``): ``[kind, text]``, None for a clean point. Kinds:
    ``unvalued`` (held, not valued: no number at all), ``method`` (valued without core, ``QUALITY_TEXT``: a risk of
    another method), ``minor`` (core's numbers with an assumption, or the book's positions from an earlier report)."""
    _, quality, why, book = h
    reason = f" ({why})" if why else ""
    if not valued:
        return ["unvalued", "позиция не оценена" + reason]
    if quality in QUALITY_TEXT:
        return ["method", QUALITY_TEXT[quality] + reason]
    notes = []
    if quality == "assumed":
        notes.append("оценка ядра с допущением" + reason)
    if book_date and book_date < d.isoformat():
        notes.append(f"позиции книги {book} — из отчёта за {book_date} (за дату отчёта нет)")
    return ["minor", "; ".join(notes)] if notes else None


OPTIONAL = ("zspr", "dm", "dmPx", "icpi", "ir", "bei", "cpi", "ofzSpr", "ofzRuo")
"""Series of ``positions_history`` given only when the position has a value (``ofzSpr``: an OFZ's spread risk to the
KBD, runs of 0.2.6 on; ``basis`` is its par-par basis then, its spread to RUONIA before; ``ofzRuo``: its RUONIA risk
computed apart from the basis, runs of 0.2.7 on, TDM#213: what the «КБД» view takes off RUONIA, the basis before): MOEX's Z-spread to the KBD, the desk's
discount margin of a floater (its run spread) and its estimate from the price on every date of the price history
(``dm_from_price``), MOEX's linker analytics (contract 1.2), and for a linker CPI year on year as known on the date
(R25)."""


def _annuity(t: float, r: float) -> float:
    return t if r <= 0 else (1.0 - math.exp(-r * t)) / r


def dm_from_price(dates: list[date], price: list[float | None],
                  anchors: list[tuple[int, float, float | None, float | None, float | None]],
                  horizon: date | None, rate: float) -> list[float | None] | None:
    """A RUB floater's discount margin on every date of its MOEX price history (batch 1, item 6): the runs give core's
    DM only from the day the position appeared (``anchors``: date index, DM, clean price, spread risk and face of each
    run with a DM). From the latest run at or before the date (else the first after it): DM_run - (clean - clean_run)
    / 100 / spread duration, the spread duration per face of the run (-CS01 / face; the OFZ basis of an OFZ-PK)
    averaged with the same moved to the date in proportion to the annuity to the ``horizon`` at ``rate`` (the current
    coupon). First order in the spread; the curves' move between the dates is left out, small for a floater whose
    coupon resets. Equal to the run's DM on a run date with a MOEX price; None without a price or past the horizon."""
    if horizon is None:
        return None
    out: list[float | None] = []
    j = 0
    for i, (d, px) in enumerate(zip(dates, price)):
        while j + 1 < len(anchors) and anchors[j + 1][0] <= i:
            j += 1
        ai, dm, px_a, s01, face = anchors[j]
        ta, td = (horizon - dates[ai]).days / 365.25, (horizon - d).days / 365.25
        sd_a = -s01 / face if s01 and face else 0.0  # RUB per bp per RUB of face, > 0 long and short
        if px is None or px_a is None or ta <= 0 or td <= 0 or sd_a <= 0:
            out.append(None)
            continue
        sd = sd_a * (1.0 + _annuity(td, rate) / _annuity(ta, rate)) / 2.0
        out.append(_rnd(dm - (px - px_a) / 100.0 / sd, 2))
    return out if any(v is not None for v in out) else None
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


# ---- the aggregated risk table: currency x instrument type (UI-02; lib/instr.js mirrors it) ----

INSTR_KINDS: tuple[tuple[str, str, str], ...] = (
    ("OFZ_PD", "GOV", "ОФЗ-ПД"), ("OFZ_PK", "GOV", "ОФЗ-ПК (флоатеры)"), ("OFZ_IN", "GOV", "ОФЗ-ИН (линкеры)"),
    ("MINFIN_FX", "GOV", "Минфин валютные (евробонды, замещающие)"),
    ("SUBFED", "SUB", "Субфедеральные"), ("MUNI", "SUB", "Муниципальные"),
    ("CORP_FIX", "CORP", "Корпоративные фикс"), ("CORP_FLT", "CORP", "Корпоративные флоатеры"),
    ("SECUR", "CORP", "Секьюритизация (ИЦБ, ABS)"),
    ("IRS", "DER", "Процентные свопы (IRS / OIS)"), ("CCS", "DER", "Валютно-процентные свопы (CCS)"),
    ("CDS", "DER", "CDS"), ("TRS", "DER", "TRS"),
    ("BOND_FUT", "DER", "Фьючерсы на облигации"), ("BOND_FWD", "DER", "Форварды на облигации"),
    ("FX_FUT", "DER", "Валютные фьючерсы"), ("FX_FWD", "DER", "FX форварды / NDF"),
    ("FX", "DER", "Валюта (спот, прочие FX)"), ("OCP", "DER", "Перенос ОВП"),
    ("OTC", "DER", "Внебиржевые без группы продукта"),
    ("REPO", "MM", "РЕПО"),
    ("EQUITY", "OTH", "Акции"), ("BOND_NA", "OTH", "Облигации без класса (не сопоставлены)"),
    ("UNCLASSIFIED", "OTH", "Не классифицировано"),
)
"""The instrument kinds of the table, in its order: (kind, group, label). The one mapping table of the
classification: a bond by issuer type and coupon (``kind_of``, plus the static's class: subfederal / municipal,
securitisation; the Russian Minfin's bonds by the issuer, ``summary.is_minfin``: in roubles the OFZ kinds, in a
foreign currency ``MINFIN_FX`` -- its Eurobonds and replacement bonds, never a generic "sovereigns" kind, R39), a derivative or FX row by the adapter's product group (``PRODUCT_KIND``), else by the legacy tdm
instrument type (``LEGACY_KIND``); a REPO (product group ``repo``, valued or not) is ``REPO`` in the group MM, money
market and funding, as ``kind_of``'s MM REPO (the owner's decision in TDM#361: funding, not a derivative); an OTC row
without any product group is ``OTC`` (DER, as ``kind_of``'s DER OTC, TDM#345), one with a group the table does not know
(a new one) stays ``UNCLASSIFIED`` (OTH; kind_of's DER OTC: a known difference, ``tests/fixtures/instr_kinds.json``
``knownMismatch``); what none of them names is ``UNCLASSIFIED`` (shown, never dropped). The group is
``kind_of``'s for every ``instr_type`` (``tests/fixtures/instr_kinds.json`` checks the three classifications: this,
``kind_of`` and ``lib/real.js`` / ``lib/instr.js``) but for an unmapped or unvalued bond (OTH, not CORP) and an OTC
row with an unknown product group. A kind
here may be named apart from ``kind_of``'s (``CCS`` is its ``XCCY``, ``CDS`` its ``OTC``): the table is finer."""
KIND_GROUP = {k: g for k, g, _ in INSTR_KINDS}
KIND_LABEL = {k: lab for k, _, lab in INSTR_KINDS}
PRODUCT_KIND = {"irs": "IRS", "ccs": "CCS", "cds": "CDS", "trs": "TRS", "bond_future": "BOND_FUT",
                "bond_forward": "BOND_FWD", "fx_future": "FX_FUT", "fx_ndf": "FX_FWD", "equity": "EQUITY",
                "repo": "REPO"}
"""The private adapter's ``x_product_group`` (``extra.reported.x``, ``product_group``) -> kind. ``bond`` and ``bond_custom`` are bonds:
classified by the bond's own fields."""
LEGACY_KIND = {"IRS": "IRS", "XCCY": "CCS", "CDS": "CDS"}
"""A legacy tdm store's instrument type -> kind."""
BOND_KIND = {("GOV", "PD"): "OFZ_PD", ("GOV", "PK"): "OFZ_PK", ("GOV", "IN"): "OFZ_IN", ("GOV", "SOVFX"): "MINFIN_FX",
             ("CORP", "FIX"): "CORP_FIX", ("CORP", "FLT"): "CORP_FLT"}
CROSS_CCY_KINDS = ("CCS", "FX_FWD", "FX_FUT", "FX")
"""Kinds whose risk lies in more than one currency: the attribution rule (``ATTRIBUTIONS``) decides where."""
ATTRIBUTIONS = {
    "legs": "each measure in its own currency: DV01 by curve in the curve's, FX delta in its currency's; "
            "position count, notional, MV, CS01 and the OFZ basis in the position's",
    "position": "everything of a cross-currency kind in the position's currency (the report's CCY); its risk on "
                "another currency's curves is shown apart (dv01_foreign_rub), outside the block's DV01",
}
ATTRIBUTION_DEFAULT = "legs"
"""Provisional (UI-02): the owner decides how a CCS, an FX forward or an FX future is attributed."""
RISK_KINDS = frozenset(k for k, g, _ in INSTR_KINDS if g in ("GOV", "SUB", "CORP")) | {
    "IRS", "CCS", "CDS", "TRS", "BOND_FUT", "BOND_FWD", "FX_FUT", "FX_FWD", "BOND_NA"}
"""Kinds whose position always has a rate or spread risk: one without any is ``risk missing``, never a zero (but
an accrual-only HTM bond: no market risk by the rule)."""
OFZ_VIEWS = {
    "kbd": "«КБД»: an OFZ's rate risk on its own curve, the KBD's par yields (RUB OFZ in dv01_by_curve_rub), RUONIA "
           "without it; no OFZ basis (ofz_basis_rub 0): every DV01 and spread field adds up once. An OFZ-PK has no KBD "
           "risk: its basis (its spread to RUONIA) is in ofz_spread_kbd_rub. A run before 0.2.6 had its OFZ on RUONIA: "
           "their KBD risk is approximated by their basis to RUONIA (ofz_view_note)",
    "basis": "«RUONIA + базис» (the monitors' default): an OFZ's risk to RUONIA OIS quotes with the basis held (in "
             "dv01_by_curve_rub['RUB RUONIA']) and its risk to the OFZ basis, the KBD's par yields with RUONIA held "
             "(ofz_basis_rub): two computed risks to two factors (0.2.7, core 0.39), like DV01 and CS01. Their sum is no "
             "rate DV01 (it moves the KBD twice): never add ofz_basis_rub to the DV01 fields. The DV01 here is the OFZ's "
             "RUONIA risk, a few tenths of a percent off its KBD risk in «КБД». A run of 0.2.6 (and an OFZ off core) "
             "has its KBD risk on RUONIA and again as the basis (ofz_view_note)",
}
"""How the risk table shows the OFZ risk (TDM#173, TDM#213; the monitors' switch «ОФЗ: RUONIA + базис / КБД»,
``lib/real.js`` ``M.ofzView``). The DV01 of an OFZ in RUONIA + basis is its RUONIA risk in «RUONIA + базис» and its
basis risk (the KBD's par risk) in «КБД»; with the basis a copy of the KBD risk (a run of 0.2.6) both are the same."""
OFZ_VIEW_DEFAULT = "basis"
"""The API's default, the monitors' one (the owner's decision in TDM#205): «RUONIA + базис», two computed risks since
0.2.7 (TDM#213), as the API of 0.2.6 gave; «КБД» (TDM#173), where every field adds up without a second rate factor,
when asked for by name."""
OFZ_VIEW_APPROX = ("a run before 0.2.6: its OFZ were on RUONIA, their KBD risk in the «КБД» view is their basis to "
                   "RUONIA (ofz_spr01), moved off RUONIA")
BASIS_NOT_SPLIT = ("basis not split: no RUONIA curve in the market of the run's date, {n} RUB bonds are discounted on the "
                   "KBD (extra.curve_proxy), their risk is on RUB OFZ and the OFZ basis is in their CS01, not apart (TDM#467)")
OFZ_VIEW_P = ("{n} OFZ or KBD floaters without a computed RUONIA risk (a run of 0.2.6, an OFZ or a KBD floater off core):"
              " their RUONIA risk is their KBD risk and the basis the same risk again (core basis_view, variant P)")
MISSING_TEXT = {"not_valued": "не оценена", "reported_no_risk": "оценена по отчёту, риска в отчёте нет",
                "no_risk": "риск не посчитан",
                "implausible": "риск невозможен (|DV01| не сходится с MV и номиналом): не в суммах, причина в журнале "
                               "прогона"}
GROUPS = ("GOV", "SUB", "CORP", "DER", "MM", "OTH")


def instr_kind(f: dict[str, Any], gov_issuers: frozenset[str] | set[str] = frozenset()) -> str:
    """The table's instrument kind of a position's fact, or of an unvalued position's report row given as
    ``{"ccy", "asset_class" (bond | fx | otc | other), "extra": {"reported": {x_*}}}`` (no ``instr_type``).
    ``gov_issuers``: the run's issuers flagged as the Minfin (``gov_issuers``), for ``summary.is_minfin``."""
    ex = f.get("extra") if isinstance(f.get("extra"), dict) else {}
    pg = product_group(ex.get("reported"))
    it = f.get("instr_type")
    if pg in PRODUCT_KIND:
        return PRODUCT_KIND[pg]
    if it in LEGACY_KIND:
        return LEGACY_KIND[it]
    if it is None and f.get("segment") is None:  # unvalued: its report row
        it = {"fx": "FX", "otc": "OTC", "other": "OTHER"}.get(str(f.get("asset_class") or "").lower())
        if it is None:
            return "BOND_NA"
    if it == "FX":
        ocp = f.get("ocp_transfer") or ex.get("ocp_transfer") or f.get("asset_class") == "Перенос ОВП"
        return "OCP" if ocp else "FX"
    if it == "OTC" and not pg:  # no product group at all: kind_of's DER OTC, not UNCLASSIFIED (TDM#345); a group the
        return "OTC"  # table does not know (a new one) stays UNCLASSIFIED (repo is REPO above, TDM#361)
    if it not in ("BOND", None):
        return "UNCLASSIFIED"
    if f.get("asset_class") == "Не сопоставлено":
        return "BOND_NA"
    g, k = kind_of(f, gov_issuers)
    if g == "SUB":
        return "MUNI" if f.get("asset_class") == "Муниципальные" else "SUBFED"
    if g == "CORP" and f.get("segment") == "Секьюритизация":
        return "SECUR"
    return BOND_KIND[(g, k)]


def _curve_ccy(curve: str) -> str:
    return curve.split()[0]


def _cell() -> dict[str, Any]:
    return {"ids": set(), "legs": set(), "notional_ccy": 0.0, "notional_rub": 0.0, "mv_rub": 0.0, "curves": {},
            "foreign": 0.0, "basis": 0.0, "in_proxy": 0.0, "ofz_spr": 0.0, "cs01": 0.0, "fx_delta_ccy": 0.0,
            "missing": {}}


def is_kbd_floater_row(f: dict[str, Any], ex: dict[str, Any]) -> bool:
    """Whether a row is a KBD floater (``bonds.is_kbd_floater``, TDM#33) valued in RUONIA + basis: a floating coupon with the
    OFZ basis (``extra.ofz_basis01``; only an OFZ of the KBD scheme or a KBD floater has it, an OFZ-PK none). Its coupons
    project on the KBD, it is discounted on RUONIA. By the coupon type, not the kind: an OFZ outside the OFZ kinds is no floater."""
    return ex.get("ofz_basis01") is not None and f.get("coupon_type") == "float"


def _risk_curves(f: dict[str, Any], kbd: bool = False) -> dict[str, float]:
    """A fact's DV01 by the pages' curve (``CURVE_MAP``: the KBD's rate risk is RUONIA's; with ``kbd``, the «КБД» view,
    it stays on ``RUB OFZ``), finite and nonzero."""
    out: dict[str, float] = {}
    for curve, bk in (f.get("risk") or {}).items():
        cid = curve if kbd and curve == "RUB OFZ" else CURVE_MAP.get(curve, curve)
        out[cid] = out.get(cid, 0.0) + sum(v or 0.0 for v in (bk or {}).values())
    return {c: v for c, v in out.items() if v and math.isfinite(v)}


def risk_items(con, run: dict[str, Any], books: list[str] | None = None
               ) -> tuple[list[dict[str, Any]], dict[str, float | None]]:
    """The positions of a run for ``risk_table`` (an unvalued one with its report row: currency, asset class and
    ``x_*``) and the run's RUB rates (the reference's; a currency it lacks at the rate its bonds were valued at,
    ``extra.fx_rate``, as ``compat.assemble``)."""
    rid = run["run_id"]
    sql, args = "SELECT position_id, fact FROM run_position WHERE run_id = %s", [rid]
    if books:
        sql, args = sql + " AND book_id = ANY(%s)", [*args, books]
    rows = con.execute(sql + " ORDER BY ord", args).fetchall()
    unvalued = [r["position_id"] for r in rows if r["fact"] is None]
    inputs = {i["position_id"]: i for i in con.execute(
        "SELECT position_id, ccy, asset_class, x FROM position_input WHERE set_id = ANY(%s) AND position_id = ANY(%s)",
        (run["position_set_ids"], unvalued))} if unvalued else {}
    items = []
    for r in rows:
        if r["fact"] is not None:
            items.append({"position_id": r["position_id"], "fact": r["fact"]})
            continue
        i = inputs.get(r["position_id"]) or {}
        items.append({"position_id": r["position_id"], "fact": None,
                      "input": {"ccy": i.get("ccy"), "asset_class": i.get("asset_class"),
                                "extra": {"reported": i.get("x") or {}}}})
    ref = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'fx'", (rid,)).fetchone()
    fx: dict[str, float | None] = {r["ccy"]: r.get("rate") for r in (ref["data"] if ref else []) or []}
    for it in items:
        f = it["fact"] or {}
        ex = f.get("extra") if isinstance(f.get("extra"), dict) else {}
        if f.get("ccy") and fx.get(f["ccy"]) is None and ex.get("fx_rate"):
            fx[f["ccy"]] = ex["fx_rate"]
    return items, fx


def gov_issuers(con, run_id: int) -> frozenset[str]:
    """The issuer keys of a run's issuer reference flagged ``is_gov`` (the Minfin: ``summary.is_minfin``)."""
    ref = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'issuers'", (run_id,)).fetchone()
    return frozenset(r["issuer"] for r in (ref["data"] if ref else None) or [] if r.get("is_gov") and r.get("issuer"))


MATRIX_MEASURES: tuple[tuple[str, str, str], ...] = (
    ("dv01", "DV01", "labelled"), ("dv01_curve", "DV01 по кривой", "yes"), ("cs01", "CS01", "yes"),
    ("ofz_basis", "Базис ОФЗ", "yes"), ("ofz_spread_kbd", "Спред ОФЗ к КБД", "yes"), ("fx_delta", "FX-дельта", "repr"),
    ("mv", "MV", "yes"),
    ("notional", "Номинал", "yes"), ("n_positions", "Позиций", "yes"),
)
"""The measures of the matrix (R38: one shown at a time), with whether a cell sums across currencies (the total row,
in RUB): ``yes``; ``repr`` only at the run's rates (``fx_delta_rub``: null when a rate is missing); ``labelled`` --
DV01 over the curves of different currencies sums, but is not a currency's risk (``dv01_all_curves_rub``)."""


def risk_table(items: list[dict[str, Any]], fx: dict[str, float | None],
               attribution: str = ATTRIBUTION_DEFAULT, gov_issuers: frozenset[str] = frozenset(),
               ofz_view: str = OFZ_VIEW_DEFAULT) -> dict[str, Any]:
    """The table currency x instrument type over a run's positions, as a matrix (R38): ``ccys`` are its rows,
    ``columns`` the instrument kinds present (in ``INSTR_KINDS`` order, with each kind's total over all currencies:
    what sums, in RUB), a cell is the ``rows`` entry of level ``kind`` (ccy, kind), a row total the entry of level
    ``ccy`` (and of level ``group`` its subtotal by group); ``total`` the corner. ``items``: ``{"position_id", "fact"}`` of a
    valued position, ``{"position_id", "fact": None, "input": <report row as for instr_kind>}`` of an unvalued
    one; ``fx`` the run's RUB rate per currency. Amounts in RUB (``*_rub``); the notional and FX delta in the
    currency (``*_ccy``), DV01 and MV too where its rate is known. Rows: one per currency (``level`` ccy), group
    (group) and kind (kind) with a position or a risk there; ``total`` only what sums across currencies, DV01 over
    the curves of different currencies only as ``dv01_all_curves_rub`` (labelled so, never as a currency's).
    ``ofz_view`` (``OFZ_VIEWS``, TDM#173, TDM#213) says where an OFZ's risk is, as the monitors' switch does: ``kbd`` its
    KBD par risk on ``RUB OFZ`` with no basis, ``basis`` its RUONIA risk on RUONIA plus the OFZ basis (``ofz_basis_rub``). An OFZ-IN's nominal proxy
    (``summary.ofz_in_proxy``) is never a basis: its rate risk is where the view puts the KBD, ``dv01_ofz_in_proxy_rub``
    says how much of it is the proxy (a memo, already in the DV01)."""
    if attribution not in ATTRIBUTIONS:
        raise ValueError(f"unknown attribution {attribution!r}")
    if ofz_view not in OFZ_VIEWS:
        raise ValueError(f"unknown ofz_view {ofz_view!r}")
    kbd, approx, n_p, n_nb = ofz_view == "kbd", False, 0, 0
    cells: dict[tuple[str, str], dict[str, Any]] = {}

    def cell(ccy: str, kind: str) -> dict[str, Any]:
        return cells.setdefault((ccy, kind), _cell())

    every: set[str] = set()
    for it in items:
        pid, f = it["position_id"], it.get("fact")
        every.add(pid)
        if f is None:
            inp = it.get("input") or {}
            c = cell(inp.get("ccy") or "RUB", instr_kind({"asset_class": None, **inp}))
            c["ids"].add(pid)
            c["missing"]["not_valued"] = c["missing"].get("not_valued", 0) + 1
            continue
        kind, home = instr_kind(f, gov_issuers), f.get("ccy") or "RUB"
        legs = attribution == "legs" or kind not in CROSS_CCY_KINDS
        c = cell(home, kind)
        c["ids"].add(pid)
        if summary.risk_problem(f.get("risk"), f.get("dv01"), f.get("mv_rub"), f.get("notional_rub"),
                                f.get("instr_type") == "BOND"):
            # TDM#170: an impossible risk is counted as missing, none of it in the sums (its MV and notional are)
            for key, src in (("notional_ccy", "notional"), ("notional_rub", "notional_rub"), ("mv_rub", "mv_rub")):
                c[key] += f.get(src) or 0.0
            c["missing"]["implausible"] = c["missing"].get("implausible", 0) + 1
            continue
        for key, src in (("notional_ccy", "notional"), ("notional_rub", "notional_rub"), ("mv_rub", "mv_rub"),
                         ("cs01", "cs01")):
            c[key] += f.get(src) or 0.0
        ex = f.get("extra") if isinstance(f.get("extra"), dict) else {}
        cp = ex.get("curve_proxy")
        n_nb += isinstance(cp, dict) and cp.get("reason") == "no_ruonia_curve"  # TDM#467: its basis is in the CS01
        b, spr = summary.basis_of({"ofz_basis": ex.get("ofz_basis01"), "ofz": f.get("ofz_spr01")})
        # «КБД»: the run's KBD risk of an OFZ of the KBD scheme stays on RUB OFZ (as lib/real.js: another row's, e.g. a
        # corporate's on a date without RUONIA, is on RUONIA in both views)
        curves = _risk_curves(f, kbd and ex.get("ofz_basis01") is not None)
        if kbd and ex.get("ofz_ruonia01") is not None and not is_kbd_floater_row(f, ex):
            # RUONIA + basis: the row's RUB RUONIA is RUONIA - basis (bonds.ofz_levels), the KBD view keeps its basis, the
            # KBD's par risk. A KBD floater (TDM#467) keeps both: its RUONIA - basis is the discounting on RUONIA, its
            # basis the projection on the KBD, so the DV01 sums to its RUONIA risk in both views
            curves.pop("RUB RUONIA", None)
        if summary.ofz_in_proxy(kind == "OFZ_IN", ex.get("ofz_basis01"), ex.get("quality")):
            c["in_proxy"] += b  # a linker's nominal DV01 on the KBD: in the DV01 (RUONIA or RUB OFZ), not a basis
        elif not kbd:
            c["basis"] += b
            n_p += ex.get("ofz_basis01") is not None and ex.get("ofz_ruonia01") is None
        elif kind == "OFZ_PK":  # «КБД»: an OFZ-PK has no KBD risk, its basis is its own spread to RUONIA
            spr = (spr or 0.0) + b
        elif ex.get("ofz_basis01") is None and b:  # a run before 0.2.6: the OFZ on RUONIA, its KBD risk ~ its basis
            curves = {**curves, "RUB OFZ": curves.get("RUB OFZ", 0.0) + b,
                      "RUB RUONIA": curves.get("RUB RUONIA", 0.0) - b}
            curves = {cid: v for cid, v in curves.items() if v}
            approx = True
        c["ofz_spr"] += spr or 0.0
        for cid, v in curves.items():
            cc = _curve_ccy(cid)
            o = c if cc == home or not legs else cell(cc, kind)
            o["curves"][cid] = o["curves"].get(cid, 0.0) + v
            if o is not c:
                o["legs"].add(pid)
            elif cc != home:
                c["foreign"] += v
        for cc, amt in (f.get("fx_delta") or {}).items():
            if not amt:
                continue
            if cc == home:
                c["fx_delta_ccy"] += amt
            elif legs:
                o = cell(cc, kind)
                o["legs"].add(pid)
                o["fx_delta_ccy"] += amt
            elif fx.get(cc) and fx.get(home):  # in the position's currency at the day's rates
                c["fx_delta_ccy"] += amt * fx[cc] / fx[home]
        if kind in RISK_KINDS and not (curves or f.get("cs01") or f.get("ofz_spr01") or f.get("accrual_only")):
            why = "reported_no_risk" if (f.get("extra") or {}).get("quality") == "reported_value" else "no_risk"
            c["missing"][why] = c["missing"].get(why, 0) + 1

    def out(ccy: str, level: str, group: str | None, kind: str | None, cs: list[dict[str, Any]]) -> dict[str, Any]:
        ids = set().union(*(c["ids"] for c in cs))
        curves: dict[str, float] = {}
        missing: dict[str, int] = {}
        for c in cs:
            for cid, v in c["curves"].items():
                curves[cid] = curves.get(cid, 0.0) + v
            for k, n in c["missing"].items():
                missing[k] = missing.get(k, 0) + n
        dv01 = sum(v for cid, v in curves.items() if _curve_ccy(cid) == ccy)
        mv = sum(c["mv_rub"] for c in cs)
        rate = 1.0 if ccy == "RUB" else fx.get(ccy)
        return {"ccy": ccy, "level": level, "group": group, "kind": kind, "label": KIND_LABEL[kind] if kind else None,
                "n_positions": len(ids), "n_legs_only": len(set().union(*(c["legs"] for c in cs)) - ids),
                "notional_ccy": sum(c["notional_ccy"] for c in cs),
                "notional_rub": sum(c["notional_rub"] for c in cs), "mv_rub": mv, "mv_ccy": mv / rate if rate else None,
                "dv01_rub": dv01, "dv01_ccy": dv01 / rate if rate else None, "dv01_by_curve_rub": curves,
                "dv01_foreign_rub": sum(c["foreign"] for c in cs), "ofz_basis_rub": sum(c["basis"] for c in cs),
                "ofz_spread_kbd_rub": sum(c["ofz_spr"] for c in cs), "dv01_ofz_in_proxy_rub": sum(c["in_proxy"] for c in cs),
                "cs01_rub": sum(c["cs01"] for c in cs), "fx_delta_ccy": sum(c["fx_delta_ccy"] for c in cs),
                "n_risk_missing": sum(missing.values()),
                "risk_missing": [{"reason": k, "text": MISSING_TEXT[k], "n": n} for k, n in sorted(missing.items())]}

    order = {k: i for i, (k, _, _) in enumerate(INSTR_KINDS)}
    ccys = sorted({c for c, _ in cells}, key=lambda c: (c != "RUB", FX_CCYS.index(c) if c in FX_CCYS else 99, c))
    rows: list[dict[str, Any]] = []
    for ccy in ccys:
        mine = {k: c for (cc, k), c in cells.items() if cc == ccy}
        rows.append(out(ccy, "ccy", None, None, list(mine.values())))
        for g in GROUPS:
            kinds = sorted((k for k in mine if KIND_GROUP[k] == g), key=order.__getitem__)
            if kinds:
                rows.append(out(ccy, "group", g, None, [mine[k] for k in kinds]))
                rows.extend(out(ccy, "kind", g, k, [mine[k]]) for k in kinds)
    blocks = [r for r in rows if r["level"] == "ccy"]
    curves_all: dict[str, float] = {}
    for r in blocks:
        for cid, v in r["dv01_by_curve_rub"].items():
            curves_all[cid] = curves_all.get(cid, 0.0) + v
    no_rate = [r["ccy"] for r in blocks if r["ccy"] != "RUB" and r["fx_delta_ccy"] and not fx.get(r["ccy"])]
    total = {"n_positions": len(every), "notional_rub": sum(r["notional_rub"] for r in blocks),
             "mv_rub": sum(r["mv_rub"] for r in blocks), "dv01_by_curve_rub": curves_all,
             "dv01_all_curves_rub": sum(curves_all.values()),
             "ofz_basis_rub": sum(r["ofz_basis_rub"] for r in blocks), "cs01_rub": sum(r["cs01_rub"] for r in blocks),
             "ofz_spread_kbd_rub": sum(r["ofz_spread_kbd_rub"] for r in blocks),
             "dv01_ofz_in_proxy_rub": sum(r["dv01_ofz_in_proxy_rub"] for r in blocks),
             "fx_delta_rub": None if no_rate else sum(r["fx_delta_ccy"] * (fx.get(r["ccy"]) or 0.0)
                                                      for r in blocks if r["ccy"] != "RUB"),
             "n_risk_missing": sum(r["n_risk_missing"] for r in blocks)}
    columns = []
    for k in sorted({k for _, k in cells}, key=order.__getitem__):
        mine = [(cc, c) for (cc, kk), c in cells.items() if kk == k]
        curves: dict[str, float] = {}
        for _, c in mine:
            for cid, v in c["curves"].items():
                curves[cid] = curves.get(cid, 0.0) + v
        fx_rows = [(cc, c["fx_delta_ccy"]) for cc, c in mine if cc != "RUB" and c["fx_delta_ccy"]]
        columns.append({
            "kind": k, "group": KIND_GROUP[k], "label": KIND_LABEL[k],
            "n_positions": len(set().union(*(c["ids"] for _, c in mine))),
            "notional_rub": sum(c["notional_rub"] for _, c in mine), "mv_rub": sum(c["mv_rub"] for _, c in mine),
            "dv01_by_curve_rub": curves, "dv01_all_curves_rub": sum(curves.values()),
            "ofz_basis_rub": sum(c["basis"] for _, c in mine), "cs01_rub": sum(c["cs01"] for _, c in mine),
            "ofz_spread_kbd_rub": sum(c["ofz_spr"] for _, c in mine),
            "dv01_ofz_in_proxy_rub": sum(c["in_proxy"] for _, c in mine),
            "fx_delta_rub": None if any(not fx.get(cc) for cc, _ in fx_rows) else sum(v * fx[cc] for cc, v in fx_rows),
            "n_risk_missing": sum(sum(c["missing"].values()) for _, c in mine)})
    return {"attribution": attribution, "attribution_rule": ATTRIBUTIONS[attribution],
            "attribution_provisional": True, "cross_ccy_kinds": list(CROSS_CCY_KINDS),
            "ofz_view": ofz_view, "ofz_view_rule": OFZ_VIEWS[ofz_view],
            "ofz_view_note": OFZ_VIEW_APPROX if approx else OFZ_VIEW_P.format(n=n_p) if n_p else None,
            "n_basis_not_split": n_nb, "basis_not_split_note": BASIS_NOT_SPLIT.format(n=n_nb) if n_nb else None,
            "kinds": [{"kind": k, "group": g, "label": lab} for k, g, lab in INSTR_KINDS],
            "measures": [{"measure": m, "label": lab, "across_ccys": a} for m, lab, a in MATRIX_MEASURES
                         if not (kbd and m == "ofz_basis")],
            "fx": {c: fx.get(c) for c in ccys if c != "RUB"}, "ccys": ccys, "columns": columns, "rows": rows,
            "total": total}
