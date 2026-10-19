"""Slice S6 scenario engine: the definition and its hash, the T1 Taylor against core's own approximation,
the T2 exact repricing of the golden run, and the parity fixture the browser's TS port is checked on.

Regenerate the fixture with ``DESK_REGEN_FIXTURES=1 pytest tests/test_slice_s6.py -k parity``."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest
from rub_fixed_income import BondRiskEngine, BondRiskRequest, BondScenario

from desk.marketdata.bundle import SnapshotDir, SnapshotError
from desk.orchestrator import prepare, value
from desk.publish import desk_run, tdm_run
from desk.scenario import exact
from desk.scenario.definition import DefinitionError, definition_hash, normalize, own_spread_shock
from desk.scenario.exact import reprice_run
from desk.scenario.taylor import risk_payload, run_taylor
from desk.valuation.bonds import SPREAD_CONVENTION

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
PARITY = Path(__file__).parent / "fixtures" / "taylor_parity.json"
DATE = "2026-10-15"
OFZ, FLOATER = "SU26238RMFS4", "RU000A109XR1"  # OFZ-PD bullet; corporate floater on the key rate
KEYHIKE = {"curves": {"RUB OFZ": [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35]}, "ofzSpread": 10,
           "spreads": {"_all": 25}}
CASES = {
    "par+1": {"curves": {"RUB OFZ": 1}},
    "keyhike": KEYHIKE,
    "stress22": {"curves": {"RUB OFZ": [1000, 950, 800, 650, 480, 400, 320, 270, 240, 220, 200]}, "ofzSpread": -20,
                 "fx": {"USD": 35, "CNY": 30, "EUR": 32, "CHF": 32}, "spreads": {"_mult": 2.5}},
    "sector": {"spreads": {"sector": {"Девелопмент": 300, "Лизинг": 150}, "rating": {"A": 80, "BB": 250},
                           "issuer": {"ООО Синтетик": 40}, "_mult": 1.5}},
    "rubdep": {"curves": {"RUB OFZ": 50}, "ofzSpread": 10, "fx": {"USD": 15, "CNY": 15, "EUR": 15, "CHF": 15}},
}


@pytest.fixture(scope="module")
def golden():
    """The golden date as a stored run: every fixture bond, 1000 each (one short), with gamma."""
    snaps = SnapshotDir(FIX)
    ctx = prepare(snaps, DATE)
    book = [(f"P{i:02d}", "OFZ" if s.startswith("SU") else "CORP", s, 1000.0 * (-1 if i == 3 else 1))
            for i, s in enumerate(sorted(ctx.static.bonds))]
    outs = value(ctx, [(s, q) for *_, s, q in book], position_ids=[p for p, *_ in book], second_order=True)
    content = tdm_run.run_content(ctx, [(p, b, o) for (p, b, *_), o in zip(book, outs)], None, {})
    rows = [{"position_id": r["position_id"], "book_id": r["book_id"], "secid": r["instrument_id"],
             "quantity": r["extra"]["quantity_bonds"], "quality": r["extra"]["quality"], "pv_rub": r["mv_rub"],
             "fact": desk_run._finite(r)} for r in content.rows]
    run = {"run_id": 1, "business_date": date(2026, 10, 15), "cut_type": "eod_final",
           "market_snapshot_id": ctx.market.snapshot.id}
    return snaps, ctx, run, rows, content.reference


def test_normalize_and_hash():
    a = normalize({"curves": {"RUB OFZ": 100}, "ofzSpread": 0, "spreads": {"_all": 0, "_mult": 1, "sector": {"X": 0}},
                   "fx": {"USD": 0}})
    assert a == {"curves": {"RUB OFZ": [100.0] * 11}}
    b = normalize({"fx": {}, "curves": {"RUB OFZ": [100] * 11, "OTHER": 0}})
    assert a == b and definition_hash(a) == definition_hash(b) == definition_hash(normalize(a))
    c = normalize({"spreads": {"rating": {"BB": 1}, "_all": 5}, "ofzSpread": -0.0, "curves": None})
    d = normalize({"spreads": {"_all": 5.0, "rating": {"BB": 1.0}}})
    assert c == d and definition_hash(c) == definition_hash(d) != definition_hash(a)
    assert normalize({"curves": {"RUB OFZ": [0, -0.0] + [0] * 9}}) == {} == normalize({})
    for bad in ({"curves": {"RUB OFZ": [1] * 10}}, {"name": "x"}, {"spreads": {"_avg": 1}}, {"fx": {"USD": -100}},
                {"spreads": {"_mult": 0}}, {"ofzSpread": 5001}, {"curves": {"RUB OFZ": float("nan")}},
                {"ofzSpread": True}, {"spreads": {"sector": {"": 1}}}, {"ofzSpread": 10**400}):
        with pytest.raises(DefinitionError):
            normalize(bad)
    with pytest.raises(DefinitionError, match="not published"):
        normalize({"curves": {"RUB KEY": 1}}, curves=["RUB OFZ"])
    assert normalize({"curves": {"RUB KEY": 0}}, curves=["RUB OFZ"]) == {}


def test_own_spread_shock():
    n = normalize({"ofzSpread": 7, "spreads": {"_all": 10, "_mult": 2, "sector": {"S": 3}, "issuer": {"I": 5},
                                               "rating": {"R": 11}}})
    pos = {"spread_kind": "credit", "spread_bp": 100.0, "sector": "S", "issuer": "I", "rating": "R"}
    assert own_spread_shock(n, pos) == 10 + 100 + 3 + 5 + 11
    assert own_spread_shock(n, {**pos, "spread_bp": None, "sector": None}) == 10 + 5 + 11  # no 100 bp default
    assert own_spread_shock(n, {**pos, "spread_kind": "ofz"}) == 7
    assert own_spread_shock(n, {**pos, "spread_kind": None}) == 0


def test_taylor_matches_core_approximation(golden):
    _, ctx, run, rows, reference = golden
    (row,) = [r for r in rows if r["secid"] == OFZ]
    payload = risk_payload(run, [row], reference)
    norm = normalize(KEYHIKE)
    t = run_taylor(payload, norm)["positions"][0]
    (o,) = value(ctx, [(OFZ, row["quantity"])], second_order=True)
    v = o.valuation
    engine = BondRiskEngine(BondRiskRequest(v.cashflows, v.valuation_date, ctx.curve, SPREAD_CONVENTION, "ofz",
                                            dirty_price_rub=v.dirty_rub, quantity=row["quantity"]))
    core = engine.approximate(BondScenario(tuple(norm["curves"]["RUB OFZ"]), norm["ofzSpread"]))
    assert t["total"] == pytest.approx(core.approximate_pnl_rub, rel=1e-6)
    assert t["convexity"] == pytest.approx(core.rates_gamma_pnl_rub + core.spread_gamma_pnl_rub
                                           + core.rates_spread_cross_pnl_rub, rel=1e-6)
    assert t["second_order"] and t["max_move_bp"] == 200


def test_reprice_run_parallel_bp_is_taylor(golden):
    snaps, _, run, rows, reference = golden
    for bp in (1, -1):
        res = reprice_run(snaps, run, rows, reference, {"curves": {"RUB OFZ": bp}})
        covered = [p for p in res["positions"] if p["exact"] is not None]
        assert len(covered) == res["coverage"]["n_exact"] >= 8
        for p in covered:
            assert abs(p["residual"]) <= 1e-3 * abs(p["taylor"]["total"]) + 1e-9, p


def test_reprice_run_keyhike(golden):
    snaps, _, run, rows, reference = golden
    cache: dict = {}
    res = reprice_run(snaps, run, rows, reference, KEYHIKE, cache)
    assert res["method"] == "desk-scenario-exact-v1" and res["definition_hash"] == definition_hash(normalize(KEYHIKE))
    by_q = {r["position_id"]: r["quality"] for r in rows}
    fallback = sorted(pid for pid, q in by_q.items() if q not in ("exact", "assumed"))
    cov = res["coverage"]
    assert cov["status"] == "partial" and fallback
    assert cov == {"status": "partial", "n_positions": len(rows), "n_exact": len(rows) - len(fallback),
                   "missing": [{"position_id": pid, "reason": by_q[pid]} for pid in fallback]}
    assert not any(p["code"] == "base_mismatch" for p in res["positions"])
    (fl,) = [p for p, r in zip(res["positions"], rows) if r["secid"] == FLOATER]
    assert fl["quality"] == "assumed" and fl["exact"] is not None and fl["code"] is None
    tot = res["totals"]["_total"]
    covered = [p for p in res["positions"] if p["exact"] is not None]
    assert tot["n_positions"] == len(rows) and tot["n_exact"] == len(covered)
    assert tot["exact"] == pytest.approx(sum(p["exact"] for p in covered))
    assert tot["residual"] == pytest.approx(tot["exact"] - tot["taylor_covered"])
    assert tot["blended"] == pytest.approx(tot["exact"] + tot["taylor"] - tot["taylor_covered"])
    assert set(res["totals"]) == {"_total", "OFZ", "CORP"}
    assert list(cache) == [run["run_id"]]
    assert reprice_run(snaps, run, rows, reference, KEYHIKE, cache) == res  # from the cached context


def test_reprice_run_passes_the_fx_shocks(golden, monkeypatch):
    snaps, _, run, rows, reference = golden
    seen, core_value = [], exact.value
    monkeypatch.setattr(exact, "value", lambda *a, scenarios, **k: seen.extend(scenarios) or core_value(
        *a, scenarios=scenarios, **k))
    reprice_run(snaps, run, rows, reference, {"curves": {"RUB OFZ": 1}, "fx": {"USD": 15, "CNY": 10}})
    assert seen and all(s.fx_shocks_pct == (("CNY", 10.0), ("USD", 15.0)) for s in seen)


def test_reprice_run_needs_the_runs_own_snapshot(golden):
    snaps, ctx, run, rows, reference = golden
    with pytest.raises(SnapshotError):
        reprice_run(snaps, {**run, "market_snapshot_id": "0" * 64}, rows, reference, KEYHIKE)
    with pytest.raises(SnapshotError):
        prepare(snaps, DATE, market_snapshot_id=ctx.static.snapshot.id)  # the static one, not a market


def _sig(x):
    """Payload numbers at 10 significant digits: the fixture checks arithmetic parity, so it must not move
    with the last bit of core's numerics on another platform."""
    if isinstance(x, float):
        return float(f"{x:.10g}")
    if isinstance(x, list):
        return [_sig(v) for v in x]
    if isinstance(x, dict):
        return {k: _sig(v) for k, v in x.items()}
    return x


def _parity(golden) -> str:
    _, _, run, rows, reference = golden
    risk = {"RUB OFZ": dict.fromkeys(["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y+"], 0.0)}
    synth = [  # an FX position, a credit row with sector and issuer rules, a credit row without a spread
        {"position_id": "S01", "book_id": "FX", "secid": "SYNTH-FX", "quantity": 1.0, "quality": "market_value",
         "pv_rub": 11_500_000.0, "fact": {"name": "Синтетический CNY", "issuer": "ООО Синтетик", "cs01": 0.0,
                                          "ofz_spr01": 0.0, "spread_bp": None, "fx_delta": {"CNY": 1_000_000.0},
                                          "extra": {"fx_rate": 11.5}, "gamma": [],
                                          "risk": {"RUB OFZ": {**risk["RUB OFZ"], "1Y": -95.0}}}},
        {"position_id": "S02", "book_id": "CORP", "secid": "SYNTH-DEV", "quantity": 100.0, "quality": "exact",
         "pv_rub": 98_000.0, "fact": {"name": "Девелопер", "issuer": "ООО Синтетик", "cs01": -21.5, "ofz_spr01": 0.0,
                                      "spread_bp": 412.25, "risk": {"RUB OFZ": {**risk["RUB OFZ"], "2Y": -12.0,
                                                                                "3Y": -9.5}},
                                      "gamma": [["IR|RUB OFZ|2Y", "IR|RUB OFZ|2Y", 0.0031],
                                                ["IR|RUB OFZ|3Y", "IR|RUB OFZ|2Y", 0.0007],
                                                ["CS", "CS", 0.0042], ["IR|RUB OFZ|2Y", "CS", 0.0019]]}},
        {"position_id": "S03", "book_id": "CORP", "secid": "SYNTH-NOSPR", "quantity": 10.0, "quality": "model_price",
         "pv_rub": 9_700.0, "fact": {"name": "Без спреда", "issuer": "АО Лизинг-Синт", "cs01": -3.25,
                                     "ofz_spr01": 0.0, "spread_bp": None, "gamma": [],
                                     "risk": {"RUB OFZ": {**risk["RUB OFZ"], "5Y": -3.0}}}},
    ]
    ref = {**reference, "issuers": [*reference["issuers"],
                                    {"issuer": "ООО Синтетик", "sector": "Девелопмент", "rating": None},
                                    {"issuer": "АО Лизинг-Синт", "sector": "Лизинг", "rating": "BB"}]}
    payload = _sig(risk_payload(run, rows + synth, ref))
    cases = []
    for name, d in CASES.items():
        norm = normalize(d, payload["curves"])
        cases.append({"name": name, "definition": norm, "payload": payload, "expected": run_taylor(payload, norm)})
    return json.dumps({"version": 1, "tolerance_rel": 1e-9, "cases": cases}, sort_keys=True, indent=1,
                      ensure_ascii=False, allow_nan=False) + "\n"


def test_parity_fixture_is_current(golden):
    fresh = _parity(golden)
    if os.environ.get("DESK_REGEN_FIXTURES") == "1":
        PARITY.write_text(fresh, encoding="utf-8")
    assert PARITY.read_text(encoding="utf-8") == fresh, \
        "taylor_parity.json is stale: DESK_REGEN_FIXTURES=1 pytest tests/test_slice_s6.py -k parity"
    doc = json.loads(fresh)
    keyhike = next(c for c in doc["cases"] if c["name"] == "keyhike")
    p = keyhike["payload"]
    assert p["factors"][-3:] == ["CS", "FX|CNY", "FX|USD"] and p["curves"] == ["RUB OFZ"]
    by = {x["position_id"]: x for x in p["positions"]}
    assert (by["S02"]["sector"], by["S03"]["rating"], by["S03"]["spread_bp"]) == ("Девелопмент", "BB", None)
    assert all(i <= j for x in p["positions"] for i, j, _ in x["gamma"])
    sector = next(c for c in doc["cases"] if c["name"] == "sector")
    s02 = next(x for x in sector["expected"]["positions"] if x["position_id"] == "S02")
    assert s02["spread"] == pytest.approx(-21.5 * (0.5 * 412.25 + 300 + 40))
