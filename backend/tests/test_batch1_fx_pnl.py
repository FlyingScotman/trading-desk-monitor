"""Batch 1, item 5: the flash PnL of foreign-face bonds is decomposed, not all in «Прочее» (R34).

On the contract 1.4 golden snapshot of 15.10.2026 with the synthetic CNY copy of its USD-face bond and a UST par set
(``test_fx_core_035.synthetic_dir``), rolled to 16.10 with prices, curve quotes and FX rates moved (invented moves):

- a bond core values on its own curve on both dates (CNY on XCCY; USD on the UST proxy with ``proxy_curves_for_risk``)
  is explained by core in its currency: carry, rolldown, rates and spread on that curve, the rate move on the D-1 PV in
  ``fx_gross`` (and at the CBR rates in ``fx_gross_cbr``), the identity in RUB;
- a foreign bond on the fallback ladder (USD without the proxies) gets its carry at its yield; the yield move stays
  in ``other`` (no curve to split it), the FX revaluation in ``fx_gross``.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from test_fx_core_035 import CNY_BOND, DATE, USD_BOND, synthetic_dir

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import attach_pnl, prepare, value
from desk.pnl.reasons import METHOD_TEXT

NEXT = "2026-10-16"
FX_MOVE = Decimal("1.01")


def _roll(doc: dict, d0: str, d1: str) -> dict:
    """``doc`` (a market snapshot of ``d0``) as of ``d1``: dates moved, prices -0.2 %, MOEX yields +5 bp, curve quotes
    +3 bp, FX rates of the date x ``FX_MOVE``."""
    m = copy.deepcopy(doc)
    m["business_date"], m["known_at"] = d1, f"{d1}T18:00:00Z"
    data = m["data"]
    for q in data["bond_quotes"]:
        q["trade_date"] = d1
        for k in ("legal_close_pct", "close_pct", "waprice_pct", "last_pct", "market_price2_pct",
                  "market_price3_pct", "admitted_quote_pct"):
            if q.get(k) is not None:
                q[k] = str(Decimal(q[k]) - Decimal("0.2"))
        sa = q.get("source_analytics") or {}
        if sa.get("effective_yield_pct") is not None:
            sa["effective_yield_pct"] = str(Decimal(sa["effective_yield_pct"]) + Decimal("0.05"))
    for z in data["zcyc_params"]:
        z["trade_date"] = d1
    for qs in data["curve_quote_sets"]:
        qs["snapshot_date"] = d1
        if qs.get("fetched_at"):
            qs["fetched_at"] = qs["fetched_at"].replace(d0, d1)
        for q in qs["quotes"]:
            for k in ("mid_pct", "bid_pct", "ask_pct"):
                if q.get(k) is not None:
                    q[k] = str(Decimal(q[k]) + Decimal("0.03"))
    for k in ("fx_swaps", "futures_prices"):
        for r in data[k]:
            if r.get("trade_date") == d0:
                r["trade_date"] = d1
    rows = []
    for r in data["fx_rates"]:
        if r.get("trade_date") == d0 or r.get("effective_date") == d0:
            r = copy.deepcopy(r)
            for k in ("trade_date", "effective_date", "set_date"):
                if r.get(k) in (d0,):
                    r[k] = d1
                elif k == "set_date" and r.get(k):
                    r[k] = d0
            for k in ("rate_rub_per_unit", "close_rub_per_unit", "waprice_rub_per_unit"):
                if r.get(k) is not None:
                    r[k] = str(Decimal(r[k]) * FX_MOVE)
            rows.append(r)
    data["fx_rates"] = data["fx_rates"] + rows
    return m


def _put(folder: Path, doc: dict) -> str:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    sid = hashlib.sha256(raw).hexdigest()
    (folder / f"{sid}.json.gz").write_bytes(gzip.compress(raw))
    return sid


@pytest.fixture(scope="module")
def two_dates(tmp_path_factory) -> SnapshotDir:
    tmp = tmp_path_factory.mktemp("fx_pnl")
    sd = synthetic_dir(tmp)
    _put(tmp, _roll(sd.market(DATE, "eod_final").doc, DATE, NEXT))
    return SnapshotDir(tmp)


def _pnl(sd: SnapshotDir, requests, *, proxy: bool = False):
    prev, cur = prepare(sd, DATE), prepare(sd, NEXT)
    if proxy:
        prev, cur = replace(prev, proxy_curves=True), replace(cur, proxy_curves=True)
    po, co = value(prev, requests), value(cur, requests)
    attach_pnl(prev, po, cur, co)
    return prev, cur, {o.secid: o for o in co}


def test_cny_bond_explained_in_its_currency(two_dates):
    prev, _, out = _pnl(two_dates, [(CNY_BOND, 1000.0)])
    o = out[CNY_BOND]
    assert o.valuation.discount_curve_id == "CNY XCCY" and o.no_pnl is None, o.no_pnl
    p = o.pnl
    assert p.extra()["method"] == "desk-flash-exact-fx-v1" and p.ccy == "CNY"
    f0, f1 = p.fx_rates
    assert f1 / f0 == pytest.approx(float(FX_MOVE), rel=1e-9)
    # carry and rolldown (the pages' carry) are about a day of the bond's yield on its value; the FX part is the D-1
    # PV x the rate move
    assert p.carry > 0 and p.carry + p.rolldown == pytest.approx(
        o.valuation.pv_rub * o.valuation.ytm_pct / 100 / 365, rel=0.15)
    assert p.fx_gross == pytest.approx(p.fx_delta_ccy * (f1 - f0)) and p.fx_gross > 0
    assert p.rates != 0.0 and p.spread != 0.0
    assert abs(p.other) < 0.05 * abs(p.total - p.fx_gross)
    assert p.explained == pytest.approx(p.total, abs=0.01) and abs(p.identity_error) <= 0.01
    # in RUB: the total is the move of the published RUB valuations
    assert p.total == pytest.approx(o.valuation.pv_rub - 1000.0 * prev_dirty(prev, CNY_BOND) + p.received_rub)
    assert p.fx_gross_cbr is not None and p.as_tdm()["fx_gross_cbr"] == p.fx_gross_cbr
    assert METHOD_TEXT[p.extra()["method"]]


def prev_dirty(prev, secid):
    (o,) = value(prev, [(secid, 1.0)])
    return o.valuation.dirty_rub


def test_usd_bond_on_the_proxy_explained_by_core(two_dates):
    _, _, out = _pnl(two_dates, [(USD_BOND, 1000.0)], proxy=True)
    p = out[USD_BOND].pnl
    assert out[USD_BOND].valuation.discount_curve_id == "USD UST" and p is not None, out[USD_BOND].no_pnl
    assert p.ccy == "USD" and p.carry > 0 and p.fx_gross > 0 and p.rates != 0.0
    assert p.explained == pytest.approx(p.total, abs=0.01)
    # the same day of carry as the yield rung's (below) within its curve shape
    v = out[USD_BOND].valuation
    assert p.carry + p.rolldown == pytest.approx(v.pv_rub * v.ytm_pct / 100 / 365, rel=0.15)


def test_usd_bond_without_a_curve_gets_its_yield_carry(two_dates):
    prev, _, out = _pnl(two_dates, [(USD_BOND, 1000.0)])
    o = out[USD_BOND]
    assert o.valuation.quality == "market_value"
    p = o.pnl
    assert p.extra()["method"] == "desk-flash-yield-v1" and METHOD_TEXT["desk-flash-yield-v1"]
    (po,) = value(prev, [(USD_BOND, 1000.0)])
    y, days = po.valuation.ytm_pct, 1
    assert p.carry > 0 and p.carry == pytest.approx(p.v0 * ((1 + y / 100) ** (days / 365) - 1) * float(FX_MOVE), rel=1e-6)
    assert p.rates == 0.0 and p.spread == 0.0 and p.fx_gross > 0
    assert p.carry + p.other + p.fx_gross == pytest.approx(p.total)
    assert "carry = PV D-1" in p.note and "yield move stays in other" in p.note
