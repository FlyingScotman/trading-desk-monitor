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
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ..inbox.schema import alias_position
from . import concepts

AMOUNTS = ("amount_ccy", "face_amount_ccy", "face_outstanding_ccy")
SWAPS = ("IRS", "CCS", "CDS", "TRS")
TERMS_SWAPS = ("IRS", "CCS")
"""The swaps core values from their trade terms: only they «wait for the terms»; a CDS or a TRS has terms in no source
(TDM#607) and is valued at the report's value or not at all (``NO_TERMS_CAUSES``)."""
NO_TERMS_CAUSES: dict[str, tuple[str, str]] = {
    "report_no_value": (("в отчёте позиций нет стоимости (NPV) этой строки; условий таких сделок нет ни в одном "
                         "источнике, поэтому другой оценки нет"),
                        "проверьте в отчёте позиций, заполнена ли стоимость этой сделки"),
    "report_value_no_rate": (("стоимость в отчёте позиций есть, но в валюте строки, а курса ЦБ этой валюты на дату "
                              "нет"), "курсы ЦБ приходят со срезом хаба: обновите данные за дату"),
    "report_value_off": (("ни у одной строки деривативов этой книги в отчёте позиций нет стоимости: возможно, выключена "
                          "«Оценка и риск из отчёта о позициях» в настройках адаптера, или отчёт её не даёт"),
                         ("проверьте «Оценка и риск из отчёта о позициях» (Настройки → Адаптер → Источники); если "
                          "включена — стоимости нет в самом отчёте")),
    "run_before_report_value": (("прогон сделан программой до версии 0.2.19: стоимость в отчёте позиций есть, но та "
                                 "версия её не брала"), "пересчитайте дату: Настройки → История прогонов"),
}
"""TDM#607: why a swap without trade terms (CDS, TRS) is not valued, by what its report row and the run say."""
REPORT_MV = ("стоимость и риск из отчёта о позициях: Настройки → Адаптер → Источники, «Оценка и риск из отчёта о "
             "позициях»")
FIX = {"terms": ("Условия сделок", "../settings.html#adapter/acq"),
       "indexes": ("Индексы ставок", "../settings.html#indexes"),
       "report_risk": ("Оценка и риск из отчёта о позициях", "../settings.html#adapter/sources"),
       "hub": ("Маркет-дата сервер", "../settings.html#hub"),
       "history": ("История прогонов", "../settings.html#bf"),
       "diag": ("Диагностика", "../settings.html#diag")}
"""The settings screen that fixes a cause (``CAUSE_FIX``): (its name, the link from a concept page)."""

CAUSES: dict[str, tuple[str, str]] = {
    "unmapped_instrument": ("бумага не найдена в статике хаба (ISIN / рег. номер / тикер неизвестны)",
                            ("проверьте ISIN в отчёте; погашенная бумага в статике после погашения не появится — "
                             "тогда это закрытая позиция")),
    "not_in_static_snapshot": ("бумаги нет в статике хаба на дату", "проверьте ISIN; погашена — закрытая позиция"),
    "no_quantity": ("облигация без количества бумаг и без MV в отчёте", "в отчёте нужны Quantity или номинал"),
    "no_fx_rate": ("нет курса валюты строки в срезе хаба", "курс ЦБ приходит со срезом хаба: обновите данные за дату"),
    "fx_row_without_ccy_or_amount": ("FX-строка без валюты или суммы", "проверьте строку отчёта (CCY, Quantity)"),
    # a row with trade terms core does not value (valuation.derivatives, TDM#45) and no MV in the report
    "ccs_unsupported": (("валютно-процентный своп (CCS): условия сделки есть, но ядро в мониторе их пока не "
                        "оценивало (расчёт сделан прежней версией программы); MV в отчёте нет"),
                        "пересчитайте дату — или " + REPORT_MV),
    "ccs_foreign_floating_unsupported": (("валютно-процентный своп (CCS) с плавающей ставкой по валютной ноге: индекс "
                                         "валютной ноги не поддерживается (оцениваются юани на RUSFAR CNY, доллары на SOFR, "
                                         "евро на €STR); MV в отчёте нет"), "до оценки ядром — " + REPORT_MV),
    "index_unmapped": (("индекс плавающей ноги не сопоставлен с кодом хаба в настройках адаптера; MV в отчёте "
                       "нет"), "выберите его в Настройках, экран «Индексы ставок», кнопка «Сохранить и пересчитать» — "
                               "или " + REPORT_MV),
    "ccs_index_averaging_unsupported": (("валютно-процентный своп (CCS), у которого ставка плавающей ноги (валютной "
                                        "или RUSFAR) считается средней арифметической, а не компаундингом: ядро "
                                        "оценивает только компаундинг; MV в отчёте нет"),
                                        "до оценки ядром — " + REPORT_MV),
    "key_rate_curve_too_short": (("валютно-процентный своп (CCS) на ключевой ставке длиннее кривой ключевой ставки "
                                 "(котировки IRS KEYRATE идут примерно на 10 лет); MV в отчёте нет"),
                                 "до оценки ядром — " + REPORT_MV),
    "no_foreign_projection_curve": (("валютно-процентный своп (CCS) с плавающей валютной ногой: нет кривой прогноза "
                                    "индекса валютной ноги за дату (для юаней — котировок OIS RUSFAR CNY, для "
                                    "долларов и евро — кривой гособлигаций UST / ECB AAA в срезе хаба); MV в отчёте нет"),
                                    "котировки приходят со срезом хаба: обновите данные за дату — или " + REPORT_MV),
    "ccs_ccy_unsupported": (("валютно-процентный своп (CCS) не «рубли против валюты» (обе ноги в валюте или обе "
                            "в рублях): ядро в мониторе такие не оценивает; MV в отчёте нет"),
                            "проверьте валюты ног в условиях сделки — или " + REPORT_MV),
    "terms_ambiguous": (("условия одной сделки подходят нескольким строкам отчёта (один deal_id на нескольких "
                        "строках, например ноги CCS отдельными строками): ядро не оценивает ни одну из них; "
                        "MV в отчёте нет"),
                        "нужна одна строка отчёта позиций на сделку — или " + REPORT_MV),
    "basis_swap_unsupported": (("базисный своп (обе ноги плавающие): ядро в мониторе их пока не оценивает; MV в "
                               "отчёте нет"), "до оценки ядром — " + REPORT_MV),
    "repo_open": (("открытое РЕПО (без даты окончания): для оценки ядром нужна предполагаемая дата окончания, она не "
                  "задана; MV в отчёте нет"), "дата второй части в условиях сделки (maturity_date) — или " + REPORT_MV),
    "swap_index_unsupported": (("индекс плавающей ноги не поддерживается: у IRS — RUONIA и ключевая ставка, у рублёвой "
                               "ноги CCS — RUONIA, ключевая ставка, RUSFAR или фикс; MV в отчёте нет"),
                              "до оценки ядром — " + REPORT_MV),
    "no_fixings": (("нет фиксингов индекса сделки в срезе хаба (или индекс без фиксингов в мониторе, напр. RUSFAR у РЕПО); "
                   "MV в отчёте нет"), "фиксинги приходят со срезом хаба: обновите данные за дату — или " + REPORT_MV),
    "no_key_rate_curve": ("нет кривой ключевой ставки в срезе хаба за дату (котировки IRS KEYRATE); MV в отчёте нет",
                          "кривая строится из среза хаба: обновите данные за дату — или " + REPORT_MV),
    # TDM#589: every reason valuation.derivatives and valuation.positions give, in words (test_reason_texts_589)
    "terms_incomplete": (("условия сделки неполные: не хватает поля, без которого сделку не оценить (дата, номинал, "
                          "ставка, валюта ноги); MV в отчёте нет"),
                         "проверьте условия сделки в источнике (Настройки → Адаптер → Условия сделок) — или " + REPORT_MV),
    "terms_invalid": (("условия сделки противоречивы или не читаются (дата окончания раньше начала, номинал не число, "
                       "сторона не задана); MV в отчёте нет"),
                      "проверьте условия сделки в источнике (Настройки → Адаптер → Условия сделок) — или " + REPORT_MV),
    "terms_mixed": (("под одной строкой отчёта позиций условия сделок разных видов (валютная и процентная): ядро не "
                     "оценивает такую строку; MV в отчёте нет"),
                    "нужна одна строка отчёта позиций на сделку — или " + REPORT_MV),
    "no_rub_curve": ("нет рублёвой кривой RUONIA OIS в срезе хаба за дату: дисконтировать нечем; MV в отчёте нет",
                     "кривая строится из среза хаба: обновите данные за дату — или " + REPORT_MV),
    "no_foreign_curve": ("нет кривой валюты сделки в срезе хаба за дату; MV в отчёте нет",
                         "кривые приходят со срезом хаба: обновите данные за дату — или " + REPORT_MV),
    "no_fx_spot": ("нет спот-курса валюты сделки в срезе хаба за дату; MV в отчёте нет",
                   "курсы приходят со срезом хаба: обновите данные за дату — или " + REPORT_MV),
    "no_fx_calendar": (("нет календаря рабочих дней валюты сделки в статике хаба: даты расчётов не посчитать; MV в "
                        "отчёте нет"), "календари приходят со статикой хаба — или " + REPORT_MV),
    "day_count_unsupported": ("база начисления процентов в условиях сделки не поддерживается ядром; MV в отчёте нет",
                              "проверьте базу (day_count) в условиях сделки — или " + REPORT_MV),
    "swap_frequency_unsupported": ("частота выплат ноги свопа не поддерживается ядром; MV в отчёте нет",
                                   "проверьте частоту выплат в условиях сделки — или " + REPORT_MV),
    "swap_dates_mismatch": (("у ног процентного свопа (IRS) разные даты начала или окончания: прогон сделан до версии "
                             "0.2.21, которая оценивает каждую ногу по её датам; MV в отчёте нет"),
                            "пересчитайте дату: Настройки → История прогонов"),
    "swap_notional_mismatch": ("у ног свопа разные номиналы: ядро так не оценивает; MV в отчёте нет",
                               "проверьте номиналы ног в условиях сделки — или " + REPORT_MV),
    "swap_ccy_unsupported": ("процентный своп не в рублях: ядро в мониторе такие не оценивает; MV в отчёте нет",
                             "до оценки ядром — " + REPORT_MV),
    "repo_ccy_unsupported": ("РЕПО не в рублях: ядро в мониторе такие не оценивает; MV в отчёте нет",
                             "до оценки ядром — " + REPORT_MV),
    "repo_index_unsupported": (("индекс плавающей ставки РЕПО не поддерживается (оцениваются фикс, RUONIA и ключевая "
                                "ставка); MV в отчёте нет"), "до оценки ядром — " + REPORT_MV),
    "ndf_unsupported": ("NDF с расчётами не в рублях или кросс-NDF: ядро в мониторе такие не оценивает; MV в отчёте нет",
                        "до оценки ядром — " + REPORT_MV),
    "future_terms_incomplete": (("валютный фьючерс: у контракта в срезе хаба нет базовой валюты, расчётной цены, лота "
                                 "или даты исполнения; MV в отчёте нет"),
                                "данные фьючерсов приходят со срезом хаба: обновите данные за дату — или " + REPORT_MV),
    "settled": ("сделка исполнена до даты прогона: оценивать нечего (погашенная сделка в отчёте позиций)",
                "не пропуск: сделка закрыта; уйдёт из отчёта позиций"),
    "not_valued": ("ядро не вернуло оценку строки и не назвало причину", "журнал прогона: Настройки → Диагностика"),
    "pv_not_finite": ("ядро дало нечисловую оценку (бесконечность или «не число»): оценка не показана",
                      "журнал прогона: Настройки → Диагностика; сообщите в задачу с датой прогона"),
    "no_trade_terms": ("условий сделки нет: оценка взята из отчёта позиций", "до условий сделки — " + REPORT_MV),
    # the bond rules of valuation.bonds (before the fallback ladder: a bond with its terms is valued somehow; one of
    # these is the reason when even the ladder had nothing)
    "no_quote": ("у бумаги нет котировки за дату и нет цены в отчёте", "котировки приходят со срезом хаба"),
    "no_price": ("у бумаги нет цены за дату", "котировки приходят со срезом хаба"),
    "stale_quote": ("котировка бумаги устарела", "котировки приходят со срезом хаба"),
    "matured": ("бумага погашена до даты прогона", "не пропуск: закрытая позиция"),
    "not_rub": ("бумага не в рублях: ядро в мониторе её не оценивает", "оценка по рыночной цене (без ядра)"),
    "structured": ("структурная бумага: ядро её не оценивает", "оценка по рыночной цене (без ядра)"),
    "face_indexed": ("номинал бумаги индексируется: ядро её не оценивает", "оценка по рыночной цене (без ядра)"),
    "securitized": ("ипотечная или секьюритизированная бумага: погашения зависят от досрочных выплат",
                    "оценка по рыночной цене (без ядра)"),
    "floater_formula_missing": ("у флоатера нет формулы купона в статике хаба", "оценка по рыночной цене (без ядра)"),
    "floater_formula_incomplete": ("формула купона флоатера неполная (нет спреда)", "оценка по рыночной цене (без ядра)"),
    "floater_benchmark_unsupported": ("индекс купона флоатера не поддерживается", "оценка по рыночной цене (без ядра)"),
    "perpetual_no_schedule": ("бессрочная бумага без графика купонов", "оценка по рыночной цене (без ядра)"),
    "coupon_not_known": ("купон бумаги не известен", "оценка по рыночной цене (без ядра)"),
    "redemption_incomplete": ("график погашения бумаги неполный", "оценка по рыночной цене (без ядра)"),
    "redemption_amount_missing": ("в графике погашения нет сумм", "оценка по рыночной цене (без ядра)"),
    "no_face_value": ("у бумаги нет номинала в статике хаба", "оценка по рыночной цене (без ядра)"),
    "face_mismatch": ("номинал бумаги в статике и в котировке расходятся", "оценка по рыночной цене (без ядра)"),
    "calendar_unknown": ("нет календаря торгов бумаги в статике хаба", "календари приходят со статикой хаба"),
}
"""Reason code -> (why, what to do) of an unvalued row whatever its kind."""

CORE_CAUSES: dict[str, tuple[str, str]] = {
    "missing_market_data": (("ядру не хватило рыночных данных за дату (кривой, курса или фиксинга индекса за начало "
                             "периода сделки); MV в отчёте нет"),
                            ("фиксинги, кривые и курсы приходят со срезом хаба, старые фиксинги — из хранимой истории "
                             "фиксингов (Настройки → История прогонов); обновите данные за дату — или " + REPORT_MV)),
    "unsupported_product": ("ядро не поддерживает такой вид сделки; MV в отчёте нет", "до оценки ядром — " + REPORT_MV),
    "invalid_terms": ("ядро не приняло условия сделки (противоречивы или неполны); MV в отчёте нет",
                      "проверьте условия сделки в источнике (Настройки → Адаптер → Условия сделок) — или " + REPORT_MV),
    "calendar_out_of_range": ("даты сделки выходят за календарь рабочих дней хаба; MV в отчёте нет",
                              "календари приходят со статикой хаба — или " + REPORT_MV),
    "unknown_curve_spec": ("ядро не знает кривую, на которой нужно оценить сделку; MV в отчёте нет",
                           "журнал прогона: Настройки → Диагностика — или " + REPORT_MV),
    "expired_contract": ("контракт истёк до даты прогона", "не пропуск: сделка закрыта"),
    "settled_leg": ("нога сделки уже исполнена до даты прогона", "не пропуск: исполненная нога"),
    "undeclared_fixing_gap": (("в истории фиксингов индекса пропуск внутри периода сделки (день без фиксинга); MV в "
                               "отчёте нет"),
                              ("фиксинги приходят со срезом хаба и из хранимой истории фиксингов (Настройки → История "
                               "прогонов) — или " + REPORT_MV)),
    "missing_basket_price": ("нет цены бумаги из корзины фьючерса", "котировки приходят со срезом хаба"),
    "missing_assumption": ("ядру не хватило допущения для оценки (например даты исполнения)",
                           "журнал прогона: Настройки → Диагностика — или " + REPORT_MV),
    "calculation_failed": ("расчёт ядра упал на этой сделке", "журнал прогона: Настройки → Диагностика"),
    "time_budget_exceeded": ("ядро не уложилось во время расчёта", "пересчитайте дату"),
    "accrued_failed": ("ядро не посчитало НКД бумаги", "оценка по рыночной цене (без ядра)"),
}
"""A refusal of core (``core_<code>``, ``rub_fixed_income.errors``): (why, what to do)."""

SHORT = {"report_no_value": "нет стоимости в отчёте", "report_value_no_rate": "нет курса",
         "report_value_off": "нет стоимости в книге", "run_before_report_value": "старый прогон",
         "swap_terms": "ждёт условий", "repo": "ждёт условий", "no_trade_terms": "ждёт условий",
         "future_ticker": "ждёт настройки", "index_unmapped": "индекс не сопоставлен",
         "terms_incomplete": "условия неполные", "terms_invalid": "условия с ошибкой", "terms_mixed": "условия смешаны",
         "terms_ambiguous": "условия на нескольких строках", "settled": "исполнена", "closed": "закрыта",
         "unmapped_instrument": "бумага не найдена", "not_in_static_snapshot": "бумага не найдена",
         "no_quantity": "нет количества", "no_fx_rate": "нет курса", "no_fx_spot": "нет курса",
         "no_fixings": "нет фиксингов", "core_missing_market_data": "нет рыночных данных",
         "core_undeclared_fixing_gap": "пропуск фиксинга", "no_report_mv": "нет оценки в отчёте",
         "no_fx_calendar": "нет календаря", "core_calendar_out_of_range": "нет календаря",
         "repo_open": "нет даты окончания", "key_rate_curve_too_short": "кривая короче сделки"}
"""Cause key -> the few words the blotter's «Купон» column and the card's «Ставка / курс» show (TDM#589); a key
not listed: by its words below (``short``)."""

CAUSE_FIX = {"report_value_off": "report_risk", "report_value_no_rate": "hub", "run_before_report_value": "history",
             "swap_terms": "terms", "repo": "terms", "no_trade_terms": "terms", "terms_incomplete": "terms",
             "terms_invalid": "terms", "terms_mixed": "terms", "terms_ambiguous": "terms", "repo_open": "terms",
             "core_invalid_terms": "terms", "index_unmapped": "indexes", "swap_index_unsupported": "indexes",
             "no_report_mv": "report_risk", "bond_derivative": "report_risk", "future_ticker": "report_risk",
             "core_missing_market_data": "history", "core_undeclared_fixing_gap": "history", "no_fixings": "hub",
             "no_fx_rate": "hub", "no_fx_spot": "hub", "no_rub_curve": "hub", "no_key_rate_curve": "hub",
             "no_foreign_curve": "hub", "no_foreign_projection_curve": "hub", "future_terms_incomplete": "hub",
             "not_valued": "diag", "pv_not_finite": "diag", "core_calculation_failed": "diag"}
"""Cause key -> the settings screen that fixes it (``FIX``); a cause not listed whose text names the report's value
gets ``report_risk``."""


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


def cause(code: str, kind: str, product: str | None, futures_by_ticker: bool | None, *,
          report_value: bool = False, report_value_on: bool | None = None) -> tuple[str, str, str]:
    """(group key, why, what to do) of an unvalued row: the reason code (before ``:``) read with its kind.
    ``report_value``: the row has the report's value in its currency (``x_npv_ccy_value``); ``report_value_on``: some
    derivative row of the book's report has a value (``report_value_by_book``; False: none has, the adapter's report
    value looks off; None: unknown)."""
    if kind in SWAPS and kind not in TERMS_SWAPS and code in ("no_reported_value", "no_fx_rate"):
        return (key := no_terms_cause(code, report_value, report_value_on), *NO_TERMS_CAUSES[key])
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
    if code.startswith("core_") and code[5:] in CORE_CAUSES:
        return (code, *CORE_CAUSES[code[5:]])
    if code.startswith("core_"):
        return (code, f"ядро отказало оценивать сделку (код {code[5:]}); MV в отчёте нет",
                "журнал прогона: Настройки → Диагностика — или " + REPORT_MV)
    return (code or "unknown", f"не оценено ({code or 'причина не записана'})", "журнал прогона: Настройки → Диагностика")


def no_terms_cause(code: str, report_value: bool, report_value_on: bool | None) -> str:
    """The key of ``NO_TERMS_CAUSES`` of a CDS or TRS without a value (TDM#607): no CBR rate of the value's currency,
    the value there but not taken (a run before 0.2.19), the report's value off in the adapter, none in the report."""
    if code == "no_fx_rate":
        return "report_value_no_rate"
    if report_value:
        return "run_before_report_value"
    return "report_value_off" if report_value_on is False else "report_no_value"


def has_report_value(x: dict[str, Any] | None) -> bool:
    """The report row (``x_*`` fields, flat or under ``x``) has the report's value in its currency (TDM#591)."""
    x = x or {}
    xx = x.get("x") if isinstance(x.get("x"), dict) else x
    return xx.get("x_npv_ccy_value") is not None or x.get("x_npv_ccy_value") is not None


REPORT_VALUE_SQL = ("SELECT s.book_id, bool_or(i.mv_rub IS NOT NULL OR i.x ? 'x_npv_ccy_value'"
                    " OR i.x ? 'x_report_risk_assumption') FROM position_input i JOIN position_set s USING (set_id)"
                    " WHERE i.set_id = ANY(%s) AND lower(i.asset_class) IN ('otc', 'other') GROUP BY 1")


def report_value_by_book(con, set_ids: list[int] | None) -> dict[str, bool]:
    """Per book of the run's position sets with derivative rows (otc, other): whether any has a report value
    (``mv_rub``, the value in its currency, the adapter's note of its report value). False: none has, the adapter's
    «Оценка и риск из отчёта о позициях» looks off for that book's report; a book without such rows is not listed."""
    if not set_ids:
        return {}
    out = {}
    for row in con.execute(REPORT_VALUE_SQL, (list(set_ids),)).fetchall():
        book, v = (row[0], row[1]) if isinstance(row, tuple | list) else tuple(row.values())[:2]
        out[str(book)] = bool(v)
    return out


FIXING_RE = re.compile(r"no (\w+) fixing on or before (\d{4})-(\d{2})-(\d{2})")


HOLE_RE = re.compile(r"outside calendar .*?(RUONIA).*?\+published_from_(\d{4})-(\d{2})-(\d{2})")


def _dm(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[0:4]}"   # with the year: the stored history goes years back


def history_hole(lineage: dict[str, Any] | None) -> dict[str, str] | None:
    """The hole of the stored RUONIA history of a run (``rates.fixings.RUONIA.calendar_from_fixings.hole``,
    ``curves.rates.published_before``), None without one."""
    r = ((((lineage or {}).get("rates") or {}).get("fixings") or {}).get("RUONIA") or {})
    h = (r.get("calendar_from_fixings") or {}).get("hole") if isinstance(r, dict) else None
    return h if isinstance(h, dict) and h.get("from") and h.get("to") else None


def is_history_hole(detail: str | None) -> bool:
    """Core's refusal of a period over a hole of the stored history (TDM#590: the calendar of the stored RUONIA starts
    after its last hole)."""
    return HOLE_RE.search(detail or "") is not None


def detail_text(detail: str | None, hole: dict[str, str] | None = None) -> str | None:
    """The words of a known detail of core's refusal (TDM#590): «нет фиксинга RUONIA на 10.04.2026 или раньше …»,
    «в хранимой истории RUONIA пропуск с ДД.ММ.ГГГГ по ДД.ММ.ГГГГ: период сделки его пересекает» (``hole``: the run's,
    ``history_hole``; without it the day before the history resumes); None for any other detail."""
    if (h := HOLE_RE.search(detail or "")) is not None:
        if hole:
            span = f"с {_dm(hole['from'])} по {_dm(hole['to'])}"
        else:
            resumed = date(int(h.group(2)), int(h.group(3)), int(h.group(4))) - timedelta(days=1)
            span = f"по {_dm(resumed.isoformat())}"
        return f"в хранимой истории {h.group(1)} пропуск {span}: период сделки его пересекает"
    m = FIXING_RE.search(detail or "")
    if m is None:
        return None
    return (f"нет фиксинга {m.group(1)} на {m.group(4)}.{m.group(3)}.{m.group(2)} или раньше: начало периода сделки "
            "раньше истории фиксингов прогона")


def short(key: str) -> str:
    """The few words of a cause (``SHORT``), else by its kind of trouble."""
    if key in SHORT:
        return SHORT[key]
    if key.endswith("_unsupported") or key in ("bond_derivative", "core_unsupported_product"):
        return "вид не поддержан"
    if "curve" in key:
        return "нет кривой"
    if key.startswith("core_"):
        return "отказ ядра"
    return "не оценено"


def explain(reason: str | None, kind: str, product: str | None, futures_by_ticker: bool | None,
            hole: dict[str, str] | None = None, *, report_value: bool = False,
            report_value_on: bool | None = None) -> dict[str, Any]:
    """The words of an unvalued row's reason (``run_position.reason``: ``code: detail``) for the position card and the
    blotter (TDM#589): ``key``, ``why``, ``todo``, ``short``, ``detail`` (a known detail in words, else the raw one),
    ``fix`` ({label, href}: the settings screen that fixes it, or None)."""
    code, _, detail = (reason or "").partition(":")
    key, why, todo = cause(code.strip(), kind, product, futures_by_ticker, report_value=report_value,
                           report_value_on=report_value_on)
    detail = detail.strip()
    if is_history_hole(detail):  # not the hub's calendar: a hole of the monitor's stored fixings (TDM#590)
        why = ("период начисления сделки пересекает пропуск в хранимой истории фиксингов: дни без фиксинга подряд "
               "дольше праздников, это недостающие данные, а не выходные")
        todo = ("история фиксингов догружается в фоне: проверьте строку «История фиксингов» в «Истории прогонов»; "
                "после догрузки дата пересчитается")
    fk = "history" if is_history_hole(detail) else CAUSE_FIX.get(key) or ("report_risk" if REPORT_MV in todo else None)
    return {"key": key, "why": why, "todo": todo, "short": "пропуск фиксингов" if is_history_hole(detail) else short(key),
            "detail": detail_text(detail, hole) or detail or None,
            "fix": {"label": FIX[fk][0], "href": FIX[fk][1]} if fk else None}


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
    for i in inputs.values():  # TDM#601: CNH is CNY, as the run read the row (store.positions_for)
        alias_position(i)
    fx = _rates(con, run["run_id"])
    on = report_value_by_book(con, run.get("position_set_ids"))
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
            key, why, todo = cause(code, kind, product, fbt, report_value=has_report_value(x),
                                   report_value_on=on.get(str(r["book_id"])))
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


PROVENANCE_STATE = {"present": "есть", "absent": "нет данных", "unknown": "нет сведений", "not_needed": "не нужны"}
"""TDM#617: whether a source has data on a deal, as the card's «Откуда данные» says it (``api.provenance``)."""
PROVENANCE_LEVEL = {"deal": "по сделке", "book_ccy": "по книге и валюте"}
"""What a source is matched by: the deal itself, or only its book and currency (the OCP report and the balances have no
key of a deal)."""
MARKET_STATE = {"ok": "есть", "replaced": "заменена", "stale": "устарела", "missing": "нет", "pending": "ещё не наступил",
                "unknown": "нет сведений"}
"""TDM#617: the state of a market input a deal's valuation used or needs."""
MISMATCH_TEXT = {
    "report_no_terms": "есть в отчёте позиций, но условия сделки не загружены",
    "trades_no_report": "пришла из списка сделок сервиса, но её нет в отчёте позиций",
    "terms_no_report": "условия загружены, строки в отчёте позиций нет",
    "deal_on_rows": "одна сделка на нескольких строках отчёта позиций",
    "report_trades_gap": "количество в отчёте позиций не сходится с началом дня и сделками дня",
}
"""TDM#617: the mismatches between a deal's sources, in words."""
PROVENANCE_TEXT = {
    "report_row": "строка отчёта позиций",
    "report_no_row": "отчёт позиций книги загружен, строки этой позиции в нём нет",
    "report_no_book": "отчёта позиций этой книги в прогоне нет",
    "terms_rows": "условия сделки",
    "terms_none": "условий этой сделки в загруженных наборах нет",
    "terms_book_none": "условия сделок этой книги в прогон не загружались",
    "terms_bond": "облигация: условия выпуска — из статики хаба, условия сделки не нужны",
    "terms_not_needed": "для этой строки условия сделки не используются",
    "terms_by_ticker": "валютный фьючерс оценён по тикеру биржи: условия сделки не нужны",
    "trades_rows": "сделки дня из списка сделок",
    "trades_none": "в списке сделок дня этой позиции нет",
    "trade_earlier": "сделка за {day}: в позиции начала дня",
    "deal_on_rows_terms": "условия не отнести к одной строке",
    "trades_no_lineage": "нет сведений: прогон не записал, какие сделки дня он читал",
    "book_ccy_rows": "строки по книге и валюте, у сделки своего ключа нет",
    "book_ccy_none": "по книге и валюте сделки строк нет",
    "book_ccy_later": "после прогона загружен более новый отчёт",
    "no_lineage": "нет сведений: прогон сделан версией, которая этого не записывала",
    "market_used": "что использовала оценка сделки",
    "market_needed": "сделка не оценена: что нужно для оценки по её условиям и есть ли это в рыночных данных прогона",
    "market_unknown": "нет сведений: прогон не записал, какие рыночные данные использовала оценка",
}
"""TDM#617: the card's «Откуда данные» in words (``api.provenance``)."""


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
        rc = (f.get("extra") or {}).get("reported_ccy")
        rc = rc.get("ccy") if isinstance(rc, dict) else None  # TDM#591: the report's value in its currency, CBR rate
        key = f"{r['quality']}|{kind}|{int(no_terms)}" + (f"|{rc}" if rc else "")
        text = (f"стоимость из отчёта в {rc}, по курсу ЦБ" if rc else APPROX_TEXT.get(r["quality"], r["quality"])) \
            + ("; условий сделки (trade_terms) нет: FX-дельта = номинал" if no_terms else "")
        g = groups.setdefault(key, {"key": key, "quality": r["quality"], "kind": kind,
                                    "kindLabel": concepts.KIND_LABEL.get(kind, kind), "text": text, "n": 0,
                                    "mvRub": 0.0, "ids": []})
        g["n"] += 1
        g["mvRub"] += _f(r["pv_rub"]) or 0.0
        g["ids"].append(r["position_id"])
    return sorted(groups.values(), key=lambda g: (-g["n"], g["key"]))


def off_report(con, run: dict[str, Any]) -> dict[str, Any] | None:
    """The derivatives outside the positions report (TDM#468) for the panel «Чего нет в данных»: the run's
    ``lineage.positions.off_report`` (switch, counts, the trades not included: excluded, suspect, invalid), or for a run
    from before the switch only its ``unlinked_swap_terms`` count (``legacy``). None when the run has neither."""
    pos = (run.get("lineage") or {}).get("positions") or {}
    o = pos.get("off_report")
    if isinstance(o, dict):
        listed = [t for t in o.get("list") or [] if t.get("state") not in ("included", "closed")]
        return {**{k: o.get(k) for k in ("mode", "trades", "included", "valued", "suspect", "invalid", "excluded",
                                         "closed", "matured", "books")}, "list": listed,
                "truncated": bool(o.get("list_truncated"))}
    u = pos.get("unlinked_swap_terms")
    if isinstance(u, dict) and u.get("trades"):
        return {"mode": None, "legacy": True, "trades": u["trades"], "books": u.get("books") or {}, "list": []}
    return None


def why_of(reason: str | None, x: dict[str, Any] | None, futures_by_ticker: bool | None,
           hole: dict[str, str] | None = None, report_value_on: bool | None = None) -> dict[str, Any]:
    """``explain`` of an unvalued position from its report row ``x`` (``position_input``: ``ccy``, ``asset_class``,
    its ``x_*`` under ``x``): the ``extra.why`` of the concept pages' and compat's not-valued position (TDM#589)."""
    x = x or {}
    rep = x.get("x") if isinstance(x.get("x"), dict) else {}
    kind = concepts.instr_kind({"ccy": x.get("ccy"), "asset_class": x.get("asset_class"), "extra": {"reported": rep}})
    product = rep.get("x_product") if isinstance(rep.get("x_product"), str) else None
    return explain(reason, kind, product, futures_by_ticker, hole, report_value=has_report_value(rep),
                   report_value_on=report_value_on)
