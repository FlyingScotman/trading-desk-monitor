"""DEC18 (owner, 05.11.2026, TDM#588): the OCP of books with futures, on synthetic rows (every number invented).

- Only an FX future is a currency derivative in the OCP: core's product ``fx_future`` or the report's product group
  exactly ``fx_future``. Any other future (on bonds, rates, an equity or an index, a commodity, an option on a future:
  any group with ``future`` in it) has no nominal and no PV in the OCP, whatever its PV in the currency;
- an FX future of the positions report gives its nominal in the base currency; on a cross also the quote leg
  (− nominal x price); a pair against RUB has no RUB leg;
- a book of a reported entity without a row in the report on B (base 0) gets its cash balances (the futures' result
  in the currency) from the cash report, by currency;
- a trade after B in a future: an FX future's nominal in its base currency, no OCP for any other future;
- a line where one derivative is larger than the share of its OCP is marked.
"""

from __future__ import annotations

import pytest
from test_ocp_base_plus_day import BOOKS, POLICY, REPORT, UNITS, B, D, assert_parts_sum, pub
from test_positions_v1 import ctx, one  # noqa: F401 - the module's market

from desk.publish import tdm_run
from desk.rules import ocp as O
from desk.valuation.fallback import fx_rate

NON_FX = ("bond_future", "ir_future", "equity_future", "index_future", "commodity_future", "future_option",
          "some_new_future")


def report_row(pid, book, group, delta, asset_class="otc"):
    """A row valued at the report's value (no core): its FX delta is its PV in the currency."""
    return {"position_id": pid, "book_id": book, "instr_type": "OTC", "fx_delta": delta,
            "extra": {"reported": {"asset_class": asset_class, "x": {"x_product_group": group},
                                   "value_date": "2026-12-15"}}}


@pytest.mark.parametrize("group", NON_FX)
def test_a_future_not_on_a_currency_has_no_ocp(group):
    row = report_row("D1|F", "D1", group, {"USD": 2_000_000_000.0})
    assert O.future_class(row) == "other"
    comp, amounts, _, note = O.classify(row, (), BOOKS, B, POLICY)
    assert comp is None and amounts == {} and note == O.NON_FX_FUTURE_NOTE
    assert O.derivative_kind(row, ()) is None
    assert O.derivative_lines([row], BOOKS, {}, B, POLICY) == []
    lines, _, notes = O.position_lines([row], BOOKS, {}, B, POLICY)
    assert lines == [] and notes == [f"D1|F: {O.NON_FX_FUTURE_NOTE}"]


def test_only_the_fx_future_group_or_core_product_is_an_fx_future():
    fx = report_row("D1|FX", "D1", "fx_future", {"USD": 1_000_000.0}, "fx")
    assert O.future_class(fx) == "fx" and O.derivative_kind(fx, ()) == "fx_future"
    assert O.classify(fx, (), BOOKS, B, POLICY)[0] == "futures"
    core = {"position_id": "D1|C", "book_id": "D1", "instr_type": "FX", "fx_delta": {"USD": 5.0},
            "extra": {"core": {"product": "fx_future"}, "legs": [{"value_date": "2026-12-15"}]}}
    assert O.future_class(core) == "fx" and O.derivative_kind(core, ()) == "fx_future"
    # an FX option is still an option, an NDF an NDF
    opt = report_row("D1|O", "D1", "fx_option", {"USD": 1.0})
    assert O.future_class(opt) is None and O.derivative_kind(opt, ()) == "fx_option"


def fx_future(ccy, amount, quote):
    return {"asset_class": "fx", "ccy": ccy, "amount_ccy": amount, "value_date": "2026-12-17", "deal_id": "F1",
            "source_instrument_id": "F1", "x": {"x_product_group": "fx_future", "x_prim_cur": ccy, "x_sec_cur": quote}}


def test_an_fx_future_on_a_cross_has_both_legs(ctx):  # noqa: F811
    x = fx_future("EUR", 1_000_000.0, "USD")
    row = tdm_run.direct_row(ctx, "D1", "P1", one(ctx, x), x, None)
    price = fx_rate(ctx, "EUR")[0] / fx_rate(ctx, "USD")[0]
    assert row["fx_delta"] == pytest.approx({"EUR": 1_000_000.0, "USD": -1_000_000.0 * price})
    assert any("quote leg" in a for a in row["extra"]["assumptions"])
    comp, amounts, _, _ = O.classify(row, (), BOOKS, ctx.business_date, POLICY)
    assert comp == "futures" and amounts == pytest.approx(row["fx_delta"])


def test_an_fx_future_against_the_rouble_has_no_rouble_leg(ctx):  # noqa: F811
    x = fx_future("USD", -250_000.0, "RUB")
    row = tdm_run.direct_row(ctx, "D1", "P1", one(ctx, x), x, None)
    assert row["fx_delta"] == {"USD": -250_000.0}


def test_a_book_absent_from_the_report_takes_its_cash_balances():
    # X1 (entity ENT1, reported) has no row on B: base 0 + its cash balance (the futures' result) + its FX future's
    # nominal; its bond future gives nothing
    rows = [report_row("X1|FUT", "X1", "fx_future", {"USD": 400_000.0}, "fx"),
            report_row("X1|BF", "X1", "bond_future", {"USD": 9_000_000_000.0})]
    cash = [{"book_id": "X1", "entity": "ENT1", "ccy": "USD", "balance_ccy": "12500", "ledger": "L1"}]
    r = O.compute_ocp(rows, BOOKS, B, report_rows=REPORT, report_units=UNITS, policy=POLICY, cash=cash)
    x1 = pub(r)[("X1", "USD")]
    assert x1["base_kind"] == "book_absent" and x1["parts"]["balances"] == 12_500.0
    assert x1["parts"]["derivatives"] == 400_000.0 and x1["amount"] == 412_500.0
    assert x1["contributions"]["balances"]["items"][0]["kind"] == "balance"
    assert_parts_sum(r)
    # a reported book keeps its cash in the report's base: no balances term
    assert "balances" not in pub(r)[("D1", "USD")]["parts"]


def trade(tid, group, ccy="USD", amount="1000000", **kw):
    return {"trade_id": tid, "trade_version": 1, "status": "live", "trade_date": D.isoformat(), "book_id": "D1",
            "position_id": f"D1|{tid}", "asset_class": "fx", "side": "buy", "ccy": ccy, "amount_ccy": amount,
            "settle_date": "2026-12-17", "x_product_group": group, **kw}


def test_trades_after_the_base_in_futures():
    pol = POLICY  # the report holds futures (every component but options and NDFs)
    legs = O.trade_flows([trade("T1", "fx_future"), trade("T2", "bond_future"), trade("T3", "index_future"),
                          trade("T4", "fx_future", ccy="RUB", amount="90000000", fx_rate="90", x_prim_cur="USD"),
                          trade("T5", "fx_future", ccy="RUB", amount="90000000", x_prim_cur="USD")],
                         BOOKS, {}, {}, pol, after=B)
    got = {f.trade_id: (f.ccy, f.amount, f.leg) for f in legs.flows}
    assert got == {"T1": ("USD", 1_000_000.0, "fx"), "T4": ("USD", pytest.approx(1_000_000.0), "fx")}
    assert legs.not_valued == ["T5 (fx_future)"]  # an amount in RUB without the rate: not guessed
    # the report without futures: they come from positions
    out = O.trade_flows([trade("T1", "fx_future")], BOOKS, {}, {},
                        O.OcpPolicy(report_components=frozenset(set(O.COMPONENTS) - {"futures"})), after=B)
    assert out.flows == [] and out.not_valued == []


def test_one_derivative_larger_than_the_book_ocp_is_marked():
    # D1 on B: the report's 100k USD, its forward's nominal 500k (no forward among these rows) and two futures that
    # nearly offset: each is several times the line
    rows = [report_row("D1|FUT", "D1", "fx_future", {"USD": 5_000_000.0}, "fx"),
            report_row("D1|FUT2", "D1", "fx_future", {"USD": -4_900_000.0}, "fx")]
    r = O.compute_ocp(rows, BOOKS, B, report_rows=REPORT, report_units=UNITS, policy=POLICY)
    d1 = pub(r)[("D1", "USD")]
    assert d1["amount"] == pytest.approx(700_000.0)
    big = {x["position_id"]: x for x in d1["derivative_share"]}
    assert set(big) == {"D1|FUT", "D1|FUT2"} and big["D1|FUT"]["ratio"] == 7.14
    gap = {g["code"]: g for g in r.gaps}["derivative_share"]
    assert "D1|FUT" in gap["text"]
    # the default share is the book's whole OCP; a line whose derivatives are smaller is not marked
    small = O.compute_ocp([report_row("D1|FUT", "D1", "fx_future", {"USD": 50_000.0}, "fx")], BOOKS, B,
                          report_rows=REPORT, report_units=UNITS, policy=POLICY)
    assert "derivative_share" not in pub(small)[("D1", "USD")]
