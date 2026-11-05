"""TDM#565, stage 2: the RUB leg of a cross-currency swap on the key rate and on RUSFAR (and the review notes of stage 2).

On the golden snapshot of 15.10.2026 (``fixtures/mdhub_v1_4``; its KEY_RATE and RUSFAR fixings) with synthetic trades
(invented amounts, rates and spreads). The key-rate leg is averaged arithmetically over calendar days and projected on
``RUB_KEY_RATE_PROJ`` (published as ``RUB KEY``); RUSFAR is compounded and projected on RUONIA (provisional).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from test_ccs_float2_usd_565 import DATE, build
from test_swaps_repo_tdm45 import BD, FIX, ccs, ccs_plan

from desk.curves.rates import KEY_RATE_CURVE_ID, KEY_RATE_SWAP_CURVE_ID
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.valuation import derivatives as dv

FIXED = {"fixed_rate_pct": "15"}
NOT_STATED = ("payment frequency", "day count")


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), BD.isoformat())


def rub(index, tid="K1", rub_side="pay", spread="0", start="2026-10-08", end="2028-10-08", foreign=None, **kw):
    """A CCS of two rows: RUB floating on ``index`` (+ ``spread`` bp) against 100m CNY (fixed 3 %, or ``foreign``: an
    index of the CNY leg) with a 1.25bn RUB notional."""
    t = ccs(tid, rub_side, 1_250_000_000, 100_000_000, start=start, end=end, **kw)
    t[0] = {**t[0], "index_code": index, "spread_bp": spread}
    if foreign:
        t[1] = {k: v for k, v in {**t[1], "fixed_rate_pct": None, "index_code": foreign, "spread_bp": "0"}.items()
                if v is not None}
    return t


def value(ctx, terms, pid="K1"):
    bp, inp = ccs_plan(ctx, terms, pid)
    return bp.direct[pid], inp


def without_curve(ctx, curve_id):
    return replace(ctx, core_market=replace(ctx.core_market, curves=tuple(
        c for c in ctx.core_market.curves if c.curve_id != curve_id)))


# --- the key rate ----------------------------------------------------------------------------------------------------


def test_a_key_rate_leg_against_a_fixed_cny_leg_is_valued_with_risk_on_the_key_rate_curve(ctx):
    o, _ = value(ctx, rub("KEY_RATE", spread="50"))
    v = o.valuation
    assert o.core_not_valued is None and v.extra["core"]["product"] == "ccs", (o.reason, o.detail)
    assert set(v.curve_buckets) == {"RUB KEY", "RUB RUONIA", "CNY XCCY"}, set(v.curve_buckets)
    assert v.curve_buckets["RUB KEY"]["2Y"] != 0 and v.quality == "assumed"
    (fact,) = v.extra["swaps"]
    assert (fact["index"], fact["spread_bp"], fact["projection_curve"], fact["fixing_id"]) == (
        "KEY_RATE", 50.0, "RUB KEY", "KEY_RATE")
    assert fact["side"] == "receiver"  # the desk pays the floating RUB leg and receives the fixed CNY one
    a = " | ".join(v.assumptions)
    assert "arithmetically over calendar days as MOEX IRS KEYRATE" in a and "RUB KEY" in a
    assert v.pv_rub == pytest.approx(fact["rub_leg_pv_rub"] + fact["foreign_leg_pv_rub"], rel=1e-9)
    assert "KEY_RATE" in v.extra["index_fixings"]


def test_the_key_rate_coupon_is_the_arithmetic_average_of_the_forward_curve_plus_the_spread(ctx):
    """A swap that starts after the business date and pays only its final exchange and one coupon: the RUB leg's cash is
    ``N (1 + (average + s) alpha)`` with the average of the run's key-rate forward curve over the period."""
    start, end = date(2026, 10, 16), date(2026, 10, 29)
    t = rub("KEY_RATE", spread="50", start=start.isoformat(), end=end.isoformat(), exchange="final")
    o, _ = value(ctx, t)
    assert o.core_not_valued is None, o.core_not_valued
    fwd = next(c.curve for c in ctx.swap_curves if c.curve_id == KEY_RATE_SWAP_CURVE_ID)
    avg = fwd.arithmetic_average(start, end)
    alpha = (end - start).days / 365.0  # ACT/ACT ISDA inside 2026
    want = -1.25e9 * (1.0 + (avg + 0.0050) * alpha)
    got = sum(f["amount"] for f in o.valuation.extra["flows"] if f["ccy"] == "RUB" and f["amount"] is not None)
    assert got == pytest.approx(want, rel=1e-8)
    # an arithmetic average is not a compounded one: the coupon differs from the compounded figure by a visible amount
    assert avg > 0.10


def test_the_started_key_rate_period_reads_the_key_rate_history(ctx):
    t = rub("KEY_RATE", start="2026-10-08", end="2026-10-22", exchange="final")
    o, _ = value(ctx, t)
    assert o.core_not_valued is None, o.core_not_valued
    assert o.valuation.extra["swaps"][0]["fixing_id"] == "KEY_RATE"
    flows = [f for f in o.valuation.extra["flows"] if f["amount"] is not None]  # the horizon: the forward curve is passed
    assert flows and {f["ccy"] for f in flows} == {"CNY", "RUB"}
    no_hist = replace(ctx, core_market=replace(ctx.core_market, fixings=tuple(
        f for f in ctx.core_market.fixings if f.fixing_id != "KEY_RATE")))
    o, _ = value(no_hist, t)
    assert o.core_not_valued[0] == "no_fixings" and "KEY_RATE" in o.core_not_valued[1]
    assert value(no_hist, rub("KEY_RATE", start="2026-10-20", end="2028-10-20"))[0].core_not_valued is None


def test_without_the_key_rate_curve_the_swap_is_not_valued(ctx):
    o, _ = value(without_curve(ctx, KEY_RATE_CURVE_ID), rub("KEY_RATE"))
    assert o.core_not_valued[0] == "no_key_rate_curve"
    o, _ = value(replace(ctx, swap_curves=()), rub("KEY_RATE"))  # the forward curve of the horizon is missing too
    assert o.core_not_valued[0] == "no_key_rate_curve"
    assert value(without_curve(ctx, KEY_RATE_CURVE_ID), rub("RUONIA"))[0].core_not_valued is None


def test_the_key_rate_averaging_flag_is_not_read(ctx):
    t = rub("KEY_RATE")
    t[0]["x_overnight_arithmetic"] = True
    base = value(ctx, rub("KEY_RATE"))[0].valuation.pv_rub
    o, _ = value(ctx, t)
    assert o.core_not_valued is None and o.valuation.pv_rub == pytest.approx(base, rel=1e-12)


def test_a_key_rate_leg_against_a_floating_cny_leg_takes_its_side_from_the_foreign_leg(ctx):
    for rub_side, side in (("pay", "receiver"), ("receive", "payer")):
        o, _ = value(ctx, rub("KEY_RATE", rub_side=rub_side, foreign="RUSFARCNY"))
        assert o.core_not_valued is None, o.core_not_valued
        v = o.valuation
        assert {"RUB KEY", "CNY RUSFAR", "CNY XCCY", "RUB RUONIA"} <= set(v.curve_buckets)
        (fact,) = v.extra["swaps"]
        assert fact["side"] == side  # payer: the desk pays the CNY leg
        assert (fact["index"], fact["foreign_index"]) == ("KEY_RATE", "RUSFARCNY")


# --- RUSFAR --------------------------------------------------------------------------------------------------------


def test_a_rusfar_leg_is_valued_projected_on_ruonia_and_reads_the_rusfar_fixings(ctx):
    assert "RUSFAR" in {e.fixing_id for e in ctx.fx.index_fixings}
    o, _ = value(ctx, rub("RUSFAR", spread="40"))
    v = o.valuation
    assert o.core_not_valued is None, (o.reason, o.detail)
    assert set(v.curve_buckets) == {"RUB RUONIA", "CNY XCCY"}
    (fact,) = v.extra["swaps"]
    assert (fact["index"], fact["fixing_id"], fact["projection_curve"]) == ("RUSFAR", "RUSFAR", "RUB RUONIA")
    a = " | ".join(v.assumptions)
    assert "RUSFAR compounded on 365 / 366" in a and "without a RUSFAR-RUONIA basis" in a and "provisional" in a
    assert "RUSFAR" in v.extra["index_fixings"]
    # the fixings of the bonds' market are untouched
    assert all(f.fixing_id != "RUSFAR" for f in ctx.core_market.fixings)
    assert all(e.fixing_id != "RUSFAR" for e in ctx.core_market.index_fixings)


def test_a_new_rusfar_swap_needs_no_fixings(ctx):
    fixings = tuple(e for e in ctx.fx.index_fixings if e.fixing_id != "RUSFAR")
    no = replace(ctx, fx=replace(ctx.fx, index_fixings=fixings))
    o, _ = value(no, rub("RUSFAR"))
    assert o.core_not_valued[0] == "no_fixings" and "RUSFAR" in o.core_not_valued[1]
    assert value(no, rub("RUSFAR", start="2026-10-20", end="2028-10-20"))[0].core_not_valued is None


def test_a_rusfar_leg_against_sofr_is_valued_and_the_bridged_day_is_said(tmp_path):
    def mut(m):  # RUSFAR not published on the Tuesday 13.10, a business day of the exchange's calendar
        m["data"]["fixings"] = [f for f in m["data"]["fixings"]
                                if not (f["index_code"] == "RUSFAR" and f["fixing_date"] == "2026-10-13")]

    c = prepare(build(tmp_path, mut), DATE)
    t = rub("RUSFAR", tid="U1")
    t[1] = {k: v for k, v in {**t[1], "ccy": "USD", "fixed_rate_pct": None, "index_code": "SOFR",
                              "spread_bp": "0", "notional_ccy": "100000000"}.items() if v is not None}
    o, _ = value(c, t, "U1")
    assert o.core_not_valued is None, o.core_not_valued
    a = " | ".join(o.valuation.assumptions)
    assert "RUSFAR not published on 2026-10-13" in a and "the previous fixing holds" in a
    assert {"USD SOFR", "USD FX", "RUB RUONIA"} == set(o.valuation.curve_buckets)
    (fact,) = o.valuation.extra["swaps"]
    assert (fact["index"], fact["foreign_index"]) == ("RUSFAR", "SOFR")
    assert c.fx.lineage["index_fixings"]["RUSFAR"]["bridged_days"] == ["2026-10-13"]


def test_the_rusfar_fixings_move_the_value_of_a_started_swap(tmp_path):
    def bumped(m):
        for f in m["data"]["fixings"]:
            if f["index_code"] == "RUSFAR":
                f["value"] = str(float(f["value"]) + 1.0)

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = prepare(build(tmp_path / "a"), DATE)
    b = prepare(build(tmp_path / "b", bumped), DATE)
    t = rub("RUSFAR")
    pa, pb = value(a, t)[0].valuation.pv_rub, value(b, t)[0].valuation.pv_rub
    assert pa != pytest.approx(pb, rel=1e-9) and pb < pa  # a higher RUSFAR: the desk pays more
    new = rub("RUSFAR", start="2026-10-20", end="2028-10-20")
    assert value(a, new)[0].valuation.pv_rub == pytest.approx(value(b, new)[0].valuation.pv_rub, rel=1e-12)


def test_an_arithmetically_averaged_rusfar_leg_is_not_valued(ctx):
    t = rub("RUSFAR")
    t[0]["x_overnight_arithmetic"] = True
    o, _ = value(ctx, t)
    assert o.core_not_valued[0] == "ccs_index_averaging_unsupported" and "RUSFAR" in o.core_not_valued[1]
    t[0]["x_overnight_arithmetic"] = False
    assert value(ctx, t)[0].core_not_valued is None


def _with_rusfar_calendar(tmp):
    """The golden snapshot with a ``MOEX_RUSFAR`` calendar (a copy of ``MOEX_STOCK`` under another version) in its static."""
    import copy
    import gzip
    import hashlib
    import json

    sd = SnapshotDir(FIX)
    market = sd.market(DATE, "eod_final")
    static = copy.deepcopy(sd.get(market.static_snapshot_id).doc)
    m = copy.deepcopy(market.doc)
    stock = next(c for c in static["data"]["calendars"] if c["calendar"] == "MOEX_STOCK")
    static["data"]["calendars"].append({**stock, "calendar": "MOEX_RUSFAR", "version": "rusfar-test-version"})

    def put(doc):
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        sid = hashlib.sha256(raw).hexdigest()
        (tmp / f"{sid}.json.gz").write_bytes(gzip.compress(raw))
        return sid

    m["static_snapshot_id"] = put(static)
    put(m)
    return SnapshotDir(tmp)


def test_the_rusfar_calendar_is_moex_rusfar_else_moex_stock(ctx, tmp_path):
    cal = next(e.fixings.calendar for e in ctx.fx.index_fixings if e.fixing_id == "RUSFAR")
    assert cal.version.endswith("+RUSFAR") and cal.is_business_day(date(2026, 10, 14))
    assert cal.version.startswith("MOEX_STOCK@")  # no MOEX_RUSFAR in the golden static
    own = prepare(_with_rusfar_calendar(tmp_path), DATE)
    cal = next(e.fixings.calendar for e in own.fx.index_fixings if e.fixing_id == "RUSFAR")
    assert cal.version.startswith("MOEX_RUSFAR@") and cal.version.endswith("+RUSFAR")
    from desk.curves import fx as fxcurves

    assert fxcurves.RUB_OVERNIGHT["RUSFAR"].base_calendar == "MOEX_RUSFAR"
    assert "RUSFAR" not in fxcurves.FOREIGN_OVERNIGHT  # a foreign leg cannot float on it


# --- other indices, regression of RUONIA and fixed --------------------------------------------------------------------


def test_another_rub_index_is_not_valued_with_the_new_text(ctx):
    for index in ("SHIBOR", "RUSFARCNY", "RUONIA_INDEX"):
        o, _ = value(ctx, rub(index))
        assert o.core_not_valued[0] == "swap_index_unsupported", (index, o.core_not_valued)
        assert "RUB leg on " + index + " (RUONIA, KEY_RATE or RUSFAR)" in o.core_not_valued[1]


# PV of the synthetic trades on the commit before this stage (RUONIA and fixed legs are not changed by it)
BASE = {"fixed": -132461936.98683167, "ruonia": -124253512.04983091, "ruonia_spread": -130405771.7975347}


@pytest.mark.parametrize("name,kw", [("fixed", FIXED), ("ruonia", {"index_code": "RUONIA"}),
                                     ("ruonia_spread", {"index_code": "RUONIA", "spread_bp": "30"})])
def test_ruonia_and_fixed_legs_are_valued_as_before(ctx, name, kw):
    t = ccs("C1", "pay", 1_250_000_000, 100_000_000, start="2026-10-08", end="2028-10-08", **kw)
    o, _ = value(ctx, t, "C1")
    assert o.valuation.pv_rub == pytest.approx(BASE[name], rel=1e-12)
    assert set(o.valuation.curve_buckets) == {"RUB RUONIA", "CNY XCCY"}
    (fact,) = o.valuation.extra["swaps"]
    assert fact["projection_curve"] is None  # unchanged: RUONIA is the discount curve
    assert fact["fixing_id"] == (None if name == "fixed" else "RUONIA")


# --- review of stage 2 (M1): the notes ----------------------------------------------------------------------------------


def test_a_matured_unmapped_trade_is_said_like_a_matured_mapped_one(ctx):
    live = rub("RUONIA", tid="L1", start="2026-10-08", end="2028-10-08")
    dead = ccs("D1", "pay", 1_250_000_000, 100_000_000, start="2024-10-08", end="2026-04-08", fixed_rate_pct="15")
    dead[1]["index_code"], dead[1]["fixed_rate_pct"], dead[1]["x_index_unmapped"] = "OTHER", None, True
    live[0]["index_code"] = "RUONIA"
    terms = [{**r, "position_id": "K1"} for r in (*live, *dead)]
    o, _ = value(ctx, terms)
    assert o.core_not_valued is None, o.core_not_valued
    assert any(a == "settled trades left out: D1" for a in o.valuation.assumptions), o.valuation.assumptions
    # a matured mapped trade says it the same way
    old = ccs("D2", "pay", 1_250_000_000, 100_000_000, start="2024-10-08", end="2026-04-08", fixed_rate_pct="15")
    both = [{**r, "position_id": "K1"} for r in (*live, *old)]
    o, _ = value(ctx, both)
    assert any(a == "settled trades left out: D2" for a in o.valuation.assumptions)
    # both kinds at once
    o, _ = value(ctx, [*terms, *[{**r, "position_id": "K1"} for r in old]])
    assert any(a.startswith("settled trades left out: D1") and "D2" in a for a in o.valuation.assumptions)


def test_the_standard_convention_note_names_only_what_the_standard_gives(tmp_path):
    c = prepare(build(tmp_path), DATE)

    def note(freq=None, dc=None):
        t = ccs("S1", "pay", 1_250_000_000, 100_000_000, start="2026-10-08", end="2028-10-08", fixed_rate_pct="15")
        t[1] = {k: v for k, v in {**t[1], "ccy": "USD", "fixed_rate_pct": None, "index_code": "SOFR",
                                  "spread_bp": "0"}.items() if v is not None}
        for r in t:
            if freq:
                r["payment_frequency"] = freq
            if dc:
                r["day_count"] = dc
        o, _ = value(c, t, "S1")
        assert o.core_not_valued is None, o.core_not_valued
        return [a for a in o.valuation.assumptions if "MOEX XCS convention" in a]

    (n,) = note()  # nothing stated: the RUB leg takes annual / ACT/ACT ISDA, the SOFR leg annual and ACT/360
    assert "(annual, ACT/ACT ISDA)" in n and "payment frequency or day count" in n
    assert "ACT/360" not in n
    assert note(freq="1Y", dc="ACT/360") == []  # both legs state both: nothing from the standard
    (n,) = note(freq="1Y")  # the RUB leg takes the standard day count, a SOFR leg takes ACT/360 (said elsewhere)
    assert "(ACT/ACT ISDA)" in n and "no day count" in n and "frequency" not in n
    (n,) = note(dc="ACT/365")  # only the frequency is missing
    assert "(annual)" in n and "no payment frequency" in n and "ACT/ACT" not in n


@pytest.mark.parametrize("val", ["N/A", "n/a", "-0", "+0", "-0.0", "0 calendar days", "0 business days", "0 days",
                                 "-", "0BD"])
def test_more_spellings_of_no_offset_are_not_stated(val):
    assert dv._nonzero(val) is False


@pytest.mark.parametrize("val", ["1", "+1", "-1", "2 calendar days", "5 business days", "T-2", "1D"])
def test_a_real_offset_is_still_stated(val):
    assert dv._nonzero(val) is True


# --- review of the key-rate and RUSFAR legs ------------------------------------------------------------------------------


def test_a_preliminary_rusfar_fixing_is_said(tmp_path):
    def mut(m):
        for f in m["data"]["fixings"]:
            if f["index_code"] == "RUSFAR" and f["fixing_date"] == "2026-10-14":
                f["labels"] = ["moex_marketdata_preliminary"]

    c = prepare(build(tmp_path, mut), DATE)
    assert c.fx.lineage["index_fixings"]["RUSFAR"]["preliminary_days"] == ["2026-10-14"]
    o, _ = value(c, rub("RUSFAR"))
    assert any("RUSFAR of 2026-10-14 is a preliminary value" in a and "provisional" in a
               for a in o.valuation.assumptions)


def test_a_key_rate_swap_longer_than_the_curve_has_its_own_reason(ctx):
    node = next(e.curve.nodes[-1][0] for e in ctx.core_market.curves if e.curve_id == KEY_RATE_CURVE_ID)
    o, _ = value(ctx, rub("KEY_RATE", end="2040-10-08"))
    assert o.core_not_valued[0] == "key_rate_curve_too_short" and node.isoformat() in o.core_not_valued[1]
    from desk.api import coverage

    assert "key_rate_curve_too_short" in coverage.CAUSES
    ok = date(node.year - 1, 10, 8).isoformat()
    assert value(ctx, rub("KEY_RATE", end=ok))[0].core_not_valued is None


def test_the_reason_texts_have_no_commands_for_the_owner():
    from desk.api import coverage

    for code, (why, todo) in coverage.CAUSES.items():
        assert "desk sync" not in why + todo, code


def test_a_row_of_matured_trades_only_names_the_unmapped_one_too(ctx):
    dead = ccs("D1", "pay", 1_250_000_000, 100_000_000, start="2024-10-08", end="2026-10-13", fixed_rate_pct="15")
    dead[1]["index_code"], dead[1]["fixed_rate_pct"], dead[1]["x_index_unmapped"] = "OTHER", None, True
    old = ccs("D2", "pay", 1_250_000_000, 100_000_000, start="2024-10-08", end="2026-10-12", fixed_rate_pct="15")
    o, _ = value(ctx, [{**r, "position_id": "K1"} for r in (*dead, *old)])
    v = o.valuation
    assert v is not None and o.core_not_valued is None
    assert sorted(v.extra["core"]["trades"]) == ["D1", "D2"]
    assert any(a == "settled trades left out: D1" for a in v.assumptions), v.assumptions
