"""Interest rate swaps and REPO from private/1.4 ``trade_terms`` through core ``value_portfolio`` (TDM#45).

On the hub's contract 1.4 golden snapshot of 15.10.2026 (``fixtures/mdhub_v1_4``: RUONIA OIS and IRS KEYRATE quotes,
RUONIA and key-rate fixings from 01.10) and synthetic trades (invented amounts and rates):

- an OIS on RUONIA (seasoned: it reads the fixings) and a key-rate IRS (forward start: projected on the key-rate
  forward curve) become core ``InterestRateSwap`` positions with rates risk per curve (``RUB RUONIA``, ``RUB KEY``),
  not one bucket of the report's DV01;
- a REPO (fixed and on the key rate) becomes a core ``RepoTrade``;
- the run's RUONIA fixings reach the core request; a declared unpublished day is carried and said, a gap inside the
  history makes core reject the swap (the report's value, with the reason);
- the parallel report (``derivatives_parallel``) shows them ``both``, a CCS ``imported_only`` with ``ccs_unsupported``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from rub_fixed_income import FixingEntry

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.publish import derivatives_parallel, tdm_run
from desk.store import PositionInputs
from desk.valuation import derivatives as dv
from desk.valuation import positions as pv1

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1_4"
BD = date(2026, 10, 15)


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), BD.isoformat())


def leg(tid, leg_id, side, kind="ois", **kw):
    """One swap leg row as the store gives it (amounts as decimal strings, dates ISO)."""
    return {"trade_id": tid, "leg_id": leg_id, "book_id": "IRB", "product_kind": kind, "trade_date": "2026-10-05",
            "side": side, "ccy": "RUB", "notional_ccy": "1000000000", "start_date": "2026-10-06",
            "maturity_date": "2028-10-06", **kw}


def repo(tid, side, start, end, **kw):
    return {"trade_id": tid, "book_id": "IRB", "product_kind": "repo", "trade_date": start, "side": side,
            "ccy": "RUB", "notional_ccy": "500000000", "start_date": start, "maturity_date": end, **kw}


TERMS = [
    leg("S1", "fix", "pay", fixed_rate_pct="15.5", pv_rub="-14000000"),
    leg("S1", "flt", "receive", index_code="RUONIA"),
    leg("K1", "fix", "receive", kind="irs", fixed_rate_pct="14", start_date="2026-10-16", maturity_date="2027-10-18"),
    leg("K1", "flt", "pay", kind="irs", index_code="KEY_RATE", start_date="2026-10-16", maturity_date="2027-10-18"),
    repo("R1", "reverse_repo", "2026-10-14", "2026-11-16", fixed_rate_pct="16.1", day_count="ACT/365"),
    repo("R2", "repo", "2026-10-09", "2026-10-20", index_code="KEY_RATE", spread_bp="-50"),
    repo("R3", "repo", "2026-10-14", None, fixed_rate_pct="15"),
    {**leg("C1", "rub", "pay", kind="ccs", fixed_rate_pct="15"), "pv_rub": "250000"},
    {**leg("C1", "usd", "receive", kind="ccs", index_code="SOFR"), "ccy": "USD", "notional_ccy": "12000000"},
    leg("B1", "a", "pay", index_code="RUONIA"), leg("B1", "b", "receive", kind="ois", index_code="KEY_RATE"),
]
REPORT_PV = {"S1": -14_000_000.0, "K1": -3_000_000.0, "R1": 500_100_000.0, "R2": None, "R3": -300_000_000.0,
             "C1": None, "B1": 1000.0}


def inputs(trade_terms=TERMS, pids=tuple(REPORT_PV)):
    extra = {pid: {"asset_class": "otc", "ccy": "RUB", "source_instrument_id": pid, "deal_id": pid,
                   **({"mv_rub": REPORT_PV[pid]} if REPORT_PV[pid] is not None and pid != "S1" else {})}
             for pid in pids}
    return PositionInputs([(p, "IRB", None, None) for p in extra], [], {"IRB": BD}, datetime.now(UTC), "0" * 64,
                          extra, trade_terms=list(trade_terms))


@pytest.fixture(scope="module")
def plan(ctx):
    return pv1.plan(ctx, inputs())


def test_swaps_and_repos_valued_by_core_with_risk_per_curve(plan):
    s1, k1, r1, r2 = (plan.direct[k].valuation for k in ("S1", "K1", "R1", "R2"))
    assert s1.extra["core"]["product"] == "ois" and k1.extra["core"]["product"] == "irs"
    assert r1.extra["core"]["product"] == r2.extra["core"]["product"] == "repo"
    # the payer of a seasoned 2Y OIS: risk on the RUONIA curve, by bucket, long rates (gains when rates rise)
    assert set(s1.curve_buckets) == {"RUB RUONIA"} and s1.dv01_rub > 0
    assert sum(1 for x in s1.curve_buckets["RUB RUONIA"].values() if x) >= 2  # not one bucket of the report's DV01
    sw = s1.extra["swaps"][0]
    assert (sw["side"], sw["index"], sw["notional_rub"]) == ("payer", "RUONIA", 1e9)
    assert sw["pv_rub"] == pytest.approx(sw["fixed_leg_pv_rub"] + sw["float_leg_pv_rub"])
    assert sw["pv_rub"] == pytest.approx(s1.pv_rub) and sw["fixing_id"] == "RUONIA"
    assert "known_fixings" in s1.extra["core"]["model_flags"]
    # the receiver of a key-rate IRS starting tomorrow: discounted on RUONIA, projected on the key rate
    assert set(k1.curve_buckets) == {"RUB RUONIA", "RUB KEY"} and k1.extra["swaps"][0]["projection_curve"] == "RUB KEY"
    assert k1.extra["swaps"][0]["fixing_id"] is None  # nothing fixed yet: no history read
    assert k1.curve_buckets["RUB KEY"] and sum(k1.curve_buckets["RUB KEY"].values()) < 0  # receives fixed
    # REPOs: a lender's PV near its cash, a borrower's key-rate REPO with key-rate risk
    rp = r1.extra["repos"][0]
    assert r1.quantity == 5e8 and 5e8 < r1.pv_rub < 5.1e8 and rp["end_amount_source"] == "fixed"
    assert r2.quantity == -5e8 and r2.pv_rub < 0 and "RUB KEY" in r2.curve_buckets
    assert r2.extra["repos"][0]["fixing_id"] == "KEY_RATE"
    assert any("ACT/365-366" in a for a in r2.assumptions) and not any("ACT/365-366" in a for a in r1.assumptions)
    # the reconciliation with the report: mv_rub of the row, else the trades' pv_rub
    assert s1.extra["reported_pv_rub"] == -14e6 and s1.extra["pv_diff_rub"] == pytest.approx(s1.pv_rub + 14e6)
    assert k1.extra["reported_pv_rub"] == -3e6
    assert plan.unlinked_terms == []  # swaps and REPOs are not FX trades to add


def test_at_par_rate_a_swap_is_worth_nothing(ctx, plan):
    k1 = plan.direct["K1"].valuation.extra["swaps"][0]
    par = f"{k1['par_rate_pct']:.10f}"
    t = [{**r, "fixed_rate_pct": par} if r["trade_id"] == "K1" and r.get("fixed_rate_pct") else r for r in TERMS]
    v = pv1.plan(ctx, inputs(t, ("K1",))).direct["K1"].valuation
    assert abs(v.pv_rub) < 1.0 and abs(v.extra["swaps"][0]["par_rate_pct"] - k1["par_rate_pct"]) < 1e-9


def test_what_core_does_not_value_keeps_the_report(plan):
    for pid, reason in (("C1", "ccs_unsupported"), ("B1", "basis_swap_unsupported"), ("R3", "repo_open")):
        o = plan.direct[pid]
        assert o.core_not_valued[0] == reason, (pid, o.core_not_valued)
        assert o.valuation is None or o.valuation.assumptions[0].startswith(f"trade terms not valued by core ({reason}")


def test_unvalued_reason_is_cores_not_waiting_for_terms(plan):
    """PR #199 review: a row with terms core does not value and no mv_rub is unvalued for core's reason, which the
    coverage screen explains (not «a swap waiting for its terms»); the report's gap stays in the detail."""
    from desk.api import coverage

    c1 = plan.direct["C1"]  # a CCS without mv_rub
    assert c1.valuation is None and c1.reason == "ccs_unsupported" and "no mv_rub" in c1.detail
    assert plan.direct["R3"].reason == "repo_open"  # an open REPO with mv_rub: the report's value, core's reason
    for code in ("ccs_unsupported", "basis_swap_unsupported", "repo_open", "no_fixings", "no_key_rate_curve"):
        key, why, todo = coverage.cause(code, "IRS", None, None)
        assert key == code and "trade_terms" not in why and todo
    assert "условий сделки" in coverage.cause("no_reported_value", "IRS", None, None)[1]  # no terms at all: as before
    repo = coverage.cause("no_reported_value", "UNCLASSIFIED", "REPO", None)
    assert repo[0] == "repo" and "рыночного риска" not in repo[1]


def test_parallel_report_both_and_imported_only(plan, ctx):
    inp = inputs()
    block = derivatives_parallel.compare(pv1.merge(plan, []), inp.extra, inp.trade_terms)
    rows = {r["position_id"]: r for r in block["rows"]}
    s1 = rows["S1"]
    assert (s1["status"], s1["used"], s1["product"], s1["imported_pv_rub"]) == ("both", "computed", "ois", -14e6)
    assert s1["over"] == (abs(s1["diff_rub"]) > max(10_000.0, 0.01 * 14e6))
    assert (rows["K1"]["status"], rows["K1"]["product"]) == ("both", "irs")
    assert (rows["R1"]["status"], rows["R2"]["status"]) == ("both", "computed_only")
    assert (rows["C1"]["status"], rows["C1"]["reason"], rows["C1"]["product"]) == ("imported_only", "ccs_unsupported",
                                                                                   "ccs")
    assert rows["C1"]["imported_pv_rub"] == 250000.0  # the trades' pv_rub
    assert (rows["R3"]["status"], rows["R3"]["reason"], rows["R3"]["product"]) == ("imported_only", "repo_open",
                                                                                   "repo")
    assert {"ois", "irs", "repo", "ccs"} <= set(block["by_product"])


def test_published_row_risk_by_curve_side_and_notional(ctx, plan):
    inp = inputs()
    s1 = tdm_run.direct_row(ctx, "IRB", "S1", plan.direct["S1"], inp.extra["S1"], None)
    assert s1["instr_type"] == "OTC" and s1["side"] == "PAY" and s1["notional"] == 1e9 and s1["ccy"] == "RUB"
    assert set(s1["risk"]) == {"RUB RUONIA"} and s1["maturity"] == "2028-10-06" and "fx_delta" not in s1
    assert s1["extra"]["valuation"] == "computed" and s1["extra"]["swaps"][0]["trade_id"] == "S1" and s1["gamma"]
    assert "notional unknown: the market value is shown" not in s1["extra"]["assumptions"]
    k1 = tdm_run.direct_row(ctx, "IRB", "K1", plan.direct["K1"], inp.extra["K1"], None)
    assert set(k1["risk"]) == {"RUB RUONIA", "RUB KEY"} and k1["side"] == "REC"
    r2 = tdm_run.direct_row(ctx, "IRB", "R2", plan.direct["R2"], inp.extra["R2"], None)
    assert r2["side"] == "REPO" and r2["notional"] == -5e8 and "legs" not in r2["extra"]
    assert dv.legs_from_fact(s1["extra"]) == []  # no FX legs: the exact scenario keeps it on the Taylor


def _ruonia_without(ctx, drop):
    """``ctx`` with the RUONIA fixings of ``drop`` left out of the history (as not yet published, or missing)."""
    fixings = []
    for e in ctx.core_market.fixings:
        if e.fixing_id == "RUONIA":
            h = e.history
            e = FixingEntry("RUONIA", replace(h, fixings=tuple((d, r) for d, r in h.fixings if d not in drop)))
        fixings.append(e)
    return replace(ctx, core_market=replace(ctx.core_market, fixings=tuple(fixings)))


def test_ruonia_fixings_reach_core_with_the_declared_gap(ctx, plan):
    full = plan.direct["S1"].valuation
    info = full.extra["index_fixings"]["RUONIA"]
    assert (info["latest"], info["unpublished_business_days"], info["missing_business_days"]) == ("2026-10-14", [], [])
    assert info["max_unpublished_business_days"] == 1 and full.quality == "assumed"  # the provisional convention
    # 14.10 not yet published: declared (one trailing business day), carried at the fixing of 13.10, said
    late = pv1.plan(_ruonia_without(ctx, {date(2026, 10, 14)}), inputs(TERMS, ("S1",))).direct["S1"].valuation
    got = late.extra["index_fixings"]["RUONIA"]
    assert (got["latest"], got["unpublished_business_days"]) == ("2026-10-13", ["2026-10-14"])
    assert any(a.startswith("RUONIA not yet published for 2026-10-14") for a in late.assumptions)
    assert late.pv_rub != pytest.approx(full.pv_rub, abs=1e-6)
    # a business day missing inside the history: core rejects the swap, the row keeps the report's rule
    gap = pv1.plan(_ruonia_without(ctx, {date(2026, 10, 8)}), inputs(TERMS, ("S1",))).direct["S1"]
    assert gap.core_not_valued[0].startswith("core_") and gap.valuation is None  # no mv_rub on the S1 row
    # a swap whose running period started before the history's first fixing cannot be valued either (one period)
    early = [{**r, "start_date": "2026-09-01", "maturity_date": "2027-09-01"} if r["trade_id"] == "S1" else r
             for r in TERMS]
    assert pv1.plan(ctx, inputs(early, ("S1",))).direct["S1"].core_not_valued[0].startswith("core_")


def test_leg_conventions_from_the_terms(ctx):
    stated = [{**r, "payment_frequency": "3M", "day_count": "ACT/365"} if r["trade_id"] == "S1" else r for r in TERMS]
    v = pv1.plan(ctx, inputs(stated, ("S1",))).direct["S1"].valuation
    assert "3M" in v.extra["swaps"][0]["convention"] and "ACT/365F" in v.extra["swaps"][0]["convention"]
    assert not any("provisional" in a for a in v.assumptions) and v.quality == "exact"
    for field, value, reason in (("payment_frequency", "1W", "swap_frequency_unsupported"),
                                 ("day_count", "30/360", "day_count_unsupported")):
        bad = [{**r, field: value} if r["trade_id"] == "S1" else r for r in TERMS]
        assert pv1.plan(ctx, inputs(bad, ("S1",))).direct["S1"].core_not_valued[0] == reason
    with pytest.raises(dv.NotValued, match="swap_notional_mismatch"):
        dv.SwapTerms.of([TERMS[0], {**TERMS[1], "notional_ccy": "900000000"}])
    with pytest.raises(dv.NotValued, match="swap_index_unsupported"):
        dv.SwapTerms.of([TERMS[0], {**TERMS[1], "index_code": "MOSPRIME"}])


def test_without_the_key_rate_curve_key_rate_swaps_keep_the_report(ctx):
    v = pv1.plan(replace(ctx, swap_curves=()), inputs(TERMS, ("K1", "S1"))).direct
    assert v["K1"].core_not_valued[0] == "no_key_rate_curve" and v["S1"].valuation.extra["core"]["product"] == "ois"


def test_publish_swaps_through_the_store_and_the_api(ctx, store_url):
    from fastapi.testclient import TestClient

    from desk import store
    from desk.api import create_app
    from desk.publish import desk_run

    inp = inputs()
    bp = pv1.plan(ctx, inp)
    with store.connect(store_url) as con:
        rid, status, _ = desk_run.publish(con, ctx, pv1.merge(bp, []), prev=None, inputs=inp,
                                          core_results=[bp.core_result])
    assert status == "complete"
    c = TestClient(create_app(store_url))
    got = c.get(f"/api/v1/runs/{rid}/derivatives").json()
    rows = {r["position_id"]: r for r in got["rows"]}
    assert rows["S1"]["status"] == "both" and rows["C1"]["status"] == "imported_only"
    pos = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid}/positions").json()["positions"]}
    assert set(pos["K1"]["fact"]["risk"]) == {"RUB RUONIA", "RUB KEY"}
    assert c.get(f"/api/v1/runs/{rid}/risk").status_code == 200
