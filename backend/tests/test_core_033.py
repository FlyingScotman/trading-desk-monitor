"""R26: what the backend takes from core 0.33 instead of computing it itself, on the golden fixtures of 15.10.2026.

- a floater's accrued interest (``Instrument.accrued``; the backend's ``_floater_accrued`` is gone);
- the G-spread to the KBD whatever the discount curve (``g_spread_curve_id``), a floater's discount margin over its
  projection curve (``discount_margin``), CS01 / the OFZ basis by tenor bucket (``spread_buckets``), one core call for
  the book (``spread_kinds``);
- puts go to core with their exercise windows and core's ``open_put`` horizon chooses (rule F2 applied by core);
- core's ``explain_horizon_pnl`` explains floaters too (``fixings`` / ``end_fixings``), next to the flash.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from rub_fixed_income import RISK_BUCKETS, OfferKind, kbd_effective_yield, solve_z_spread
from test_pnl_compare import _rows

from desk.marketdata.adapter import KBD_CURVE_ID, Offer, core_instrument, put_offers
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.publish import tdm_run
from desk.valuation import bonds
from desk.valuation.bonds import fixed_schedule, floater_rule, next_offer

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
OFZ, CORP, KEY, OFZ_PK = "SU26238RMFS4", "RU000A0JXQ44", "RU000A109XR1", "SU29008RMFS8"


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), "2026-10-15")


@pytest.fixture(scope="module")
def outs(ctx):
    ids = (OFZ, CORP, KEY, OFZ_PK)
    return dict(zip(ids, value(ctx, [(s, 1000.0) for s in ids], fallback=False, second_order=True)))


def test_g_spread_of_a_fixed_bond_from_core_whatever_its_discount_curve(ctx, outs):
    for s in (OFZ, CORP):
        v = outs[s].valuation
        # an OFZ on the KBD (#32), a corporate on RUONIA OIS (core had no G-spread on it before 0.33)
        assert v.discount_curve_id == (KBD_CURVE_ID if s == OFZ else bonds.DISCOUNT_CURVE_ID)
        # MOEX's definition: the effective yield minus the KBD at the Macaulay duration
        g = (v.ytm_pct / 100 - kbd_effective_yield(ctx.kbd, v.macaulay_years)) * 1e4
        assert v.g_spread_bp == pytest.approx(g, abs=1e-6)
        assert v.discount_margin_bp is None


def test_discount_margin_of_floaters(outs):
    key, pk = outs[KEY].valuation, outs[OFZ_PK].valuation
    # a key-rate floater projects on RUB KEY and discounts on RUONIA: its DM (to the projection curve) is not its Z
    assert key.discount_margin_bp is not None and abs(key.discount_margin_bp - key.z_spread_bp) > 1.0
    # an OFZ-PK projects and discounts on RUONIA OIS: one curve, DM = Z
    assert pk.discount_margin_bp == pytest.approx(pk.z_spread_bp, abs=1e-6)
    assert key.g_spread_bp is None and pk.g_spread_bp is None  # no G-spread for a floater


def test_spread_risk_by_bucket_sums_to_the_spread_risk(outs):
    for s, o in outs.items():
        v = o.valuation
        assert set(v.spread_buckets) == set(RISK_BUCKETS)
        assert math.fsum(v.spread_buckets.values()) == pytest.approx(v.spread01_rub, rel=1e-9, abs=1e-9), s
    ofz = outs[OFZ].valuation  # a 2041 bullet: the basis risk along the curve, most of it long, none past 20Y+
    assert ofz.spread_kind == "ofz" and sum(1 for x in ofz.spread_buckets.values() if x) > 2
    assert min(ofz.spread_buckets, key=ofz.spread_buckets.get) in ("10Y", "15Y")


def test_published_row_carries_core_analytics(ctx, outs):
    c = tdm_run.run_content(ctx, [(f"P{i}", "MIXED", o) for i, o in enumerate(outs.values())], None, {})
    rows = {r["instrument_id"]: r for r in c.rows}
    ofz, key = rows[OFZ]["extra"], rows[KEY]["extra"]
    assert ofz["g_spread_bp"] == outs[OFZ].valuation.g_spread_bp and "dm_bp" not in ofz
    assert key["dm_bp"] == outs[KEY].valuation.discount_margin_bp and "g_spread_bp" not in key
    assert math.fsum(ofz["spread_buckets"].values()) == pytest.approx(rows[OFZ]["ofz_spr01"])
    corp = rows[CORP]
    assert math.fsum(corp["extra"]["spread_buckets"].values()) == pytest.approx(corp["cs01"])
    assert "gspread_bp" in corp["extra"]["quote"]  # MOEX's G-spread stays beside core's


def test_published_row_carries_the_z_spread_to_the_kbd(ctx, outs):
    """TDM#232: every RUB bond priced at the day's price has its Z-spread to the KBD beside its Z to the discount curve:
    an OFZ on the KBD its own Z, a corporate on RUONIA OIS its spread to the government curve, core's scalar solver's."""
    c = tdm_run.run_content(ctx, [(f"P{i}", "MIXED", o) for i, o in enumerate(outs.values())], None, {})
    rows = {r["instrument_id"]: r for r in c.rows}
    assert rows[OFZ]["extra"]["z_kbd_bp"] == pytest.approx(outs[OFZ].valuation.z_spread_bp, abs=1e-3)
    for s in (CORP, KEY, OFZ_PK):  # on RUONIA OIS: the Z to the KBD is another number (a floater on projected flows)
        v = outs[s].valuation
        z = solve_z_spread([(f.payment_date, f.amount) for f in v.cashflows], v.dirty_rub, ctx.curve,
                           settlement_date=v.valuation_date, convention=bonds.SPREAD_CONVENTION) * 1e4
        assert rows[s]["extra"]["z_kbd_bp"] == pytest.approx(z, abs=1e-6), s
        assert abs(z - v.z_spread_bp) > 1.0
    assert rows[CORP]["extra"]["z_kbd_bp"] > rows[CORP]["spread_bp"]  # the KBD lies under RUONIA OIS on the fixture


def test_one_core_call_with_the_spread_kind_per_bond(outs):
    (result,) = {id(o.core_result): o.core_result for o in outs.values()}.values()
    kinds = {p.instrument_id: p.spread_kind for p in result.positions}
    assert kinds == {OFZ: "ofz", CORP: "credit", KEY: "credit", OFZ_PK: "ofz"}


def test_floater_accrued_is_core_s(ctx, outs):
    """No MOEX accrued: core's (``Instrument.accrued``) prices the floater, and it is MOEX's figure on the fixture."""
    for s in (KEY, OFZ_PK):
        o = outs[s]
        moex = float(o.quote.accrued_per_bond)
        assert o.valuation.accrued_rub == pytest.approx(moex)
        terms, q = o.terms, dataclasses.replace(o.quote, accrued_per_bond=None)
        r = bonds.bond_request("X", terms, q, q.trade_date, 1000.0)
        (v,) = bonds.value_bonds(ctx.core_market, [r])[0].values()
        assert v.accrued_rub == pytest.approx(moex, abs=0.005), s
        # a published coupon accrues pro rata exactly: no assumption (only a projected or assumed period is flagged)
        assert not any(a.startswith("accrued computed") for a in v.assumptions)
    assert not hasattr(bonds, "_floater_accrued")


def test_puts_go_to_core_with_their_windows_and_core_chooses(ctx):
    t = ctx.static.bonds[CORP]
    bd = ctx.business_date
    (put,) = [o for o in t.offers if o.status == "scheduled" and o.date and o.date > bd]
    closed = Offer(bd + timedelta(days=60), "put", "scheduled", Decimal(100), bd - timedelta(days=1),
                   bd - timedelta(days=20))
    bad = Offer(bd + timedelta(days=90), "put", "past", Decimal(100), None)  # not scheduled: left out
    t2 = dataclasses.replace(t, offers=(*t.offers, closed, bad))
    assert next_offer(t2, bd)[0] == put.date  # rule F2: the earlier put's window has closed
    s = fixed_schedule(t2, bd)
    inst = core_instrument(t2, bd, s.horizon_date, s.offer, coupons=s.coupons)
    assert [o.offer_date for o in inst.offers] == [closed.date, put.date]
    assert inst.offers[0].window_end == closed.window_end and inst.offers[0].window_start == closed.window_start
    assert inst.horizon("open_put", bd) == (put.date, inst.offers[1])  # core skips the closed window
    assert all(o.kind is OfferKind.PUT for o in inst.offers)
    q = ctx.market.quote(t2)
    (v,) = bonds.value_bonds(ctx.core_market, [bonds.bond_request("X", t2, q, q.trade_date, 1.0)])[0].values()
    assert v.horizon_date == put.date and not any(a.startswith("horizon:") for a in v.assumptions)
    # a window ending after the offer date (bad data) is dropped, not refused by core
    odd = Offer(put.date, "put", "scheduled", Decimal(100), put.date + timedelta(days=3), put.date - timedelta(days=5))
    (o,) = put_offers(dataclasses.replace(t, offers=(odd,)), bd, (put.date, Decimal(100), "offer"))
    assert (o.window_start, o.window_end) == (put.date - timedelta(days=5), None)


def test_cap_only_floater_is_described():
    """A floater with a cap and no floor (live static 20.10: a crash of the whole run and of every model price)."""
    t = type("T", (), {"floater_formula": {"benchmark": "KEY_RATE", "spread_bp": "100", "cap_pct": "25",
                                            "floor_pct": None, "status": "curated"},
                       "bond_class": "corporate", "secid": "X"})()
    rule, _, _, desc = floater_rule(t)  # type: ignore[arg-type]
    assert rule.bounds.cap == 0.25 and rule.bounds.floor is None and desc.endswith(", floor — cap 25.00%")


def test_core_explains_floaters_next_to_the_flash(ctx):
    floaters = [o for o in value(ctx, [(KEY, 1000.0), (OFZ_PK, 1000.0)], fallback=False)]
    rows = {r.secid: r for r in _rows(ctx, floaters)}
    assert set(rows) == {KEY, OFZ_PK}
    for r in rows.values():
        assert r.flash and r.core, r.reason  # core 0.33 explains a floater with the fixings of both dates
        assert r.core["total"] == pytest.approx(r.identity_total, abs=0.05)
        assert r.delta["carry"] == pytest.approx(0, abs=0.5)  # the same repricing at today's forwards


def test_history_bench_vectorised_equals_scalar(ctx):
    """``positions_history`` reads the KBD at each bond's term per date in one ``kbd_effective_yields`` call."""
    from desk.marketdata.history import curve_rate, curve_rates_by_day

    d, d0 = ctx.business_date, ctx.business_date - timedelta(days=1)
    got = curve_rates_by_day({d: ctx.kbd, d0: None}, {d: {30, 365, 5000}, d0: {7}})
    for n in (30, 365, 5000):
        assert got[(d, n)] == pytest.approx(curve_rate(ctx.kbd, n / 365.25), abs=1e-12)
    assert got[(d0, 7)] is None
