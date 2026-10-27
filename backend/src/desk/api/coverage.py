"""What a run did not value, by why and by instrument (R45): the «Чего нет в данных» panel of the concept pages
(``window.DESK_EXTRAS.unvalued`` and ``.approx``, ``lib/kit.js``).

``unvalued(con, run)``: the run's positions without a fact (``run_position.fact`` null) grouped by the cause (the
reason code of ``valuation.positions`` / ``orchestrator``, read with the instrument kind ``concepts.instr_kind`` of the
report row) and the kind, each group with its count, the face or amount in RUB where the run has the currency's rate
(``noRate``: the currencies it has none for), the report's MV where it gives one, what to do (``todo``) and the
position ids for the drill-down. A row whose every amount is zero (a closed trade, a zero quantity left in the
report) is not a gap in the book: it is counted apart (``closed``), never in ``open``.

``approx(con, run)``: the valued positions whose value is not core's from their terms (quality other than exact
and assumed, and an FX forward without its trade terms whatever its quality), by quality and kind, with their ids: the rows the risk views show with the report's or a proxy's
numbers (an FX forward without trade terms: the notional as FX delta)."""

from __future__ import annotations

import math
import os
from decimal import Decimal
from typing import Any

from . import concepts

AMOUNTS = ("amount_ccy", "face_amount_ccy", "face_outstanding_ccy")
SWAPS = ("IRS", "CCS", "CDS", "TRS")
REPORT_MV = "MV и DV01 отчёта — опция report_risk адаптера отчётов (options.json)"

CAUSES: dict[str, tuple[str, str]] = {
    "unmapped_instrument": ("бумага не найдена в статике хаба (ISIN / рег. номер / тикер неизвестны)",
                            ("проверьте ISIN в отчёте; погашенная бумага в статике после погашения не появится — "
                             "тогда это закрытая позиция")),
    "not_in_static_snapshot": ("бумаги нет в статике хаба на дату", "проверьте ISIN; погашена — закрытая позиция"),
    "no_quantity": ("облигация без количества бумаг и без MV в отчёте", "в отчёте нужны Quantity или номинал"),
    "no_fx_rate": ("нет курса валюты строки в срезе хаба", "курс ЦБ приходит со срезом хаба: desk sync за дату"),
    "fx_row_without_ccy_or_amount": ("FX-строка без валюты или суммы", "проверьте строку отчёта (CCY, Quantity)"),
    # a row with trade terms core does not value (valuation.derivatives, TDM#45) and no MV in the report
    "ccs_unsupported": (("валютно-процентный своп (CCS): условия сделки есть, но ядро в мониторе их пока не "
                        "оценивает; MV в отчёте нет"), "до оценки ядром — " + REPORT_MV),
    "basis_swap_unsupported": (("базисный своп (обе ноги плавающие): ядро в мониторе их пока не оценивает; MV в "
                               "отчёте нет"), "до оценки ядром — " + REPORT_MV),
    "repo_open": (("открытое РЕПО (без даты окончания): для оценки ядром нужна предполагаемая дата окончания, она не "
                  "задана; MV в отчёте нет"), "дата второй части в условиях сделки (maturity_date) — или " + REPORT_MV),
    "no_fixings": (("нет фиксингов индекса сделки в срезе хаба (или индекс без фиксингов в мониторе, напр. RUSFAR); "
                   "MV в отчёте нет"), "фиксинги приходят со срезом хаба: desk sync за дату — или " + REPORT_MV),
    "no_key_rate_curve": ("нет кривой ключевой ставки в срезе хаба за дату (котировки IRS KEYRATE); MV в отчёте нет",
                          "кривая строится из среза хаба: desk sync за дату — или " + REPORT_MV),
}
"""Reason code -> (why, what to do) of an unvalued row whatever its kind."""


def _f(v: Any) -> float | None:
    if isinstance(v, Decimal):
        v = float(v)
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def closed(quantity: Any, row: dict[str, Any]) -> bool:
    """Every amount of the row (the run's quantity, the report's amounts and MV) is zero or absent, one at least
    zero: a closed trade or a zero position left in the report, not a gap."""
    vals = [_f(quantity), *(_f(row.get(k)) for k in (*AMOUNTS, "mv_rub"))]
    known = [v for v in vals if v is not None]
    return bool(known) and all(abs(v) < 1e-9 for v in known)


def cause(code: str, kind: str, product: str | None, futures_by_ticker: bool | None) -> tuple[str, str, str]:
    """(group key, why, what to do) of an unvalued row: the reason code (before ``:``) read with its kind."""
    if code in CAUSES:
        return (code, *CAUSES[code])
    if code == "no_reported_value":
        if kind in SWAPS:
            return ("swap_terms", "своп ждёт условий сделки (trade_terms): ядро оценит его по ним; MV в отчёте нет",
                    "до условий сделки — " + REPORT_MV)
        if kind == "FX_FUT":
            on = {True: "включена", False: "выключена", None: "не прочитана"}[futures_by_ticker]
            return ("future_ticker", f"валютный фьючерс: оценка ядром по тикеру FORTS — настройка futures_by_ticker ({on})",
                    "включите futures_by_ticker (Настройки) — или " + REPORT_MV)
        if kind in ("BOND_FUT", "BOND_FWD"):
            return ("bond_derivative", "фьючерс / форвард на облигации: ядро их пока не оценивает, MV в отчёте нет",
                    REPORT_MV)
        if kind == "REPO" or (product and "repo" in product.lower()):
            return ("repo", ("РЕПО ждёт условий сделки (trade_terms): ядро оценит его по ним (PV, риск ставок); "
                    "MV в отчёте нет"), "до условий сделки — " + REPORT_MV)
        return ("no_report_mv", "оценки нет: MV в отчёте нет", REPORT_MV)
    return (code or "unknown", f"не оценено ({code or 'причина не записана'})", "журнал прогона: Диагностика")


def _rates(con, run_id: int) -> dict[str, float]:
    ref = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'fx'", (run_id,)).fetchone()
    out = {"RUB": 1.0}
    for r in (ref["data"] if ref else None) or []:
        if _f(r.get("rate")):
            out[r["ccy"]] = float(r["rate"])
    return out


def _futures_by_ticker() -> bool | None:
    from ..settings import SettingsError, load

    try:
        return bool(load(dict(os.environ)).futures_by_ticker)  # a copy: no proxy keys exported
    except (SettingsError, OSError):  # no settings (a read-only copy): unknown
        return None


def unvalued(con, run: dict[str, Any], futures_by_ticker: bool | None = None) -> dict[str, Any]:
    """The module doc's breakdown of the run's unvalued positions. ``futures_by_ticker``: the setting (None: read)."""
    rows = con.execute("SELECT position_id, book_id, quantity, reason FROM run_position WHERE run_id = %s"
                       " AND fact IS NULL ORDER BY ord", (run["run_id"],)).fetchall()
    if not rows:
        return {"total": 0, "open": 0, "closed": 0, "groups": [], "fxMissing": []}
    fbt = _futures_by_ticker() if futures_by_ticker is None else futures_by_ticker
    inputs = {i["position_id"]: i for i in con.execute(
        "SELECT position_id, asset_class, ccy, amount_ccy, face_amount_ccy, face_outstanding_ccy, mv_rub, x"
        " FROM position_input WHERE set_id = ANY(%s) AND position_id = ANY(%s)",
        (run["position_set_ids"], [r["position_id"] for r in rows]))}
    fx = _rates(con, run["run_id"])
    groups: dict[str, dict[str, Any]] = {}
    missing: set[str] = set()
    n_closed = 0
    for r in rows:
        i = inputs.get(r["position_id"]) or {}
        x = i.get("x") or {}
        kind = concepts.instr_kind({"ccy": i.get("ccy"), "asset_class": i.get("asset_class"),
                                    "extra": {"reported": x}})
        code = (r["reason"] or "").split(":", 1)[0].strip()
        product = x.get("x_product") if isinstance(x.get("x_product"), str) else None
        if closed(r["quantity"], i):
            key, why, todo = "closed", "закрытая сделка или нулевой объём в отчёте", "не пропуск: в счёт не входит"
            n_closed += 1
        else:
            key, why, todo = cause(code, kind, product, fbt)
        gk = f"{key}|{kind}"
        g = groups.setdefault(gk, {"key": gk, "cause": key, "kind": kind, "kindLabel": concepts.KIND_LABEL.get(kind, kind),
                                   "why": why, "todo": todo, "closed": key == "closed", "n": 0, "mvRub": None,
                                   "notionalRub": None, "noRate": [], "ids": [], "reasons": {}})
        g["n"] += 1
        g["ids"].append(r["position_id"])
        if code:
            g["reasons"][code] = g["reasons"].get(code, 0) + 1
        if (mv := _f(i.get("mv_rub"))) is not None:
            g["mvRub"] = (g["mvRub"] or 0.0) + mv
        amt = next((v for k in AMOUNTS if (v := _f(i.get(k))) is not None), None)
        ccy = (i.get("ccy") or "RUB").upper()
        if amt:
            if ccy in fx:
                g["notionalRub"] = (g["notionalRub"] or 0.0) + abs(amt) * fx[ccy]
            else:
                if ccy not in g["noRate"]:
                    g["noRate"].append(ccy)
                if key != "closed":
                    missing.add(ccy)
    out = sorted(groups.values(), key=lambda g: (g["closed"], -g["n"], g["key"]))
    total = len(rows)
    return {"total": total, "open": total - n_closed, "closed": n_closed, "groups": out,
            "fxMissing": sorted(missing)}


APPROX_TEXT = {"market_value": "по рыночной стоимости (без ядра)", "stale_price": "по старой цене (нет котировки дня)",
               "model_price": "по модельной цене", "par_proxy": "по номиналу, без риска",
               "reported_value": "по отчёту банка", "assumed": "PV принят 0 (MV в отчёте нет)"}


def approx(con, run: dict[str, Any]) -> list[dict[str, Any]]:
    """The module doc's valued-but-not-by-core positions: [{key, quality, kind, text, n, mvRub, ids}], largest
    first."""
    groups: dict[str, dict[str, Any]] = {}
    for r in con.execute("SELECT position_id, quality, pv_rub, fact FROM run_position WHERE run_id = %s"
                         " AND fact IS NOT NULL AND (quality <> ALL(%s)"
                         "  OR fact->'extra'->>'not_exact_reason' LIKE 'no_trade_terms%%') ORDER BY ord",
                         (run["run_id"], ["exact", "assumed"])):
        f = r["fact"]
        kind = concepts.instr_kind(f)  # the report's x_* are under extra.reported.x (concepts.product_group)
        nr = (f.get("extra") or {}).get("not_exact_reason")
        no_terms = isinstance(nr, str) and nr.startswith("no_trade_terms")
        key = f"{r['quality']}|{kind}|{int(no_terms)}"
        text = APPROX_TEXT.get(r["quality"], r["quality"]) + ("; условий сделки (trade_terms) нет: FX-дельта = номинал"
                                                              if no_terms else "")
        g = groups.setdefault(key, {"key": key, "quality": r["quality"], "kind": kind,
                                    "kindLabel": concepts.KIND_LABEL.get(kind, kind), "text": text, "n": 0,
                                    "mvRub": 0.0, "ids": []})
        g["n"] += 1
        g["mvRub"] += _f(r["pv_rub"]) or 0.0
        g["ids"].append(r["position_id"])
    return sorted(groups.values(), key=lambda g: (-g["n"], g["key"]))
