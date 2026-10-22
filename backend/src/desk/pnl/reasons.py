"""Why a position or a run has no PnL, and how a PnL was computed: codes and the Russian text the Desk Monitor
shows (R10). A missing PnL is said, never shown as 0: the run carries ``pnlAvailable`` with a code, a position
without PnL its ``no_pnl`` code (``NoPnl.reason``), and both get a text from here."""

from __future__ import annotations

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


def no_pnl_text(reason: str | None, detail: str | None = None) -> str:
    """The Russian text of a no-PnL code (the code itself when unknown), with the detail in brackets."""
    text = NO_PNL_TEXT.get(reason or "not_computed", reason or NO_PNL_TEXT["not_computed"])
    return f"{text} ({detail})" if detail else text
