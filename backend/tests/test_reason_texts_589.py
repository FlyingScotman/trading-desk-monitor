"""TDM#589: every reason the valuation can leave on an unvalued row has words and a hint (``api.coverage``), the
position card and the blotter read them (``concepts_run._not_valued`` ``extra.why``), core's refusals included
(``core_<code>``, ``rub_fixed_income.errors``) and the detail of a missing fixing in words (TDM#590)."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
import rub_fixed_income.errors as core_errors

from desk.api import concepts_run, coverage

SRC = Path(coverage.__file__).resolve().parents[1]
EMITTERS = [*sorted((SRC / "valuation").glob("*.py")), SRC / "orchestrator.py"]
PATTERNS = (r'NotValued\(\s*"([a-z_]+)"', r'Rejected\(\s*"([a-z_]+)"', r'reason="([a-z_]+)"',
            r'reason = "([a-z_]+)"', r'o\.reason, o\.detail = "([a-z_]+)"', r'else "([a-z_]+_unsupported)"',
            r'PV_NOT_FINITE = "([a-z_]+)"')


def emitted() -> set[str]:
    """The reason codes the valuation's source gives as literals (an f-string code is core's: ``core_codes``)."""
    out: set[str] = set()
    for p in [*EMITTERS, SRC / "publish" / "derivatives_parallel.py"]:
        text = p.read_text(encoding="utf-8")
        for pat in PATTERNS:
            out |= set(re.findall(pat, text))
    return out


def core_codes() -> set[str]:
    return {f"core_{c.code.lower()}" for _, c in inspect.getmembers(core_errors, inspect.isclass)
            if isinstance(getattr(c, "code", None), str)}


def test_the_scan_finds_the_codes_of_the_issue():
    found = emitted()
    for code in ("terms_incomplete", "terms_invalid", "terms_mixed", "no_rub_curve", "no_foreign_curve", "no_fx_spot",
                 "no_fx_calendar", "day_count_unsupported", "swap_frequency_unsupported",
                 "swap_notional_mismatch", "swap_ccy_unsupported", "repo_ccy_unsupported", "ndf_unsupported",
                 "future_terms_incomplete", "settled", "not_valued", "no_reported_value", "repo_index_unsupported"):
        assert code in found, code
    assert "core_missing_market_data" in core_codes()


@pytest.mark.parametrize("code", sorted(emitted() | core_codes()))
def test_every_code_has_words_and_a_hint(code):
    key, why, todo = coverage.cause(code, "IRS", None, None)
    assert not why.startswith("не оценено ("), code  # the raw code is what the owner saw (TDM#589)
    assert todo and "options.json" not in why + todo and "desk " not in todo, code  # no files, no commands (TDM#553)
    assert coverage.short(key)


def test_a_missing_fixing_in_words():
    w = coverage.explain("core_missing_market_data: no RUONIA fixing on or before 2026-04-10", "IRS", None, None)
    assert w["key"] == "core_missing_market_data" and "рыночных данных" in w["why"]
    assert w["detail"].startswith("нет фиксинга RUONIA на 10.04.2026 или раньше")
    assert w["fix"] == {"label": "История прогонов", "href": "../settings.html#bf"}
    assert w["short"] == "нет рыночных данных"


@pytest.mark.parametrize(("reason", "kind", "short", "fix"), [
    ("no_reported_value: IRS-1: no mv_rub", "IRS", "ждёт условий", "Условия сделок"),
    ("index_unmapped: OTHER", "IRS", "индекс не сопоставлен", "Индексы ставок"),
    ("basis_swap_unsupported", "IRS", "вид не поддержан", "Оценка и риск из отчёта о позициях"),
    ("no_rub_curve", "IRS", "нет кривой", "Маркет-дата сервер"),
    ("no_reported_value: X: no mv_rub", "OTC", "нет оценки в отчёте", "Оценка и риск из отчёта о позициях"),
])
def test_short_words_and_the_screen_that_fixes_it(reason, kind, short, fix):
    w = coverage.explain(reason, kind, None, None)
    assert w["short"] == short and w["fix"]["label"] == fix and w["fix"]["href"].startswith("../settings.html#")


def test_an_unknown_core_code_still_says_it_is_core():
    key, why, _ = coverage.cause("core_something_new", "IRS", None, None)
    assert key == "core_something_new" and why.startswith("ядро отказало") and "something_new" in why


def test_the_unvalued_position_carries_the_words():
    r = {"position_id": "D|S1", "book_id": "D", "secid": None, "quantity": None,
         "reason": "core_missing_market_data: no RUONIA fixing on or before 2026-04-10"}
    x = {"asset_class": "otc", "ccy": "RUB", "x": {"x_product_group": "irs", "x_product": "Swap"}}
    p = concepts_run._not_valued(r, {}, x, None, False)
    w = p["extra"]["why"]
    assert p["notValued"] and w["key"] == "core_missing_market_data" and w["detail"].startswith("нет фиксинга RUONIA")
    assert "why" in concepts_run.EXTRA_KEYS_READ


def test_a_period_over_a_hole_of_the_stored_history_in_words():
    """TDM#590 re-review: core refuses a period over a hole of the stored RUONIA (its calendar starts after the hole):
    the card says so, with the hole of the run when known, and the fix is «История прогонов»."""
    reason = ("core_calendar_out_of_range: 2025-11-05 is outside calendar RUONIA_PUB(RU_PRODUCTION@ab, year-end from "
              "2026-10-15)+published_from_2025-11-29 (2025-11-29..2079-12-31)")
    w = coverage.explain(reason, "IRS", None, None, {"from": "2025-11-10", "to": "2025-11-28"})
    assert w["detail"] == "в хранимой истории RUONIA пропуск с 10.11.2025 по 28.11.2025: период сделки его пересекает"
    assert w["fix"]["label"] == "История прогонов" and w["short"] == "пропуск фиксингов"
    assert "хранимой истории фиксингов" in w["why"] and "календар" not in w["why"] + w["todo"]   # not the hub's
    w = coverage.explain(reason, "IRS", None, None)  # no hole in the run's lineage: up to the day before
    assert w["detail"] == "в хранимой истории RUONIA пропуск по 28.11.2025: период сделки его пересекает"
    lin = {"rates": {"fixings": {"RUONIA": {"calendar_from_fixings": {"hole": {"from": "2025-11-10",
                                                                             "to": "2025-11-28"}}}}}}
    assert coverage.history_hole(lin) == {"from": "2025-11-10", "to": "2025-11-28"}
    assert coverage.history_hole({}) is None


def test_the_diagnostics_show_the_hole_and_what_was_not_used():
    from desk.diagnostics import Scrubber, _rates_lineage

    rates = {"fixings": {"RUONIA": {"rows": 3, "calendar_from_fixings": {"hole": {"from": "2025-11-10",
                                                                               "to": "2025-11-28"}}}},
             "fixings_history": {"codes": {}, "dropped": "the run's fixings failed", "errors": {"SOFR": "bad"}}}
    out = _rates_lineage(rates, Scrubber())
    assert out["fixings_history_hole"] == {"RUONIA": {"from": "2025-11-10", "to": "2025-11-28"}}
    assert out["fixings_history_dropped"] and out["fixings_history_errors"] == {"SOFR": "bad"}
