"""Slice S4 (core portfolio path) on the hub's golden fixtures: the mapping to core types, the
coupon checks the backend keeps, and the stored hash-verified core result."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest
from rub_fixed_income import (
    AccrualBasis,
    FixedCouponLeg,
    OfferKind,
    RateAccrualCouponLeg,
    portfolio_result_from_json,
)

from desk.marketdata.adapter import core_instrument
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.publish import tdm_run
from desk.valuation.bonds import fixed_schedule

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
OFZ, CORP = "SU26238RMFS4", "RU000A0JXQ44"  # OFZ-PD bullet; RZD fixed with a scheduled put


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


def _instrument(ctx, secid):
    t = ctx.static.bonds[secid]
    s = fixed_schedule(t, ctx.business_date)
    return t, s, core_instrument(t, ctx.business_date, s.horizon_date, s.offer)


def test_ofz_maps_to_a_fixed_coupon_leg(ctx):
    t, s, inst = _instrument(ctx, OFZ)
    assert isinstance(inst.coupon_leg, FixedCouponLeg)
    assert inst.settlement_lag_days == 0  # F9: core settles on the trade date of the MOEX accrued
    assert inst.offers == () and inst.maturity_date == t.maturity_date == s.horizon_date
    assert inst.initial_face == pytest.approx(float(t.face_value))
    known = [c for c in t.coupons if c.pay_date > ctx.business_date]
    assert [p.amount for p in inst.coupon_leg.periods] == [float(c.amount_per_bond) for c in known]


def test_corporate_maps_to_a_rate_accrual_leg_with_its_put(ctx):
    t, s, inst = _instrument(ctx, CORP)
    leg = inst.coupon_leg
    assert isinstance(leg, RateAccrualCouponLeg) and leg.accrual_basis is AccrualBasis.RATE_ACT_365
    coupons = {c.pay_date: c for c in t.coupons}
    assert all(p.rate == pytest.approx(float(coupons[p.payment_date].rate_pct) / 100) for p in leg.periods)
    assert all(p.payment_date <= s.horizon_date for p in leg.periods)
    (offer,) = inst.offers  # only the offer the F2 rule chose
    assert offer.kind is OfferKind.PUT and offer.confirmed and offer.offer_date == s.horizon_date
    o = value(ctx, [(CORP, 1.0)])[0]
    v = o.core_result.positions[0]
    assert v.horizon == "confirmed_put" and v.horizon_date == s.horizon_date
    assert o.valuation.cashflows[-1].payment_date == s.horizon_date


def test_coupon_gaps_are_assumed_and_flagged(ctx):
    t = ctx.static.bonds[CORP]
    cps = [c for c in t.coupons if c.pay_date > ctx.business_date]
    nxt, later = cps[0], cps[1:]
    no_amount = dataclasses.replace(t, coupons=tuple(dataclasses.replace(c, amount_per_bond=None) if c == nxt else c
                                                     for c in t.coupons))
    s = fixed_schedule(no_amount, ctx.business_date)
    filled = next(c for c in s.coupons if c.no == nxt.no)
    days = (filled.pay_date - filled.start).days
    assert float(filled.amount_per_bond) == pytest.approx(round(float(nxt.rate_pct) * 10 * days / 365, 2), abs=0.005)
    assert s.assumptions == ("coupon amount from rate x face x days/365: 1 coupon(s)",)
    unknown = dataclasses.replace(t, coupons=tuple(dataclasses.replace(c, status="unknown", rate_pct=None,
                                                                       amount_per_bond=None) if c in later else c
                                                   for c in t.coupons))
    s = fixed_schedule(unknown, ctx.business_date)
    assumed = [c for c in s.coupons if c.status == "assumed"]
    assert assumed and all(c.rate_pct == nxt.rate_pct for c in assumed)
    assert s.assumptions == (f"future coupons assumed at {nxt.rate_pct.normalize():f} %",)
    o = value(ctx, [(CORP, 1.0)])[0]
    assert (o.valuation.quality, o.valuation.assumptions) == ("exact", ())


def test_one_core_call_per_spread_kind(ctx):
    outs = value(ctx, [(OFZ, 10.0), (CORP, 5.0), (OFZ, -3.0)])
    assert all(o.valuation for o in outs)
    ofz_result, corp_result = outs[0].core_result, outs[1].core_result
    assert outs[2].core_result is ofz_result and corp_result is not ofz_result
    assert len(ofz_result.positions) == 2 and ofz_result.coverage.status == "complete"
    assert outs[0].valuation.pv_rub == pytest.approx(outs[2].valuation.pv_rub * -10 / 3)


def test_core_result_is_stored_hash_verified(ctx, tmp_path):
    outs = value(ctx, [(OFZ, 10.0), (CORP, 5.0)], second_order=True)
    db = tmp_path / "tdm.sqlite"
    run_id, status, _ = tdm_run.publish(str(db), ctx, [("P1", "OFZ", outs[0]), ("P2", "CORP", outs[1])], prev=None)
    assert status == "complete"
    lineage = json.loads(sqlite3.connect(db).execute("SELECT comment FROM calc_run WHERE run_id=?",
                                                     (run_id,)).fetchone()[0])
    stored = lineage["core_results"]
    assert {s["result_sha256"] for s in stored} == {o.core_result.result_sha256 for o in outs}
    for s in stored:
        path = Path(s["path"])
        assert path.parent == tmp_path / "results" and path.name == f"{s['result_sha256']}.json"
        r = portfolio_result_from_json(path.read_bytes())  # raises unless the hash verifies
        assert r.result_sha256 == s["result_sha256"] and r.schema_version == "rub-portfolio-risk-result.v2"
    with pytest.raises(ValueError):
        portfolio_result_from_json(path.read_bytes().replace(b'"pv_rub":', b'"pv_rub":1', 1))
