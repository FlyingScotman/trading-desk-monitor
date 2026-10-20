"""R21: «Прочее» of the PnL tab, explained. compat splits a position's ``other`` into ``noDecomp`` (the whole PnL of a
position core does not reprice step by step: a fallback rung, a change of discount curve) and ``residual`` (the price
residual of a bond with the step decomposition), each with its Russian reason (``otherWhy``); ``other`` stays their sum.
R19: the payload carries what the drill-down needs per position (risk per curve and bucket, DV01, CS01, PV, the PnL
components) and the components add up, per position and in total. On the golden fixtures with the synthetic
previous date of test_slice_s2. Synthetic rows only."""

from __future__ import annotations

import copy
import dataclasses
import math
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from test_pnl_r10 import FIX
from test_slice_s2 import prev_outcome, shifted_prev

from desk import store
from desk.api import create_app
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare, value
from desk.pnl import direct
from desk.pnl.reasons import EXACT_METHODS, FX_METHOD, REPORTED_METHOD, other_split
from desk.publish import desk_run
from desk.store import PositionInputs
from desk.valuation.bonds import BASIS_CURVE
from desk.valuation.fallback import METHOD as SIMPLE_METHOD
from desk.valuation.positions import merge, plan

DATE = "2026-10-15"
PREV = date(2026, 10, 14)
QTY = 1000.0
PARTS = ("carry", "rolldown", "rates", "spread", "fx", "funding", "convexity", "newTrades", "noDecomp", "residual",
         "reported")
"""The PnL tab's components after R21: they add up to ``explained``."""


def inputs(rows, extra, valid: date) -> PositionInputs:
    return PositionInputs(rows, [], {b: valid for _, b, *_ in rows}, datetime.now(UTC), "0" * 64, extra)


@pytest.fixture(scope="module")
def book():
    """Every fixture bond (exact ones repriced from the shifted D-1, fallback rungs 0.3% dearer the day before: a
    simple PnL), a USD FX row and an OTC row valued at the report's figure on both dates."""
    ctx = prepare(SnapshotDir(FIX), DATE)
    usd = ctx.market.fx("USD")["cbr"]
    prev = shifted_prev(ctx, PREV)
    m = copy.copy(ctx.market)
    m.business_date = PREV
    m.fx = lambda ccy: {"cbr": usd * 0.99 if ccy == "USD" else None, "cbr_next": None, "moex_tom": None}
    prev = dataclasses.replace(prev, market=m)
    rows = [(f"P{i:02d}", "OFZ" if s.startswith("SU") else "CORP", s, QTY) for i, s in enumerate(sorted(ctx.static.bonds))]
    rows += [("FX1", "TSY", None, None), ("O1", "DER", None, None)]
    extra = {"FX1": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 2e5},
             "O1": {"asset_class": "otc", "source_instrument_id": "IRS-1", "ccy": "RUB", "mv_rub": 5e5}}
    inp = inputs(rows, extra, date.fromisoformat(DATE))
    bp = plan(ctx, inp)
    outs = value(ctx, bp.requests, position_ids=bp.position_ids)
    prevs = []
    for o in outs:
        v = o.valuation
        if v is None or v.quality == "exact":
            prevs.append(prev_outcome(prev, o) if v else o)
        else:
            k = 1.003 * (0.99 if (o.terms.face_unit or o.terms.currency or "RUB") == "USD" else 1.0)  # USD 1% lower on D-1
            prevs.append(Outcome(o.secid, o.terms, o.quote, valuation=dataclasses.replace(
                v, valuation_date=PREV, dirty_rub=v.dirty_rub * k, pv_rub=v.pv_rub * k)))
    attach_pnl(prev, prevs, ctx, outs)
    direct.attach(prev, ctx, bp.direct, inp,
                  plan(prev, inputs(rows, {**extra, "O1": {**extra["O1"], "mv_rub": 3.9e5}}, PREV)).direct)
    return ctx, inp, merge(bp, outs), m


@pytest.fixture
def desk(store_url, book):
    ctx, inp, positions, m = book
    with store.connect(store_url) as con:
        rid, status, _ = desk_run.publish(con, ctx, positions, prev=m, inputs=inp)
    assert status == "complete"
    c = TestClient(create_app(store_url))
    return c.get(f"/api/v1/compat/desk?run_id={rid}").json(), c.get(f"/api/v1/runs/{rid}").json()


def test_other_is_split_and_the_components_add_up(desk):
    d, run = desk
    ps = [p for p in d["positions"] if p["pnl"]["available"]]
    by = {p["pnl"]["method"]: p for p in ps}
    assert {SIMPLE_METHOD, REPORTED_METHOD, FX_METHOD} <= set(by) and set(EXACT_METHODS) & set(by)
    for p in ps:
        e = p["pnl"]
        assert e["other"] == pytest.approx(e["noDecomp"] + e["residual"], abs=1e-6), p["id"]
        assert math.fsum(e[k] for k in PARTS) == pytest.approx(e["explained"], abs=1e-6), p["id"]
        if e["method"] in EXACT_METHODS:
            assert e["noDecomp"] == 0.0 and abs(e["residual"]) < 1.0  # z1 is calibrated to D's price: about 0
        else:
            assert e["residual"] == 0.0
    simple = [p for p in ps if p["pnl"]["method"] == SIMPLE_METHOD]
    assert simple and all(p["pnl"]["noDecomp"] and p["pnl"]["otherWhy"].startswith("без разложения") for p in simple)
    assert any("рыночная стоимость в оба дня" in p["pnl"]["otherWhy"] for p in simple)
    usd = next(p for p in simple if p["pnl"]["fx"])  # the USD-face bond: its FX revaluation is not in noDecomp
    assert usd["pnl"]["noDecomp"] == pytest.approx(usd["pnl"]["explained"] - usd["pnl"]["fx"])
    assert "валютная переоценка вынесена в FX" in usd["pnl"]["otherWhy"]
    o1 = next(p for p in ps if p["id"] == "O1")["pnl"]
    assert (o1["reported"], o1["noDecomp"], o1["residual"], o1["otherWhy"]) == (pytest.approx(1.1e5), 0.0, 0.0, None)
    # the desk total: Σ components == Σ explained == the run's explained PnL; «Прочее» is all no-decomposition here
    tot = {k: math.fsum(p["pnl"][k] for p in ps) for k in PARTS + ("explained", "other")}
    assert math.fsum(tot[k] for k in PARTS) == pytest.approx(tot["explained"], abs=1e-4)
    assert tot["explained"] == pytest.approx(run["totals"]["_total"]["pnl"]["total"], abs=1e-2)
    assert tot["other"] == pytest.approx(tot["noDecomp"], abs=1.0) and abs(tot["noDecomp"]) > 1e5
    # positions without PnL: the new amounts are null too (R10), not 0
    for p in d["positions"]:
        if not p["pnl"]["available"]:
            assert p["pnl"]["noDecomp"] is None and p["pnl"]["residual"] is None and p["pnl"]["otherWhy"] is None


def test_payload_has_what_the_drill_down_needs(desk):
    """Every aggregate the pages show is a sum over positions of a field they carry: the drill-down lists them."""
    d, _ = desk
    ps, B = d["positions"], d["BUCKETS"]
    for p in ps:
        assert {"id", "name", "book", "issuer", "isin", "segment", "mvRub", "dv01", "cs01", "risk", "fxDelta",
                "pnl"} <= set(p)
        assert all(len(v) == len(B) for v in p["risk"].values())
        e = p["pnl"]
        assert e["available"] is False or (e["method"] and e["methodText"] and "desk-" not in e["methodText"])
    # the KPIs are sums over the listed positions: DV01 and CS01 as the run's history row of the date
    assert d["history"][-1]["dv01"] == pytest.approx(math.fsum(p["dv01"] for p in ps))
    assert d["history"][-1]["cs01"] == pytest.approx(math.fsum(p["cs01"] for p in ps))
    # a position's bucket risk adds up to its DV01 when all of it is on curves (the heatmap cells reconcile); the
    # OFZ-RUONIA basis (R18) repeats an OFZ's RUONIA buckets and is not in DV01
    for p in ps:
        if p["risk"] and p.get("instrType") == "BOND":
            assert math.fsum(sum(v) for c, v in p["risk"].items() if c != BASIS_CURVE) == pytest.approx(p["dv01"], rel=1e-6, abs=1e-6), p["id"]


@pytest.mark.parametrize(("method", "other", "pnl_extra", "fact_extra", "fx", "want"), [
    ("desk-flash-exact-v2", 1234.5, {"z0_bp": 100.0, "z1_bp": 90.0, "prev_price_basis": "legal_close"},
     {"price_basis": "waprice"}, 0.0, (0.0, 1234.5, ["невязка цены", "Z 100 → 90", "база цены сменилась: legal_close → waprice"])),
    ("desk-flash-exact-v2", 0.004, {}, {}, 0.0, (0.0, 0.004, None)),
    (SIMPLE_METHOD, -5e5, {"note": "simple PnL (model_price -> market_value): q x (...)", "flag": "price discovery: model -> market"},
     {}, 0.0, (-5e5, 0.0, ["модельная цена → рыночная стоимость", "смена модельной цены на рыночную"])),
    (SIMPLE_METHOD, 7e4, {"note": "simple PnL: discounted on RUB KBD on 2026-10-14, on RUB RUONIA on 2026-10-15"}, {}, 0.0,
     (7e4, 0.0, ["кривая дисконтирования сменилась"])),
    ("desk-other-v9", 10.0, {}, {}, 0.0, (10.0, 0.0, ["без разложения: desk-other-v9"])),
])
def test_other_split_reasons(method, other, pnl_extra, fact_extra, fx, want):
    nd, res, why = other_split(method, other, pnl_extra, fact_extra, fx)
    assert (nd, res) == want[:2]
    if want[2] is None:
        assert why is None
    else:
        assert all(w in why for w in want[2]), why
