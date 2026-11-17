"""TDM#762: a fixed-coupon RUB bond's Z-spread to the KBD (``z_kbd_bp``) and to RUONIA OIS (``z_ruonia_bp``).

An OFZ-PD / AD / IN is discounted on the KBD, so its ``spread_bp`` is the spread to the KBD (about nothing) and the pages
called it a spread to RUONIA. The row now carries both spreads, each by core's ``solve_z_spread`` on the same flows and the
same dirty price as ``spread_bp``; the one that is to the bond's own discount curve is ``spread_bp`` itself.

On the golden fixtures of 15.10.2026 (public bonds), and the same date with its RUONIA OIS quotes dropped."""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import pytest
from test_rates_inputs_r14 import cache_without

from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.marketdata.adapter import KBD_CURVE_ID, bond_currency
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.publish import tdm_run
from desk.valuation import bonds

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
OFZ, CORP, KEY, OFZ_PK = "SU26238RMFS4", "RU000A0JXQ44", "RU000A109XR1", "SU29008RMFS8"
PERPETUAL = "RU000A100GW5"  # a fixed-coupon perpetual of the fixture, never called (TDM#35)
Q = 1000.0


def _rows(ctx, ids):
    outs = value(ctx, [(s, Q) for s in ids], fallback=False)
    return {o.secid: (o, tdm_run.position_row(ctx, "B", o.secid, o, "x")) for o in outs if o.valuation is not None}


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


@pytest.fixture(scope="module")
def rows(ctx):
    return _rows(ctx, (OFZ, CORP, KEY, OFZ_PK, PERPETUAL))


@pytest.fixture(scope="module")
def no_ois(tmp_path_factory):
    c = prepare(SnapshotDir(cache_without(tmp_path_factory.mktemp("no_ois"), "ruonia_ois")), DATE)
    return c, _rows(c, (OFZ, CORP, PERPETUAL))


def test_an_ofz_pd_has_its_own_spread_to_the_kbd_and_another_to_ruonia(ctx, rows):
    o, r = rows[OFZ]
    v = o.valuation
    assert o.terms.bond_class in bonds.OFZ_KBD_CLASSES and v.discount_curve_id == KBD_CURVE_ID
    assert r["spread_curve"] == "RUB OFZ" and r["z_kbd_bp"] == r["spread_bp"] == v.z_spread_bp
    # the spread to RUONIA OIS is a solve of its own on the run's RUONIA curve, not the KBD one under another name
    assert r["z_ruonia_bp"] is not None and abs(r["z_ruonia_bp"] - r["spread_bp"]) > 1.0
    z = bonds.z_spread_to(v, RUONIA_OIS_CURVE_ID, ctx.z_curves)
    assert z == r["z_ruonia_bp"]
    # it reprices the dirty price on RUONIA OIS: the core solve with the run's own convention, flows and price
    from rub_fixed_income import solve_z_spread
    flows = [(c.payment_date, c.amount) for c in v.cashflows]
    again = solve_z_spread(flows, v.dirty_rub, ctx.z_curves[RUONIA_OIS_CURVE_ID], settlement_date=v.valuation_date,
                           convention=bonds.SPREAD_CONVENTION) * 1e4
    assert r["z_ruonia_bp"] == pytest.approx(again, abs=1e-9)
    # and the spread to the KBD, solved apart, is the one core calibrated
    assert bonds.z_spread_to(dataclasses.replace(v, discount_curve_id=RUONIA_OIS_CURVE_ID), KBD_CURVE_ID,
                             ctx.z_curves) == pytest.approx(v.z_spread_bp, abs=1e-6)


def test_a_corporate_has_its_ruonia_spread_as_the_spread_and_another_to_the_kbd(ctx, rows):
    o, r = rows[CORP]
    v = o.valuation
    assert v.discount_curve_id == RUONIA_OIS_CURVE_ID and r["spread_curve"] == "RUB RUONIA"
    assert r["z_ruonia_bp"] == r["spread_bp"] == v.z_spread_bp
    assert r["z_kbd_bp"] is not None and r["z_kbd_bp"] != r["spread_bp"]
    # the solve reproduces core's own spread to the bit of its tolerance: the same flows, price and curve
    moved = dataclasses.replace(v, discount_curve_id=KBD_CURVE_ID)
    assert bonds.z_spread_to(moved, RUONIA_OIS_CURVE_ID, ctx.z_curves) == pytest.approx(v.z_spread_bp, abs=1e-6)


def test_a_perpetual_is_solved_with_its_tail(ctx, rows):
    o, r = rows[PERPETUAL]
    v = o.valuation
    assert v.perpetual_tail is not None and r["z_kbd_bp"] is not None and r["z_ruonia_bp"] is not None
    own = RUONIA_OIS_CURVE_ID if v.discount_curve_id == RUONIA_OIS_CURVE_ID else KBD_CURVE_ID
    other = KBD_CURVE_ID if own == RUONIA_OIS_CURVE_ID else RUONIA_OIS_CURVE_ID
    assert r["z_ruonia_bp" if own == RUONIA_OIS_CURVE_ID else "z_kbd_bp"] == v.z_spread_bp
    assert bonds.z_spread_to(dataclasses.replace(v, discount_curve_id=other), own, ctx.z_curves) \
        == pytest.approx(v.z_spread_bp, abs=1e-6)


def test_a_floater_has_neither(rows):
    for s in (KEY, OFZ_PK):
        o, r = rows[s]
        assert o.terms.coupon_type == "floating"
        assert r["z_kbd_bp"] is None and r["z_ruonia_bp"] is None
        assert r["spread_bp"] == o.valuation.z_spread_bp  # the floater's Z-spread to RUONIA stays where it was


def test_without_ruonia_ois_no_spread_to_it_is_made_up(no_ois):
    c, rs = no_ois
    assert RUONIA_OIS_CURVE_ID not in c.z_curves
    for s in (OFZ, CORP, PERPETUAL):
        o, r = rs[s]
        assert o.valuation.discount_curve_id == KBD_CURVE_ID
        assert r["z_ruonia_bp"] is None
        assert r["z_kbd_bp"] == r["spread_bp"]  # on that date every RUB bond is discounted on the KBD


def test_a_row_off_rub_or_without_a_spread_has_none(ctx, rows):
    o, _ = rows[OFZ]
    v = o.valuation
    curves = ctx.z_curves
    assert bonds.z_spread_to(dataclasses.replace(v, z_spread_bp=math.nan), RUONIA_OIS_CURVE_ID, curves) is None
    assert bonds.z_spread_to(dataclasses.replace(v, cashflows=()), RUONIA_OIS_CURVE_ID, curves) is None
    assert bonds.z_spreads(dataclasses.replace(v, fx_rate=("CNY", 12.0)), curves) == (None, None)
    assert bonds.z_spread_to(v, "no-such-curve", curves) is None
    # a price no spread reproduces is a missing figure, not an error of the run
    assert bonds.z_spread_to(dataclasses.replace(v, dirty_rub=1e-9), RUONIA_OIS_CURVE_ID, curves) is None
    for r in (rows[x][1] for x in rows if bond_currency(rows[x][0].terms) != "RUB"):
        assert r["z_kbd_bp"] is None and r["z_ruonia_bp"] is None
