"""Automatic checks of a period's saved PnL (TDM#407, docs/architecture/briefs/PNL_PERIOD.md ``/checks``).

Pure functions over the rows of one pass (``observe``): each returns ``ok``, ``findings`` or ``no_data`` with a
reason, never ``ok`` without inputs. Texts are shown on the page: Russian, short, without amounts (``value`` has
them). Runs and filters as ``pnl_history``.
"""
from __future__ import annotations

import statistics
from collections.abc import Iterable
from datetime import date
from typing import Any

from psycopg.rows import dict_row

from .pnl_history import amounts, market_dates, period_runs, rows_query
from .pnl_period import TRIMMED_PNL
from .runs import PNL_KEYS

THR_ABS = 3e6
THR_PCT = 0.25
RECON_RUB = 1000.0
LIN_ABS, LIN_PCT = 3e5, 0.05
FX_MOVE = 0.02
STALE_RUNS = 3
OUTLIER_MAD, OUTLIER_MIN_ROWS = 7.0, 5
EXPLAINED = tuple(k for k in PNL_KEYS if k != "other")
LABELS = {"recon": "Сверка тождества", "unexpl": "Необъяснённое", "stale": "Старая цена",
          "fsign": "Знак фондирования", "lin": "Ставки: линейно и точно", "fxrev": "Скачок курса",
          "outlier": "Выброс по позиции", "norun": "Нет прогона", "life": "Жизнь позиции"}
SEV = {"crit": 0, "warn": 1, "info": 2}

COLUMNS = TRIMMED_PNL + """, p.pv_rub AS pv, p.dv01_rub AS dv01, p.quantity, p.quality,
 (p.fact->>'price')::float8 AS clean"""

Obs = dict[str, Any]
Check = dict[str, Any]


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", "\u00a0")


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


def check_recon(obs: Iterable[Obs]) -> Check:
    """|grossCheck| > 1000 RUB: the saved total differs from the sum of its components."""
    out, n = [], 0
    for o in obs:
        g = o["a"]["grossCheck"]
        if g is None:
            continue
        n += 1
        if abs(g) > RECON_RUB:
            out.append(finding("crit", "recon", o, g, "итог метода не равен сумме компонент"))
    return result(out, n, f"порог {_rub(RECON_RUB)} RUB, строк: {n}",
                  "нет сохранённого итога метода для сверки")


def check_unexpl(obs: Iterable[Obs], thr_abs: float = THR_ABS, thr_pct: float = THR_PCT) -> Check:
    """|other| >= thr_abs and >= thr_pct of the sum of the explained components' moduli."""
    out, n = [], 0
    for o in obs:
        c = o["a"]["components"]
        v = c["other"]
        if v is None:
            continue
        n += 1
        explained = sum(abs(c[k]) for k in EXPLAINED if c[k] is not None)
        if abs(v) >= thr_abs and abs(v) >= thr_pct * explained:
            out.append(finding("warn", "unexpl", o, v, "крупное необъяснённое относительно объяснённого"))
    return result(out, n, f"порог {_rub(thr_abs)} RUB и {thr_pct:.0%} объяснённого",
                  "нет компоненты «прочее» (только PV по отчётам или нет PnL)")


def series(obs: Iterable[Obs]) -> dict[str, list[Obs]]:
    """Rows by position, in date order."""
    out: dict[str, list[Obs]] = {}
    for o in sorted(obs, key=lambda o: o["date"]):
        out.setdefault(o["ref"], []).append(o)
    return out


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


def check_stale(by_pos: dict[str, list[Obs]], run_ids: list[int]) -> Check:
    """The clean price unchanged for STALE_RUNS+ runs in a row with a non-zero DV01, or ``quality == stale_price``."""
    index = {r: i for i, r in enumerate(run_ids)}
    out, n = [], 0
    for rows in by_pos.values():
        n += sum(o["clean"] is not None or o["quality"] is not None for o in rows)
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
    return result(out, n, f"{STALE_RUNS}+ прогона подряд при ненулевом DV01 или качество stale_price",
                  "нет чистых цен и качества оценки")


def check_fsign(obs: Iterable[Obs]) -> Check:
    """Funding of the same sign as the PV: a long position earning its funding, a short one paying it."""
    out, n = [], 0
    for o in obs:
        f, pv = o["a"]["components"]["funding"], o["pv"]
        if f is None or pv is None or not f or not pv:
            continue
        n += 1
        if (f > 0) == (pv > 0):
            out.append(finding("crit", "fsign", o, f, "знак фондирования совпадает со знаком PV"))
    return result(out, n, f"строк с фондированием: {n}", "нет фондирования или PV")


def check_lin(obs: Iterable[Obs]) -> Check:
    """|rates - rates_linear| > max(LIN_ABS, LIN_PCT |rates_linear|)."""
    out, n = [], 0
    for o in obs:
        exact, lin = o["a"]["components"]["rates"], o["ratesLinear"]
        if exact is None or lin is None:
            continue
        n += 1
        gap = exact - lin
        if abs(gap) > max(LIN_ABS, LIN_PCT * abs(lin)):
            out.append(finding("warn", "lin", o, gap, "точная переоценка ставок далеко от линейной"))
    return result(out, n, f"допуск max({_rub(LIN_ABS)} RUB, {LIN_PCT:.0%})",
                  "нет линейной оценки ставок (rates_linear)")


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


def check_outlier(by_pos: dict[str, list[Obs]]) -> Check:
    """|total - median| > OUTLIER_MAD x MAD of the position's totals in the period."""
    out, n = [], 0
    for rows in by_pos.values():
        vals = [(o, o["a"]["total"]) for o in rows if o["a"]["total"] is not None]
        if len(vals) < OUTLIER_MIN_ROWS:
            continue
        n += 1
        med = statistics.median(v for _o, v in vals)
        mad = statistics.median(abs(v - med) for _o, v in vals)
        if not mad:
            continue
        for o, v in vals:
            if abs(v - med) > OUTLIER_MAD * mad:
                out.append(finding("info", "outlier", o, v, "итог дня далеко от обычного для позиции"))
    return result(out, n, f"{OUTLIER_MAD:g} MAD, позиций с {OUTLIER_MIN_ROWS}+ днями: {n}",
                  f"нет позиций с {OUTLIER_MIN_ROWS}+ днями PnL")


def check_norun(market: list[date], run_dates: list[date], intraday_only: Iterable[date] = ()) -> Check:
    """A market day without a run of the period."""
    have, intraday = set(run_dates), set(intraday_only)
    out = [finding("warn", "norun", None, None,
                   "только внутридневной прогон, закрытия нет" if d in intraday else "нет прогона на рыночный день",
                   day=d) for d in market if d not in have]
    return result(out, len(market), f"рыночных дней: {len(market)}", "нет календаря рыночных дней (КБД)")


def check_life(by_pos: dict[str, list[Obs]], run_ids: list[int], run_dates: list[date]) -> Check:
    """Appearance, closing, quantity change and ``no_pnl`` of each position."""
    index = {r: i for i, r in enumerate(run_ids)}
    out, n = [], 0
    for rows in by_pos.values():
        n += len(rows)
        for k, o in enumerate(rows):
            i = index.get(o["runId"])
            prev = rows[k - 1] if k else None
            if i is not None and i > 0 and (prev is None or index.get(prev["runId"]) != i - 1):
                out.append(finding("info", "life", o, None, "позиция появилась" if prev is None
                                   else f"позиция вернулась после пропуска {_fmt(run_dates[i - 1])}"))
            if i is not None and prev is not None and index.get(prev["runId"]) == i - 1 \
                    and prev["quantity"] != o["quantity"]:
                out.append(finding("info", "life", o, o["quantity"], "количество изменилось"))
            if o["noPnl"]:
                out.append(finding("info", "life", o, None,
                                   "нет PnL: " + str(o["noPnl"].get("reason") or "причина не указана")))
            nxt = rows[k + 1] if k + 1 < len(rows) else None
            if i is not None and i + 1 < len(run_ids) and (nxt is None or index.get(nxt["runId"]) != i + 1):
                out.append(finding("info", "life", o, None, f"позиции нет в прогоне {_fmt(run_dates[i + 1])}"
                                   if nxt is not None else f"позиция закрыта: нет с {_fmt(run_dates[i + 1])}",
                                   day_to=run_dates[i + 1]))
    return result(out, n, f"строк: {n}", "нет строк позиций")


def unexplained(obs: Iterable[Obs]) -> dict[str, Any]:
    """Heat map of ``other``: dates x positions, cells only where it is saved."""
    obs = [o for o in obs if o["a"]["components"]["other"] is not None]
    dates = sorted({o["date"] for o in obs})
    refs = sorted({o["ref"] for o in obs})
    di, ri = {d: i for i, d in enumerate(dates)}, {r: i for i, r in enumerate(refs)}
    return {"dates": dates, "refs": refs,
            "cells": [[di[o["date"]], ri[o["ref"]], o["a"]["components"]["other"]] for o in obs]}


def evaluate(obs: list[Obs], run_ids: list[int], run_dates: list[date], market: list[date],
             fx: list[tuple[date, dict[str, float]]], intraday_only: Iterable[date] = (),
             thr_abs: float = THR_ABS, thr_pct: float = THR_PCT, all_dates: list[date] | None = None
             ) -> dict[str, Any]:
    """Every check over the period's rows; ``run_ids``/``run_dates``: its runs in order; ``all_dates``: the run
    dates of the coverage, a separate anchor's included (default ``run_dates``)."""
    covered = run_dates if all_dates is None else all_dates
    by_pos = series(obs)
    checks = {"recon": check_recon(obs), "unexpl": check_unexpl(obs, thr_abs, thr_pct),
              "stale": check_stale(by_pos, run_ids), "fsign": check_fsign(obs), "lin": check_lin(obs),
              "fxrev": check_fxrev(fx, (o["ccy"] for o in obs)), "outlier": check_outlier(by_pos),
              "norun": check_norun(market, covered, intraday_only), "life": check_life(by_pos, run_ids, run_dates)}
    findings = sorted((f for c in checks.values() for f in c["findings"]),
                      key=lambda f: (SEV[f["sev"]], f["date"] or date.min, f["ref"] or "", f["check"]))
    have = set(covered)
    return {"checks": [{"key": k, "label": LABELS[k], "status": c["status"], "count": len(c["findings"]),
                        "note": c["note"]} for k, c in checks.items()],
            "findings": findings,
            "coverage": {"marketDates": market, "runDates": covered,
                         "missingRuns": [d for d in market if d not in have]},
            "unexplained": unexplained(obs)}


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
    with con.transaction(), con.cursor(name="pnl_checks_rows", row_factory=dict_row) as cursor:
        cursor.execute(sql + ' ORDER BY d.date, d.book, d."positionId"', args)
        obs = [observe(r) for r in cursor if r["runId"] in ids]
    return {"from": start, "to": end, "anchorRunId": anchor["run_id"], "thrAbs": thr_abs, "thrPct": thr_pct,
            **evaluate(obs, [r["run_id"] for r in runs], [r["business_date"] for r in runs],
                       market_dates(con, start, end), fx_rates(con, runs), scope["intradayOnly"], thr_abs, thr_pct,
                       [r["business_date"] for r in scope["runs"]])}
