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
from desk.scenario.definition import RUB_CURVES, DefinitionError, definition_hash, normalize, own_spread_shock
from desk.scenario.exact import REPRICED, reprice_run
from desk.scenario.taylor import risk_payload, run_taylor
from desk.valuation.bonds import SPREAD_CONVENTION

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
PARITY = Path(__file__).parent / "fixtures" / "taylor_parity.json"
DATE = "2026-10-15"
OFZ, FLOATER = "SU26238RMFS4", "RU000A109XR1"  # OFZ-PD bullet; corporate floater on the key rate
KEYHIKE = {"curves": {"RUB RUONIA": [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35]}, "ofzSpread": 10,
           "spreads": {"_all": 25}}
CASES = {
    "par+1": {"curves": {"RUB RUONIA": 1}},
    "keyhike": KEYHIKE,
    "stress22": {"curves": {"RUB RUONIA": [1000, 950, 800, 650, 480, 400, 320, 270, 240, 220, 200]}, "ofzSpread": -20,
                 "fx": {"USD": 35, "CNY": 30, "EUR": 32, "CHF": 32}, "spreads": {"_mult": 2.5}},
    "sector": {"spreads": {"sector": {"Девелопмент": 300, "Лизинг": 150}, "rating": {"A": 80, "BB": 250},
                           "issuer": {"ООО Синтетик": 40}, "_mult": 1.5}},
    "rubdep": {"curves": {"RUB RUONIA": 50}, "ofzSpread": 10, "fx": {"USD": 15, "CNY": 15, "EUR": 15, "CHF": 15}},
    # R8: every RUB curve, the key-rate basis alone, a currency's curve with its FX
    "rub+50": {"curves": dict.fromkeys(RUB_CURVES, 50)},
    "basis": {"curves": {"RUB KEY": [50, 50, 45, 40, 30, 25, 20, 15, 10, 10, 10]}},
    "usd": {"curves": {"USD": [30, 30, 30, 25, 20, 20, 15, 15, 10, 10, 10]}, "fx": {"USD": 10}},
    # the KBD alone moves the fixed-coupon OFZ only (#32)
    "kbd": {"curves": {"RUB OFZ": 100}},
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
        normalize({"curves": {"USD": 1}}, curves=["RUB OFZ"])
    assert normalize({"curves": {"USD": 0}}, curves=["RUB OFZ"]) == {}
    # the RUB curves core reprices are valid for every run, also one published with RUB OFZ only (before R8)
    assert normalize({"curves": {"RUB KEY": 1}}, curves=["RUB OFZ"]) == {"curves": {"RUB KEY": [1.0] * 11}}


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
    # the key-rate hike on the KBD: a fixed OFZ is discounted on it (#32), its par buckets are RUB OFZ's factors
    norm = normalize({**KEYHIKE, "curves": {"RUB OFZ": KEYHIKE["curves"]["RUB RUONIA"]}})
    t = run_taylor(payload, norm)["positions"][0]
    (o,) = value(ctx, [(OFZ, row["quantity"])], second_order=True)
    v = o.valuation
    assert v.discount_curve_id == "RUB_KBD"
    engine = BondRiskEngine(BondRiskRequest(v.cashflows, v.valuation_date, ctx.curve, SPREAD_CONVENTION, "ofz",
                                            dirty_price_rub=v.dirty_rub, quantity=row["quantity"]))
    # core 0.38 refuses the KBD's 1M-6M unless 0 or the 1Y shift: as the exact repricing (exact.kbd_shock)
    core = engine.approximate(BondScenario(exact.kbd_shock(tuple(norm["curves"]["RUB OFZ"])), norm["ofzSpread"]))
    assert t["total"] == pytest.approx(core.approximate_pnl_rub, rel=1e-6)
    assert t["convexity"] == pytest.approx(core.rates_gamma_pnl_rub + core.spread_gamma_pnl_rub
                                           + core.rates_spread_cross_pnl_rub, rel=1e-6)
    assert t["second_order"] and t["max_move_bp"] == 150  # its largest move where it has risk: 1Y (KBD 1M-6M carry none)


def test_reprice_run_parallel_bp_is_taylor(golden):
    snaps, _, run, rows, reference = golden
    dv01 = {r["position_id"]: r["fact"]["dv01"] for r in rows}
    for bp in (1, -1):
        res = reprice_run(snaps, run, rows, reference, {"curves": {"RUB RUONIA": bp}})
        covered = [p for p in res["positions"] if p["exact"] is not None]
        assert len(covered) == res["coverage"]["n_exact"] >= 8
        for p in covered:  # + an OFZ's RUONIA rows: core's closed form is central differences of the rebuilt OIS
            # curve there (core docs/ofz-basis.md, ~3e-6 of its DV01 at 1 bp), and RUONIA alone moves it by RUONIA - basis
            assert abs(p["residual"]) <= 1e-3 * abs(p["taylor"]["total"]) + 5e-6 * abs(dv01[p["position_id"]]) + 1e-9, p


def test_reprice_run_keyhike(golden):
    snaps, _, run, rows, reference = golden
    cache: dict = {}
    res = reprice_run(snaps, run, rows, reference, KEYHIKE, cache)
    assert res["method"] == "desk-scenario-exact-v3" and res["definition_hash"] == definition_hash(normalize(KEYHIKE))
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


def test_reprice_run_moves_each_curve_by_its_own_shifts(golden):
    """R8: core moves the RUONIA and key-rate curves by name and the KBD by RUB OFZ, each by its own vector only, as
    the Taylor moves each curve's factors: one bp on every RUB curve, and one curve alone, reprice the bonds as their
    per-curve risk says. The KBD alone moves the fixed-coupon OFZ (on it since #32) and no other bond: with RUONIA held
    it is the OFZ basis (TDM#213), its risk the KBD's par risk; RUONIA moves the OFZ by RUONIA with the basis held
    (``RUB OFZ - RUB RUONIA``), so a move of every RUB curve alike is the OFZ's RUONIA risk."""
    snaps, _, run, rows, reference = golden
    sc = exact.core_scenario("x", normalize({"curves": {"RUB KEY": 2, "RUB OFZ": 1, "RUB RUONIA": 0.5}}), 3.0)
    assert sc.shock.curve_shifts_bp == (0.0,) * 3 + (1.0,) * 8 and sc.shock.spread_shift_bp == 3.0  # KBD: no 1M-6M
    assert dict(sc.curve_shifts_bp) == {"RUB_KEY_RATE_PROJ": (2.0,) * 11, "RUB_RUONIA_OIS": (0.5,) * 11,
                                        "RUB_OFZ_BASIS": (0.0,) * 3 + (0.5,) * 8}  # the basis: KBD - RUONIA
    dv01 = {r["position_id"]: r["fact"]["dv01"] for r in rows}
    floaters = {r["position_id"] for r in rows if r["secid"] in ("SU29008RMFS8", FLOATER)}
    assert len(floaters) == 2  # the OFZ-PK on RUONIA, the corporate on the key rate
    on_kbd = {r["position_id"] for r in rows if "RUB OFZ" in r["fact"]["risk"]}
    assert on_kbd and all(r["secid"].startswith("SU") for r in rows if r["position_id"] in on_kbd)
    computed = {r["position_id"]: r["fact"]["extra"] for r in rows if "ofz_ruonia01" in r["fact"]["extra"]}
    assert computed and set(computed) <= on_kbd
    for d in ({"curves": dict.fromkeys(RUB_CURVES, 1)}, {"curves": {"RUB KEY": 1}}, {"curves": {"RUB RUONIA": -1}}):
        res = reprice_run(snaps, run, rows, reference, d)
        moved = [p for p in res["positions"] if p["position_id"] in floaters and p["taylor"]["rates"]]
        assert moved and all(p["exact"] is not None for p in moved)
        for p in res["positions"]:
            if p["exact"] is not None:
                assert abs(p["residual"]) <= 1e-3 * abs(p["taylor"]["total"]) + 5e-6 * abs(dv01[p["position_id"]]) \
                    + 1e-6, (d, p)
    kbd = reprice_run(snaps, run, rows, reference, {"curves": {"RUB OFZ": 100}})
    assert kbd["coverage"]["n_exact"] >= 8
    for p in kbd["positions"]:
        if p["position_id"] in on_kbd and p["exact"] is None:  # an OFZ-IN on its fallback rung: Taylor only
            assert p["code"] not in REPRICED and p["taylor"]["rates"], p
        elif p["position_id"] in on_kbd:  # 100 bp of par yields: the Taylor (with its gamma) and core agree closely
            assert p["exact"] * p["taylor"]["rates"] > 0
            assert abs(p["residual"]) <= 1e-2 * abs(p["taylor"]["total"]), p
        else:
            assert p["taylor"]["total"] == 0 and (p["exact"] is None or abs(p["exact"]) < 1e-6), p
    # a move of all RUB curves alike: for a fixed bond (one curve) the move of its own curve alone; for an OFZ in
    # RUONIA + basis its RUONIA risk (the basis held), the KBD alone its basis risk (RUONIA held), two computed risks
    par = {p["position_id"]: p for p in reprice_run(snaps, run, rows, reference,
                                                    {"curves": dict.fromkeys(RUB_CURVES, 1)})["positions"]}
    ruonia = reprice_run(snaps, run, rows, reference, {"curves": {"RUB RUONIA": 1}})["positions"]
    ofz = {p["position_id"]: p for p in reprice_run(snaps, run, rows, reference,
                                                    {"curves": {"RUB OFZ": 1}})["positions"]}
    for p in ruonia:
        pid = p["position_id"]
        if p["exact"] is None or pid in floaters:
            continue
        if pid in computed:
            assert par[pid]["exact"] == pytest.approx(computed[pid]["ofz_ruonia01"], rel=1e-3)
            assert ofz[pid]["exact"] == pytest.approx(computed[pid]["ofz_basis01"], rel=1e-3)
            assert par[pid]["exact"] != pytest.approx(ofz[pid]["exact"], rel=1e-5)  # not one risk seen twice
        else:
            own = ofz[pid]["exact"] if pid in on_kbd else p["exact"]
            assert par[pid]["exact"] == pytest.approx(own, rel=1e-9, abs=1e-6)


def test_kbd_short_end_is_dropped_before_core(golden):
    """Core 0.38 refuses a scenario on the par-yield KBD (``rub_ofz_kbd_v3``) whose 1M-6M shifts are neither 0 nor its
    1Y shift (``InvalidTerms``, the position ``CALCULATION_FAILED``); 0.37 dropped them. ``core_scenario`` sets them to
    0, so a KBD shock with an uneven short end reprices the OFZ exactly, as the same shock without it (below 1Y the
    overlay follows 1Y) and as the Taylor (no KBD risk there)."""
    snaps, _, run, rows, reference = golden
    tail = [200.0] * 8
    uneven = {"curves": {"RUB OFZ": [300.0, 250.0, 225.0, *tail]}}
    sc = exact.core_scenario("x", normalize(uneven), 0.0)
    assert sc.shock.curve_shifts_bp == (0.0, 0.0, 0.0, *tail)
    on_kbd = {r["position_id"] for r in rows if "RUB OFZ" in r["fact"]["risk"]}
    res = {p["position_id"]: p for p in reprice_run(snaps, run, rows, reference, uneven)["positions"]}
    flat = {p["position_id"]: p for p in reprice_run(snaps, run, rows, reference,
                                                      {"curves": {"RUB OFZ": [0.0, 0.0, 0.0, *tail]}})["positions"]}
    par = {p["position_id"]: p for p in reprice_run(snaps, run, rows, reference,
                                                     {"curves": {"RUB OFZ": 200}})["positions"]}
    repriced = [pid for pid in on_kbd if res[pid]["quality"] in REPRICED]
    assert repriced
    for pid in repriced:
        assert res[pid]["exact"] is not None and res[pid]["code"] is None, res[pid]
        assert res[pid]["exact"] == flat[pid]["exact"] == pytest.approx(par[pid]["exact"], rel=1e-12)
        assert res[pid]["taylor"] == flat[pid]["taylor"]


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


GAMMA_EPS = 1e-8
"""RUB/bp^2: a Hessian entry below it is numerical noise of core's second order (2^-35-sized entries that
come and go between platforms), not risk (1e-8 x 500 bp x 500 bp = 2.5e-3 RUB); dropped from the payload."""
RTOL, ATOL = 1e-6, 1e-8
"""How far a payload number may move between platforms (core's last bits, then ``_sig``)."""


def _denoise(payload):
    return {**payload, "positions": [{**p, "gamma": [g for g in p["gamma"] if abs(g[2]) >= GAMMA_EPS]}
                                     for p in payload["positions"]]}


def _same(a, b, path="", rtol=RTOL, atol=ATOL) -> list[str]:
    """Structural comparison: same keys, lengths, strings and None; numbers to rtol/atol."""
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return [f"{path}: keys {sorted(set(a) ^ set(b))}"]
        return [d for k in a for d in _same(a[k], b[k], f"{path}/{k}", rtol, atol)]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in _same(x, y, f"{path}[{i}]", rtol, atol)]
    num = (int, float)
    if isinstance(a, num) and isinstance(b, num) and not isinstance(a, bool) and not isinstance(b, bool):
        return [] if abs(a - b) <= rtol * max(abs(a), abs(b)) + atol else [f"{path}: {a!r} != {b!r}"]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


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
    payload = _denoise(_sig(risk_payload(run, rows + synth, ref)))
    cases = []
    for name, d in CASES.items():
        norm = normalize(d, payload["curves"])
        cases.append({"name": name, "definition": norm, "payload": payload, "expected": run_taylor(payload, norm)})
    return json.dumps({"version": 1, "tolerance_rel": 1e-9, "cases": cases}, sort_keys=True, indent=1,
                      ensure_ascii=False, allow_nan=False) + "\n"


def test_parity_fixture_is_current(golden):
    """Structural, not textual: the committed fixture's definitions and payloads (Hessian noise dropped on both
    sides) equal a fresh generation to RTOL/ATOL, and its expected results are ``run_taylor`` of its own payload
    (what the TS port is checked against). A textual compare broke on the last bits of core's second order."""
    fresh = _parity(golden)
    if os.environ.get("DESK_REGEN_FIXTURES") == "1":
        PARITY.write_text(fresh, encoding="utf-8")
    stale = "taylor_parity.json is stale: DESK_REGEN_FIXTURES=1 pytest tests/test_slice_s6.py -k parity"
    doc, new = json.loads(PARITY.read_text(encoding="utf-8")), json.loads(fresh)
    assert {k: v for k, v in doc.items() if k != "cases"} == {k: v for k, v in new.items() if k != "cases"}, stale
    assert [c["name"] for c in doc["cases"]] == [c["name"] for c in new["cases"]], stale
    for c, n in zip(doc["cases"], new["cases"]):
        assert c["definition"] == n["definition"], stale
        diff = _same(_denoise(c["payload"]), n["payload"], c["name"])
        assert not diff, stale + "\n" + "\n".join(diff[:20])
        diff = _same(c["expected"], run_taylor(c["payload"], c["definition"]), c["name"], doc["tolerance_rel"], 1e-12)
        assert not diff, stale + "\n" + "\n".join(diff[:20])
    keyhike = next(c for c in doc["cases"] if c["name"] == "keyhike")
    p = keyhike["payload"]
    assert p["factors"][-3:] == ["CS", "FX|CNY", "FX|USD"]
    assert p["curves"] == ["RUB KEY", "RUB OFZ", "RUB RUONIA", "USD"]
    assert {f.rsplit("|", 1)[0] for f in p["factors"] if f.startswith("IR|")} == {f"IR|{c}" for c in p["curves"]}
    basis = next(c for c in doc["cases"] if c["name"] == "basis")["expected"]["positions"]
    assert sum(1 for x in basis if x["rates"]) == 1  # only the key-rate floater has risk on RUB KEY
    by = {x["position_id"]: x for x in p["positions"]}
    assert (by["S02"]["sector"], by["S03"]["rating"], by["S03"]["spread_bp"]) == ("Девелопмент", "BB", None)
    assert all(i <= j for x in p["positions"] for i, j, _ in x["gamma"])
    sector = next(c for c in doc["cases"] if c["name"] == "sector")
    s02 = next(x for x in sector["expected"]["positions"] if x["position_id"] == "S02")
    assert s02["spread"] == pytest.approx(-21.5 * (0.5 * 412.25 + 300 + 40))


def test_exact_context_takes_the_runs_curve_risk_unit(golden):
    """#171: a run made with ``quote_curve_risk`` (its lineage flag, ``scenario.jobs``) is repriced with its curves in
    the same unit as its Taylor factors; a run without the flag, as before."""
    snaps, _, run, *_ = golden
    assert not exact._context(snaps, run, None).quote_curve_risk
    assert exact._context(snaps, {**run, "quote_curve_risk": True}, None).quote_curve_risk


def test_fx_shock_is_net_of_the_funding_liability_in_both_scenarios(golden):
    """TDM#249: a bond funded in its currency has its FX delta net of the liability (#248); the Taylor shocks that net
    delta and the exact repricing adds the liability's move to the bond's (``exact.liability_pnl``), so an FX shock
    gives the same PnL in both. A RUB bond of the golden run is given a USD liability of 1000 and that net delta."""
    import copy

    snaps, _, run, rows, reference = golden
    rows = copy.deepcopy(rows)
    r = next(x for x in rows if x["quality"] in REPRICED and x["secid"] and x["secid"].startswith("RU"))
    ex = r["fact"]["extra"]
    ex.update(fx_rate=90.0, fx_liability={"state": "computed", "ccy": "USD", "amount_ccy": -1000.0})
    r["fact"]["fx_delta"] = {"USD": -1000.0}
    res = {p["position_id"]: p for p in reprice_run(snaps, run, rows, reference, {"fx": {"USD": 10}})["positions"]}
    p = res[r["position_id"]]
    assert p["taylor"]["fx"] == pytest.approx(-1000.0 * 90.0 * 10 / 100)
    assert p["exact"] == pytest.approx(p["taylor"]["total"], abs=1e-6)  # the RUB bond itself does not move
    assert exact.liability_pnl(r["fact"], {"fx": {"USD": 10}}) == pytest.approx(-9000.0)
    assert exact.liability_pnl(r["fact"], {"fx": {"CNY": 10}}) == 0.0
    ex["fx_liability"]["state"] = "none"
    assert exact.liability_pnl(r["fact"], {"fx": {"USD": 10}}) == 0.0
