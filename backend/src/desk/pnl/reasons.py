"""Why a position or a run has no PnL, and how a PnL was computed: codes and the Russian text the Desk Monitor
shows (R10). A missing PnL is said, never shown as 0: the run carries ``pnlAvailable`` with a code, a position
without PnL its ``no_pnl`` code (``NoPnl.reason``), and both get a text from here."""

from __future__ import annotations

import re
from typing import Any

RUN_NO_PREV_MARKET = "no_prev_market"
"""The run has no previous market (no ``eod_final`` snapshot before its date): no flash PnL at all."""
RUN_NO_POSITION_PNL = "no_position_pnl"
"""A previous market, but no position got a PnL (``desk eod --no-pnl``, or every position failed)."""

NO_PNL_TEXT = {
    RUN_NO_PREV_MARKET: "нет финального среза рынка за предыдущий рабочий день",
    RUN_NO_POSITION_PNL: "PnL не посчитан ни по одной позиции",
    "not_computed": "PnL не считался",
    "not_valued": "позиция не оценена",
    "valued_without_core": "бумага оценена по отчёту, без ядра",
    "not_priced_prev": "не оценена за предыдущий день",
    "par_proxy": "оценка по номиналу, нет цены",
    "quantity_changed": "количество изменилось: сделки дня не учитываются",
    "spread_kind_changed": "сменилась кривая спреда",
    "stale_quote": "котировка не обновилась с предыдущего дня",
    "core_error": "ошибка ядра при пересчёте",
    "identity_failed": "компоненты не сходятся с итогом",
    "received_unknown": "неизвестна сумма купона или погашения за день",
    "no_prev_fx_rate": "нет курса валюты за предыдущий день",
    "no_prev_report": "нет отчётной стоимости позиции за предыдущий день",
    "report_carried": "отчёт не обновлялся с предыдущего дня",
}

METHOD_TEXT = {
    "desk-flash-exact-v1": "флэш: пересчёт ядром",
    "desk-flash-exact-v2": "флэш: пересчёт ядром по кривым бумаги",
    "desk-flash-simple-v1": "флэш: изменение грязной цены, без разложения",
    "desk-fx-spot-v1": "по курсу: сумма × изменение курса",
    "desk-reported-v1": "по отчёту: разница отчётных стоимостей",
}
REPORTED_METHOD = "desk-reported-v1"
FX_METHOD = "desk-fx-spot-v1"
EXACT_METHODS = ("desk-flash-exact-v1", "desk-flash-exact-v2")
"""Methods with the full step decomposition: their ``other`` is the price residual (R21), not a whole PnL."""

QUALITY_TEXT = {
    "exact": "точная оценка ядром", "assumed": "ядро с допущением",
    "market_value": "рыночная стоимость", "stale_price": "прошлая цена",
    "model_price": "модельная цена", "par_proxy": "по номиналу", "reported_value": "по отчёту",
}
FLAG_TEXT = {
    "model price on both dates": "модельная цена в оба дня",
    "price discovery: model -> market": "смена модельной цены на рыночную",
    "price discovery: market -> model": "смена рыночной цены на модельную",
}
_SIMPLE_RUNGS = re.compile(r"simple PnL \((\w+) -> (\w+)\)")


def other_split(method: str | None, other: float, pnl_extra: dict[str, Any] | None,
                fact_extra: dict[str, Any] | None, fx_gross: float = 0.0) -> tuple[float, float, str | None]:
    """R21: what the ``other`` of a position's flash PnL is (the report-valued part, ``REPORTED_METHOD``, is taken
    out before). Returns (no_decomp, residual, why): ``residual`` for a method with the step decomposition (the
    market dirty price of D against core's value of the D-1 flows at the D spread: a changed schedule, a price and
    an accrued that disagree; about 0), ``no_decomp`` for every other method (the whole PnL of a position core does
    not reprice step by step: a fallback rung's price change, a change of discount curve); ``why`` is the Russian
    text the drill-down shows, None when there is nothing to explain."""
    x, fx = pnl_extra or {}, fact_extra or {}
    note, flag = x.get("note") or "", x.get("flag") or ""
    if abs(other) < 0.01:  # below a kopeck (flash.IDENTITY_TOLERANCE_RUB): rounding, nothing to explain
        return (0.0, other, None) if method in EXACT_METHODS else (other, 0.0, None)
    if method in EXACT_METHODS:
        why = ("невязка цены: рыночная грязная цена D минус стоимость вчерашних денежных потоков ядром при Z-спреде D"
               + (f" (Z {x['z0_bp']:g} → {x['z1_bp']:g} б.п.)" if x.get("z0_bp") is not None and x.get("z1_bp")
                  is not None else "")
               + "; бывает при смене графика (оферта, новый купон) или несогласованных цене и НКД")
        pb0, pb1 = x.get("prev_price_basis"), fx.get("price_basis")
        if pb0 and pb1 and pb0 != pb1:
            why += f"; база цены сменилась: {pb0} → {pb1}"
        return 0.0, other, why
    if m := _SIMPLE_RUNGS.search(note):
        q0, q1 = (QUALITY_TEXT.get(q, q) for q in m.groups())
        how = f"{q0} в оба дня" if q0 == q1 else f"{q0} → {q1}"
        why = (f"без разложения: у оценки нет денежных потоков для пересчёта ядром ({how}); "
               "PnL = количество × (грязная цена D − грязная цена D−1) + выплаты")
    elif note.startswith("simple PnL: discounted on"):
        why = ("без разложения: кривая дисконтирования сменилась между днями, спреды к разным кривым; "
               "PnL = количество × (грязная цена D − грязная цена D−1) + выплаты")
    else:
        why = "без разложения: " + METHOD_TEXT.get(method or "", method or "метод не указан")
    if flag:
        why += "; " + FLAG_TEXT.get(flag, flag)
    if fx_gross:
        why += "; валютная переоценка вынесена в FX"
    return other, 0.0, why


def no_pnl_text(reason: str | None, detail: str | None = None) -> str:
    """The Russian text of a no-PnL code (the code itself when unknown), with the detail in brackets."""
    text = NO_PNL_TEXT.get(reason or "not_computed", reason or NO_PNL_TEXT["not_computed"])
    return f"{text} ({detail})" if detail else text
