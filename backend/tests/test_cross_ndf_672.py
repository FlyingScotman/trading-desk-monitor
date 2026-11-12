"""TDM#672: an FX row of the positions report that no trade's terms value (an NDF on a cross, EUR/USD) is taken by the
pair of its report columns (``x_prim_cur`` / ``x_sec_cur``), not as one amount in the row's currency. Synthetic rows on
the golden market of test_positions_v1 (every number invented).

- a pair with the rouble on either side, or no pair: as before, to the bit;
- a cross with both amounts of the far leg: the legs of the row;
- a cross, the row's currency the first of the pair: the nominal and the second leg at the cross of the run's rates;
- a cross, the row's currency another: no FX delta, the warning in the row, the OCP's notes and gap, no PnL;
- the trade price only checks the cross (20 %, either way of the quote), the legs stay;
- the PnL by the rate move agrees with the FX delta.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest
import test_pnl_r10 as R
from test_ocp_base_plus_day import BOOKS, POLICY, B
from test_positions_v1 import ctx, inputs, one, run  # noqa: F401 - the module's market

from desk.pnl import direct
from desk.pnl.reasons import FX_METHOD, NO_PNL_TEXT
from desk.publish import tdm_run
from desk.rules import ocp as O
from desk.valuation import fx_cross
from desk.valuation.fallback import fx_rate
from desk.valuation.positions import plan

NO_TERMS = "без условий сделки: номинал, не PV"


def ndf(ccy, amount, prim, sec, price=None, far=None, group="fx_ndf", **x):
    row = {"asset_class": "fx", "ccy": ccy, "amount_ccy": amount, "value_date": "2027-01-15", "deal_id": "N1",
           "source_instrument_id": "N1",
           "x": {"x_product_group": group, "x_prim_cur": prim, "x_sec_cur": sec, **x}}
    if price is not None:
        row["x"]["x_trade_price"] = price
    if far is not None:
        row["x"]["x_fx_far_prim_amount"], row["x"]["x_fx_far_sec_amount"] = far
    return row


def pub(ctx, x):  # noqa: F811
    return tdm_run.direct_row(ctx, "D1", "D1|N1", one(ctx, x), x, None)


def cross(ctx, base="EUR", quote="USD"):  # noqa: F811
    return fx_rate(ctx, base)[0] / fx_rate(ctx, quote)[0]


# ---- rule 1: the rouble in the pair, or no pair --------------------------------------------------------------------

@pytest.mark.parametrize("ccy, prim, sec", [("USD", "USD", "RUB"), ("CNY", "CNY", "RUB"), ("USD", "RUB", "USD"),
                                            ("CNY", "RUB", "CNY"), ("USD", None, None), ("USD", "USD", None),
                                            ("USD", "USD", "USD")])
def test_a_pair_with_the_rouble_or_no_pair_is_as_before(ctx, ccy, prim, sec):  # noqa: F811
    bare = {k: v for k, v in ndf(ccy, -1_000_000.0, prim, sec).items() if k != "x"}
    bare["x"] = {"x_product_group": "fx_ndf"}
    a, b = pub(ctx, bare), pub(ctx, ndf(ccy, -1_000_000.0, prim, sec))
    assert b["fx_delta"] == a["fx_delta"] == {ccy: -1_000_000.0}
    assert b["extra"]["assumptions"] == a["extra"]["assumptions"]
    assert b["extra"]["fx_delta_basis"] == a["extra"]["fx_delta_basis"]
    assert "ocp_unconfirmed" not in b["extra"]
    assert b["notional"] == a["notional"] and b["mv_rub"] == a["mv_rub"]
    assert fx_cross.cross_row(ctx, one(ctx, ndf(ccy, -1.0, prim, sec)), ndf(ccy, -1.0, prim, sec), ccy, -1.0) is None


def test_the_far_amounts_of_a_pair_with_the_rouble_are_not_read(ctx):  # noqa: F811
    x = ndf("USD", -1_000_000.0, "USD", "RUB", far=(-1_000_000.0, 90_000_000.0))
    assert pub(ctx, x)["fx_delta"] == {"USD": -1_000_000.0}


# ---- rule 2: the amounts of both legs ------------------------------------------------------------------------------

@pytest.mark.parametrize("ccy", ["EUR", "USD"])
def test_a_cross_with_both_far_amounts_takes_them_as_given(ctx, ccy):  # noqa: F811
    x = ndf(ccy, 1_000_000.0, "EUR", "USD", far=(1_000_000.0, -1_080_000.0))
    row = pub(ctx, x)
    assert row["fx_delta"] == {"EUR": 1_000_000.0, "USD": -1_080_000.0}
    assert any(NO_TERMS in a and "ноги из строки отчёта" in a for a in row["extra"]["assumptions"])
    assert "ocp_unconfirmed" not in row["extra"]
    comp, amounts, _, note = O.classify(row, (), BOOKS, B, POLICY)
    assert comp == "ndf" and amounts == row["fx_delta"] and note == NO_TERMS


def test_one_far_amount_is_not_enough(ctx):  # noqa: F811
    x = ndf("EUR", 1_000_000.0, "EUR", "USD")
    x["x"]["x_fx_far_prim_amount"] = 1_000_000.0
    row = pub(ctx, x)
    assert row["fx_delta"]["EUR"] == 1_000_000.0 and "USD" in row["fx_delta"]  # the nominal and the cross leg
    y = ndf("USD", 1_000_000.0, "EUR", "USD")
    y["x"]["x_fx_far_sec_amount"] = -1_080_000.0
    assert "fx_delta" not in pub(ctx, y)  # the unit is not confirmed


# ---- rule 3: the row's currency the first of the pair ------------------------------------------------------------

def test_a_cross_in_the_first_currency_has_the_nominal_and_the_cross_leg(ctx):  # noqa: F811
    row = pub(ctx, ndf("EUR", 1_000_000.0, "EUR", "USD"))
    price = cross(ctx)
    assert row["fx_delta"] == pytest.approx({"EUR": 1_000_000.0, "USD": -1_000_000.0 * price})
    assert any(NO_TERMS in a and "кросс-курсу" in a for a in row["extra"]["assumptions"])
    assert row["extra"]["fx_delta_basis"].startswith("notional")
    comp, amounts, basis, note = O.classify(row, (), BOOKS, B, POLICY)
    assert comp == "ndf" and amounts == pytest.approx(row["fx_delta"]) and note == NO_TERMS and basis == "notional"


def test_a_short_cross_has_the_opposite_signs(ctx):  # noqa: F811
    row = pub(ctx, ndf("EUR", -2_000_000.0, "EUR", "USD"))
    assert row["fx_delta"]["EUR"] == -2_000_000.0 and row["fx_delta"]["USD"] > 0


def test_a_cross_without_the_rate_of_the_second_currency_has_the_nominal_only(ctx):  # noqa: F811
    row = pub(ctx, ndf("EUR", 1_000_000.0, "EUR", "ZWG"))  # a currency the run has no rate of
    assert row["fx_delta"] == {"EUR": 1_000_000.0}
    assert any(NO_TERMS in a and "ZWG" in a and "второй ноги в ОВП нет" in a for a in row["extra"]["assumptions"])
    assert "ocp_unconfirmed" not in row["extra"]


def test_a_cross_forward_that_is_no_ndf_group_is_taken_the_same_way(ctx):  # noqa: F811
    row = pub(ctx, ndf("EUR", 1_000_000.0, "EUR", "USD", group="fx_forward"))
    assert set(row["fx_delta"]) == {"EUR", "USD"}
    assert O.classify(row, (), BOOKS, B, POLICY)[0] == "fx_legs"


def test_an_fx_future_is_not_touched(ctx):  # noqa: F811
    # the future has its own rule (DEC18): a future whose currency is the quote stays one amount, as before
    row = pub(ctx, ndf("USD", 1_000_000.0, "EUR", "USD", group="fx_future"))
    assert row["fx_delta"] == {"USD": 1_000_000.0} and "ocp_unconfirmed" not in row["extra"]
    row = pub(ctx, ndf("EUR", 1_000_000.0, "EUR", "USD", group="fx_future"))
    assert any("FX future" in a for a in row["extra"]["assumptions"])


# ---- rule 4: the row's currency is not the first of the pair ----------------------------------------------------------

def unconfirmed(ctx):  # noqa: F811
    return pub(ctx, ndf("USD", 1_000_000.0, "EUR", "USD"))


def test_a_cross_in_another_currency_has_no_fx_delta_and_says_why(ctx):  # noqa: F811
    row = unconfirmed(ctx)
    assert "fx_delta" not in row and "fx_delta_basis" not in row["extra"]
    u = row["extra"]["ocp_unconfirmed"]
    assert u["pair"] == "EUR/USD" and u["ccy"] == "USD" and u["reason"] == fx_cross.UNCONFIRMED
    assert "не совпадает с первой валютой пары EUR/USD" in u["text"] and "единица номинала не подтверждена" in u["text"]
    assert "не включено до загрузки условий сделки" in u["text"]
    assert u["text"] in row["extra"]["assumptions"]


def test_the_ocp_says_it_in_the_notes_and_in_a_gap(ctx):  # noqa: F811
    row = unconfirmed(ctx)
    row["position_id"], row["book_id"] = "D1|N1", "D1"
    comp, amounts, _, note = O.classify(row, (), BOOKS, B, POLICY)
    assert comp is None and amounts == {} and note == row["extra"]["ocp_unconfirmed"]["text"]
    lines, _, notes = O.position_lines([row], BOOKS, {}, B, POLICY)
    assert lines == [] and notes == [f"D1|N1: {note}"]
    g = O.unconfirmed_gap([row])
    assert [(x["code"], x["items"]) for x in g] == [(O.UNCONFIRMED_GAP, ["D1|N1"])] and "EUR/USD" in g[0]["text"]
    res = O.compute_ocp([row], BOOKS, B, policy=POLICY)
    assert [x["code"] for x in res.gaps if x["code"] == O.UNCONFIRMED_GAP] == [O.UNCONFIRMED_GAP]
    assert res.lines == [] and any(note in n for n in res.notes)


def test_a_run_without_such_a_row_has_no_gap(ctx):  # noqa: F811
    assert O.unconfirmed_gap([pub(ctx, ndf("EUR", 1.0, "EUR", "USD")), pub(ctx, ndf("USD", 1.0, "USD", "RUB"))]) == []


def test_the_gap_code_is_in_the_api_model():
    from desk.api.models import OcpGap
    assert OcpGap(code=O.UNCONFIRMED_GAP, text="t", items=["a"]).code == "fx_unit_unconfirmed"


def test_a_row_in_the_rouble_on_a_cross_is_not_included_either(ctx):  # noqa: F811
    row = pub(ctx, ndf("RUB", 1_000_000.0, "EUR", "USD"))
    assert "fx_delta" not in row and row["extra"]["ocp_unconfirmed"]["ccy"] == "RUB"


# ---- the trade price only checks ----------------------------------------------------------------------------------

@pytest.mark.parametrize("factor, inverse, said", [(1.0, False, False), (1.15, False, False), (0.85, False, False),
                                                   (1.0, True, False), (1.15, True, False), (1.3, False, True),
                                                   (0.5, False, True), (2.0, True, True), (3.0, False, True)])
def test_the_trade_price_is_checked_against_the_cross_either_way_of_the_quote(ctx, factor, inverse, said):  # noqa: F811
    c = cross(ctx)
    price = (1 / c if inverse else c) * factor
    plain = pub(ctx, ndf("EUR", 1_000_000.0, "EUR", "USD"))
    row = pub(ctx, ndf("EUR", 1_000_000.0, "EUR", "USD", price=price))
    assert row["fx_delta"] == plain["fx_delta"]  # the legs are the cross's, whatever the price
    assert any("цена сделки" in a and "20%" in a for a in row["extra"]["assumptions"]) is said


def test_the_trade_price_is_checked_beside_the_far_legs_too(ctx):  # noqa: F811
    row = pub(ctx, ndf("EUR", 1_000_000.0, "EUR", "USD", price=cross(ctx) * 2, far=(1e6, -1.1e6)))
    assert row["fx_delta"] == {"EUR": 1e6, "USD": -1.1e6}
    assert any("цена сделки" in a for a in row["extra"]["assumptions"])


# ---- the PnL by the rate move -------------------------------------------------------------------------------------------

PREV_RATES = {"USD": (80.4, R.USD_PREV), "EUR": (90.0, 89.0)}


def two(rows):
    """(prev ctx, ctx, the plan of D, the plan of D-1) of rows ``[(pid, x)]`` on the golden market, USD and EUR moved."""
    from desk.orchestrator import prepare as prep
    c = prep(R.SnapshotDir(R.FIX), R.DATE)
    p = dataclasses.replace(R.shifted_prev(c, R.PREV), market=copy.copy(c.market))
    p.market.business_date = R.PREV
    p.market.fx = lambda ccy: ({"cbr": PREV_RATES[ccy][1], "cbr_next": None, "moex_tom": None,
                                "moex_19": PREV_RATES[ccy][0]} if ccy in PREV_RATES
                               else {"cbr": None, "cbr_next": None, "moex_tom": None})
    book = [(pid, "TSY", None, None) for pid, _ in rows]
    extra = dict(rows)
    inp, prev_inp = R.inputs(book, extra), R.inputs(book, extra, R.PREV)
    bp = plan(c, inp)
    direct.attach(p, c, bp.direct, inp, plan(p, prev_inp).direct, prev_inp.valid_dates)
    return p, c, bp


def move(c, ccy):
    return c.market.fx(ccy)["moex_19"] - PREV_RATES[ccy][0]


def test_the_pnl_of_a_pair_with_the_rouble_is_the_amount_times_the_move_as_before():
    _p, c, bp = two([("U1", ndf("USD", -400.0, "USD", "RUB")), ("U2", ndf("USD", -400.0, "RUB", "USD")),
                    ("U3", ndf("USD", -400.0, None, None))])
    for pid in ("U1", "U2", "U3"):
        pnl = bp.direct[pid].pnl
        assert pnl.method == FX_METHOD and pnl.total == pytest.approx(-400 * move(c, "USD"))
        assert pnl.fx_gross == pnl.total


def test_the_pnl_of_a_cross_is_over_both_legs_like_its_fx_delta():
    _p, c, bp = two([("E1", ndf("EUR", 1_000_000.0, "EUR", "USD")),
                    ("E2", ndf("USD", 1_000_000.0, "EUR", "USD", far=(1_000_000.0, -1_080_000.0)))])
    price = fx_rate(c, "EUR")[0] / fx_rate(c, "USD")[0]
    e1 = bp.direct["E1"].pnl
    assert e1.method == FX_METHOD
    assert e1.total == pytest.approx(1_000_000.0 * move(c, "EUR") + (-1_000_000.0 * price) * move(c, "USD"))
    assert e1.fx_gross == e1.total and "EUR" in e1.note and "USD" in e1.note
    e2 = bp.direct["E2"].pnl
    assert e2.total == pytest.approx(1_000_000.0 * move(c, "EUR") - 1_080_000.0 * move(c, "USD"))


def test_the_pnl_of_a_cross_in_another_currency_is_not_nominal_times_the_move_of_the_other():
    _p, _c, bp = two([("X1", ndf("USD", 1_000_000.0, "EUR", "USD"))])
    o = bp.direct["X1"]
    assert o.pnl is None and o.no_pnl is not None and o.no_pnl.reason == "fx_unit_unconfirmed"
    assert "fx_unit_unconfirmed" in NO_PNL_TEXT and "единица номинала не подтверждена" in NO_PNL_TEXT["fx_unit_unconfirmed"]


def test_a_leg_without_a_rate_on_the_previous_day_is_left_out_and_said():
    p, c, bp = two([("G1", ndf("EUR", 1_000_000.0, "EUR", "USD"))])
    p.market.fx = lambda ccy: ({"cbr": 89.0, "cbr_next": None, "moex_tom": None, "moex_19": 90.0} if ccy == "EUR"
                               else {"cbr": None, "cbr_next": None, "moex_tom": None})
    o = bp.direct["G1"]
    x = ndf("EUR", 1_000_000.0, "EUR", "USD")
    pnl = direct._fx_row(p, c, o, x)
    assert pnl.fx_gross == pytest.approx(1_000_000.0 * move(c, "EUR"))
    assert "no rate of USD" in pnl.note and pnl.assumption["code"] == "fx_rate_carried"


def test_the_history_day_carries_the_gap_for_the_screen():
    from desk.api.ocp_history import _unconfirmed
    assert _unconfirmed({}) == {} and _unconfirmed({"gaps": [{"code": "no_report", "text": "t", "items": []}]}) == {}
    g = {"code": O.UNCONFIRMED_GAP, "text": "t", "items": ["D1|N1"]}
    assert _unconfirmed({"gaps": [{"code": "no_report", "text": "x", "items": []}, g]}) \
        == {"unconfirmed": {"text": "t", "items": ["D1|N1"]}}


# ---- review: rows with trade terms, the far amounts, the report's own currency ---------------------------------------------

def test_the_no_terms_reason_is_positions_own():
    from desk.valuation.positions import NO_TERMS
    assert fx_cross.NO_TERMS_ROW == NO_TERMS


@pytest.mark.parametrize("reason", ["no_foreign_curve", "no_fx_spot", "ndf_unsupported", "terms_invalid", "settled"])
@pytest.mark.parametrize("ccy", ["EUR", "USD"])
def test_a_row_with_terms_core_could_not_value_is_not_this_rule(ctx, reason, ccy):  # noqa: F811
    x = ndf(ccy, 1_000_000.0, "EUR", "USD")
    o = one(ctx, x)
    o.core_not_valued = (reason, "d")  # the row's terms exist: the unit of its amount is not in question
    assert fx_cross.cross_row(ctx, o, x, ccy, 1_000_000.0) is None
    row = tdm_run.direct_row(ctx, "D1", "D1|N1", o, x, None)
    assert row["fx_delta"] == {ccy: 1_000_000.0} and "ocp_unconfirmed" not in row["extra"]
    assert O.unconfirmed_gap([{**row, "position_id": "D1|N1"}]) == []


def test_a_row_the_terms_do_not_name_is_this_rule(ctx):  # noqa: F811
    x = ndf("EUR", 1_000_000.0, "EUR", "USD")
    o = one(ctx, x)
    for nv in (None, (fx_cross.NO_TERMS_ROW, "no trade_terms row names this position")):
        o.core_not_valued = nv
        assert fx_cross.cross_row(ctx, o, x, "EUR", 1_000_000.0) is not None


def test_the_pnl_of_a_settled_cross_with_terms_is_not_lost():
    p, c, bp = two([("S1", ndf("USD", 1_000_000.0, "EUR", "USD"))])
    o = bp.direct["S1"]
    o.core_not_valued = ("settled", "")
    pnl = direct._fx_row(p, c, o, ndf("USD", 1_000_000.0, "EUR", "USD"))
    assert pnl.method == FX_METHOD and pnl.total == pytest.approx(1_000_000.0 * move(c, "USD"))


@pytest.mark.parametrize("far", [(1e6, 1.08e6), (-1e6, -1.08e6)])
def test_far_amounts_of_one_sign_give_legs_of_opposite_signs_and_say_so(ctx, far):  # noqa: F811
    row = pub(ctx, ndf("EUR", far[0], "EUR", "USD", far=far))
    assert row["fx_delta"] == {"EUR": far[0], "USD": -far[1]}
    assert any("знаки сумм в строке совпадают" in a for a in row["extra"]["assumptions"])


def test_far_amounts_of_opposite_signs_are_taken_without_a_remark(ctx):  # noqa: F811
    row = pub(ctx, ndf("EUR", 1e6, "EUR", "USD", far=(1e6, -1.08e6)))
    assert row["fx_delta"] == {"EUR": 1e6, "USD": -1.08e6}
    assert not any("знаки сумм" in a for a in row["extra"]["assumptions"])


@pytest.mark.parametrize("far", [(1e6, 0.0), (0.0, 1.08e6), (0.0, 0.0)])
@pytest.mark.parametrize("ccy", ["EUR", "USD"])
def test_a_zero_far_amount_is_no_amount(ctx, far, ccy):  # noqa: F811
    row = pub(ctx, ndf(ccy, 1_000_000.0, "EUR", "USD", far=far))
    if ccy == "EUR":  # the rule of the first currency: the nominal and the cross leg
        assert set(row["fx_delta"]) == {"EUR", "USD"} and row["fx_delta"]["EUR"] == 1_000_000.0
        assert not any("ноги из строки" in a for a in row["extra"]["assumptions"])
    else:
        assert "fx_delta" not in row and row["extra"]["ocp_unconfirmed"]["ccy"] == "USD"


def test_the_warning_names_the_report_rows_own_currency_when_it_is_shown_in_roubles(ctx):  # noqa: F811
    x = ndf("USD", 1_000_000.0, "EUR", "USD")
    o = one(ctx, x)
    o.valuation = dataclasses.replace(o.valuation, face_rub=None)  # no rate of the row's currency: shown in RUB
    row = tdm_run.direct_row(ctx, "D1", "D1|N1", o, x, None)
    assert row["ccy"] == "RUB" and row["extra"]["ocp_unconfirmed"]["ccy"] == "USD"
    assert "(USD)" in row["extra"]["ocp_unconfirmed"]["text"]
