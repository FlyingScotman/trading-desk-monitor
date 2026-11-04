"""TDM#555: a derivative valued from its trade terms shows the same figures on the risk, PnL and OCP screens.

The whole path on the hub's contract 1.4 golden snapshot of 15.10.2026 (``fixtures/mdhub_v1_4``; a copy one day
earlier is D-1) and synthetic trades (invented amounts and rates), as the private adapter delivers them: positions
rows as the report gives them (an FX forward and an NDF as ``fx`` rows, an FX swap, an OIS and a CCS as ``otc`` rows,
each with its ``deal_id``) and the trades' ``trade_terms`` (no ``position_id``: the monitor links them by
``trade_id``). Both days are published to the store; then the API:

- ``/runs/{id}/derivatives``: every row valued by core (``used`` computed), its PV the position's;
- the risk: ``/runs/{id}/positions`` (DV01 by curve, FX delta), the Taylor payload ``/risk`` (the same deltas per
  factor), the risk table ``/risk-table`` (DV01 by curve and FX delta per currency summed over the derivatives);
- the PnL: ``/runs/{id}/pnl`` per position, ``/aggregates`` and the period ``/history/pnl/period`` (the same total);
- the OCP: ``/runs/{id}/ocp``: one line per position at its FX delta (the spot equivalent), the published OCP by
  currency their sum;
- the concept pages' run (``/concepts/run``): the same PV, FX delta and PnL per position.
"""

from __future__ import annotations

import copy
import dataclasses
import math
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from test_slice_s2 import shifted_prev

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.pnl import direct
from desk.store import PositionInputs
from desk.valuation import positions as pv1

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1_4"
BD = date(2026, 10, 15)
PREV = date(2026, 10, 14)


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), BD.isoformat())


@pytest.fixture(scope="module")
def prev(ctx):
    p = shifted_prev(ctx, PREV, 0.0)
    m = copy.copy(ctx.market)
    m.business_date = PREV
    m.fx = ctx.market.fx
    return dataclasses.replace(p, market=m)


@pytest.fixture(scope="module")
def moved(ctx):
    """D with the CNY market rate ``CNY_MOVE`` above D-1's: the FX part of the PnL has something to show."""
    m = copy.copy(ctx.market)
    was = ctx.market.fx
    m.fx = lambda ccy: {**was(ccy), "moex_19": was(ccy)["moex_19"] + CNY_MOVE} if ccy == "CNY" else was(ccy)
    return dataclasses.replace(ctx, market=m)


def fx(tid, base, quote, side, nb, nq, vd, **kw):
    return {"trade_id": tid, "book_id": "DB", "product_kind": kw.pop("product_kind", "fx_forward"),
            "trade_date": "2026-10-01", "value_date": vd, "base_ccy": base, "quote_ccy": quote, "side": side,
            "notional_base": str(nb), "notional_quote": str(nq), **kw}


def leg(tid, leg_id, side, kind="ois", **kw):
    return {"trade_id": tid, "leg_id": leg_id, "book_id": "DB", "product_kind": kind, "trade_date": "2026-10-05",
            "side": side, "ccy": "RUB", "notional_ccy": "1000000000", "start_date": "2026-10-06",
            "maturity_date": "2028-10-06", **kw}


TERMS = [
    fx("F1", "CNY", "RUB", "buy", 10e6, 127.46e6, "2026-12-17", contract_rate="12.746"),
    fx("S2", "CNY", "RUB", "buy", 50e6, 625e6, "2026-10-16", product_kind="fx_swap", leg_id="near"),
    fx("S2", "CNY", "RUB", "sell", 50e6, 630e6, "2027-01-19", product_kind="fx_swap", leg_id="far"),
    fx("N3", "USD", "RUB", "sell", 1e6, 86e6, "2027-01-20", ndf=True, settle_ccy="RUB", fixing_date="2027-01-18",
       fixing_source="CBR"),
    leg("O4", "fix", "pay", fixed_rate_pct="15.5"), leg("O4", "flt", "receive", index_code="RUONIA"),
    {**leg("C5", "rub", "pay", kind="ccs", index_code="RUONIA", notional_ccy="1250000000"),
     "notional_exchange": "initial_and_final"},
    {**leg("C5", "cny", "receive", kind="ccs", fixed_rate_pct="3"), "ccy": "CNY", "notional_ccy": "100000000",
     "notional_exchange": "initial_and_final"},
    # a CCS paying its annual coupons on D (14.10 + a business day): both fixed, in RUB and in CNY
    *({"trade_id": "C6", "leg_id": lid, "book_id": "DB", "product_kind": "ccs", "trade_date": "2025-10-10",
       "start_date": "2025-10-14", "maturity_date": "2027-10-14", "notional_exchange": "initial_and_final",
       "payment_frequency": "1Y", "day_count": "ACT/365", "side": side, "ccy": ccy, "notional_ccy": n,
       "fixed_rate_pct": rate}
      for lid, side, ccy, n, rate in (("rub", "pay", "RUB", "1250000000", "15"), ("cny", "receive", "CNY", "100000000", "3"))),
]
ROWS = {  # as the adapter reads the positions report: the report's PV of a row is not used once core values it
    "DB|F1": {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 10e6, "value_date": date(2026, 12, 17), "deal_id": "F1"},
    "DB|S2": {"asset_class": "otc", "ccy": "CNY", "deal_id": "S2", "mv_rub": 1.0},
    "DB|N3": {"asset_class": "fx", "ccy": "USD", "amount_ccy": -1e6, "value_date": date(2027, 1, 20), "deal_id": "N3"},
    "DB|O4": {"asset_class": "otc", "ccy": "RUB", "deal_id": "O4", "mv_rub": 2.0},
    "DB|C5": {"asset_class": "otc", "ccy": "RUB", "deal_id": "C5", "mv_rub": 3.0},
    "DB|C6": {"asset_class": "otc", "ccy": "RUB", "deal_id": "C6", "mv_rub": 4.0},
}
PRODUCTS = {"DB|F1": "fx_forward", "DB|S2": "fx_swap", "DB|N3": "fx_ndf", "DB|O4": "ois", "DB|C5": "ccs",
            "DB|C6": "ccs"}
CNY_MOVE = 0.4
"""The CNY market rate of D less that of D-1 (the fixture gives D-1 the rates of D)."""


def inputs(valid):
    return PositionInputs([(p, "DB", None, None) for p in ROWS], [], {"DB": valid}, datetime.now(UTC), "0" * 64,
                          {k: dict(x) for k, x in ROWS.items()}, trade_terms=[dict(t) for t in TERMS])


@pytest.fixture(scope="module")
def published(ctx, prev, moved, pg_server):
    """D-1 and D published to a store of their own (once for the module), and an API client on it."""
    from fastapi.testclient import TestClient

    from desk import store
    from desk.api import create_app
    from desk.publish import desk_run

    name = "desk_" + uuid.uuid4().hex[:12]
    pg_server.psql(f"CREATE DATABASE {name};")
    url = pg_server.get_uri(name)
    store.migrate(url)
    pin, din = inputs(PREV), inputs(BD)
    pbp, bp = pv1.plan(prev, pin), pv1.plan(moved, din)
    direct.attach(prev, moved, bp.direct, din, pbp.direct, pin.valid_dates)
    with store.connect(url) as con:
        desk_run.publish(con, prev, pv1.merge(pbp, []), prev=None, inputs=pin, core_results=[pbp.core_result])
        rid, status, _ = desk_run.publish(con, moved, pv1.merge(bp, []), prev=prev.market, inputs=din,
                                          core_results=[bp.core_result])
    assert status == "complete"
    return TestClient(create_app(url)), rid, bp


def facts(c, rid):
    return {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid}/positions").json()["positions"]}


def test_every_derivative_is_valued_by_core_from_its_terms(published):
    c, rid, bp = published
    der = {r["position_id"]: r for r in c.get(f"/api/v1/runs/{rid}/derivatives").json()["rows"]}
    pos = facts(c, rid)
    assert set(der) == set(ROWS)
    for pid, product in PRODUCTS.items():
        r, p = der[pid], pos[pid]
        assert (r["used"], r["product"]) == ("computed", product), r
        assert r["status"] in ("both", "computed_only") and not r.get("reason")
        assert p["fact"]["extra"]["core"]["product"] == product and p["reason"] is None
        assert r["computed_pv_rub"] == pytest.approx(p["pv_rub"]) == pytest.approx(bp.direct[pid].valuation.pv_rub)
    assert bp.unlinked_terms == [] and bp.unlinked_rates == []


def test_risk_screens_show_the_same_dv01_and_fx_delta(published, ctx):
    c, rid, _ = published
    pos = {k: p["fact"] for k, p in facts(c, rid).items()}
    risk = c.get(f"/api/v1/runs/{rid}/risk").json()
    factors = risk["factors"]
    by_pos = {p["position_id"]: {factors[i]: x for i, x in p["delta"]} for p in risk["positions"]}
    dv01: dict[str, float] = defaultdict(float)
    delta: dict[str, float] = defaultdict(float)
    for pid, f in pos.items():
        d = by_pos[pid]
        for curve, buckets in (f.get("risk") or {}).items():
            for b, v in buckets.items():
                if v:
                    assert d[f"IR|{curve}|{b}"] == pytest.approx(v, rel=1e-12), (pid, curve, b)
                dv01[curve] += v
        for ccy, a in (f.get("fx_delta") or {}).items():
            assert d[f"FX|{ccy}"] == pytest.approx(a * ctx.fx.rates[ccy][0] / 100, rel=1e-12), (pid, ccy)  # per 1 %
            delta[ccy] += a
    assert set(pos["DB|C5"]["risk"]) == {"RUB RUONIA", "CNY XCCY"} and pos["DB|C5"]["fx_delta"]["CNY"] > 0
    assert set(pos["DB|O4"]["risk"]) == {"RUB RUONIA"} and not pos["DB|O4"].get("fx_delta")
    table = c.get(f"/api/v1/runs/{rid}/risk-table").json()
    by_ccy = {r["ccy"]: r for r in table["rows"] if r["level"] == "ccy"}
    got: dict[str, float] = defaultdict(float)
    for r in by_ccy.values():
        for curve, v in (r.get("dv01_by_curve_rub") or {}).items():
            got[curve] += v
    for curve, v in dv01.items():
        assert got[curve] == pytest.approx(v, rel=1e-9), curve
    for ccy, a in delta.items():
        assert by_ccy[ccy]["fx_delta_ccy"] == pytest.approx(a, rel=1e-9), ccy


def test_pnl_daily_and_period_are_the_same(published):
    c, rid, bp = published
    rows = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid}/pnl").json()["positions"]}
    daily = 0.0
    for pid in ROWS:
        o = bp.direct[pid]
        if o.pnl is None:  # the fixture's D-1 has no USD curve: the NDF is valued from its terms on D only
            assert pid == "DB|N3" and rows[pid]["total"] is None and rows[pid]["no_pnl"]["reason"]
            continue
        assert rows[pid]["total"] == pytest.approx(o.pnl.total), pid
        daily += o.pnl.total
    assert rows["DB|C5"]["total"] is not None and rows["DB|O4"]["total"] is not None
    (agg,) = c.get(f"/api/v1/runs/{rid}/aggregates").json()["rows"]
    assert agg["pnl"]["total"] == pytest.approx(daily)
    period = c.get("/api/v1/history/pnl/period", params={"from": BD.isoformat(), "to": BD.isoformat(),
                                                         "gran": "day", "run_id": rid}).json()
    assert period["summary"]["total"] == pytest.approx(daily) and period["summary"]["noTotal"] == 1
    assert {p["positionId"] for p in period["positions"]} == set(ROWS)


def test_ocp_takes_the_fx_delta_of_every_derivative(published):
    c, rid, _ = published
    pos = {k: p["fact"] for k, p in facts(c, rid).items()}
    ocp = c.get(f"/api/v1/runs/{rid}/ocp").json()
    lines: dict[str, dict[str, float]] = defaultdict(dict)
    for ln in ocp["lines"]:
        lines[ln["position_id"]][ln["ccy"]] = ln["amount"]
        assert ln["basis"] == "spot_equivalent", ln
    want: dict[str, float] = defaultdict(float)
    for pid, f in pos.items():
        fd = {k: v for k, v in (f.get("fx_delta") or {}).items() if v}
        assert lines.get(pid, {}) == pytest.approx(fd), pid
        assert (f["extra"].get("ocp") or {}).get("amounts", {}) == pytest.approx(fd), pid
        for ccy, a in fd.items():
            want[ccy] += a
    assert {"DB|F1", "DB|S2", "DB|N3", "DB|C5"} <= set(lines)  # the OIS has no FX delta: no line
    by_ccy = ocp["published"]["by_ccy"]
    assert set(by_ccy) == set(want)
    for ccy, a in want.items():
        assert by_ccy[ccy]["total"] == pytest.approx(a, rel=1e-12), ccy
        assert math.isfinite(by_ccy[ccy]["total_rub"])


def test_concept_pages_read_the_same_figures(published):
    c, rid, bp = published
    r = c.get("/api/v1/concepts/run", params={"run_id": rid})
    assert r.status_code == 200
    pages = {p["id"]: p for p in r.json()["positions"]}
    pos = {k: p["fact"] for k, p in facts(c, rid).items()}
    for pid in ROWS:
        p, f = pages[pid], pos[pid]
        assert not p.get("notValued") and p["mvRub"] == pytest.approx(f["mv_rub"])
        assert p["fxDelta"] == pytest.approx({k: v for k, v in (f.get("fx_delta") or {}).items() if v})
        o = bp.direct[pid]
        if o.pnl is not None:
            assert p["pnl"]["available"] and p["pnl"]["explained"] == pytest.approx(o.pnl.total)


def test_pnl_fx_part_is_the_rate_move_on_the_delta_without_the_cash_paid(published, ctx, prev):
    """The rate moved by ``CNY_MOVE`` between the two dates; C6 paid its coupons on D (a CNY one: a flow in CNY, out of
    the exposure the move applies to), C5 and the FX rows paid nothing in (D-1, D]. Over a period of the two days the
    total and the parts are the day's."""
    c, rid, bp = published
    pbp = pv1.plan(prev, inputs(PREV))
    flows = {f["ccy"]: f["amount"] for f in pbp.direct["DB|C6"].valuation.extra["flows"] if f["date"] == BD.isoformat()}
    assert flows == pytest.approx({"RUB": -0.15 * 1.25e9, "CNY": 0.03 * 1e8})  # each in its own currency
    for pid in ("DB|C5", "DB|C6", "DB|F1"):
        o, p = bp.direct[pid], pbp.direct[pid]
        paid = sum(f["amount"] for f in p.valuation.extra["flows"] if f["date"] == BD.isoformat() and f["ccy"] == "CNY")
        want = (p.valuation.fx_delta["CNY"] - paid) * CNY_MOVE
        assert o.pnl.fx_gross == pytest.approx(want), pid
        assert o.pnl.other == pytest.approx(o.pnl.total - want), pid
    assert bp.direct["DB|C6"].pnl.received_rub == pytest.approx(-0.15 * 1.25e9 + 0.03 * 1e8 * ctx.fx.rates["CNY"][0])
    rows = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid}/pnl").json()["positions"]}
    assert rows["DB|C6"]["total"] == pytest.approx(bp.direct["DB|C6"].pnl.total)
    two = c.get("/api/v1/history/pnl/period", params={"from": PREV.isoformat(), "to": BD.isoformat(), "gran": "day",
                                                      "run_id": rid}).json()
    one = c.get("/api/v1/history/pnl/period", params={"from": BD.isoformat(), "to": BD.isoformat(), "gran": "day",
                                                      "run_id": rid}).json()
    assert two["summary"]["total"] == pytest.approx(one["summary"]["total"])
    assert {p["positionId"] for p in two["positions"]} == set(ROWS)
