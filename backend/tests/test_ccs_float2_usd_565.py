"""Floating USD and EUR legs of a cross-currency swap (TDM#565, stage 2): SOFR and the euro short-term rate.

On the golden snapshot of 15.10.2026 (``fixtures/mdhub_v1_4``) with synthetic additions (invented rates and trades):
fixings of ``SOFR`` from two sources with different values on the same dates, ``ESTR``, and the government par-yield
sets ``usd_ust_par`` / ``eur_aaa_par`` the forecast curves ``USD SOFR`` / ``EUR ESTR`` are renamed from. Also the
``index_unmapped`` reason and the extrapolation note, which says so only for a period that ends past the last quote.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import shutil
from dataclasses import replace
from datetime import date

import pytest
from test_swaps_repo_tdm45 import BD, FIX, ccs, ccs_plan, leg

from desk.curves import fx as fxcurves
from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.marketdata import adapter_rates as ar
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.publish import tdm_run
from desk.rules import ocp as O
from desk.valuation import derivatives as dv

DATE = BD.isoformat()
UST = (("1M", 4.18), ("3M", 4.10), ("6M", 3.98), ("1Y", 3.81), ("2Y", 3.65), ("5Y", 3.68), ("10Y", 4.01))
AAA = (("1Y", 2.10), ("2Y", 2.15), ("5Y", 2.40), ("10Y", 2.70))
# business days of US_FED / TARGET from the swap's start to the day before the business date (12.10 is a US holiday)
SOFR_DAYS = ("2026-10-08", "2026-10-09", "2026-10-13", "2026-10-14")
ESTR_DAYS = ("2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14")
FIXED = {"fixed_rate_pct": "15"}
RUONIA = {"index_code": "RUONIA"}


def _put(tmp, doc) -> str:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    sid = hashlib.sha256(raw).hexdigest()
    (tmp / f"{sid}.json.gz").write_bytes(gzip.compress(raw))
    return sid


def _fixing(code, source, day, value):
    return {"effective_date": None, "fixing_date": day, "hub_ingested_at": "2026-10-16T13:19:16.934011Z",
            "index_code": code, "labels": [], "origin": "md.index_eod", "published_at": None, "published_on": None,
            "source": source, "unit": "pct", "value": value}


def _par_set(dataset, source, set_id, quotes, template):
    qs = copy.deepcopy(template)
    qs.update(dataset=dataset, source=source, quote_set_id=set_id, labels=["mid_only"],
              quotes=[{"tenor": t, "position": i, "mid_pct": str(v), "bid_pct": None, "ask_pct": None,
                       "tenor_days": None, "tenor_months": None, "pillar_date": None, "extra": None}
                      for i, (t, v) in enumerate(quotes)])
    return qs


def build(tmp, mutate=None, *, ust=True, aaa=True, fixings=True, short_rusfar=False):
    """The golden snapshot plus the synthetic rows, re-hashed into ``tmp``."""
    sd = SnapshotDir(FIX)
    market = sd.market(DATE, "eod_final")
    m = copy.deepcopy(market.doc)
    d = m["data"]
    if fixings:
        d["fixings"] += [_fixing("SOFR", "nyfed", day, "4.30") for day in SOFR_DAYS]
        d["fixings"] += [_fixing("SOFR", "fred", day, "4.10") for day in SOFR_DAYS]  # same dates, other values
        d["fixings"] += [_fixing("ESTR", "ecb_data_portal", day, "1.95") for day in ESTR_DAYS]
    for r in [r for r in d["futures_prices"] if r.get("is_fx") and r.get("base_ccy") == "USD"]:
        # the golden has no EUR swap points or futures: a EUR FX-implied curve from the USD futures, scaled to EUR's spot
        k = 93.7693 / 83.76
        scaled = {f: str(round(float(r[f]) * k, 4)) for f in ("close", "settle_price", "settle_rub_per_unit",
                                                             "close_rub_per_unit", "open", "high", "low", "waprice")
                  if r.get(f) is not None}
        d["futures_prices"].append({**r, **scaled, "asset_code": "EUR", "base_ccy": "EUR",
                                    "secid": "E" + r["secid"][1:], "short_name": "EUR-" + str(r.get("short_name"))})
    template = d["curve_quote_sets"][0]
    if ust:
        d["curve_quote_sets"].append(_par_set("usd_ust_par", "us_treasury", 990001, UST, template))
    if aaa:
        d["curve_quote_sets"].append(_par_set("eur_aaa_par", "ecb", 990002, AAA, template))
    if short_rusfar:  # the CNY RUSFAR OIS quotes up to 1Y only
        for qs in d["curve_quote_sets"]:
            if qs["dataset"] == "cny_rusfar_ois":
                qs["quotes"] = [q for q in qs["quotes"]
                                if q["tenor"][-1] in "WD" or (q["tenor"][-1] == "M" and int(q["tenor"][:-1]) <= 12)
                                or q["tenor"] == "1Y"]
    if mutate:
        mutate(m)
    shutil.copy(FIX / f"{market.static_snapshot_id}.json.gz", tmp)
    _put(tmp, m)
    return SnapshotDir(tmp)


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    return prepare(build(tmp_path_factory.mktemp("sofr")), DATE)


def fl(tid="F1", ccy="USD", index="SOFR", rub_side="pay", spread="0", start="2026-10-08", end="2028-10-08",
       notional=100_000_000, **kw):
    """A CCS of two rows: RUB (``kw``: fixed or RUONIA) against ``ccy`` floating on ``index`` (+ ``spread`` bp)."""
    t = ccs(tid, rub_side, 1_250_000_000 if ccy == "USD" else 1_500_000_000, notional, start=start, end=end, **kw)
    t[1] = {k: v for k, v in {**t[1], "ccy": ccy, "fixed_rate_pct": None, "index_code": index,
                              "spread_bp": spread}.items() if v is not None}
    return t


def value(ctx, terms, pid="F1"):
    bp, inp = ccs_plan(ctx, terms, pid)
    return bp.direct[pid], inp


# --- the run's inputs ----------------------------------------------------------------------------------------------


def test_fixings_of_several_sources_are_chosen_by_the_order_not_an_error(ctx):
    snap = ctx.market.snapshot
    with pytest.raises(Exception, match="duplicate SOFR"):
        ar.fixings(snap, "SOFR")
    rows = ar.fixings(snap, "SOFR", ("nyfed", "fred"))
    assert [(r.fixing_date.isoformat(), r.source, float(r.rate)) for r in rows] == [
        (d, "nyfed", pytest.approx(0.043)) for d in SOFR_DAYS]
    rows = ar.fixings(snap, "SOFR", ("fred", "nyfed"))
    assert {r.source for r in rows} == {"fred"}
    assert {r.source for r in ar.fixings(snap, "SOFR", ())} == {"fred"}  # outside the order: by name
    assert ar.FIXING_SOURCE_ORDER["ESTR"][0] == "ecb_data_portal"
    from desk.api import market_history

    assert market_history.SOURCE_ORDER is ar.FIXING_SOURCE_ORDER


def test_the_run_has_the_fixings_and_the_forecast_curves(ctx):
    lin = ctx.fx.lineage
    assert {e.fixing_id for e in ctx.fx.index_fixings} == {"RUSFARCNY", "SOFR", "ESTR"}
    sofr = lin["index_fixings"]["SOFR"]
    assert (sofr["first"], sofr["latest"], sofr["sources"]) == ("2026-10-08", "2026-10-14", ["nyfed"])
    assert sofr["bridged_days"] == [] and sofr["unpublished_business_days"] == []
    assert lin["index_fixings"]["ESTR"]["sources"] == ["ecb_data_portal"]
    assert lin["index_fixings"]["SOFR"]["calendar"].endswith("+SOFR")
    for cid, ccy, spec, src in (("USD SOFR", "USD", "usd_discount_factors_v1", "USD UST"),
                                ("EUR ESTR", "EUR", "eur_discount_factors_v1", "EUR AAA")):
        c = ctx.fx.curves[cid]
        assert (c.ccy, c.kind, c.spec_id) == (ccy, "ois", spec) and "не OIS" in c.name
        assert cid in {e.curve_id for e in ctx.core_market.curves}
        info = lin["curves"][cid]
        assert info["status"] == "ok" and info["source_curve"] == src and info["proxy"] is True
        assert info["quote_date"] and info["last_node"] and "flat-forward beyond" in info["extrapolation"]
        assert "stand for the OIS" in info["provisional"]
        assert cid not in fxcurves.FX_CURVE_KINDS and not c.proxy
        assert ctx.fx.fx_curve_ids(ccy, fxcurves.FxOptions())[0] == f"{ccy} FX"


def test_the_forecast_curve_has_the_discount_factors_of_the_proxy(ctx):
    for cid, src in (("USD SOFR", "USD UST"), ("EUR ESTR", "EUR AAA")):
        a, b = ctx.fx.curves[cid].curve, ctx.fx.curves[src].curve
        assert a.nodes == b.nodes
        for d in (date(2026, 11, 20), date(2027, 3, 31), date(2028, 10, 8), date(2031, 6, 1), date(2040, 1, 1)):
            assert a.discount_factor(d) == pytest.approx(b.discount_factor(d), rel=1e-12), (cid, d)


def test_without_the_proxy_set_there_is_no_curve_and_the_lineage_says_so(tmp_path):
    c = prepare(build(tmp_path, ust=False), DATE)
    assert "USD SOFR" not in c.fx.curves and c.fx.lineage["curves"]["USD SOFR"]["status"] == "missing"
    assert "EUR ESTR" in c.fx.curves
    assert "USD SOFR" not in {e.curve_id for e in c.core_market.curves}


# --- valuation -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ccy,index,forecast,rub", [("USD", "SOFR", "USD SOFR", FIXED), ("USD", "SOFR", "USD SOFR", RUONIA),
                                                     ("EUR", "ESTR", "EUR ESTR", RUONIA)])
def test_a_floating_usd_or_eur_leg_is_valued_with_risk_on_the_forecast_curve(ctx, ccy, index, forecast, rub):
    o, _inp = value(ctx, fl(ccy=ccy, index=index, **rub))
    v = o.valuation
    assert o.core_not_valued is None and v.extra["core"]["product"] == "ccs", (o.reason, o.detail)
    want = {forecast, f"{ccy} FX", "RUB RUONIA"}
    assert set(v.curve_buckets) == want, set(v.curve_buckets)
    assert v.curve_buckets[forecast]["2Y"] != 0 and v.curve_buckets[f"{ccy} FX"]["2Y"] != 0
    assert v.quality == "assumed"
    (fact,) = v.extra["swaps"]
    assert (fact["foreign_ccy"], fact["foreign_index"], fact["foreign_projection_curve"],
            fact["foreign_fixing_id"]) == (ccy, index, forecast, index)
    assert fact["foreign_curve"] == f"{ccy} FX"  # the leg is discounted as a fixed USD / EUR CCS is
    assert v.pv_rub == pytest.approx(fact["rub_leg_pv_rub"] + fact["foreign_leg_pv_rub"], rel=1e-9)
    assert v.fx_delta[ccy] == pytest.approx(fact["foreign_leg_pv"])
    a = " | ".join(v.assumptions)
    assert "proxy" in a and "not OIS" in a and "ACT/360" in a and "provisional" in a
    assert ("UST par" if ccy == "USD" else "ECB AAA par") in a
    assert index in v.extra["index_fixings"]
    assert forecast in v.extra["core"]["curves"] or f"{forecast}" in str(v.extra["core"])


def test_the_started_period_reads_the_first_source_not_the_other(ctx):
    """SOFR from nyfed is 4.30 %, from fred 4.10 %: the PV equals that on a snapshot with only the nyfed rows, and
    differs from one with only the fred rows."""
    t = fl(**FIXED)
    pv = value(ctx, t)[0].valuation.pv_rub

    def only(source):
        def mut(m):
            m["data"]["fixings"] = [f for f in m["data"]["fixings"]
                                    if not (f["index_code"] == "SOFR" and f["source"] != source)]
        return mut

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        nyfed = prepare(build(Path(a), only("nyfed")), DATE)
        fred = prepare(build(Path(b), only("fred")), DATE)
        assert value(nyfed, t)[0].valuation.pv_rub == pytest.approx(pv, rel=1e-12)
        assert value(fred, t)[0].valuation.pv_rub != pytest.approx(pv, rel=1e-9)
    ((_, info),) = [(1, ctx.fx.lineage["index_fixings"]["SOFR"])]
    assert info["sources"] == ["nyfed"]


def test_the_fixings_not_yet_known_fall_back_to_the_next_source(ctx):
    cut = ar.known_at(ctx.market.snapshot)
    cal = next(c.calendar for c in ctx.core_market.calendars if c.calendar_id == "FX:USD")
    snap = copy.deepcopy(ctx.market.snapshot)
    for f in snap.data["fixings"]:
        if f["index_code"] == "SOFR" and f["source"] == "nyfed" and f["fixing_date"] == "2026-10-14":
            f["published_at"] = "2026-10-30T00:00:00Z"  # after the snapshot's known_at
    spec = fxcurves.FOREIGN_OVERNIGHT["SOFR"]
    entry, _info = fxcurves.index_fixings(snap, spec, cal, BD, cut)
    assert dict(entry.fixings.fixings)[date(2026, 10, 14)] == pytest.approx(0.041)  # fred's
    assert dict(entry.fixings.fixings)[date(2026, 10, 13)] == pytest.approx(0.043)  # nyfed's


def test_a_started_swap_without_the_fixings_is_not_valued(tmp_path):
    c = prepare(build(tmp_path, fixings=False), DATE)
    o, _ = value(c, fl(**FIXED))
    assert o.core_not_valued[0] == "no_fixings" and "SOFR" in o.core_not_valued[1]
    o, _ = value(c, fl(start="2026-10-20", end="2028-10-20", **FIXED))  # a new swap needs none
    assert o.core_not_valued is None, o.core_not_valued


def test_a_missing_day_inside_the_history_names_the_index(tmp_path):
    def mut(m):
        m["data"]["fixings"] = [f for f in m["data"]["fixings"]
                                if not (f["index_code"] == "ESTR" and f["fixing_date"] in ("2026-10-13", "2026-10-14"))]

    c = prepare(build(tmp_path, mut), DATE)
    o, _ = value(c, fl(ccy="EUR", index="ESTR", **FIXED))
    assert o.core_not_valued[0] == "no_fixings" and "no ESTR fixing for 2026-10-13" in o.core_not_valued[1]


def test_without_the_proxy_set_the_swap_is_not_valued_and_the_text_names_the_curve(tmp_path):
    c = prepare(build(tmp_path, ust=False), DATE)
    o, _ = value(c, fl(**FIXED))
    assert o.core_not_valued[0] == "no_foreign_projection_curve" and "USD SOFR" in o.core_not_valued[1]
    assert value(c, fl(ccy="EUR", index="ESTR", **FIXED))[0].core_not_valued is None


def test_other_and_foreign_indices_keep_their_reason_with_the_new_text(ctx):
    for ccy, index in (("CNY", "SHIBOR"), ("CNY", "SOFR"), ("USD", "SHIBOR"), ("USD", "ESTR"), ("EUR", "SOFR"),
                       ("EUR", "EURIBOR"), ("USD", "RUSFARCNY")):
        o, _ = value(ctx, fl(ccy=ccy, index=index, **FIXED))
        assert o.core_not_valued[0] == "ccs_foreign_floating_unsupported", (ccy, index, o.core_not_valued)
        assert "not supported (valued: RUSFARCNY in CNY, SOFR in USD, ESTR in EUR)" in o.core_not_valued[1]


def test_a_fixing_offset_is_ignored_and_said(ctx):
    t = fl(**FIXED)
    t[1]["x_fixing_offset"] = 2
    v = value(ctx, t)[0].valuation
    assert any("fixing offset / lookback" in a and "ignored" in a for a in v.assumptions)
    assert not any("fixing offset" in a for a in value(ctx, fl(**FIXED))[0].valuation.assumptions)


# --- index_unmapped ------------------------------------------------------------------------------------------------


def test_an_unmapped_index_is_its_own_reason_before_the_index_checks(ctx):
    t = fl(**FIXED)
    t[1]["index_code"], t[1]["x_index_unmapped"] = "OTHER", True
    t[1]["x_index_raw"] = "SECRET-NAME"
    o, _ = value(ctx, t)
    assert o.core_not_valued[0] == "index_unmapped"
    assert "OTHER" in o.core_not_valued[1] and "SECRET-NAME" not in o.core_not_valued[1]
    # the flag on any row of the trade counts, whatever the index checks would say
    t2 = fl(**RUONIA)
    t2[0]["x_index_unmapped"] = True
    t2[1]["index_code"] = "SHIBOR"
    assert value(ctx, t2)[0].core_not_valued[0] == "index_unmapped"


def test_unmapped_irs_and_repo_are_index_unmapped():
    irs = [leg("S1", "fix", "pay", kind="irs", fixed_rate_pct="15", ccy="RUB", notional_ccy="1000000"),
           {**leg("S1", "flt", "receive", kind="irs", ccy="RUB", notional_ccy="1000000"), "index_code": "OTHER",
            "x_index_unmapped": True}]
    with pytest.raises(dv.NotValued) as e:
        dv.SwapTerms.of(irs)
    assert e.value.reason == "index_unmapped"
    repo = {"trade_id": "P1", "product_kind": "repo", "side": "repo", "ccy": "RUB", "notional_ccy": "1000",
            "start_date": "2026-10-01", "maturity_date": "2026-11-01", "index_code": "OTHER", "x_index_unmapped": "true"}
    with pytest.raises(dv.NotValued) as e:
        dv.RepoTerms.of(repo)
    assert e.value.reason == "index_unmapped"
    repo["x_index_unmapped"] = False
    repo["index_code"] = "RUONIA"
    assert dv.RepoTerms.of(repo).index_code == "RUONIA"


# --- the extrapolation note ----------------------------------------------------------------------------------------


def _cny(c, end):
    t = ccs("R1", "pay", 1_250_000_000, 100_000_000, start="2026-10-08", end=end, **FIXED)
    t[1] = {k: v for k, v in {**t[1], "fixed_rate_pct": None, "index_code": "RUSFARCNY", "spread_bp": "0"}.items()
            if v is not None}
    return value(c, t, "R1")[0]


def test_the_extrapolation_is_said_only_for_a_leg_that_ends_past_the_last_quote(tmp_path):
    c = prepare(build(tmp_path, short_rusfar=True), DATE)
    assert c.fx.lineage["curves"]["CNY RUSFAR"]["tenors_used"][-1] == "1Y"
    last = c.fx.lineage["curves"]["CNY RUSFAR"]["last_node"]
    short = _cny(c, "2027-04-08").valuation
    assert short is not None
    a = [x for x in short.assumptions if "CNY RUSFAR quotes to" in x]
    assert len(a) == 1 and "extrapolated" not in a[0] and "within them" in a[0] and last in a[0]
    long = _cny(c, "2028-10-08").valuation
    a = [x for x in long.assumptions if "CNY RUSFAR quotes to" in x]
    assert len(a) == 1 and "extrapolated flat-forward (provisional)" in a[0]


def test_the_forecast_curve_extrapolation_follows_the_same_rule(ctx):
    last = ctx.fx.lineage["curves"]["USD SOFR"]["last_node"]
    assert last >= "2036"  # to 10Y
    a = [x for x in value(ctx, fl(**FIXED))[0].valuation.assumptions if "USD SOFR quotes to" in x]
    assert len(a) == 1 and "extrapolated" not in a[0]
    far = value(ctx, fl(end="2040-10-08", **FIXED))[0]
    if far.core_not_valued is None:
        assert any("USD SOFR quotes to" in x and "extrapolated flat-forward" in x for x in far.valuation.assumptions)


# --- the run's reference, scenarios, OCP -----------------------------------------------------------------------------


def test_the_forecast_curve_is_in_the_reference_only_with_risk_on_it_and_moves_by_day(ctx):
    names = lambda rows: {c["curve_id"] for c in tdm_run.curves_reference(ctx, ctx.market, rows, [{"ccy": "USD"}])}
    assert "USD SOFR" not in names([]) and "USD FX" in names([])
    got = tdm_run.curves_reference(ctx, ctx.market, [{"risk": {"USD SOFR": {"2Y": 1.0}}}], [{"ccy": "USD"}])
    (c,) = [c for c in got if c["curve_id"] == "USD SOFR"]
    assert c["ccy"] == "USD" and c["points"]
    ois = next(e.curve for e in ctx.core_market.curves if e.curve_id == RUONIA_OIS_CURVE_ID)
    old = tdm_run._prev_fx(ctx, ctx.market, ois)
    assert "USD SOFR" in old and old["USD SOFR"].nodes == ctx.fx.curves["USD SOFR"].curve.nodes


def test_the_fx_delta_and_the_ocp_amount_are_the_same_in_the_risk_row_and_the_ocp(ctx):
    for ccy, index in (("USD", "SOFR"), ("EUR", "ESTR")):
        t = fl(ccy=ccy, index=index, **RUONIA)  # receives 100m floating, pays RUONIA
        o, inp = value(ctx, t)
        v = o.valuation
        row = tdm_run.direct_row(ctx, "IRB", "F1", o, inp.extra["F1"], None)
        assert row["extra"]["ocp_amount"] == {ccy: 1e8} and v.extra["ocp_amount"] == {ccy: 1e8}
        pol = lambda b: replace(O.PROVISIONAL_POLICY, amount_basis=b)
        comp, amounts, basis, _ = O.classify(row, t, {}, BD, pol(O.AmountBasis.NOMINAL))
        assert (comp, amounts, basis) == ("fx_legs", {ccy: 1e8}, "nominal")
        comp, amounts, basis, _ = O.classify(row, t, {}, BD, pol(O.AmountBasis.SPOT_EQUIVALENT))
        assert comp == "fx_legs" and amounts == pytest.approx({ccy: v.fx_delta[ccy]})
        assert row["fx_delta"] == {ccy: v.fx_delta[ccy]}
        assert set(row["risk"]) == {f"{ccy} {'SOFR' if ccy == 'USD' else 'ESTR'}", f"{ccy} FX", "RUB RUONIA"}


def test_the_calendars_of_the_fixings_are_the_hubs(ctx):
    cal = next(e.fixings.calendar for e in ctx.fx.index_fixings if e.fixing_id == "SOFR")
    assert not cal.is_business_day(date(2026, 10, 12)) and cal.is_business_day(date(2026, 10, 13))  # Columbus Day
    cal = next(e.fixings.calendar for e in ctx.fx.index_fixings if e.fixing_id == "ESTR")
    assert cal.is_business_day(date(2026, 10, 12))
