"""Market history for the concept pages, loaded lazily (not in ``/concepts-data.js``, so a page that does not chart it
stays fast): ``GET /api/v1/history/market?run_id=`` and ``GET /api/v1/history/status`` (R24).

``market``: as of the run's date, the latest market day not after it and the market days 1D / 1W / 1M / 3M / YTD
before it (the latest day on or before one day, 7 days, one month, three months back, and the last day of the
previous year), and for those days

* ``bonds``: per quoted bond (secid) its yield change in bp and its clean price change in points over each period
  (``md_bond_day``: the history API's rows, else the snapshots');
* ``indices``: КС, RUONIA, RUSFAR, RUSFAR CNY, SOFR and €STR (hub 0.3.0, contract 1.3: one series per day by the
  hub's source order, ``SOURCE_ORDER``) and CPI year on year (the CBR's official ``CPI_RU_YOY`` of 1.3, else computed
  from CPI_RU_MOM, the product of the last twelve months, said in ``source``) from the hub's fixings, each with its
  value, its changes in bp and its last year of values; an index the hub does not have is absent;
* ``fx``: the CBR rates of USD, EUR and CNY (RUB per unit) with their changes in %, and MOEX's TOM close;
* ``crosses``: EUR/USD and USD/CNY: the hub's cross-rate series (1.3: ``CROSS_PREF``, the kind shown in ``kind``,
  every kind's value in ``kinds``), else computed from the two CBR rates (``kind`` ``cbr_derived``);
* ``curves``: RUONIA OIS (``RUB RUONIA``), key-rate swaps (``RUB KEY``), the KBD (``OFZ``) and the other curve datasets
  of the hub at the risk pillars on each of those days (linear in the term between quoted tenors, none outside),
  and two years of daily mids of 1Y, 2Y, 5Y and 10Y. A day's quote set of a curve is that of one source: two-sided
  (bid/ask) before mid-only, then the most tenors, then the latest fetched. The government par-yield proxies of 1.3
  (``usd_ust_par``, ``eur_aaa_par``, ``eur_bund_par``) carry ``proxy`` «прокси (UST par), не OIS» and their
  currency: display only, never the risk of a run (``curves.proxy``).

Without the history API's tables the bonds come from the snapshots and the rest is empty, with a note. Numbers are
floats rounded for display; the ETag is the body's hash."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from rub_fixed_income import RISK_PILLARS, kbd_effective_yield

from ..curves import proxy
from ..marketdata import hubhistory
from ..marketdata.history import kbd
from ..marketdata.history import latest_market_day as latest_day
from . import runs
from .app import ApiError, db

PERIODS = ("1D", "1W", "1M", "3M", "YTD")
INDICES = (("КС", "KEY_RATE"), ("RUONIA", "RUONIA"), ("RUSFAR", "RUSFAR"), ("RUSFAR CNY", "RUSFARCNY"),
           ("SOFR", "SOFR"), ("€STR", "ESTR"))
CPI = "CPI_RU_MOM"
CPI_YOY = "CPI_RU_YOY"
SOURCE_ORDER = {"ESTR": ("ecb_data_portal", "bundesbank", "dbnomics", "fred"), "SOFR": ("nyfed", "fred"),
                "SOFR30DAVG": ("nyfed", "fred"), "SOFR90DAVG": ("nyfed", "fred"), "SOFR180DAVG": ("nyfed", "fred"),
                "SOFRINDEX": ("nyfed", "fred")}
"""Codes whose sources republish one figure (the hub's HISTORY_API): per day the first source present in this order.
Any other code: the source with the most rows (one series per code, sources are not merged)."""
CROSSES = (("EUR/USD", "EUR", "USD"), ("USD/CNY", "USD", "CNY"))
CROSS_PREF = {"EUR/USD": ("ecb", "fred_h10", "cbr_cross", "moex_indicative_cross", "moex_cets"),
              "USD/CNY": ("moex_cets", "cbr_cross", "moex_indicative_cross", "fred_h10", "ecb")}
CROSS_LABELS = {"ecb": "ЕЦБ", "fred_h10": "ФРС H.10", "cbr_cross": "кросс курсов ЦБ", "moex_cets": "MOEX CETS",
                "moex_indicative_cross": "кросс индикативных MOEX 19:00", "cbr_derived": "кросс курсов ЦБ (расчёт)"}
FX = ("USD", "EUR", "CNY")
CURVE_IDS = {"ruonia_ois": "RUB RUONIA", "key_rate_swap": "RUB KEY"}
SERIES_TENORS = (1.0, 2.0, 5.0, 10.0)
SERIES_DAYS = 730


def _months_back(d: date, n: int) -> date:
    m = d.month - 1 - n
    y, m = d.year + m // 12, m % 12 + 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def period_days(con, d: date) -> dict[str, date | None]:
    """The market days the periods start on (``PERIODS``)."""
    targets = {"1D": d - timedelta(days=1), "1W": d - timedelta(days=7), "1M": _months_back(d, 1),
               "3M": _months_back(d, 3), "YTD": date(d.year - 1, 12, 31)}
    return {k: latest_day(con, t) for k, t in targets.items()}


def _r(v: float | None, n: int) -> float | None:
    return None if v is None or not math.isfinite(v) else round(v, n)


def bond_changes(con, d: date, starts: dict[str, date | None]) -> dict[str, dict[str, list[float | None]]]:
    """secid -> {"y": [bp per period], "p": [price points per period]} for the bonds quoted on ``d``."""
    days = [d, *(x for x in starts.values() if x is not None)]
    by: dict[date, dict[str, tuple[float | None, float | None]]] = defaultdict(dict)
    for r in con.execute("SELECT business_date, secid, yield_pct, clean_pct FROM md_bond_day"
                         " WHERE business_date = ANY(%s)", (days,)):
        by[r["business_date"]][r["secid"]] = (r["yield_pct"], r["clean_pct"])
    out = {}
    for secid, (y, p) in by[d].items():
        ys, ps = [], []
        for k in PERIODS:
            o = by.get(starts[k]) if starts[k] is not None else None
            y0, p0 = (o or {}).get(secid, (None, None))
            ys.append(_r((y - y0) * 100, 1) if y is not None and y0 is not None else None)
            ps.append(_r(p - p0, 3) if p is not None and p0 is not None else None)
        out[secid] = {"y": ys, "p": ps}
    return out


def _source_per_code(con, codes: list[str]) -> dict[str, str]:
    """The source of each fixing code with the most rows (one series per code, sources are not merged)."""
    out: dict[str, tuple[int, str]] = {}
    for r in con.execute("SELECT index_code, source, count(*) AS n FROM md_hub_fixing WHERE index_code = ANY(%s)"
                         " GROUP BY 1, 2", (codes,)):
        if r["index_code"] not in out or r["n"] > out[r["index_code"]][0]:
            out[r["index_code"]] = (r["n"], r["source"])
    return {k: s for k, (_, s) in out.items()}


def _at(series: list[tuple[date, float]], d: date | None) -> float | None:
    """The value in force on ``d``: the latest on or before it."""
    if d is None or not series:
        return None
    i = bisect.bisect_right([x for x, _ in series], d)
    return series[i - 1][1] if i else None


def fixing_series(con, codes: list[str], d: date) -> dict[str, list[tuple[date, float]]]:
    """code -> [(date, value)] up to ``d``: one series per code (``SOURCE_ORDER``, else the source with most rows)."""
    src = _source_per_code(con, codes)
    raw: dict[str, list[tuple[date, float]]] = defaultdict(list)
    merged: dict[str, dict[date, tuple[int, float]]] = defaultdict(dict)
    for r in con.execute("SELECT index_code, source, fixing_date, value FROM md_hub_fixing WHERE index_code = ANY(%s)"
                         " AND fixing_date <= %s AND value IS NOT NULL ORDER BY fixing_date",
                         (codes, d)):
        code = r["index_code"]
        if code in SOURCE_ORDER:
            order = SOURCE_ORDER[code]
            k = order.index(r["source"]) if r["source"] in order else len(order)
            have = merged[code].get(r["fixing_date"])
            if have is None or k < have[0]:
                merged[code][r["fixing_date"]] = (k, float(r["value"]))
        elif src.get(code) == r["source"]:
            raw[code].append((r["fixing_date"], float(r["value"])))
    for code, by_day in merged.items():
        raw[code] = [(x, v) for x, (_, v) in sorted(by_day.items())]
    return raw


def cpi_yoy(raw: dict[str, list[tuple[date, float]]]) -> tuple[list[tuple[date, float]], str | None]:
    """CPI year on year, % (month = the first day of the month): the CBR's official ``CPI_RU_YOY`` (contract 1.3),
    else the product of twelve monthly indices of ``CPI_RU_MOM`` (Rosstat, % to the previous month); and its source."""
    if raw.get(CPI_YOY):
        return raw[CPI_YOY], "ЦБ: ИПЦ г/г (официальный)"
    cpi = raw.get(CPI, [])
    yoy = []
    for i in range(11, len(cpi)):
        months = cpi[i - 11:i + 1]
        if (months[-1][0].year - months[0][0].year) * 12 + months[-1][0].month - months[0][0].month == 11:
            yoy.append((months[-1][0], (math.prod(v / 100 for _, v in months) - 1) * 100))
    return yoy, ("расчёт по ИПЦ м/м Росстата (12 мес.)" if yoy else None)


def indices(con, d: date, starts: dict[str, date | None]) -> dict[str, dict[str, Any]]:
    codes = [c for _, c in INDICES] + [CPI, CPI_YOY]
    raw = fixing_series(con, codes, d)
    out: dict[str, dict[str, Any]] = {}
    series = {name: raw.get(code, []) for name, code in INDICES}
    yoy, yoy_src = cpi_yoy(raw)
    if yoy:
        series["CPI г/г"] = yoy
    for name, s in series.items():
        if not s:
            continue
        v = _at(s, d)
        last = next((x for x, _ in reversed(s) if x <= d), None)
        out[name] = {"value": _r(v, 4), "date": last and last.isoformat(),
                     "chgBp": [_r((v - o) * 100, 1) if v is not None and (o := _at(s, starts[k])) is not None
                               else None for k in PERIODS],
                     "series": [[x.isoformat(), _r(y, 4)] for x, y in s if x > d - timedelta(days=366)]}
        if name == "CPI г/г":
            out[name]["source"] = yoy_src
    return out


def crosses(con, d: date, starts: dict[str, date | None]) -> dict[str, dict[str, Any]]:
    """EUR/USD and USD/CNY (module doc): the hub's cross series of the first kind of ``CROSS_PREF`` it has (a pair
    stored the other way round is inverted), else the CBR cross; {} without either."""
    pairs = [p for p, _, _ in CROSSES] + [f"{q}/{b}" for _, b, q in CROSSES]
    by: dict[tuple[str, str], list[tuple[date, float]]] = defaultdict(list)
    for r in con.execute("SELECT kind, pair, date, per_unit FROM md_hub_fx WHERE pair = ANY(%s) AND kind <> ALL(%s)"
                         " AND date <= %s AND per_unit IS NOT NULL AND per_unit <> 0 ORDER BY date",
                         (pairs, list(hubhistory.FX_KINDS), d + timedelta(days=1))):
        by[(r["kind"], r["pair"])].append((r["date"], float(r["per_unit"])))
    cbr: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for r in con.execute("SELECT pair, date, per_unit FROM md_hub_fx WHERE kind = 'cbr_official' AND pair = ANY(%s)"
                         " AND date <= %s AND per_unit IS NOT NULL ORDER BY date",
                         ([f"{c}/RUB" for c in FX], d + timedelta(days=1))):
        cbr[r["pair"][:3]].append((r["date"], float(r["per_unit"])))
    out = {}
    for pair, base, quote in CROSSES:
        kinds: dict[str, list[tuple[date, float]]] = {}
        for k in CROSS_PREF[pair]:
            s = by.get((k, pair)) or [(x, 1 / v) for x, v in by.get((k, f"{quote}/{base}"), [])]
            if s:
                kinds[k] = s
        if not kinds and cbr.get(base) and cbr.get(quote):
            q = cbr[quote]
            kinds["cbr_derived"] = [(x, v / qv) for x, v in cbr[base] if (qv := _at(q, x))]
        if not kinds:
            continue
        kind = next(iter(kinds))
        s = kinds[kind]
        v = _at(s, d)
        last = next((x for x, _ in reversed(s) if x <= d), None)
        out[pair] = {"value": _r(v, 6), "date": last and last.isoformat(), "kind": kind,
                     "label": CROSS_LABELS.get(kind, kind),
                     "chgPct": [_r((v / o - 1) * 100, 3) if v is not None and (o := _at(s, starts[k])) else None
                                for k in PERIODS],
                     "kinds": {k: _r(_at(x, d), 6) for k, x in kinds.items()},
                     "series": [[x.isoformat(), _r(y, 6)] for x, y in s if x > d - timedelta(days=366)]}
    return out


def fx(con, d: date, starts: dict[str, date | None]) -> dict[str, dict[str, Any]]:
    out = {}
    cbr: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for r in con.execute("SELECT pair, date, per_unit FROM md_hub_fx WHERE kind = 'cbr_official' AND pair = ANY(%s)"
                         " AND date <= %s AND per_unit IS NOT NULL ORDER BY date",
                         ([f"{c}/RUB" for c in FX], d + timedelta(days=1))):
        cbr[r["pair"][:3]].append((r["date"], float(r["per_unit"])))
    moex = {r["pair"][:3]: (r["date"], float(r["per_unit"])) for r in con.execute(
        "SELECT DISTINCT ON (pair) pair, date, per_unit FROM md_hub_fx WHERE kind = 'moex_eod' AND tenor = 'TOM'"
        " AND pair = ANY(%s) AND date <= %s AND per_unit IS NOT NULL ORDER BY pair, date DESC",
        ([f"{c}/RUB" for c in FX], d))}
    for c in FX:
        s = cbr.get(c, [])
        v = _at(s, d)
        if v is None and c not in moex:
            continue
        out[c] = {"cbr": _r(v, 4), "chgPct": [_r((v / o - 1) * 100, 3) if v is not None and (o := _at(s, starts[k]))
                                              else None for k in PERIODS],
                  "moexTom": _r(moex[c][1], 4) if c in moex else None,
                  "moexDate": moex[c][0].isoformat() if c in moex else None}
    return out


def _years(r: dict[str, Any]) -> float | None:
    if r["tenor_months"] is not None:
        return r["tenor_months"] / 12
    if r["tenor_days"] is not None:
        return r["tenor_days"] / 365.25
    t = (r["tenor"] or "").upper()
    try:
        n, u = float(t[:-1]), t[-1]
    except ValueError:
        return {"ON": 1 / 365.25, "TN": 2 / 365.25, "SN": 3 / 365.25}.get(t)
    return n * {"D": 1 / 365.25, "W": 7 / 365.25, "M": 1 / 12, "Y": 1.0}.get(u, math.nan)


def interp(points: list[tuple[float, float]], t: float) -> float | None:
    """Linear in the term between the quoted tenors; None outside them."""
    if not points or t < points[0][0] - 1e-9 or t > points[-1][0] + 1e-9:
        return None
    i = bisect.bisect_left([x for x, _ in points], t - 1e-9)
    if i < len(points) and abs(points[i][0] - t) < 1e-9:
        return points[i][1]
    (x0, y0), (x1, y1) = points[i - 1], points[i]
    return y0 + (y1 - y0) * (t - x0) / (x1 - x0)


def curve_days(con, d: date) -> tuple[dict[str, dict[date, list[tuple[float, float]]]], dict[tuple[str, date], str]]:
    """dataset -> day -> [(years, mid %)] of the chosen source, the last ``SERIES_DAYS`` up to ``d``; and per
    (dataset, day) the source chosen, ``"<source>"`` or ``"<source>, mid"`` for a mid-only quote set (the hub's
    end-of-day OTC sets are mid-only, ~19:00; andromeda's history is older)."""
    sets: dict[tuple[str, date, str], list[tuple[float, float]]] = defaultdict(list)
    rank: dict[tuple[str, date, str], tuple] = {}
    for r in con.execute("SELECT dataset, snapshot_date, source, tenor, tenor_months, tenor_days, bid_pct, mid_pct,"
                         " ask_pct, quote_basis, fetched_at FROM md_hub_curve WHERE snapshot_date <= %s"
                         " AND snapshot_date > %s AND mid_pct IS NOT NULL",
                         (d, d - timedelta(days=SERIES_DAYS))):
        t = _years(r)
        if t is None or not math.isfinite(t):
            continue
        k = (r["dataset"], r["snapshot_date"], r["source"])
        sets[k].append((t, float(r["mid_pct"])))
        rank[k] = (r["quote_basis"] == "bid_ask_mid", r["fetched_at"].timestamp() if r["fetched_at"] else 0.0)
    best: dict[tuple[str, date], tuple[tuple, list, str]] = {}
    for (ds, day, src), pts in sets.items():
        a, b = rank[(ds, day, src)]
        key = (a, len(pts), b)
        if (ds, day) not in best or key > best[(ds, day)][0]:
            best[(ds, day)] = (key, sorted(pts), src if a else f"{src}, mid")
    out: dict[str, dict[date, list[tuple[float, float]]]] = defaultdict(dict)
    for (ds, day), (_, pts, _src) in best.items():
        out[ds][day] = pts
    return out, {k: v[2] for k, v in best.items()}


def curves(con, d: date, starts: dict[str, date | None]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    def entry(name: str, by_day: dict[date, Any], at: Any, src: dict[date, str] | None = None) -> None:
        days = sorted(by_day)
        if not days:
            return

        def on(x: date | None) -> date | None:
            if x is None:
                return None
            i = bisect.bisect_right(days, x)
            return days[i - 1] if i else None

        when = {"asOf": on(d), **{k: on(starts[k]) for k in PERIODS}}
        out[name] = {"days": {k: v and v.isoformat() for k, v in when.items()},
                     "sources": {k: (src or {}).get(v) for k, v in when.items()},
                     "pts": {k: [_r(at(by_day[v], t), 4) for t in RISK_PILLARS] if v else None
                             for k, v in when.items()},
                     "series": {"dates": [x.isoformat() for x in days],
                                **{f"{t:g}Y": [_r(at(by_day[x], t), 4) for x in days] for t in SERIES_TENORS}}}

    days, chosen = curve_days(con, d)
    for ds, by_day in days.items():
        entry(CURVE_IDS.get(ds, ds), by_day, interp, {day: chosen[(ds, day)] for day in by_day})
        if proxy.is_proxy(ds) and CURVE_IDS.get(ds, ds) in out:  # government par yields, not OIS: display only
            out[CURVE_IDS.get(ds, ds)].update({"proxy": proxy.label(ds), "ccy": proxy.PROXIES[ds][0]})
    params = {r["business_date"]: kbd(r["trade_date"], r["params"]) for r in con.execute(
        "SELECT business_date, trade_date, params FROM md_kbd_day WHERE business_date <= %s AND business_date > %s",
        (d, d - timedelta(days=SERIES_DAYS)))}
    entry("OFZ", {k: v for k, v in params.items() if v is not None},
          lambda p, t: kbd_effective_yield(p, t) * 100)
    return out


_CACHE: dict[tuple, dict[str, Any]] = {}
"""(run id, the history's version) -> ``market``: a page reload costs two small queries, not the curves' days."""


def version(con) -> tuple:
    """What changes when the market history does: the last fetch of the history API and the last snapshot loaded."""
    r = con.execute("SELECT (SELECT max(fetch_id) FROM md_hub_job) AS a, (SELECT sum(rows) FROM md_hub_job) AS b,"
                    " (SELECT max(loaded_at) FROM md_snapshot_load) AS c, (SELECT code FROM md_hub_state) AS d"
                    ).fetchone()
    return tuple(r.values()) if isinstance(r, dict) else tuple(r)


def market(con, run: dict[str, Any]) -> dict[str, Any]:
    key = (run["run_id"], version(con))
    if key not in _CACHE:
        if len(_CACHE) > 16:
            _CACHE.clear()
        _CACHE[key] = _market(con, run)
    return _CACHE[key]


def _market(con, run: dict[str, Any]) -> dict[str, Any]:
    as_of = run["business_date"]
    d = latest_day(con, as_of)
    if d is None:
        return {"runId": run["run_id"], "asOf": as_of.isoformat(), "marketDate": None, "periods": {}, "bonds": {},
                "indices": {}, "fx": {}, "curves": {}, "notes": ["рыночной истории нет"]}
    starts = period_days(con, d)
    st = con.execute("SELECT code, reason FROM md_hub_state").fetchone()
    notes = []
    if st is None or st["code"] != "ok":
        notes.append("история рынка — по снимкам хаба (API истории: "
                     + (st["reason"] if st else "ещё не опрашивался") + "): фиксингов, курсов и кривых по датам нет")
    if d < as_of:  # an intraday or preliminary run: no close of its own date yet (the hub's preliminary cut has no bonds)
        notes.append(f"закрытия рынка за {as_of:%d.%m.%Y} ещё нет: изменения — к закрытию {d:%d.%m.%Y}")
    ind = indices(con, d, starts)
    ruo = ind.get("RUONIA")
    if ruo and ruo["date"] and ruo["date"] < d.isoformat():  # RUONIA of a day is published the next business day
        ruo["lag"] = True
        notes.append(f"RUONIA за {d:%d.%m.%Y} ещё не опубликована (ЦБ публикует её на следующий рабочий день):"
                     f" показана за {date.fromisoformat(ruo['date']):%d.%m.%Y}")
    return {"runId": run["run_id"], "asOf": as_of.isoformat(), "marketDate": d.isoformat(),
            "periods": {k: v and v.isoformat() for k, v in starts.items()}, "periodOrder": list(PERIODS),
            "pillars": list(RISK_PILLARS), "bonds": bond_changes(con, d, starts), "indices": ind,
            "fx": fx(con, d, starts), "crosses": crosses(con, d, starts), "curves": curves(con, d, starts),
            "notes": notes}


# ---- routes (``create_app`` includes ``router``; this module imports ``app``, not the other way round) ----------

Con = Annotated[psycopg.Connection, Depends(db)]
router = APIRouter()


def _json(request: Request, data: dict[str, Any]) -> Response:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    tag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"ETag": tag, "Cache-Control": "no-cache"}
    if tag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@router.get("/api/v1/history/market")
def history_market(con: Con, request: Request, run_id: int | None = None, as_of: date | None = None) -> Response:
    """The market's changes, indices, FX and curves by date as of the run (``market_history.market``)."""
    run = runs.resolve_run(con, as_of, run_id)
    if run is None:
        raise ApiError(404, "not_found", f"run {run_id} not found" if run_id else "no runs")
    return _json(request, market(con, run))


@router.get("/api/v1/history/status")
def history_status(con: Con) -> dict[str, Any]:
    """What the store holds of the hub's history API and why it is not used (``hubhistory.status``)."""
    return hubhistory.status(con)
