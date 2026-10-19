"""Slice S1 on the hub's golden fixtures (Market Snapshot Contract v1, public data of 2026-10-15).

Real-data runs use bundles exported from the seed (tools/devdata) and are not committed; these
tests pin the same path on the ten golden instruments.
"""

from __future__ import annotations

import gzip
import math
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from rub_fixed_income import kbd_zero_rate_bp

from desk.marketdata.bundle import SnapshotDir, SnapshotError, read_bundle
from desk.orchestrator import check_against_standard_yields, prepare, value
from desk.publish import tdm_run
from desk.report import validation

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


@pytest.fixture(scope="module")
def outcomes(ctx):
    return {o.secid: o for o in value(ctx, [(s, 1.0) for s in sorted(ctx.static.bonds)], second_order=True)}


def test_bundle_identity_is_verified(tmp_path):
    src = next(FIX.glob("*.json.gz"))
    assert read_bundle(src).id == src.name.split(".")[0]
    bad = tmp_path / ("0" * 64 + ".json.gz")
    bad.write_bytes(gzip.compress(gzip.decompress(src.read_bytes()) + b" "))
    with pytest.raises(SnapshotError):
        read_bundle(bad)


def test_kbd_reproduces_published_standard_yields(ctx):
    assert check_against_standard_yields(ctx.kbd, ctx.market.zcyc["standard_yields"]) < 0.01
    # the core curve built from it agrees with the formula between nodes
    t = 3.3
    d = ctx.business_date.toordinal() + round(t * 365)
    df = ctx.curve.discount_factor(date.fromordinal(d))
    t_act = (date.fromordinal(d) - ctx.business_date).days / 365.0
    assert abs(-1e4 * math.log(df) / t_act - kbd_zero_rate_bp(ctx.kbd, t_act)) < 0.01


def test_coverage_reasons(outcomes):
    q = {s: (o.valuation.quality if o.valuation else None, o.reason) for s, o in outcomes.items()}
    assert q["SU26238RMFS4"] == ("exact", None)  # OFZ-PD bullet
    assert q["SU29008RMFS8"] == ("assumed", None)  # OFZ-PK on an inferred formula (S4)
    assert q["RU000A1034P7"] == ("assumed", None)  # perpetual to an assumed call (S4)
    assert q["SU52005RMFS4"] == ("market_value", "face_indexed")  # OFZ-IN: no CPI series yet
    # the owner's rule: every bond is valued, and every non-exact one says why
    assert all(o.valuation is not None for o in outcomes.values())
    assert all(o.valuation.assumptions for o in outcomes.values() if o.valuation.quality != "exact")


def test_ofz_matches_moex_analytics(ctx, outcomes):
    row = validation.compare(ctx, outcomes["SU26238RMFS4"])
    assert row["check"] == "ok", row
    assert abs(row["d_y_wap_bp"]) < 1.0
    assert abs(row["d_dur_days"]) < 2.0
    assert row["horizon_match"] is True


def test_put_offer_is_the_horizon(outcomes):
    v = outcomes["RU000A0JXQ44"].valuation  # RZD with a scheduled put
    assert v is not None and v.horizon_kind == "offer"


def test_risk_is_consistent(outcomes):
    v = outcomes["SU26238RMFS4"].valuation
    assert v.spread_kind == "ofz" and v.dv01_rub < 0 and v.spread01_rub < 0
    assert abs(sum(v.buckets.values()) - v.dv01_rub) < 1e-9  # core reports no separate parallel DV01
    assert v.hessian and v.curve_convexity and v.curve_convexity > 0


def test_publish_to_tdm(ctx, outcomes, tmp_path):
    db = tmp_path / "tdm.sqlite"
    positions = [("P1", "OFZ", outcomes["SU26238RMFS4"]), ("P2", "OFZ", outcomes["SU52005RMFS4"])]
    run_id, status, warnings = tdm_run.publish(str(db), ctx, positions, prev=None)
    assert status == "complete"
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM position WHERE run_id=?", (run_id,)).fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM position_gamma WHERE run_id=?", (run_id,)).fetchone()[0] > 0
    assert any("SU52005RMFS4" in w for w in warnings)  # the market-value OFZ-IN is published and flagged
