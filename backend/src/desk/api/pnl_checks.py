"""Automatic checks of a period's saved PnL (TDM#407, docs/architecture/briefs/PNL_PERIOD.md ``/checks``).

Pure functions over the rows of one position (``observe``), fed one position at a time (``Scan``): the route reads
the period ordered by position, so its memory is one position's rows, the findings and the run-level data, not the
period's rows (a year x thousands of positions). Each check returns ``ok``, ``findings`` or ``no_data`` with a reason,
never ``ok`` without inputs. Texts are shown on the page: Russian, short, without amounts (``value`` has them). Runs
and filters as ``pnl_history``.
"""
from __future__ import annotations

import heapq
import math
import statistics
from collections.abc import Iterable
from datetime import date
from itertools import groupby
from typing import Any

from psycopg.rows import dict_row

from ..pnl.reasons import CORE_FX_METHOD, FX_METHOD, REPORTED_METHOD, no_pnl_text
from ..publish.summary import FX_PARTS, fx_net
from .pnl_history import Sum, amounts, market_dates, period_runs, rows_query
from .pnl_period import TRIMMED_PNL
from .runs import PNL_KEYS

THR_ABS = 3e6
THR_PCT = 0.25
RECON_RUB = 1000.0
LIN_ABS, LIN_PCT = 3e5, 0.05
FX_MOVE = 0.02
STALE_RUNS = 3
OUTLIER_MAD, OUTLIER_MIN_ROWS = 7.0, 5
EXPLAINED = tuple(k for k in PNL_KEYS if k != "other" and k not in FX_PARTS)
"""The explained components taken one by one; FX enters once, net (``fx_net``, TDM#414): the asset's FX and the FX of
its currency funding offset each other, their moduli apart would hide a large unexplained."""
HEAT_REFS = 60
"""``unexplained`` keeps this many positions: the largest by max |other| over the period (``refsTotal``: before)."""
PER_POSITION = ("recon", "unexpl", "stale", "fsign", "lin", "outlier", "life")
LABELS = {"recon": "Сверка тождества", "unexpl": "Необъяснённое", "stale": "Старая цена",
          "fsign": "Знак фондирования", "lin": "Ставки: линейно и точно", "fxrev": "Скачок курса",
          "outlier": "Выброс по позиции", "norun": "Нет прогона", "life": "Жизнь позиции"}
SEV = {"crit": 0, "warn": 1, "info": 2}
UNEXPL_GROUPS = {"exact": "остаток точного flash", "simple": "без разложения: нет потоков / модельная цена",
                 "yield": "по доходности", "reported": "по отчёту", "fx": "FX-сделки: прочие эффекты",
                 "other": "другой метод или не указан"}
"""``unexplainedBy`` (TDM#436): what ``other`` (and ``reported``) is, by the calculation method's group, in this order."""
METHOD_GROUP = {"desk-flash-exact-v1": "exact", "desk-flash-exact-v2": "exact", "desk-flash-exact-fx-v1": "exact",
                "desk-flash-simple-v1": "simple", "desk-flash-yield-v1": "yield", REPORTED_METHOD: "reported",
                FX_METHOD: "fx", CORE_FX_METHOD: "fx"}
"""``pnl.extra.method`` -> group: the exact flash's rest (a changed schedule, a price and accrued that disagree), the
simple PnL of a bond not exact on both dates (all of q x Δdirty, ``valuation.fallback.simple_pnl``), the yield carry's
yield change, the reports' PV difference, an FX deal's other effects; any other code (or none) — ``other``."""
QUALITY_TEXT = {"exact": "точная", "assumed": "с допущениями", "market_value": "рыночная цена",
                "stale_price": "старая цена", "model_price": "модельная цена", "par_proxy": "по номиналу",
                "reported_value": "по отчёту"}
"""``run_position.quality`` of D (``valuation.fallback``); another code is shown as is, none as «не указано»."""
UNEXPL_TOP = 30
"""``unexplainedBy.top`` keeps this many positions, the largest by |Σ other + reported| (``topTotal``: before)."""
UNEXPL_KEYS_CAP = 40
"""Distinct method and quality codes kept; more (a corrupt store) are pooled under ``…``."""

COLUMNS = TRIMMED_PNL + """, p.pv_rub AS pv, p.dv01_rub AS dv01, p.quantity, p.quality,
 (p.fact->>'price')::float8 AS clean"""

Obs = dict[str, Any]
Check = dict[str, Any]
Part = tuple[list[dict[str, Any]], int]
"""Findings of one position for a check and the number of its rows (positions for ``outlier``) checked."""


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _fmt(d: date | None) -> str:
    return f"{d:%d.%m.%Y}" if d else "—"


def observe(r: dict[str, Any]) -> Obs:
    """A projected row with its ``amounts``."""
    a = amounts(r.get("pnl"))
    extra = (r.get("pnl") or {}).get("extra") or {}
    return {"date": r["date"], "runId": r["runId"], "ref": f"{r['book']}|{r['positionId']}", "book": r["book"],
            "positionId": r["positionId"], "name": r.get("name"), "ccy": r.get("ccy"), "a": a,
            "ratesLinear": _num(extra.get("rates_linear")), "clean": r.get("clean"), "quality": r.get("quality"),
            "dv01": r.get("dv01"), "pv": r.get("pv"), "quantity": r.get("quantity"), "noPnl": r.get("noPnl")}


def unexplained_part(o: Obs) -> float | None:
    """``other`` + ``reported`` of a row (a row has at most one of them by method); null when it has neither."""
    c = o["a"]["components"]
    vals = [v for v in (c.get("other"), c.get("reported")) if v is not None]
    return math.fsum(vals) if vals else None


class Unexplained:
    """``unexplainedBy`` (TDM#436): Σ ``other`` and ``reported`` by method group, method, valuation quality and date, and
    the UNEXPL_TOP positions with the largest |Σ|; fed one position at a time (``Scan``). Kept: sums by group x date
    (dates of the period), by method x quality (capped codes) and the top heap."""

    def __init__(self) -> None:
        self.total, self.other, self.reported = Sum(), Sum(), Sum()
        self.rows = 0
        self.groups: dict[str, list[Any]] = {}  # group -> [Sum total, Sum other, Sum reported, rows]
        self.methods: dict[str, dict[str, Any]] = {}  # method code ("" none) -> {group, total, rows, quality}
        self.quality: dict[str, list[Any]] = {}  # quality code ("" none) -> [Sum, rows]
        self.days: dict[date, dict[str, Sum]] = {}
        self.top: list[tuple[float, int, dict[str, Any]]] = []
        self.top_total = 0

    @staticmethod
    def _key(seen: dict[str, Any], code: str) -> str:
        return code if code in seen or len(seen) < UNEXPL_KEYS_CAP else "…"

    def add(self, rows: list[Obs]) -> None:
        pos_total, pos_other, pos_reported, n = Sum(), Sum(), Sum(), 0
        by_group: dict[str, Sum] = {}
        methods: dict[str, None] = {}
        quality: dict[str, None] = {}
        notes: dict[str, None] = {}
        no_pnl: dict[str, int] = {}
        for o in rows:
            if o["noPnl"]:
                reason = str(o["noPnl"].get("reason") or "not_computed")
                no_pnl[reason] = no_pnl.get(reason, 0) + 1
            v = unexplained_part(o)
            if v is None:
                continue
            c, a = o["a"]["components"], o["a"]
            method = a["method"] or ""
            group = METHOD_GROUP.get(method, "other")
            q = o["quality"] or ""
            n += 1
            for s, x in ((self.total, v), (self.other, c.get("other")), (self.reported, c.get("reported")),
                         (pos_total, v), (pos_other, c.get("other")), (pos_reported, c.get("reported"))):
                s.add(x)
            g = self.groups.setdefault(group, [Sum(), Sum(), Sum(), 0])
            g[0].add(v)
            g[1].add(c.get("other"))
            g[2].add(c.get("reported"))
            g[3] += 1
            m = self.methods.setdefault(self._key(self.methods, method), {"group": group, "total": Sum(), "rows": 0,
                                                                          "quality": {}})
            m["total"].add(v)
            m["rows"] += 1
            mq = m["quality"].setdefault(self._key(m["quality"], q), Sum())
            mq.add(v)
            qq = self.quality.setdefault(self._key(self.quality, q), [Sum(), 0])
            qq[0].add(v)
            qq[1] += 1
            self.days.setdefault(o["date"], {}).setdefault(group, Sum()).add(v)
            by_group.setdefault(group, Sum()).add(v)
            if len(methods) < 8:
                methods[method] = None
            if len(quality) < 8:
                quality[q] = None
            if a["note"] and len(notes) < 3:
                notes[str(a["note"])[:200]] = None
        self.rows += n
        if not n:
            return
        self.top_total += 1
        total = pos_total.value() or 0.0
        main = max(by_group, key=lambda k: abs(by_group[k].value() or 0.0))
        first = rows[0]
        item = {"ref": first["ref"], "book": first["book"], "positionId": first["positionId"], "name": first["name"],
                "total": total, "other": pos_other.value(), "reported": pos_reported.value(), "rows": n,
                "group": main, "groupLabel": UNEXPL_GROUPS[main], "methods": [m or None for m in methods],
                "quality": [q or None for q in quality], "notes": list(notes),
                "noPnl": [{"reason": k, "text": no_pnl_text(k), "days": d} for k, d in list(no_pnl.items())[:5]]}
        entry = (abs(total), -self.top_total, item)
        if len(self.top) < UNEXPL_TOP:
            heapq.heappush(self.top, entry)
        else:
            heapq.heappushpop(self.top, entry)

    def result(self) -> dict[str, Any]:
        dates = sorted(self.days)
        groups = [k for k in UNEXPL_GROUPS if k in self.groups]
        return {
            "total": self.total.value(), "other": self.other.value(), "reported": self.reported.value(),
            "rows": self.rows,
            "groups": [{"key": k, "label": UNEXPL_GROUPS[k], "total": self.groups[k][0].value(),
                        "other": self.groups[k][1].value(), "reported": self.groups[k][2].value(),
                        "rows": self.groups[k][3]} for k in groups],
            "methods": sorted(({"method": m or None, "group": x["group"], "total": x["total"].value(),
                                "rows": x["rows"], "quality": {q or "": s.value() for q, s in x["quality"].items()}}
                               for m, x in self.methods.items()), key=lambda x: -abs(x["total"] or 0.0)),
            "quality": sorted(({"quality": q or None, "label": QUALITY_TEXT.get(q, q or "не указано"),
                                "total": s.value(), "rows": k} for q, (s, k) in self.quality.items()),
                              key=lambda x: -abs(x["total"] or 0.0)),
            "dates": dates,
            "byDate": {k: [self.days[d][k].value() if k in self.days[d] else None for d in dates] for k in groups},
            "top": [h[2] for h in sorted(self.top, key=lambda h: (-h[0], -h[1]))],
            "topTotal": self.top_total,
        }


def finding(sev: str, check: str, o: Obs | None, value: float | None, text: str, *, day: date | None = None,
            day_to: date | None = None, name: str | None = None) -> dict[str, Any]:
    return {"sev": sev, "check": check, "date": day or (o["date"] if o else None), "dateTo": day_to,
            "ref": o["ref"] if o else None, "book": o["book"] if o else None,
            "positionId": o["positionId"] if o else None, "name": name or (o["name"] if o else None),
            "value": value, "text": text}


def result(findings: list[dict[str, Any]], checked: int, ok_note: str, no_data: str) -> Check:
    if not checked:
        return {"status": "no_data", "findings": [], "note": no_data}
    if findings:
        return {"status": "findings", "findings": findings, "note": f"находок: {len(findings)}; {ok_note}"}
    return {"status": "ok", "findings": [], "note": ok_note}


def _notes(key: str, n: int, thr_abs: float = THR_ABS, thr_pct: float = THR_PCT) -> tuple[str, str]:
    """``ok`` note and ``no_data`` reason of a per-position check."""
    return {
        "recon": (f"порог {_rub(RECON_RUB)} RUB, строк: {n}", "нет сохранённого итога метода для сверки"),
        "unexpl": (f"порог {_rub(thr_abs)} RUB и {thr_pct:.0%} объяснённого",
                   "нет компоненты «прочее» (только PV по отчётам или нет PnL)"),
        "stale": (f"{STALE_RUNS}+ прогона подряд при ненулевом DV01 или качество stale_price",
                  "нет чистых цен и качества оценки"),
        "fsign": (f"строк с фондированием: {n}", "нет фондирования или PV"),
        "lin": (f"допуск max({_rub(LIN_ABS)} RUB, {LIN_PCT:.0%})", "нет линейной оценки ставок (rates_linear)"),
        "outlier": (f"{OUTLIER_MAD:g} MAD, позиций с {OUTLIER_MIN_ROWS}+ днями: {n}",
                    f"нет позиций с {OUTLIER_MIN_ROWS}+ днями PnL"),
        "life": (f"строк: {n}", "нет строк позиций"),
    }[key]


def _recon(rows: list[Obs]) -> Part:
    out, n = [], 0
    for o in rows:
        g = o["a"]["grossCheck"]
        if g is None:
            continue
        n += 1
        if abs(g) > RECON_RUB:
            out.append(finding("crit", "recon", o, g, "итог метода не равен сумме компонент"))
    return out, n


def explained(c: dict[str, float | None]) -> float:
    """Sum of the explained components' moduli, FX once and net (``EXPLAINED``)."""
    return math.fsum(abs(v) for k in EXPLAINED if (v := c.get(k)) is not None) + abs(fx_net(c) or 0.0)


def _unexpl(rows: list[Obs], thr_abs: float, thr_pct: float) -> Part:
    out, n = [], 0
    for o in rows:
        c = o["a"]["components"]
        v = c["other"]
        if v is None:
            continue
        n += 1
        if abs(v) >= thr_abs and abs(v) >= thr_pct * explained(c):
            out.append(finding("warn", "unexpl", o, v, "крупное необъяснённое относительно объяснённого"))
    return out, n


def _runs(rows: list[Obs], index: dict[int, int]) -> list[list[Obs]]:
    """Rows split where the position misses a run of the period."""
    out: list[list[Obs]] = []
    for o in rows:
        if out and o["runId"] in index and out[-1][-1]["runId"] in index \
                and index[o["runId"]] == index[out[-1][-1]["runId"]] + 1:
            out[-1].append(o)
        else:
            out.append([o])
    return out


def _stale(rows: list[Obs], index: dict[int, int]) -> Part:
    out = []
    n = sum(o["clean"] is not None or o["quality"] is not None for o in rows)
    for chunk in _runs(rows, index):
        streak: list[Obs] = []
        for o in [*chunk, None]:
            if o is not None and o["clean"] is not None and o["dv01"] and streak \
                    and streak[-1]["clean"] == o["clean"]:
                streak.append(o)
                continue
            if len(streak) >= STALE_RUNS:
                out.append(finding("warn", "stale", streak[0], streak[0]["clean"],
                                   f"чистая цена не менялась, прогонов подряд: {len(streak)}",
                                   day_to=streak[-1]["date"]))
            streak = [o] if o is not None and o["clean"] is not None and o["dv01"] else []
        flagged = [o for o in chunk if o["quality"] == "stale_price"]
        if flagged:
            out.append(finding("warn", "stale", flagged[0], None,
                               f"нет котировки дня, оценка по старой цене; прогонов: {len(flagged)}",
                               day_to=flagged[-1]["date"] if len(flagged) > 1 else None))
    return out, n


def _fsign(rows: list[Obs]) -> Part:
    out, n = [], 0
    for o in rows:
        f, pv = o["a"]["components"]["funding"], o["pv"]
        if f is None or pv is None or not f or not pv:
            continue
        n += 1
        if (f > 0) == (pv > 0):
            out.append(finding("crit", "fsign", o, f, "знак фондирования совпадает со знаком PV"))
    return out, n


def _lin(rows: list[Obs]) -> Part:
    out, n = [], 0
    for o in rows:
        exact, lin = o["a"]["components"]["rates"], o["ratesLinear"]
        if exact is None or lin is None:
            continue
        n += 1
        gap = exact - lin
        if abs(gap) > max(LIN_ABS, LIN_PCT * abs(lin)):
            out.append(finding("warn", "lin", o, gap, "точная переоценка ставок далеко от линейной"))
    return out, n


def _outlier(rows: list[Obs]) -> Part:
    vals = [(o, o["a"]["total"]) for o in rows if o["a"]["total"] is not None]
    if len(vals) < OUTLIER_MIN_ROWS:
        return [], 0
    med = statistics.median(v for _o, v in vals)
    mad = statistics.median(abs(v - med) for _o, v in vals)
    if not mad:
        return [], 1
    return [finding("info", "outlier", o, v, "итог дня далеко от обычного для позиции")
            for o, v in vals if abs(v - med) > OUTLIER_MAD * mad], 1


def _life(rows: list[Obs], index: dict[int, int], run_dates: list[date]) -> Part:
    """Appearance, return after a gap, closing; a quantity change and ``no_pnl`` are folded into one finding per run
    of consecutive rows with the same text (``date``..``dateTo``, the number of days in the text)."""
    out: list[dict[str, Any]] = []
    folds: dict[str, list[Any]] = {}  # kind -> [text, first row, last row, days, value]

    def close(kind: str) -> None:
        if (f := folds.pop(kind, None)) is not None:
            text, first, last, days, value = f
            out.append(finding("info", "life", first, value, text if days == 1 else f"{text}; дней подряд: {days}",
                               day_to=last["date"] if days > 1 else None))

    def mark(kind: str, text: str, o: Obs, value: float | None, adjacent: bool) -> None:
        f = folds.get(kind)
        if f is not None and adjacent and f[0] == text:
            f[2], f[3], f[4] = o, f[3] + 1, value
        else:
            close(kind)
            folds[kind] = [text, o, o, 1, value]

    for k, o in enumerate(rows):
        i = index.get(o["runId"])
        prev = rows[k - 1] if k else None
        adjacent = i is not None and prev is not None and index.get(prev["runId"]) == i - 1
        if i is not None and i > 0 and not adjacent:
            out.append(finding("info", "life", o, None, "позиция появилась" if prev is None
                               else f"позиция вернулась после пропуска {_fmt(run_dates[i - 1])}"))
        if adjacent and prev is not None and prev["quantity"] != o["quantity"]:
            mark("qty", "количество изменилось", o, o["quantity"], adjacent)
        else:
            close("qty")
        if o["noPnl"]:
            mark("nopnl", "нет PnL: " + str(o["noPnl"].get("reason") or "причина не указана"), o, None, adjacent)
        else:
            close("nopnl")
        nxt = rows[k + 1] if k + 1 < len(rows) else None
        if i is not None and i + 1 < len(run_dates) and (nxt is None or index.get(nxt["runId"]) != i + 1):
            out.append(finding("info", "life", o, None, f"позиции нет в прогоне {_fmt(run_dates[i + 1])}"
                               if nxt is not None else f"позиция закрыта: нет с {_fmt(run_dates[i + 1])}",
                               day_to=run_dates[i + 1]))
    close("qty")
    close("nopnl")
    return out, len(rows)


def series(obs: Iterable[Obs]) -> dict[str, list[Obs]]:
    """Rows by position, in date order."""
    out: dict[str, list[Obs]] = {}
    for o in sorted(obs, key=lambda o: o["date"]):
        out.setdefault(o["ref"], []).append(o)
    return out


def _check(key: str, parts: Iterable[Part], thr_abs: float = THR_ABS, thr_pct: float = THR_PCT) -> Check:
    found, n = [], 0
    for out, k in parts:
        found.extend(out)
        n += k
    return result(found, n, *_notes(key, n, thr_abs, thr_pct))


def check_recon(obs: Iterable[Obs]) -> Check:
    """|grossCheck| > 1000 RUB: the saved total differs from the sum of its components."""
    return _check("recon", [_recon(list(obs))])


def check_unexpl(obs: Iterable[Obs], thr_abs: float = THR_ABS, thr_pct: float = THR_PCT) -> Check:
    """|other| >= thr_abs and >= thr_pct of the sum of the explained components' moduli, FX net (``EXPLAINED``)."""
    return _check("unexpl", [_unexpl(list(obs), thr_abs, thr_pct)], thr_abs, thr_pct)


def check_stale(by_pos: dict[str, list[Obs]], run_ids: list[int]) -> Check:
    """The clean price unchanged for STALE_RUNS+ runs in a row with a non-zero DV01, or ``quality == stale_price``."""
    index = {r: i for i, r in enumerate(run_ids)}
    return _check("stale", (_stale(rows, index) for rows in by_pos.values()))


def check_fsign(obs: Iterable[Obs]) -> Check:
    """Funding of the same sign as the PV: a long position earning its funding, a short one paying it."""
    return _check("fsign", [_fsign(list(obs))])


def check_lin(obs: Iterable[Obs]) -> Check:
    """|rates - rates_linear| > max(LIN_ABS, LIN_PCT |rates_linear|)."""
    return _check("lin", [_lin(list(obs))])


def check_outlier(by_pos: dict[str, list[Obs]]) -> Check:
    """|total - median| > OUTLIER_MAD x MAD of the position's totals in the period."""
    return _check("outlier", (_outlier(rows) for rows in by_pos.values()))


def check_life(by_pos: dict[str, list[Obs]], run_ids: list[int], run_dates: list[date]) -> Check:
    """Appearance, closing, quantity change and ``no_pnl`` of each position (``_life``)."""
    index = {r: i for i, r in enumerate(run_ids)}
    return _check("life", (_life(rows, index, run_dates) for rows in by_pos.values()))


def check_fxrev(fx: list[tuple[date, dict[str, float]]], ccys: Iterable[str]) -> Check:
    """A positions' currency rate moving > FX_MOVE and back > FX_MOVE on the next run. ``fx``: the period's runs in
    order with their rates (RUB per unit)."""
    wanted = sorted({c for c in ccys if c and c != "RUB"})
    out, n = [], 0
    for c in wanted:
        pts = [(d, rates.get(c)) for d, rates in fx]
        n += sum(v is not None for _d, v in pts) >= 3
        for (_d0, r0), (d1, r1), (d2, r2) in zip(pts, pts[1:], pts[2:], strict=False):
            if not r0 or not r1 or r2 is None:
                continue
            jump, back = r1 / r0 - 1, r2 / r1 - 1
            if abs(jump) > FX_MOVE and abs(back) > FX_MOVE and (jump > 0) != (back > 0):
                out.append(finding("warn", "fxrev", None, jump, f"курс {c} сдвинулся и вернулся на следующий прогон",
                                   day=d1, day_to=d2, name=c))
    return result(out, n, f"порог {FX_MOVE:.0%} туда и обратно; валюты: {', '.join(wanted)}",
                  "нет валютных позиций или меньше трёх прогонов с курсом")


def check_norun(market: list[date], run_dates: list[date], intraday_only: Iterable[date] = ()) -> Check:
    """A market day without a run of the period."""
    have, intraday = set(run_dates), set(intraday_only)
    out = [finding("warn", "norun", None, None,
                   "только внутридневной прогон, закрытия нет" if d in intraday else "нет прогона на рыночный день",
                   day=d) for d in market if d not in have]
    return result(out, len(market), f"рыночных дней: {len(market)}", "нет календаря рыночных дней (КБД)")


class Scan:
    """Every check over a period, fed one position's rows at a time (``add``, rows in date order). Kept between
    positions: the per-position checks' findings and counts, the positions' currencies (the FX reversal works on the
    runs' rates, not on rows) and the heat map's HEAT_REFS largest positions."""

    def __init__(self, run_ids: list[int], run_dates: list[date], thr_abs: float = THR_ABS,
                 thr_pct: float = THR_PCT) -> None:
        self.index = {r: i for i, r in enumerate(run_ids)}
        self.run_dates = run_dates
        self.thr = (thr_abs, thr_pct)
        self.found: dict[str, list[dict[str, Any]]] = {k: [] for k in PER_POSITION}
        self.n = dict.fromkeys(PER_POSITION, 0)
        self.ccys: set[str] = set()
        self.heat: list[tuple[float, int, str, list[tuple[date, float]]]] = []  # min-heap by max |other|
        self.refs_total = 0
        self.by = Unexplained()

    def add(self, rows: list[Obs]) -> None:
        if not rows:
            return
        thr_abs, thr_pct = self.thr
        for key, part in (("recon", _recon(rows)), ("unexpl", _unexpl(rows, thr_abs, thr_pct)),
                          ("stale", _stale(rows, self.index)), ("fsign", _fsign(rows)), ("lin", _lin(rows)),
                          ("outlier", _outlier(rows)), ("life", _life(rows, self.index, self.run_dates))):
            self.found[key].extend(part[0])
            self.n[key] += part[1]
        self.ccys.update(o["ccy"] for o in rows if o["ccy"])
        self.by.add(rows)
        cells = [(o["date"], v) for o in rows if (v := o["a"]["components"]["other"]) is not None]
        if cells:
            self.refs_total += 1  # also the tie-break: of equal maxima, the position read first stays
            item = (max(abs(v) for _d, v in cells), -self.refs_total, rows[0]["ref"], cells)
            if len(self.heat) < HEAT_REFS:
                heapq.heappush(self.heat, item)
            else:
                heapq.heappushpop(self.heat, item)

    def unexplained(self) -> dict[str, Any]:
        """Heat map of ``other``: dates x the kept positions (by ref), cells only where it is saved."""
        kept = sorted(self.heat, key=lambda h: h[2])
        dates = sorted({d for h in kept for d, _v in h[3]})
        di = {d: i for i, d in enumerate(dates)}
        return {"dates": dates, "refs": [h[2] for h in kept], "refsTotal": self.refs_total,
                "cells": [[di[d], ri, v] for ri, h in enumerate(kept) for d, v in h[3]]}

    def result(self, market: list[date], fx: list[tuple[date, dict[str, float]]], intraday_only: Iterable[date] = (),
               all_dates: list[date] | None = None) -> dict[str, Any]:
        """``all_dates``: the run dates of the coverage, a separate anchor's included (default the scan's runs)."""
        covered = self.run_dates if all_dates is None else all_dates
        per = {k: _check(k, [(self.found[k], self.n[k])], *self.thr) for k in PER_POSITION}
        checks = {"recon": per["recon"], "unexpl": per["unexpl"], "stale": per["stale"], "fsign": per["fsign"],
                  "lin": per["lin"], "fxrev": check_fxrev(fx, self.ccys), "outlier": per["outlier"],
                  "norun": check_norun(market, covered, intraday_only), "life": per["life"]}
        findings = sorted((f for c in checks.values() for f in c["findings"]),
                          key=lambda f: (SEV[f["sev"]], f["date"] or date.min, f["ref"] or "", f["check"]))
        have = set(covered)
        return {"checks": [{"key": k, "label": LABELS[k], "status": c["status"], "count": len(c["findings"]),
                            "note": c["note"]} for k, c in checks.items()],
                "findings": findings,
                "coverage": {"marketDates": market, "runDates": covered,
                             "missingRuns": [d for d in market if d not in have]},
                "unexplained": self.unexplained(), "unexplainedBy": self.by.result()}


def evaluate(obs: Iterable[Obs], run_ids: list[int], run_dates: list[date], market: list[date],
             fx: list[tuple[date, dict[str, float]]], intraday_only: Iterable[date] = (),
             thr_abs: float = THR_ABS, thr_pct: float = THR_PCT, all_dates: list[date] | None = None
             ) -> dict[str, Any]:
    """Every check over rows already in memory (``Scan`` by position); ``run_ids``/``run_dates``: the period's runs
    in order; ``all_dates`` as ``Scan.result``."""
    scan = Scan(run_ids, run_dates, thr_abs, thr_pct)
    for rows in series(obs).values():
        scan.add(rows)
    return scan.result(market, fx, intraday_only, all_dates)


def fx_rates(con, runs: list[dict[str, Any]]) -> list[tuple[date, dict[str, float]]]:
    """The market rate of each currency per run (``run_reference`` fx ``rate``), in run order."""
    refs = {r["run_id"]: r["data"] for r in con.execute(
        "SELECT run_id, data FROM run_reference WHERE run_id = ANY(%s) AND kind = 'fx'", ([r["run_id"] for r in runs],))}
    out = []
    for r in runs:
        data = refs.get(r["run_id"])
        out.append((r["business_date"], {x["ccy"]: v for x in (data if isinstance(data, list) else [])
                                         if isinstance(x, dict) and (v := _num(x.get("rate"))) is not None}))
    return out


def checks(con, anchor: dict[str, Any], start: date, end: date, filters: dict[str, Any],
           thr_abs: float = THR_ABS, thr_pct: float = THR_PCT) -> dict[str, Any]:
    scope = period_runs(con, anchor, start, end)
    runs = [r for r in scope["runs"] if not scope["separate"] or r["run_id"] != anchor["run_id"]]
    ids = {r["run_id"] for r in runs}
    sql, args = rows_query(filters, None, COLUMNS)
    args["ids"] = scope["ids"]
    scan = Scan([r["run_id"] for r in runs], [r["business_date"] for r in runs], thr_abs, thr_pct)
    with con.transaction(), con.cursor(name="pnl_checks_rows", row_factory=dict_row) as cursor:
        # By position, then date, on a server-side cursor: one position's rows in memory at a time.
        cursor.execute(sql + ' ORDER BY d.book, d."positionId", d.date, d."runId"', args)
        for _key, rows in groupby(cursor, key=lambda r: (r["book"], r["positionId"])):
            scan.add([observe(r) for r in rows if r["runId"] in ids])
    return {"from": start, "to": end, "anchorRunId": anchor["run_id"], "thrAbs": thr_abs, "thrPct": thr_pct,
            **scan.result(market_dates(con, start, end), fx_rates(con, runs), scope["intradayOnly"],
                          [r["business_date"] for r in scope["runs"]])}
