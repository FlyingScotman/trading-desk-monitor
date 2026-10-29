"""Why a position or a run has no PnL, how a PnL was computed and why a part of it is unexplained: codes and the
Russian text the Desk Monitor shows (R10, TDM#442). A missing PnL is said, never shown as 0: the run carries
``pnlAvailable`` with a code, a position without PnL its ``no_pnl`` code (``NoPnl.reason``), and both get a text from
here. A PnL that is (wholly or partly) in ``other`` or ``reported`` says why in ``pnl.extra.other_reason``
(``other_reason``, ``OTHER_REASON_TEXT``): what cannot be decomposed stays unexplained, without an approximation, and
the period's checks group it by that code (``api.pnl_checks``)."""

from __future__ import annotations

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
    "valued_differently": "за предыдущий день сделка оценена не ядром (нет условий или кривой)",
}

METHOD_TEXT = {
    "desk-flash-exact-v1": "флэш: пересчёт ядром",
    "desk-flash-exact-v2": "флэш: пересчёт ядром",
    "desk-flash-exact-fx-v1": "флэш: пересчёт ядром в валюте бумаги на её кривой, курсовая часть по PV D-1",
    "desk-flash-simple-v1": "флэш: изменение грязной цены, без разложения",
    "desk-flash-yield-v1": "флэш: кэрри по доходности бумаги, курсовая часть по PV D-1; нет кривой в валюте — "
                           "изменение доходности в «Прочем»",
    "desk-fx-spot-v1": "по курсу: сумма × изменение курса",
    "desk-reported-v1": "по отчёту: разница отчётных стоимостей",
    "desk-fx-core-v1": "FX-сделка: изменение PV ядра, курсовая часть по FX-дельте",
}
CORE_FX_METHOD = "desk-fx-core-v1"
REPORTED_METHOD = "desk-reported-v1"
FX_METHOD = "desk-fx-spot-v1"
EXACT_V1_METHOD = "desk-flash-exact-v1"
"""The exact flash on the KBD only, before the bonds' own curves (VALUATION_VERSION 4): stored rows of older runs."""

NO_CURVE_ON_DATE = "no_curve_on_date"
DISCOUNT_CURVE_SWITCH = "discount_curve_switch"
CORE_REFUSED = "core_refused"
NO_TERMS = "no_terms"
FALLBACK_PRICE = "fallback_price"
PRICE_SOURCE_SWITCH = "price_source_switch"
YIELD_NO_CURVE = "yield_no_curve"
REPORTED_VALUE = "reported_value"
FX_CORE_REST = "fx_core_rest"
FLASH_RESIDUAL = "flash_residual"
OTHER_REASON_TEXT = {
    NO_CURVE_ON_DATE: "нет кривой на дату",
    DISCOUNT_CURVE_SWITCH: "смена кривой дисконтирования",
    CORE_REFUSED: "ядро отказало",
    NO_TERMS: "нет условий бумаги или сделки",
    FALLBACK_PRICE: "запасная оценка",
    PRICE_SOURCE_SWITCH: "смена источника цены: модель ↔ рынок",
    YIELD_NO_CURVE: "валютная бумага без кривой: изменение доходности",
    REPORTED_VALUE: "оценка по отчёту",
    FX_CORE_REST: "FX-сделка: прочие эффекты",
    FLASH_RESIDUAL: "остаток точного flash",
}
"""``pnl.extra.other_reason.code`` (TDM#442) -> the label of the period's split of the unexplained, in this order. Why
the PnL of a row is (wholly or partly) in ``other`` or ``reported``; nothing is approximated in its place:

- ``no_curve_on_date``: core refused the bond for a curve the market of the date lacks (a floater on a date without
  RUONIA OIS: ``core_missing_market_data``), so it is on a fallback rung: the simple PnL; ``curve`` (published), ``date``;
- ``discount_curve_switch``: discounted on another curve on D-1 than on D (the KBD on a date without RUONIA OIS): the
  spreads are to different curves, the simple PnL; ``curves`` [D-1, D];
- ``core_refused``: core refused the bond (``core_code``), the ladder valued it (``rung``): the simple PnL;
- ``no_terms``: a report value for want of terms (a bond not in the static, an OTC deal without ``trade_terms``);
- ``fallback_price``: a fallback rung off core on either date (``rung``; ``why``: the exact path's refusal), the simple
  PnL;
- ``price_source_switch``: a model price on one date, a price of the market on the other (``detail``: the direction):
  on core the exact steps, but the spread step, model Z against market Z, is price discovery and stays in ``other``;
- ``yield_no_curve``: a foreign bond without a curve in its currency: its carry at its yield, the yield move in
  ``other`` (``desk-flash-yield-v1``);
- ``reported_value``: the difference of the reported values (``desk-reported-v1``, trades of the day inside);
- ``fx_core_rest``: an FX deal valued by core: what is not its FX delta (rates, points, time; ``desk-fx-core-v1``);
- ``flash_residual``: the rest of the exact flash (``detail``: the D-1 schedule changed, else the D price and accrued
  against the D-1 position on the D market)."""
REASONS_SINCE = 23
"""The VALUATION_VERSION from which every PnL with an unexplained part records ``other_reason``."""
NO_REASON_TEXT = f"причина не записана (прогон до версии {REASONS_SINCE})"
"""A stored row with an unexplained part and no ``other_reason``: a run before ``REASONS_SINCE``."""
UNRECORDED = "unrecorded"
"""The key of such rows in the period's split by reason (``api.pnl_checks``, ``api.pnl_period``): ``NO_REASON_TEXT``."""


def other_reason(code: str, detail: str = "", **fields: Any) -> dict[str, Any]:
    """``pnl.extra.other_reason``: ``{"code", "detail", ...}`` (``OTHER_REASON_TEXT``), the fields that are not None."""
    if code not in OTHER_REASON_TEXT:
        raise ValueError(f"unknown other_reason code {code}")
    return {"code": code, "detail": detail, **{k: v for k, v in fields.items() if v is not None}}


def no_pnl_text(reason: str | None, detail: str | None = None) -> str:
    """The Russian text of a no-PnL code (the code itself when unknown), with the detail in brackets."""
    text = NO_PNL_TEXT.get(reason or "not_computed", reason or NO_PNL_TEXT["not_computed"])
    return f"{text} ({detail})" if detail else text
