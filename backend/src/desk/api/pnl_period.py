"""PnL for a period (TDM#407, docs/architecture/briefs/PNL_PERIOD.md): components by day, position and bucket, rates
by the position's curve (``period``); curve moves, DV01 and linear rates by curve and term (``curves``); one position's
days with their inputs of D and D-1 (``position``).

Runs, filters and the net total as ``pnl_history`` (``period_runs``, ``rows_query``, ``amounts``), compensated sums
(``Sum``). Missing is null, never 0; ``m`` counts the rows lacking a component. The carry split of TDM#406
(``carry_coupon``, ``carry_pull``, ``rates_linear_curves``, ``prev``) is read when saved, else null. Only the fact
fields used are projected, never the whole fact (cashflows, gamma); one server-cursor pass per request.
"""
from __future__ import annotations

import bisect
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from psycopg.rows import dict_row
from rub_fixed_income import RISK_BUCKETS

from ..pnl.reasons import REPORTED_METHOD
from ..publish.summary import kind_of
from .pnl_history import (
    KEYS,
    Sum,
    amounts,
    catalogue_rows,
    market_dates,
    period_runs,
    rows_query,
    run_rows,
)
from .runs import run_slice

CARRY_SPLIT = ("carry_coupon", "carry_pull")
NOSPLIT = "carry_nosplit"
"""Carry of the rows with carry but without ``carry_coupon`` (flash simple / yield, undated recompute): attribution and
carry views split per row (TDM#432); never a missing component (``m`` stays 0), null when no such row."""
RATES_SPLIT = ("rates_linear", "rates_exact_lin")
"""``pnl.extra.rates_linear`` and the exact ``rates`` of the same rows, RUB bonds only (foreign ones stay whole in
``rates`` until TDM#418): «Ставки (линейно)» and «Выпуклость (ставки)» = exact − linear (TDM#435)."""
EXTRA_KEYS = (*CARRY_SPLIT, NOSPLIT, *RATES_SPLIT)
COMPONENTS = (*KEYS, *EXTRA_KEYS)
ORDER = ("carry", "carry_coupon", "carry_pull", NOSPLIT, "rolldown", "funding", "rates", *RATES_SPLIT, "spread",
         "convexity", "fx_gross", "fx_funding", "new_trades", "other", "reported")
"""``keys``: the order shown; the extra keys only when a row of the slice has them."""
GRANS = ("day", "week", "month")
MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
DROP_EXTRA = "ARRAY['prev', 'rates_linear_curves', 'curve_moves_bp']"
"""Per-row extra the period does not use (TDM#406); ``position`` reads the whole PnL of its few rows."""

KIND_COLUMNS = """p.fact->>'instr_type' AS "instrType", p.fact->>'segment' AS segment,
 p.fact->>'coupon_type' AS "couponType", p.fact->>'spread_curve' AS curve,
 p.fact->'extra'->'rating'->>'grade' AS rating, COALESCE(p.fact->'extra'->'reported'->>'x_product_group',
 p.fact->'extra'->'reported'->'x'->>'x_product_group') AS "productGroup", r.prev_date AS "runPrevDate\""""
TRIMMED_PNL = f"""((p.pnl - 'extra'::text) || jsonb_build_object('extra', (p.pnl->'extra') - {DROP_EXTRA})) AS pnl"""
PERIOD_COLUMNS = KIND_COLUMNS + f""", {TRIMMED_PNL}, p.pv_rub AS pv,
 p.fact->'extra'->'funding'->'lines' AS "fundingLines\""""
CURVES_COLUMNS = """p.fact->>'spread_curve' AS curve, p.fact->'risk' AS risk, (p.pnl->>'rates')::float8 AS rates,
 (p.pnl->'extra'->>'rates_linear')::float8 AS "ratesLinear", p.pnl->'extra'->'rates_linear_curves' AS "linearCurves\""""
POSITION_COLUMNS = KIND_COLUMNS + """, p.pnl, p.pv_rub AS pv, p.dv01_rub AS dv01, p.cs01_rub AS cs01,
 p.quantity, (p.fact->>'price')::float8 AS clean, (p.fact->>'ytm')::float8 AS ytm, p.fact->>'maturity' AS maturity,
 p.fact->'extra'->>'offer_date' AS "offerDate", p.fact->'extra'->>'offer_kind' AS "offerKind\""""
ORDER_BY = ' ORDER BY d.date, d.book, d."positionId"'


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _rub_bond(r: dict[str, Any]) -> bool:
    """A rouble bond as ``summary.kind_of`` reads it (``instr_type`` BOND or none, currency RUB or none)."""
    return r.get("instrType") in ("BOND", None) and (r.get("ccy") or "RUB") == "RUB"


def components(pnl: dict[str, Any] | None, row: dict[str, Any] | None = None
               ) -> tuple[dict[str, Any], dict[str, float | None]]:
    """``amounts`` of a row and its components with the carry split of ``pnl.extra`` (null when not saved), the carry
    of a row without it (``NOSPLIT``) and, given the row, the linear rates of a RUB bond (``RATES_SPLIT``; a
    ``position`` day has ``ratesLinear`` of its own)."""
    a = amounts(pnl)
    extra = (pnl or {}).get("extra") or {}
    comp = {**a["components"], **{k: _num(extra.get(k)) for k in CARRY_SPLIT}}
    carry = _num(comp["carry"])
    comp[NOSPLIT] = carry if carry is not None and comp["carry_coupon"] is None else None
    if row is not None:
        lin, rates = _num(extra.get("rates_linear")) if _rub_bond(row) else None, _num(comp["rates"])
        comp.update(dict.fromkeys(RATES_SPLIT) if lin is None or rates is None else
                    {"rates_linear": lin, "rates_exact_lin": rates})
    return a, comp


def keys_of(seen: set[str]) -> list[str]:
    return [k for k in ORDER if k not in EXTRA_KEYS or k in seen]


class Agg:
    """``Agg`` of the contract: net total by ``amounts``, every component, rows lacking each (``m``)."""

    def __init__(self) -> None:
        self.total, self.gap = Sum(), Sum()
        self.c = {k: Sum() for k in COMPONENTS}
        self.m = dict.fromkeys(COMPONENTS, 0)
        self.rows = self.no_total = self.partial = 0

    def add(self, a: dict[str, Any], comp: dict[str, float | None]) -> None:
        self.rows += 1
        self.no_total += a["total"] is None
        self.partial += bool(a["missing"])
        self.total.add(a["total"])
        self.gap.add(a["grossCheck"])
        for k, v in comp.items():
            # as pnl_history.Aggregate: a reported row has no ``other`` and a flash row no ``reported`` by method
            applicable = (a["method"] == REPORTED_METHOD if k == "reported" else
                          a["method"] != REPORTED_METHOD if k == "other" else k != NOSPLIT)
            self.c[k].add(v)
            self.m[k] += applicable and v is None

    def merge(self, other: Agg) -> None:
        self.total.merge(other.total)
        self.gap.merge(other.gap)
        self.rows += other.rows
        self.no_total += other.no_total
        self.partial += other.partial
        for k in COMPONENTS:
            self.c[k].merge(other.c[k])
            self.m[k] += other.m[k]

    def result(self) -> dict[str, Any]:
        return {"total": self.total.value(), "rows": self.rows, "noTotal": self.no_total,
                "partialRows": self.partial, "grossCheck": self.gap.value(), "grossCount": self.gap.n,
                "c": {k: s.value() for k, s in self.c.items()}, "m": dict(self.m)}


def buckets(start: date, end: date, gran: str) -> list[dict[str, Any]]:
    """Buckets covering [start, end]: days, weeks from Monday or calendar months, the edges clipped."""
    out, d = [], start
    while d <= end:
        if gran == "day":
            lo = hi = d
        elif gran == "week":
            lo = d - timedelta(days=d.weekday())
            hi = lo + timedelta(days=6)
        else:
            lo = d.replace(day=1)
            hi = date(lo.year + (lo.month == 12), lo.month % 12 + 1, 1) - timedelta(days=1)
        lo, hi = max(lo, start), min(hi, end)
        if gran == "day":
            key, label = lo.isoformat(), f"{lo:%d.%m.%Y}"
        elif gran == "week":
            y, w, _ = lo.isocalendar()
            key, label = f"{y}-W{w:02d}", f"{lo:%d.%m}–{hi:%d.%m.%Y}"
        else:
            key, label = f"{lo:%Y-%m}", f"{MONTHS[lo.month - 1]} {lo.year}"
        out.append({"key": key, "label": label, "from": lo, "to": hi})
        d = hi + timedelta(days=1)
    return out


def _date(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _kind_fact(r: dict[str, Any]) -> dict[str, Any]:
    """The fields ``summary.kind_of`` reads, from the projected row."""
    return {"instr_type": r.get("instrType"), "segment": r.get("segment"), "asset_class": r.get("assetClass"),
            "coupon_type": r.get("couponType"), "ccy": r.get("ccy"), "issuer": r.get("issuer"),
            "extra": {"reported": {"x_product_group": r.get("productGroup")}}}


def identity(r: dict[str, Any], gov: frozenset[str]) -> dict[str, Any]:
    group, kind = kind_of(_kind_fact(r), gov)
    return {"ref": f"{r['book']}|{r['positionId']}", "book": r["book"], "positionId": r["positionId"],
            "name": r["name"], "instrument": r["instrument"], "ccy": r["ccy"], "kind": kind, "group": group,
            "curve": r.get("curve"), "rating": r.get("rating"), "issuer": r.get("issuer")}


class Position:
    def __init__(self, n_buckets: int) -> None:
        self.agg = Agg()
        self.row: dict[str, Any] = {}
        self.first: date | None = None
        self.last: date | None = None
        self.pv = Sum()
        self.cal_days: int | None = None
        self.rate, self.weight = Sum(), Sum()
        self.buckets: dict[int, Sum] = {}
        self.n = n_buckets
        self.methods: dict[str, None] = {}
        self.no_pnl: dict[str, int] = {}

    def add(self, r: dict[str, Any], a: dict[str, Any], comp: dict[str, float | None], d: date, i: int) -> None:
        self.row = r  # latest metadata; identity always (book, position)
        self.first = self.first or d
        self.last = d
        self.agg.add(a, comp)
        if a["method"]:
            self.methods[a["method"]] = None
        if r.get("noPnl"):
            why = str((r["noPnl"] or {}).get("reason") or "")
            self.no_pnl[why] = self.no_pnl.get(why, 0) + 1
        self.pv.add(r.get("pv"))
        if a["total"] is not None:
            prev = _date(a["prevDate"]) or r.get("runPrevDate")
            if prev is not None:
                self.cal_days = (self.cal_days or 0) + (d - prev).days
        for x in r.get("fundingLines") or []:
            rate, body, days = _num(x.get("rate_pct")), _num(x.get("body_ccy")), _num(x.get("days"))
            if rate is not None and body is not None and days:
                self.rate.add(rate * abs(body) * days)
                self.weight.add(abs(body) * days)
        if a["total"] is not None:
            self.buckets.setdefault(i, Sum()).add(a["total"])

    def result(self, gov: frozenset[str], last_run: date | None) -> dict[str, Any]:
        w = self.weight.value()
        return {**identity(self.row, gov), "firstDate": self.first, "lastDate": self.last,
                "closed": last_run is not None and self.last is not None and self.last < last_run,
                "pvAvg": self.pv.value() / self.pv.n if self.pv.n else None, "calDays": self.cal_days,
                "fundingRatePct": self.rate.value() / w if w else None,
                "buckets": [self.buckets[i].value() if i in self.buckets else None for i in range(self.n)],
                "methods": sorted(self.methods), "noPnlReasons": dict(self.no_pnl), **self.agg.result()}


class Period:
    """One pass over the rows of a period: by day (run date), by position and bucket."""

    def __init__(self, start: date, end: date, gran: str) -> None:
        self.buckets = buckets(start, end, gran)
        self.edges = [b["from"] for b in self.buckets]
        self.days: dict[date, tuple[dict[str, Any], Agg, dict[str, Sum]]] = {}
        self.positions: dict[tuple[str, str], Position] = {}
        self.seen: set[str] = set()

    def add(self, r: dict[str, Any]) -> None:
        a, comp = components(r.get("pnl"), r)
        self.seen.update(k for k in EXTRA_KEYS if comp[k] is not None)
        d = r["date"]
        if d not in self.days:
            self.days[d] = ({"date": d, "prevDate": r.get("runPrevDate"), "runId": r["runId"]}, Agg(), {})
        _meta, agg, curves = self.days[d]
        agg.add(a, comp)
        curves.setdefault(r.get("curve") or "", Sum()).add(comp["rates"])
        key = (r["book"], r["positionId"])
        if key not in self.positions:
            self.positions[key] = Position(len(self.buckets))
        self.positions[key].add(r, a, comp, d, bisect.bisect_right(self.edges, d) - 1)

    def result(self, gov: frozenset[str], last_run: date | None) -> dict[str, Any]:
        total = Agg()
        for p in self.positions.values():
            total.merge(p.agg)
        positions = sorted((p.result(gov, last_run) for p in self.positions.values()),
                           key=lambda p: (p["book"], p["positionId"]))
        return {"keys": keys_of(self.seen), "buckets": self.buckets, "summary": total.result(),
                "days": [{**meta, "ratesByCurve": {c: s.value() for c, s in curves.items()}, **agg.result()}
                         for _d, (meta, agg, curves) in sorted(self.days.items())],
                "positions": positions}


def gov_issuers(con, ids: list[int]) -> frozenset[str]:
    """The issuers flagged ``is_gov`` in the period's issuer references (``summary.is_minfin``)."""
    return frozenset(r["issuer"] for r in con.execute(
        "SELECT DISTINCT e->>'issuer' AS issuer FROM run_reference x, jsonb_array_elements(x.data) e"
        " WHERE x.run_id = ANY(%s) AND x.kind = 'issuers' AND jsonb_typeof(x.data) = 'array'"
        " AND e->>'is_gov' = 'true' AND e->>'issuer' IS NOT NULL", (ids,)))


def curve_names(con, ids: list[int]) -> dict[str, str]:
    """Curve id -> name of the latest run's curve reference that has it."""
    return {r["id"]: r["name"] or r["id"] for r in con.execute(
        "SELECT DISTINCT ON (e->>'curve_id') e->>'curve_id' AS id, e->>'name' AS name"
        " FROM run_reference x, jsonb_array_elements(x.data) e WHERE x.run_id = ANY(%s) AND x.kind = 'curves'"
        " AND jsonb_typeof(x.data) = 'array' AND e->>'curve_id' IS NOT NULL ORDER BY e->>'curve_id', x.run_id DESC",
        (ids,))}


def cbr_meetings(con, start: date, end: date) -> list[dict[str, Any]]:
    """Key-rate decisions in force within the period (``md_cbr_meeting``), for the carry charts' marks."""
    return [{"date": r["meeting_date"], "effectiveDate": r["effective_date"], "decision": r["decision"],
             "ratePct": r["decision_rate_pct"], "prevRatePct": r["prev_rate_pct"], "estimated": r["is_estimated"]}
            for r in con.execute("SELECT * FROM md_cbr_meeting WHERE COALESCE(effective_date, meeting_date)"
                                 " BETWEEN %s AND %s ORDER BY meeting_date", (start, end))]


def _scan(con, scope: dict[str, Any], filters: dict[str, Any], position: tuple[str, str] | None, columns: str,
          name: str) -> Iterable[dict[str, Any]]:
    sql, args = rows_query(filters, position, columns)
    args["ids"] = scope["ids"]
    # Server cursor bounds backend memory, as pnl_history.report.
    with con.transaction(), con.cursor(name=name, row_factory=dict_row) as cursor:
        cursor.execute(sql + ORDER_BY, args)
        yield from cursor


def _last_run(scope: dict[str, Any], anchor: dict[str, Any]) -> date | None:
    dates = [r["business_date"] for r in scope["runs"] if not scope["separate"] or r["run_id"] != anchor["run_id"]]
    return max(dates) if dates else None


def period(con, anchor: dict[str, Any], start: date, end: date, gran: str, filters: dict[str, Any],
           catalogue: bool = True) -> dict[str, Any]:
    scope = period_runs(con, anchor, start, end)
    acc, apart = Period(start, end, gran), Period(anchor["business_date"], anchor["business_date"], "day")
    for r in _scan(con, scope, filters, None, PERIOD_COLUMNS, "pnl_period_rows"):
        (apart if scope["separate"] and r["runId"] == anchor["run_id"] else acc).add(r)
    gov = gov_issuers(con, scope["ids"])
    out = acc.result(gov, _last_run(scope, anchor))
    names = curve_names(con, scope["ids"])
    used = sorted({p["curve"] for p in out["positions"] if p["curve"]})
    body = {"from": start, "to": end, "gran": gran, "anchorRunId": anchor["run_id"], "currency": "RUB",
            "marketDates": market_dates(con, start, end), "runs": run_rows(scope, anchor),
            "intradayOnlyDates": scope["intradayOnly"], "keys": out.pop("keys"),
            "curves": [{"id": c, "name": names.get(c, c)} for c in used], **out,
            "cbrMeetings": cbr_meetings(con, start, end)}
    if catalogue:
        body["catalogue"] = catalogue_rows(con, scope["ids"])
    if scope["separate"]:
        sep = apart.result(gov, anchor["business_date"])
        body["separateAnchor"] = {"run": {"date": anchor["business_date"], "runId": anchor["run_id"],
                                          "prevDate": anchor["prev_date"], **run_slice(anchor)},
                                  "overlappingDates": scope["overlapping"], "summary": sep["summary"],
                                  "days": sep["days"], "positions": sep["positions"]}
    return body


def _points(con, run_id: int) -> dict[str, dict[str, dict[str, Any]]]:
    ref = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'curves'", (run_id,)).fetchone()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for c in (ref["data"] if ref and isinstance(ref["data"], list) else []):
        if isinstance(c, dict) and c.get("curve_id"):
            out[c["curve_id"]] = {p["bucket"]: p for p in c.get("points") or [] if isinstance(p, dict)
                                  and p.get("bucket")}
    return out


def curve_move(first: dict[str, dict[str, Any]], last: dict[str, dict[str, Any]], bucket: str) -> float | None:
    """bp between the first run's previous market day and the last run's: the last rate less the first's previous
    (its rate less its own move)."""
    a, b = first.get(bucket), last.get(bucket)
    if a is None or b is None or _num(a.get("rate")) is None or _num(b.get("rate")) is None \
            or _num(a.get("move_bp")) is None:
        return None
    return (b["rate"] - a["rate"]) * 100 + a["move_bp"]


def curves(con, anchor: dict[str, Any], start: date, end: date, filters: dict[str, Any]) -> dict[str, Any]:
    scope = period_runs(con, anchor, start, end)
    period_ids = {r["run_id"] for r in scope["runs"] if not scope["separate"] or r["run_id"] != anchor["run_id"]}
    dv01: dict[tuple[str, str], Sum] = {}
    linear: dict[tuple[str, str], Sum] = {}
    linear_total: dict[str, Sum] = {}
    exact: dict[str, Sum] = {}
    seen_curves: dict[str, None] = {}
    seen_buckets: dict[str, None] = {}
    runs_with_rows: set[int] = set()
    rows = linear_rows = bucket_rows = 0
    for r in _scan(con, scope, filters, None, CURVES_COLUMNS, "pnl_curves_rows"):
        if r["runId"] not in period_ids:
            continue
        rows += 1
        runs_with_rows.add(r["runId"])
        own = r.get("curve") or ""
        if own:
            seen_curves[own] = None
        exact.setdefault(own, Sum()).add(r["rates"])
        linear_total.setdefault(own, Sum()).add(r["ratesLinear"])
        linear_rows += r["ratesLinear"] is not None
        for c, bs in (r.get("risk") or {}).items():
            seen_curves[c] = None
            for b, v in (bs or {}).items():
                seen_buckets[b] = None
                dv01.setdefault((c, b), Sum()).add(_num(v))
        lc = r.get("linearCurves")
        if isinstance(lc, dict):
            bucket_rows += 1
            for c, bs in lc.items():
                seen_curves[c] = None
                for b, v in (bs or {}).items():
                    seen_buckets[b] = None
                    linear.setdefault((c, b), Sum()).add(_num(v))
    shown = [r for r in scope["runs"] if r["run_id"] in period_ids]
    first, last = (shown[0], shown[-1]) if shown else (None, None)
    p0, p1 = (_points(con, first["run_id"]), _points(con, last["run_id"])) if first and last else ({}, {})
    for c in seen_curves:  # a curve's move at every pillar it has, not only where the slice has risk
        seen_buckets.update(dict.fromkeys(p1.get(c, {})))
    order = [b for b in RISK_BUCKETS if b in seen_buckets] + [b for b in seen_buckets if b not in RISK_BUCKETS]
    names = curve_names(con, scope["ids"])
    ids = sorted(seen_curves)
    n = len(runs_with_rows)

    def grid(src: dict[tuple[str, str], Sum], scale: float = 1.0) -> dict[str, dict[str, float | None]]:
        return {c: {b: (v * scale if (v := src[(c, b)].value()) is not None else None) if (c, b) in src else None
                    for b in order} for c in ids}

    def run_ref(r: dict[str, Any] | None) -> dict[str, Any] | None:
        return {"date": r["business_date"], "runId": r["run_id"], "prevDate": r["prev_date"]} if r else None

    return {"from": start, "to": end, "firstRun": run_ref(first), "lastRun": run_ref(last), "buckets": order,
            "curves": [{"id": c, "name": names.get(c, c)} for c in ids],
            "move": {c: {b: curve_move(p0.get(c, {}), p1.get(c, {}), b) for b in order} for c in ids},
            "dv01": grid(dv01, 1 / n) if n else {c: dict.fromkeys(order) for c in ids},
            "linear": grid(linear),
            "linearTotal": {c: s.value() for c, s in linear_total.items()},
            "exact": {c: s.value() for c, s in exact.items()},
            "coverage": {"rows": rows, "linearRows": linear_rows, "bucketRows": bucket_rows}}


def _fmt(d: date | None) -> str:
    return f"{d:%d.%m.%Y}" if d else "—"


def position_days(rows: Iterable[dict[str, Any]], run_dates: list[date], gov: frozenset[str] = frozenset()
                  ) -> dict[str, Any]:
    """One position's rows, by run date, with the inputs of D and D-1 and its events; ``run_dates``: the period's
    runs in order (appearance, absence and the previous run's row)."""
    index = {d: i for i, d in enumerate(run_dates)}
    rows = list(rows)
    have = {r["date"] for r in rows}
    days, ident, prev_row = [], None, None
    for r in rows:
        d = r["date"]
        i = index.get(d)
        ident = identity(r, gov)
        a, comp = components(r.get("pnl"))
        extra = (r.get("pnl") or {}).get("extra") or {}
        prev_date = _date(a["prevDate"]) or r.get("runPrevDate")
        previous = prev_row if (prev_row is not None and i is not None and i > 0
                                and prev_row["date"] == run_dates[i - 1]) else None
        saved = extra.get("prev") if isinstance(extra.get("prev"), dict) else None
        if saved is not None:
            source, cp, yp, dp, qp = ("prev", _num(saved.get("clean_pct")), _num(saved.get("ytm_pct")),
                                      _num(saved.get("dv01_rub")), _num(saved.get("quantity")))
        elif previous is not None:
            same = previous["quantity"] == r["quantity"]
            source, qp = "previous_row", previous["quantity"]
            cp, yp, dp = ((previous["clean"], previous["ytm"], previous["dv01"]) if same else (None, None, None))
        else:
            source = cp = yp = dp = qp = None
        events = []
        if i is not None and i > 0 and run_dates[i - 1] not in have:
            events.append("позиция появилась" if not days else f"позиция снова в прогоне после {_fmt(run_dates[i - 1])}")
        if previous is not None and previous["quantity"] != r["quantity"]:
            events.append("количество изменилось")
        if r.get("noPnl"):
            events.append("нет PnL: " + str((r["noPnl"] or {}).get("reason") or "причина не указана"))
        for what, v in (("оферта", r.get("offerDate")), ("погашение", r.get("maturity"))):
            x = _date(v)
            if x is not None and prev_date is not None and prev_date < x <= d:
                events.append(f"{what} {_fmt(x)}")
        if i is not None and i + 1 < len(run_dates) and run_dates[i + 1] not in have:
            events.append(f"позиции нет в прогоне {_fmt(run_dates[i + 1])}")
        no_pnl = r.get("noPnl")
        days.append({"date": d, "prevDate": prev_date, "runId": r["runId"], "method": a["method"], "note": a["note"],
                     "noPnl": {"reason": no_pnl.get("reason"), "detail": no_pnl.get("detail")} if no_pnl else None,
                     "total": a["total"], "c": comp, "ratesLinear": _num(extra.get("rates_linear")),
                     "grossCheck": a["grossCheck"],
                     "inputs": {"source": source, "cleanPrev": cp, "clean": r.get("clean"), "ytmPrev": yp,
                                "ytm": r.get("ytm"), "zPrev": _num(extra.get("z0_bp")), "z": _num(extra.get("z1_bp")),
                                "dv01Prev": dp, "dv01": r.get("dv01"), "cs01": r.get("cs01"), "quantityPrev": qp,
                                "quantity": r.get("quantity"), "pv": r.get("pv")},
                     "events": events})
        prev_row = r
    return {"position": ident, "days": days}


def position(con, anchor: dict[str, Any], start: date, end: date, filters: dict[str, Any],
             book: str, position_id: str) -> dict[str, Any]:
    scope = period_runs(con, anchor, start, end)
    period_ids = {r["run_id"] for r in scope["runs"] if not scope["separate"] or r["run_id"] != anchor["run_id"]}
    rows = [r for r in _scan(con, scope, filters, (book, position_id), POSITION_COLUMNS, "pnl_position_rows")
            if r["runId"] in period_ids]
    return position_days(rows, [r["business_date"] for r in scope["runs"] if r["run_id"] in period_ids],
                         gov_issuers(con, scope["ids"]))
