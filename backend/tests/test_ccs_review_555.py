"""TDM#555, review of the branch: a trade two rows name, a row core refused, the CCS's curve, its OCP amount.

On the hub's contract 1.4 golden snapshot of 15.10.2026 (``fixtures/mdhub_v1_4``) and synthetic trades (invented
amounts and rates)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from test_swaps_repo_tdm45 import BD, FIX, TERMS, ccs, ccs_plan, leg, without_xccy

from desk.api import coverage
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.publish import tdm_run
from desk.rules import ocp as O
from desk.store import PositionInputs
from desk.valuation import derivatives as dv
from desk.valuation import positions as pv1


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), BD.isoformat())


def plan_of(ctx, extra, terms):
    inp = PositionInputs([(p, "IRB", None, None) for p in extra], [], {"IRB": BD}, datetime.now(UTC), "0" * 64,
                         extra, trade_terms=terms)
    return pv1.plan(ctx, inp), inp


# --- one deal on two rows ----------------------------------------------------------------------------------------


def test_a_ccs_given_as_two_rows_with_one_deal_id_is_valued_in_neither(ctx):
    """A report may give one CCS as two rows (its RUB and its CNY leg) with the same deal_id: each would take both legs
    and be valued as the whole swap, twice in PV, risk and OCP. Neither is valued; the plan says which."""
    t = ccs("C7", "pay", 1_250_000_000, 100_000_000, index_code="RUONIA")
    ex = {"C7-RUB": {"asset_class": "otc", "ccy": "RUB", "deal_id": "C7", "mv_rub": 5.0},
          "C7-CNY": {"asset_class": "otc", "ccy": "CNY", "deal_id": "C7"}}
    bp, _ = plan_of(ctx, ex, t)
    for pid in ex:
        o = bp.direct[pid]
        assert o.core_not_valued[0] == "terms_ambiguous" and "C7" in o.core_not_valued[1], pid
        assert o.valuation is None or o.valuation.extra.get("core") is None
        assert not o.valuation or not o.valuation.fx_delta  # no CNY delta twice
    assert bp.ambiguous_terms == ["C7-CNY: C7", "C7-RUB: C7"]
    assert bp.unlinked_rates == []  # the trade is named, by two rows: not «no row names it»
    why, todo = coverage.cause("terms_ambiguous", "CCS", None, None)[1:]
    assert "нескольк" in why and todo


def test_a_swap_given_as_two_rows_with_one_deal_id_is_valued_in_neither(ctx):
    t = [leg("S1", "fix", "pay", fixed_rate_pct="15.5"), leg("S1", "flt", "receive", index_code="RUONIA")]
    ex = {"S1-A": {"asset_class": "otc", "ccy": "RUB", "deal_id": "S1"},
          "S1-B": {"asset_class": "otc", "ccy": "RUB", "deal_id": "S1"}}
    bp, _ = plan_of(ctx, ex, t)
    assert {pid: bp.direct[pid].core_not_valued[0] for pid in ex} == dict.fromkeys(ex, "terms_ambiguous")
    assert all(bp.direct[pid].valuation is None for pid in ex)  # no mv_rub: not valued at all
    # one row of the deal is valued as before
    bp, _ = plan_of(ctx, {"S1": ex["S1-A"]}, t)
    assert bp.direct["S1"].valuation.extra["core"]["product"] == "ois" and bp.ambiguous_terms == []


def test_legs_naming_their_own_rows_are_not_ambiguous(ctx):
    """An FX swap whose two legs name two rows (``position_id``): each row takes its leg, as before."""
    near = {"trade_id": "W1", "leg_id": "near", "book_id": "IRB", "product_kind": "fx_swap", "trade_date": "2026-10-01",
            "value_date": "2026-10-16", "base_ccy": "CNY", "quote_ccy": "RUB", "side": "buy",
            "notional_base": "1000000", "notional_quote": "12500000", "position_id": "W1-N"}
    far = {**near, "leg_id": "far", "side": "sell", "value_date": "2027-01-19", "notional_quote": "12600000",
           "position_id": "W1-F"}
    ex = {"W1-N": {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 1e6, "deal_id": "W1"},
          "W1-F": {"asset_class": "fx", "ccy": "CNY", "amount_ccy": -1e6, "deal_id": "W1"}}
    bp, _ = plan_of(ctx, ex, [near, far])
    assert bp.ambiguous_terms == []
    assert all(bp.direct[p].valuation.extra["core"]["product"] == "fx_swap" for p in ex)


# --- a row without asset_class that core refused -----------------------------------------------------------------


def test_a_classless_row_core_refused_is_published_as_otc_not_a_bond(ctx):
    """TDM#501 review: the row keeps the report's value and the OTC rule; it is published as a derivative with core's
    reason and the OCP does not take it as a bond."""
    terms = [*TERMS[-2:]]  # B1: a basis swap
    ex = {"B1": {"ccy": "CNY", "deal_id": "B1", "source_instrument_id": "B1", "mv_rub": 1250.0}}
    bp, _ = plan_of(ctx, ex, terms)
    o = bp.direct["B1"]
    row = tdm_run.direct_row(ctx, "IRB", "B1", o, ex["B1"], None)
    assert o.core_not_valued[0] == "basis_swap_unsupported"
    assert row["instr_type"] == "OTC" and row["asset_class"] != "bond" and not row["name"].endswith(tdm_run.FROM_REPORT)
    assert row["extra"]["not_exact_reason"] == "basis_swap_unsupported"
    comp = O.classify(row, terms, {}, BD, O.PROVISIONAL_POLICY)[0]
    assert comp != "bonds"


# --- the CCS's foreign curve -------------------------------------------------------------------------------------


def test_a_ccs_discounts_its_foreign_leg_on_the_xccy_curve_when_the_run_has_it(ctx):
    bp, _ = ccs_plan(ctx, ccs("C1", "pay", 1_250_000_000, 100_000_000, index_code="RUONIA"))
    v = bp.direct["C1"].valuation
    assert set(v.curve_buckets) == {"RUB RUONIA", "CNY XCCY"}
    assert v.extra["core"]["curves"] == ["CNY XCCY", "RUB RUONIA"]
    assert not any("XCCY curve in the run" in a for a in v.assumptions)
    # the XCCY curve carries the 2Y risk, which the FX-implied one (quotes to December) extrapolates
    assert abs(v.curve_buckets["CNY XCCY"]["2Y"]) > 0


def test_without_an_xccy_curve_the_fx_curve_is_used_and_said(ctx):
    bp, _ = ccs_plan(without_xccy(ctx), ccs("C1", "pay", 1_250_000_000, 100_000_000, index_code="RUONIA"))
    v = bp.direct["C1"].valuation
    assert set(v.curve_buckets) == {"RUB RUONIA", "CNY FX"}
    note = next(a for a in v.assumptions if "no CNY XCCY curve in the run" in a)
    assert "CNY FX" in note and "2026-12-17" in note and "extrapolated" in note and "provisional" in note


def test_a_ccs_row_carries_the_anchor_of_its_curve(ctx):
    """The USD-like anchor caveat of a curve is in the assumptions of a swap row as in an FX row's (here: a fake one)."""
    fx = replace(ctx.fx, lineage={**ctx.fx.lineage, "curves": {
        **ctx.fx.lineage["curves"], "CNY XCCY": {**ctx.fx.lineage["curves"]["CNY XCCY"], "anchor": "assumed: test"}}})
    bp, _ = ccs_plan(replace(ctx, fx=fx), ccs("C1", "pay", 1_250_000_000, 100_000_000, index_code="RUONIA"))
    assert "CNY XCCY: assumed: test" in bp.direct["C1"].valuation.assumptions


def test_the_conventions_applied_by_default_are_provisional(ctx):
    bp, _ = ccs_plan(ctx, ccs("C1", "pay", 1_250_000_000, 100_000_000, index_code="RUONIA"))
    assert any("payment lag of a business day" in a and "provisional" in a for a in bp.direct["C1"].valuation.assumptions)


def test_a_spread_on_a_fixed_leg_is_not_ignored(ctx):
    t = ccs("C8", "pay", 1e9, 8e7, fixed_rate_pct="15", spread_bp="25")
    bp, _ = ccs_plan(ctx, t, "C8")
    assert bp.direct["C8"].core_not_valued[0] == "terms_invalid" and "spread_bp" in bp.direct["C8"].core_not_valued[1]


def test_a_maturity_on_the_last_day_of_the_calendar_is_not_a_crash(ctx):
    t = ccs("C9", "pay", 1e9, 8e7, end="2079-12-31", fixed_rate_pct="15")
    bp, _ = ccs_plan(ctx, t, "C9")
    assert bp.direct["C9"].core_not_valued[0] == "no_fx_calendar"


# --- the OCP amount ----------------------------------------------------------------------------------------------


def _ocp(row, terms, basis):
    return O.classify(row, terms, {}, BD, replace(O.PROVISIONAL_POLICY, amount_basis=basis))


def test_a_ccs_in_the_ocp_at_both_bases(ctx):
    t = ccs("C1", "pay", 1_250_000_000, 100_000_000, index_code="RUONIA")  # receives 100m CNY
    bp, inp = ccs_plan(ctx, t)
    o = bp.direct["C1"]
    row = tdm_run.direct_row(ctx, "IRB", "C1", o, inp.extra["C1"], None)
    assert row["extra"]["ocp_amount"] == {"CNY": 1e8}
    comp, amounts, basis, _ = _ocp(row, t, O.AmountBasis.NOMINAL)
    assert (comp, amounts, basis) == ("fx_legs", {"CNY": 1e8}, "nominal")
    comp, amounts, basis, _ = _ocp(row, t, O.AmountBasis.SPOT_EQUIVALENT)
    assert comp == "fx_legs" and amounts == pytest.approx({"CNY": o.valuation.fx_delta["CNY"]})
    # paying CNY: negative
    pay = ccs("C2", "receive", 1_250_000_000, 100_000_000, index_code="RUONIA")
    bp, inp = ccs_plan(ctx, pay, "C2")
    assert bp.direct["C2"].valuation.extra["ocp_amount"] == {"CNY": -1e8}


def test_a_ccs_core_did_not_value_has_its_notional_for_the_ocp(ctx):
    t = ccs("C3", "pay", 1e9, 8e7, fixed_rate_pct="15")
    t[1] = {k: v for k, v in {**t[1], "fixed_rate_pct": None, "index_code": "SHIBOR"}.items() if v is not None}
    ex = {"C3": {"ccy": "RUB", "deal_id": "C3", "source_instrument_id": "C3", "asset_class": "otc", "mv_rub": 1000.0}}
    bp, _ = plan_of(ctx, ex, t)
    o = bp.direct["C3"]
    assert o.core_not_valued[0] == "ccs_foreign_floating_unsupported" and o.ocp_amount == {"CNY": 8e7}
    row = tdm_run.direct_row(ctx, "IRB", "C3", o, ex["C3"], None)
    assert row["extra"]["ocp_amount"] == {"CNY": 8e7}
    for basis in (O.AmountBasis.NOMINAL, O.AmountBasis.SPOT_EQUIVALENT):  # a row without a PV on either basis
        comp, amounts, b, _ = _ocp(row, t, basis)
        assert (comp, amounts, b) == ("fx_legs", {"CNY": 8e7}, "notional"), basis
    assert dv.ccs_ocp_amount(t, BD) == {"CNY": 8e7}
    assert dv.ccs_ocp_amount(t, date(2030, 1, 1)) == {}  # matured


def _valued_ocp(ctx, terms, pid="C1"):
    bp, _ = ccs_plan(ctx, terms, pid)
    return bp.direct[pid].valuation.extra.get("ocp_amount", {})


def test_the_ccs_ocp_amount_is_the_exchanges_still_to_come(ctx):
    """Receiving 100m CNY: no exchange (``none``) is nothing; a swap starting ahead with both exchanges nets to zero
    (the initial one paid, the final one received, as an FX swap's legs); only the final exchange is +100m."""
    base = {"index_code": "RUONIA"}
    cases = {
        "none": (ccs("C1", "pay", 1_250_000_000, 100_000_000, exchange="none", **base), {}),
        "ahead": (ccs("C1", "pay", 1_250_000_000, 100_000_000, start="2026-10-20", end="2027-10-20", **base), {}),
        "final": (ccs("C1", "pay", 1_250_000_000, 100_000_000, exchange="final", **base), {"CNY": 1e8}),
        "seasoned": (ccs("C1", "pay", 1_250_000_000, 100_000_000, **base), {"CNY": 1e8}),
    }
    for name, (t, want) in cases.items():
        assert _valued_ocp(ctx, t) == want, name
        assert dv.ccs_ocp_amount(t, BD) == want, name  # the same from the terms, for a row core did not value
    paying = ccs("C1", "receive", 1_250_000_000, 100_000_000, exchange="final", **base)
    assert _valued_ocp(ctx, paying) == {"CNY": -1e8}
    # ahead, the initial exchange alone (a swap whose final one is not stated away): -100m on the start day side
    assert dv.ccs_exchange_amount("initial_and_final", "receive", 1e8, date(2026, 10, 20), date(2027, 10, 20),
                                  date(2026, 10, 21)) == 1e8


def test_a_book_with_a_valued_and_an_unvalued_ccs_keeps_both_exposures_at_the_default_basis(ctx):
    good = ccs("C1", "pay", 1_250_000_000, 100_000_000, index_code="RUONIA")
    bad = ccs("C3", "pay", 1e9, 8e7, fixed_rate_pct="15")
    bad[1] = {k: v for k, v in {**bad[1], "fixed_rate_pct": None, "index_code": "SHIBOR"}.items() if v is not None}
    ex = {p: {"asset_class": "otc", "ccy": "RUB", "deal_id": p, "source_instrument_id": p, "mv_rub": 1000.0}
          for p in ("C1", "C3")}
    bp, _ = plan_of(ctx, ex, [*good, *bad])
    rows = [tdm_run.direct_row(ctx, "IRB", p, bp.direct[p], ex[p], None) for p in ex]
    terms = {"C1": good, "C3": bad}
    lines, _, _ = O.position_lines(rows, {}, terms, BD)  # the default basis
    got = {ln.position_id: (ln.component, ln.ccy, ln.basis) for ln in lines}
    assert got["C1"][:2] == ("fx_legs", "CNY") and got["C3"] == ("fx_legs", "CNY", "notional")
    assert sum(ln.amount for ln in lines if ln.position_id == "C3") == 8e7
    der = {d.position_id: d for d in O.derivative_lines(rows, {}, terms, BD)}
    assert der["C3"].basis == "notional" and der["C3"].amount == 8e7 and der["C1"].basis == "spot_equivalent"
