"""Slice S4 on the golden fixtures: floaters, perpetuals and the quality flags of every rung below exact."""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from rub_fixed_income import BondCashflow, ObservationShift

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.valuation.bonds import Rejected, bond_request, fixed_schedule

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
OFZ_PK, KEY, VTB, ALFA, OFZ = "SU29008RMFS8", "RU000A109XR1", "RU000A1034P7", "RU000A100GW5", "SU26238RMFS4"


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), "2026-10-15")


@pytest.fixture(scope="module")
def outs(ctx):
    secids = (OFZ, OFZ_PK, KEY, VTB, ALFA)
    return dict(zip(secids, value(ctx, [(s, 1.0) for s in secids], position_ids=["P1", "P2", "P3", "P4", "P5"])))


def test_calendars_extend_by_rule_and_say_where(ctx):
    cal, _, valid_to = ctx.static.business_calendar("RU_PRODUCTION")
    assert valid_to == date(2079, 12, 31) and not cal.is_business_day(date(2030, 1, 8))
    assert not cal.is_business_day(date(2031, 11, 4)) and cal.is_business_day(date(2031, 11, 5))
    target, _, _ = ctx.static.business_calendar("TARGET")
    assert not target.is_business_day(date(2030, 4, 22)) and not target.is_business_day(date(2030, 4, 19))  # Easter
    prov = ctx.lineage()["calendar_provisional_from"]
    assert prov["RU_PRODUCTION"] == "2028-01-01" and prov["MOEX_STOCK"] == "2027-01-01"  # the hub's own, earlier
    assert set(ctx.rates) == {"ruonia_ois", "key_rate", "fixings", "status", "codes", "coverage"}
    assert ctx.rates["status"] == {"ruonia_ois": "ok", "key_rate_swap": "ok", "fixings": "ok"} and ctx.rates["codes"] == []
    assert ctx.rates["coverage"] == {"ruonia_ois": {"present": ["otc_monitor", "ratescalcs"], "missing": []},
                                     "key_rate_swap": {"present": ["otc_monitor", "ratescalcs"], "missing": []}}


def test_fixed_bonds_stay_exact_and_results_carry_the_book_ids(outs):
    o = outs[OFZ]
    assert (o.valuation.quality, o.valuation.assumptions) == ("exact", ())
    assert [p.position_id for p in o.core_result.positions] == ["P1", "P2"]  # one OFZ call: OFZ-PD and OFZ-PK


def test_ofz_pk_in_advance_is_valued_on_its_fixed_current_coupon(outs):
    v = outs[OFZ_PK].valuation
    assert v.quality == "assumed"
    assert v.assumptions == ("formula inferred: RUONIA + 140 bp, arithmetic, 6 months in advance, ACT/365F",)
    # The running coupon was fixed in advance at 15.54 %: MOEX's figure is cash; later ones are projected.
    assert v.cashflows[0] == BondCashflow(date(2027, 4, 7), 77.49) and v.cashflows[1].amount != 77.49
    assert v.accrued_rub == 3.41 and v.horizon_date == date(2029, 10, 3)
    assert 0 < -v.dv01_rub < 0.1  # a floater: about one coupon period of rate risk per bond


def test_key_rate_floater_projects_off_the_key_rate_curve(ctx, outs):
    o = outs[KEY]
    v, s = o.valuation, fixed_schedule(o.terms, ctx.business_date)
    rule, fixing_id, curve_id = s.floater
    assert (fixing_id, curve_id, rule.index, rule.margin) == ("KEY_RATE", "RUB_KEY_RATE_PROJ", "key_rate", 0.0175)
    assert rule.observation_shift == ObservationShift(6, "calendar_days", "observation")
    assert v.assumptions == ("formula inferred: KEY_RATE + 175 bp, arithmetic, 6 calendar days back, ACT/365F",)
    assert v.cashflows[0] == BondCashflow(date(2026, 10, 19), 110.36)  # the published current coupon 10.36 + amortization 100
    assert 800 * 0.15 * 30 / 365 < v.cashflows[1].amount < 800 * 0.17 * 30 / 365  # key rate 14 % path + 1.75 %
    assert v.accrued_rub == 8.98  # MOEX's; ours (10.36 * 26/30) agrees, so no mismatch flag


def test_floater_without_moex_accrued_is_computed_and_flagged(ctx, outs):
    o = outs[KEY]
    q = dataclasses.replace(o.quote, accrued_per_bond=None)
    (v,) = value_one(ctx, o.terms, q)
    assert v.accrued_rub == 8.98
    assert v.assumptions[-1] == "accrued computed: no MOEX accrued"  # the published 10.36 pro rata


def test_accrued_mismatch_keeps_the_valuation(ctx, outs):
    o = outs[OFZ]
    q = dataclasses.replace(o.quote, accrued_per_bond=o.quote.accrued_per_bond + Decimal("0.42"))
    (v,) = value_one(ctx, o.terms, q)
    assert v.quality == "assumed" and v.assumptions == ("accrued mismatch vs MOEX -0.42 RUB (ours 26.06)",)
    assert v.accrued_rub == pytest.approx(26.48)  # valued on MOEX's accrued, as always


def value_one(ctx, terms, quote):
    from desk.valuation.bonds import value_bonds

    valued, _ = value_bonds(ctx.core_market, [bond_request("x", terms, quote, ctx.business_date, 1.0)])
    return tuple(valued.values())


@pytest.mark.parametrize("secid,call,rate", [(VTB, date(2031, 11, 18), "10"), (ALFA, date(2031, 12, 13), "15")])
def test_perpetual_is_valued_to_an_assumed_call(outs, secid, call, rate):
    o = outs[secid]
    v, row = o.valuation, next(p for p in o.core_result.positions if p.instrument_id == secid)
    assert v.assumptions == (f"perpetual: call assumed {call.isoformat()}", f"future coupons assumed at {rate} %")
    assert (v.horizon_kind, v.horizon_date, row.horizon) == ("call", call, "call")
    assert row.exercise_assumption.startswith(f"exercise:to_call:{call.isoformat()}:coupons-")
    assert v.cashflows[-1].payment_date == call and v.cashflows[-1].amount > float(o.terms.face_value)
    assert 15 < v.ytm_pct < 30  # a deep-discount perpetual to five years, not to its next coupon


def test_unsupported_benchmark_is_rejected_with_its_name(ctx):
    t = ctx.static.bonds[KEY]
    zcyc = dataclasses.replace(t, floater_formula={**t.floater_formula, "benchmark": "ZCYC", "benchmark_raw": "ZR"})
    with pytest.raises(Rejected) as e:
        fixed_schedule(zcyc, ctx.business_date)
    assert (e.value.reason, e.value.detail) == ("floater_benchmark_unsupported", "benchmark ZCYC (ZR)")


def test_floater_formula_fields_override_the_defaults(ctx):
    t = ctx.static.bonds[KEY]
    f = {**t.floater_formula, "lag_days": 4, "averaging": "compounded", "floor_pct": "5", "status": "curated"}
    s = fixed_schedule(dataclasses.replace(t, floater_formula=f), ctx.business_date)
    rule = s.floater[0]
    assert rule.observation_shift.days == 4 and rule.averaging == "compounded" and rule.bounds.floor == 0.05
    assert s.assumptions == ()  # curated: no inferred formula
    (v,) = value_one(ctx, dataclasses.replace(t, floater_formula=f), ctx.market.quote(t))
    assert (v.quality, v.assumptions) == ("exact", ())  # the floor is far from the ~15.75 % coupon: intrinsic


def test_floater_risk_stays_per_curve(ctx):
    """R8 with RUONIA discounting (20.10): a fixed OFZ has its DV01 on RUB RUONIA; an OFZ-PK on RUONIA projects and
    discounts on the same curve (one factor: a short rate risk to its next reset); a key-rate floater's discount
    (RUONIA) and projection (KEY) curves are separate factors, with their own buckets and their cross terms in the
    Hessian; the totals are their sums."""
    o_ofz, o_pk, o_key = value(ctx, [(s, 1000.0) for s in (OFZ, OFZ_PK, KEY)], second_order=True)
    assert set(o_ofz.valuation.risk()) == {"RUB RUONIA"}
    pk = o_pk.valuation
    assert set(pk.risk()) == {"RUB RUONIA"} and abs(pk.dv01_rub) < 0.5 * abs(pk.spread01_rub)
    assert {f.split("|")[1] for a, b, _ in pk.hessian for f in (a, b) if f.startswith("IR|")} == {"RUB RUONIA"}
    v = o_key.valuation
    risk = v.risk()
    assert set(risk) == {"RUB RUONIA", "RUB KEY"}
    disc, fwd = sum(risk["RUB RUONIA"].values()), sum(risk["RUB KEY"].values())
    assert disc < 0 < fwd  # the basis: two legs, a small net
    assert v.dv01_rub == pytest.approx(disc + fwd, rel=1e-12)
    assert all(v.buckets[b] == pytest.approx(risk["RUB RUONIA"][b] + risk["RUB KEY"][b]) for b in v.buckets)
    curves = {f.split("|")[1] for a, b, _ in v.hessian for f in (a, b) if f.startswith("IR|")}
    assert curves == {"RUB RUONIA", "RUB KEY"} and all(a <= b for a, b, _ in v.hessian)
    assert any(a.split("|")[1] != b.split("|")[1] for a, b, _ in v.hessian if "CS" not in (a, b))  # cross terms


def test_run_publishes_the_rates_curves(ctx):
    from desk.publish import tdm_run

    outs = value(ctx, [(s, 1000.0) for s in (OFZ, OFZ_PK, KEY)], second_order=True)
    c = tdm_run.run_content(ctx, [(f"P{i}", "OFZ", o) for i, o in enumerate(outs)], None, {})
    curves = {x["curve_id"]: x for x in c.reference["curves"]}
    assert list(curves) == ["RUB OFZ", "RUB RUONIA", "RUB KEY"]  # the KBD stays a market curve, without risk
    for cid in ("RUB RUONIA", "RUB KEY"):
        x = curves[cid]
        assert (x["ccy"], x["kind"], len(x["points"])) == ("RUB", "swap", 11)
        assert all(10 < p["rate"] < 25 and p["move_bp"] == 0.0 for p in x["points"])
    # the day's move against the previous date's curves, built the same way (here the same market: 0)
    assert set(tdm_run._prev_rates(ctx, ctx.market)) == {"RUB_RUONIA_OIS", "RUB_KEY_RATE_PROJ"}
    again = tdm_run.run_content(ctx, [(f"P{i}", "OFZ", o) for i, o in enumerate(outs)], ctx.market, {})
    assert again.reference["curves"][1]["points"] == curves["RUB RUONIA"]["points"]
    ofz, pk, key = c.rows
    assert all(r["spread_type"] == "Z" and r["spread_curve"] == "RUB RUONIA" for r in c.rows)
    assert set(ofz["risk"]) == set(pk["risk"]) == {"RUB RUONIA"} and set(key["risk"]) == {"RUB RUONIA", "RUB KEY"}
    assert ofz["cs01"] == 0.0 and ofz["ofz_spr01"] < 0  # the OFZ spread to RUONIA, one number per bond
    assert key["dv01"] == pytest.approx(sum(sum(b.values()) for b in key["risk"].values()))
    assert {f.split("|")[1] for g in key["gamma"] for f in g[:2] if f.startswith("IR|")} == {"RUB RUONIA", "RUB KEY"}
