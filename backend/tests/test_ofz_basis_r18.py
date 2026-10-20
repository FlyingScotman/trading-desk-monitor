"""R18 (owner, 21.10): an OFZ is a spread to RUONIA. The OFZ curve (the KBD) is RUONIA plus the OFZ-RUONIA basis: an
OFZ is discounted on the KBD and its KBD risk is published on ``RUB RUONIA`` and, the same buckets, on ``RUB
OFZ-RUONIA``; a corporate stays on RUONIA + its own spread, with no basis risk. Checked here: the risk sums (the basis
out of DV01), the Hessian rewritten in the two factors, parallel-shift equivalence (RUONIA + basis moved together =
the old OFZ-curve move, in Taylor and in core), the scenario mapping of the curves, the flash PnL split, the run's
basis curve and the pages' notes. The 2026-10-15 fixture. Synthetic data only."""

from __future__ import annotations

import dataclasses
import itertools
import math
import random
from datetime import date, timedelta

import pytest
from rub_fixed_income import RISK_BUCKETS, CurveEntry, DiscountCurve, ShockedCurveCache, kbd_discount_curve
from test_slice_s2 import FIX, QTY, flash_curves, prev_outcome, shifted_curve
from test_slice_s6 import golden  # noqa: F401 - the stored golden run, a fixture

from desk.api.compat import gamma_summary, risk_notes
from desk.marketdata.adapter import KBD_CURVE_ID, KBD_SPEC, OFZ_CLASSES
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import EodContext, attach_pnl, prepare, value
from desk.pnl.flash import explain
from desk.publish import tdm_run
from desk.scenario.definition import BASIS_CURVE, curve_moves, normalize
from desk.scenario.exact import reprice_run
from desk.scenario.taylor import risk_payload, run_taylor
from desk.valuation.bonds import RUONIA_CURVE, rate_buckets, split_hessian, split_ofz

FIX_DATE = "2026-10-15"
OFZ, OFZ_PK, CORP = "SU26238RMFS4", "SU29008RMFS8", "RU000A0JXQ44"


@pytest.fixture(scope="module")
def ctx() -> EodContext:
    return prepare(SnapshotDir(FIX), FIX_DATE)


def quad(h: list[tuple[str, str, float]], x: dict[str, float]) -> float:
    return math.fsum((0.5 if a == b else 1.0) * v * x.get(a, 0.0) * x.get(b, 0.0) for a, b, v in h)


def test_split_keeps_the_rate_risk_and_adds_the_basis():
    kbd = dict(zip(RISK_BUCKETS, range(1, 12)))
    proj = dict.fromkeys(RISK_BUCKETS, 0.5)
    by = split_ofz({"RUB OFZ": kbd, RUONIA_CURVE: proj}, ruonia=True)
    assert set(by) == {RUONIA_CURVE, BASIS_CURVE} and by[BASIS_CURVE] == kbd
    assert by[RUONIA_CURVE] == {b: kbd[b] + 0.5 for b in RISK_BUCKETS}
    assert rate_buckets(by) == {b: kbd[b] + 0.5 for b in RISK_BUCKETS}  # the basis is not a rate curve
    assert split_ofz({"RUB OFZ": kbd}, ruonia=False) == {"RUB OFZ": kbd}  # no RUONIA: no split


def test_hessian_in_ruonia_and_basis_is_the_same_quadratic_form():
    rng = random.Random(18)
    f = [f"IR|RUB OFZ|{b}" for b in ("2Y", "3Y", "5Y")] + ["IR|RUB RUONIA|2Y", "CS"]
    h = [(a, b, rng.uniform(-1, 1)) for a, b in itertools.combinations_with_replacement(sorted(f), 2)]
    new = split_hessian(h, ruonia=True)
    assert all(a <= b for a, b, _ in new) and not any("RUB OFZ|" in a + b for a, b, _ in new)
    for _ in range(5):
        r = {b: rng.uniform(-50, 50) for b in ("2Y", "3Y", "5Y")}
        s = {b: rng.uniform(-50, 50) for b in ("2Y", "3Y", "5Y")}
        cs = rng.uniform(-20, 20)
        old_x = {**{f"IR|RUB OFZ|{b}": r[b] + s[b] for b in r}, "IR|RUB RUONIA|2Y": r["2Y"], "CS": cs}
        new_x = {**{f"IR|RUB RUONIA|{b}": r[b] for b in r}, **{f"IR|{BASIS_CURVE}|{b}": s[b] for b in s}, "CS": cs}
        assert quad(new, new_x) == pytest.approx(quad(h, old_x), rel=1e-12)
    assert split_hessian(h, ruonia=False) == h


def test_ofz_is_ruonia_plus_basis_corporate_has_no_basis(ctx):
    o_ofz, o_pk, o_corp = value(ctx, [(s, QTY) for s in (OFZ, OFZ_PK, CORP)], second_order=True)
    v = o_ofz.valuation
    r = v.risk()
    assert (v.discount_curve_id, v.spread_curve, v.quality) == (KBD_CURVE_ID, "RUB OFZ", "exact")
    assert set(r) == {RUONIA_CURVE, BASIS_CURVE} and r[RUONIA_CURVE] == r[BASIS_CURVE] == v.buckets
    assert v.dv01_rub == pytest.approx(math.fsum(r[RUONIA_CURVE].values())) and v.spread_kind == "ofz"
    names = {x.split("|")[1] for a, b, _ in v.hessian for x in (a, b) if x.startswith("IR|")}
    assert names == {RUONIA_CURVE, BASIS_CURVE}
    assert v.curve_convexity and v.curve_convexity > 0  # of a parallel move of the rates: the basis not in it
    g = gamma_summary([list(x) for x in v.hessian], v.pv_rub, v.curve_convexity, 0.0)
    assert g["gammaIr"] == pytest.approx(v.curve_convexity * v.pv_rub * 1e-8, rel=1e-9)
    pk = o_pk.valuation.risk()  # the OFZ-PK: RUONIA projection netted on RUB RUONIA, its discounting on the basis
    assert sum(pk[BASIS_CURVE].values()) < sum(pk[RUONIA_CURVE].values()) < 0
    assert set(o_corp.valuation.risk()) == {RUONIA_CURVE} and o_corp.valuation.spread_kind == "credit"


def test_curve_moves_keep_a_definition_meaning():
    assert curve_moves(normalize({"curves": {"RUB RUONIA": 10}})) == {"RUB RUONIA": [10.0] * 11, "RUB OFZ": [10.0] * 11}
    m = curve_moves(normalize({"curves": {"RUB OFZ": 30, "RUB RUONIA": 10}}))
    assert (m["RUB OFZ"], m[BASIS_CURVE]) == ([30.0] * 11, [20.0] * 11)
    m = curve_moves(normalize({"curves": {BASIS_CURVE: 5, "RUB OFZ": 99}}))  # the basis wins
    assert (m["RUB OFZ"], m[BASIS_CURVE]) == ([5.0] * 11, [5.0] * 11)
    assert BASIS_CURVE not in curve_moves(normalize({"curves": dict.fromkeys(("RUB OFZ", "RUB RUONIA", "RUB KEY"), 7)}))
    assert curve_moves({}) == {}


def test_parallel_shift_equivalence_taylor_and_core(golden):  # noqa: F811
    """RUONIA +x and the basis +y together move an OFZ as the old OFZ-curve move x + y, in Taylor and in core; a
    corporate moves by x only. The same definition written as curve levels (RUB OFZ x + y, RUONIA x) gives the same."""
    snaps, _, run, rows, reference = golden
    ofz = {r["position_id"] for r in rows if BASIS_CURVE in r["fact"]["risk"]}
    corp = {r["position_id"] for r in rows if r["fact"].get("cs01") and RUONIA_CURVE in r["fact"]["risk"]}
    assert ofz and corp
    x, y = [10, 10, 9, 8, 8, 7, 6, 5, 5, 5, 5], [20, 20, 18, 15, 12, 10, 8, 6, 5, 4, 3]
    factors = reprice_run(snaps, run, rows, reference, {"curves": {RUONIA_CURVE: x, BASIS_CURVE: y}})
    levels = reprice_run(snaps, run, rows, reference,
                         {"curves": {RUONIA_CURVE: x, "RUB OFZ": [a + b for a, b in zip(x, y)]}})
    ruonia = {p["position_id"]: p for p in reprice_run(snaps, run, rows, reference,
                                                       {"curves": {RUONIA_CURVE: x}})["positions"]}
    for a, b in zip(factors["positions"], levels["positions"]):
        assert a["taylor"]["total"] == pytest.approx(b["taylor"]["total"], rel=1e-12, abs=1e-9)
        assert (a["exact"] is None) is (b["exact"] is None)
        if a["exact"] is not None:
            assert a["exact"] == pytest.approx(b["exact"], rel=1e-9, abs=1e-9)
            assert abs(a["residual"]) <= 2e-3 * abs(a["taylor"]["total"]) + 1e-6, a
        if a["position_id"] in corp:  # no basis risk: the RUONIA move only
            assert a["taylor"]["total"] == pytest.approx(ruonia[a["position_id"]]["taylor"]["total"], rel=1e-12)
    # a fixed OFZ's rates Taylor is its KBD DV01 x (x + y): the old OFZ-curve move (an OFZ-PK also projects on RUONIA)
    payload = risk_payload(run, rows, reference)
    old_rows = [{**r, "fact": {**r["fact"], "risk": {"RUB OFZ": r["fact"]["risk"][BASIS_CURVE]}, "gamma": []}}
                for r in rows if r["position_id"] in ofz
                and r["fact"]["risk"][BASIS_CURVE] == r["fact"]["risk"][RUONIA_CURVE]]
    assert old_rows
    old = run_taylor(risk_payload(run, old_rows, reference), normalize({"curves": {"RUB OFZ": [a + b for a, b in zip(x, y)]}}))
    new = {p["position_id"]: p for p in run_taylor(payload, normalize({"curves": {RUONIA_CURVE: x, BASIS_CURVE: y}}))[
        "positions"]}
    for p in old["positions"]:
        assert new[p["position_id"]]["rates"] == pytest.approx(p["rates"], rel=1e-12)


def kbd_moved(ctx: EodContext, prev_date: date, shift_bp: float) -> EodContext:
    """``ctx``'s market on ``prev_date`` with the KBD ``shift_bp`` lower and every other curve as it is (rolled to the
    date): the basis moves, RUONIA does not."""
    params = dataclasses.replace(ctx.kbd, trade_date=prev_date, b1=ctx.kbd.b1 - shift_bp)
    curves = [CurveEntry(KBD_CURVE_ID, KBD_SPEC, params)] + [
        dataclasses.replace(e, curve=shifted_curve(e.curve, prev_date, 0.0))
        for e in ctx.core_market.curves if isinstance(e.curve, DiscountCurve)]
    market = dataclasses.replace(ctx.core_market, business_date=prev_date, curves=tuple(curves))
    return dataclasses.replace(ctx, kbd=params, curve=kbd_discount_curve(params), core_market=market,
                               cache=ShockedCurveCache())


def test_flash_pnl_of_an_ofz_splits_ruonia_and_basis(ctx):
    (o,) = value(ctx, [(OFZ, QTY)])
    prev = kbd_moved(ctx, ctx.business_date - timedelta(days=1), 10.0)
    p = prev_outcome(prev, o)
    assert set(p.valuation.risk()) == {RUONIA_CURVE, BASIS_CURVE}
    f = explain(p.valuation, o.valuation, flash_curves(prev, ctx))
    assert f.rates_basis is not None and f.rates < 0  # the KBD up 10 bp: the OFZ loses, all of it the basis
    assert f.rates_basis == pytest.approx(f.rates, rel=0.02)
    split = f.extra()["rates_split"]
    assert split == {RUONIA_CURVE: pytest.approx(f.rates - f.rates_basis), BASIS_CURVE: f.rates_basis}
    assert abs(split[RUONIA_CURVE]) < 0.02 * abs(f.rates) + 1.0
    cur = [dataclasses.replace(o)]
    attach_pnl(prev, [p], ctx, cur)
    log = tdm_run.rates_log(ctx, [("P1", "OFZ", cur[0])], [])
    assert any(m.startswith("PnL ставок ОФЗ (1 поз.): RUONIA ") and "базис ОФЗ–RUONIA" in m for _, m in log)


def test_run_reference_has_the_basis_curve_and_the_pages_say_old_runs(ctx):
    content = tdm_run.run_content(ctx, [("P1", "OFZ", value(ctx, [(OFZ, QTY)])[0])], None, {})
    curves = {c["curve_id"]: c for c in content.reference["curves"]}
    b = curves[BASIS_CURVE]
    assert (b["name"], b["kind"], b["ccy"], len(b["points"])) == ("Базис ОФЗ–RUONIA", "basis", "RUB", 11)
    assert set(content.rows[0]["risk"]) == {RUONIA_CURVE, BASIS_CURVE}
    assert content.rows[0]["dv01"] == pytest.approx(sum(content.rows[0]["risk"][RUONIA_CURVE].values()))
    v5 = {"business_date": date(2026, 10, 20), "lineage": {"logic_version": "0.1.0+v5+core1", "rates": {}}}
    v3 = {"business_date": date(2026, 10, 19), "lineage": {"logic_version": "0.1.0+v3+core1", "rates": {}}}
    v6 = {"business_date": date(2026, 10, 21), "lineage": {"logic_version": "0.1.0+v6+core1", "rates": {}}}
    assert [n["code"] for n in risk_notes(v5)] == ["run_before_ofz_basis"]
    assert [n["code"] for n in risk_notes(v3)] == ["run_before_ruonia"] and risk_notes(v6) == []
    assert "базиса ОФЗ–RUONIA" in risk_notes(v5)[0]["text"]


def test_without_ruonia_an_ofz_keeps_its_risk_on_the_ofz_curve(ctx):
    market = dataclasses.replace(ctx.core_market, curves=tuple(
        c for c in ctx.core_market.curves if c.curve_id == KBD_CURVE_ID), fixings=())
    kbd = dataclasses.replace(ctx, core_market=market, cache=ShockedCurveCache())
    o_ofz, o_corp = value(kbd, [(OFZ, QTY), (CORP, QTY)])
    assert set(o_ofz.valuation.risk()) == {"RUB OFZ"} and o_ofz.valuation.quality == "exact"
    assert set(o_corp.valuation.risk()) == {"RUB OFZ"} and o_corp.valuation.quality == "assumed"
    assert o_ofz.terms.bond_class in OFZ_CLASSES
