"""TDM#401: a floater MOEX labels «known coupons» (``variable_known``) is valued as a floater on the hub's formula."""

from __future__ import annotations

import copy
import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from desk.marketdata.adapter import FORMULA_FLOATER_LABEL, Coupon, StaticView, formula_floater
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.valuation.bonds import Rejected, bond_request, fixed_schedule, value_bonds

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
KEY = "RU000A109XR1"  # a key-rate floater of the fixture (KEY_RATE + 175 bp, inferred)
D = date(2026, 10, 15)
FORMULA = {"benchmark": "KEY_RATE", "spread_bp": "180.0", "status": "inferred"}


def _c(no: int, pay: date, status: str) -> Coupon:
    return Coupon(no, None, pay, None, None, Decimal(13) if status == "known" else None, status, Decimal(1000))


@pytest.mark.parametrize("coupon_type,formula,coupons,expected", [
    ("variable_known", FORMULA, [_c(1, date(2026, 10, 1), "known"), _c(2, date(2026, 11, 3), "unknown")], True),
    ("unknown", {**FORMULA, "benchmark": "RUONIA"}, [_c(2, date(2026, 11, 3), "unknown")], True),
    # every coming coupon known: the listed schedule is enough
    ("variable_known", FORMULA, [_c(1, date(2026, 10, 1), "unknown"), _c(2, date(2026, 11, 3), "known")], False),
    ("fixed", FORMULA, [_c(2, date(2026, 11, 3), "unknown")], False),  # a fixed bond keeps its type
    ("variable_known", None, [_c(2, date(2026, 11, 3), "unknown")], False),
    ("variable_known", {**FORMULA, "benchmark": "CPI"}, [_c(2, date(2026, 11, 3), "unknown")], False),
    ("variable_known", {**FORMULA, "benchmark": "ZCYC"}, [_c(2, date(2026, 11, 3), "unknown")], False),
    ("variable_known", {**FORMULA, "spread_bp": None}, [_c(2, date(2026, 11, 3), "unknown")], False),
])
def test_formula_floater_rule(coupon_type, formula, coupons, expected):
    assert formula_floater(coupon_type, formula, coupons, D) is expected


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), "2026-10-15")


@pytest.fixture(scope="module")
def labelled(ctx):
    """The fixture's key-rate floater as the hub lists RU000A10DQL5 (28.10.2026): ``variable_known`` from MOEX's
    «Фиксированный с известными купонами», the published coupons as amounts without a rate, the coming ones
    ``unknown`` (the fixture's history of the key rate starts after the running period's, so it stays published)."""
    snap = ctx.static.snapshot
    doc = copy.deepcopy(snap.doc)
    data = doc["data"]
    inst = next(i for i in data["instruments"] if i["secid"] == KEY)
    inst.update(coupon_type="variable_known", coupon_type_basis="schedule",
                coupon_details="Фиксированный с известными купонами")
    for c in data["coupon_periods"]:
        if c["secid"] == KEY:
            c["rate_pct"] = None
            if date.fromisoformat(c["start_date"]) > D:
                c.update(status="unknown", amount_per_bond=None, amount_rub_per_bond=None)
    return StaticView(dataclasses.replace(snap, doc=doc)).bonds[KEY]


def test_labelled_floater_is_read_as_a_floater(ctx, labelled):
    assert labelled.coupon_type == "floating" and FORMULA_FLOATER_LABEL in labelled.labels
    assert ctx.static.bonds[KEY].coupon_type == "floating"  # a hub floater is left as it is, without the label
    assert FORMULA_FLOATER_LABEL not in ctx.static.bonds[KEY].labels
    s = fixed_schedule(labelled, D)
    assert s.floater is not None and s.floater[1:] == ("KEY_RATE", "RUB_KEY_RATE_PROJ")
    assert s.assumptions[0] == "floater by its formula: MOEX lists known coupons, the coming ones are not known"


def test_as_listed_it_was_not_a_floater(labelled):
    """Before TDM#401: as the hub lists it the bond is a fixed bond at its last coupon for ever (no key-rate risk);
    with the running coupon unknown too (RU000A10DQL5 on 28.10.2026) it is rejected and falls to the fallback ladder,
    where MOEX's yield counts the unknown coupons as zero (0.1-0.3 %)."""
    hub = dataclasses.replace(labelled, coupon_type="variable_known",
                              labels=tuple(x for x in labelled.labels if x != FORMULA_FLOATER_LABEL))
    s = fixed_schedule(hub, D)
    assert s.floater is None and any(a.startswith("future coupons assumed at") for a in s.assumptions)
    running = next(c for c in hub.coupons if c.pay_date > D)
    unknown = dataclasses.replace(hub, coupons=tuple(dataclasses.replace(c, status="unknown", amount_per_bond=None)
                                                     if c.no >= running.no else c for c in hub.coupons))
    with pytest.raises(Rejected) as e:
        fixed_schedule(unknown, D)
    assert e.value.reason == "coupon_not_known"


def test_labelled_floater_values_as_the_hub_floater(ctx, labelled):
    q = ctx.market.quote(labelled)
    floater = ctx.static.bonds[KEY]
    (v, w) = value_bonds(ctx.core_market, [bond_request("x", labelled, q, ctx.business_date, 1.0),
                                           bond_request("y", floater, q, ctx.business_date, 1.0)])[0].values()
    assert v.quality == "assumed" and v.assumptions[0].startswith("floater by its formula")
    risk = v.risk()
    assert set(risk) >= {"RUB KEY", "RUB RUONIA"} and sum(risk["RUB KEY"].values()) > 0  # projection: + on a rise
    # the same coupons projected on the same formula: the same yield and key-rate risk as the hub's own floater
    assert v.ytm_pct == pytest.approx(w.ytm_pct, abs=1e-6)
    assert sum(risk["RUB KEY"].values()) == pytest.approx(sum(w.risk()["RUB KEY"].values()), rel=1e-6)
