"""TDM#614: price, risk and carry of synthetic single-name CDS with core 0.46."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.rows import dict_row, tuple_row
from rub_fixed_income import BusinessCalendar, CurveEntry, DayCount, DiscountCurve
from rub_fixed_income.cds import (
    AccrualOnDefault,
    CdsContract,
    CdsConvention,
    CdsCurveRequest,
    CdsRiskEngine,
    CdsRiskRequest,
    CdsScenario,
    CdsSide,
    CdsSpreadQuote,
    bootstrap_hazard_curve,
    standard_cds_maturity,
    value_cds,
)
from test_inbox_p1 import BOOKS, HIT, decl, fwd, result
from test_swaps_repo_tdm45 import BD, FIX

from desk import backfill, dayrun, store
from desk.api import concepts, concepts_run, coverage, provenance
from desk.autopilot import Autopilot
from desk.curves.fx import XCCY_CURVE_ID, ForeignCurve, FxMarket, implied_curve_id
from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.inbox.validate import check_read
from desk.marketdata import manual_cds as m
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.publish import tdm_run
from desk.settings import Settings
from desk.valuation import positions

NAME = "Synthetic Reference Ltd"
KEY = m.name_key(NAME)
BOOK, PID, TID = "D1", "CDS-P1", "CDS-T1"
NOTIONAL = 5_000_000
REPORT_MV = 12_345.0
START, END = date(2023, 12, 20), date(2030, 9, 20)
SPREADS = {"1Y": 115.0, "3Y": 145.0, "5Y": 160.0}


def terms(ccy="RUB", **kw):
    return {"_src": "L1", "product_kind": "cds", "trade_id": TID, "position_id": PID, "book_id": BOOK,
            "ccy": ccy, "trade_date": "2024-01-04", "start_date": START.isoformat(), "maturity_date": END.isoformat(),
            "notional_ccy": str(NOTIONAL), "reference_entity": NAME, "coupon_bp": "70",
            "side": "sell_protection", "day_count": "ACT/ACT-ISDA", "payment_frequency": "3M",
            "pay_accrued_on_default": True, "pv_rub": "321", **kw}


def report(ccy="RUB"):
    return {"asset_class": "otc", "ccy": ccy, "deal_id": TID, "mv_rub": REPORT_MV,
            "x": {"x_product_group": "cds", "x_issuer": NAME.lower()}}


def position_inputs(ctx, row, ts):
    return store.PositionInputs([(PID, BOOK, None, None)], [], {BOOK: ctx.business_date}, datetime.now(UTC), "",
                                {PID: row}, trade_terms=ts)


def curve(ccy="RUB", spreads=None, recovery=0.25):
    return m.Curve(7, KEY, NAME, BD, SPREADS if spreads is None else spreads, recovery, ccy, "test", datetime.now(UTC))


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), BD.isoformat())


def discount(ccy, on):
    cid = RUONIA_OIS_CURVE_ID if ccy == "RUB" else implied_curve_id(ccy)
    return cid, DiscountCurve.flat(on, 0.12 if ccy == "RUB" else 0.04)


def with_discount(ctx, ccy, on):
    cid, disc = discount(ccy, on)
    market = SimpleNamespace(fx=lambda _: {"cbr": 91.0}, future=lambda _: None)
    return SimpleNamespace(
        business_date=on, calendar=BusinessCalendar(),
        core_market=SimpleNamespace(curves=(CurveEntry(cid, "synthetic-flat", disc),)),
        fx=FxMarket(), fx_options=ctx.fx_options, quote_curve_risk=False, proxy_curves=False,
        risk_curve=lambda _, built: built,
        market=market, cache=ctx.cache, kbd=ctx.kbd, curve=ctx.curve,
    )


@pytest.mark.parametrize("ccy,day_count,dc,frequency,months,accrual,fx", [
    ("RUB", "ACT/ACT-ISDA", DayCount.ACT_ACT_ISDA, "3M", 3, True, 1.0),
    ("USD", "ACT/360", DayCount.ACT_360, "6M", 6, False, 91.0),
])
@pytest.mark.parametrize("side", ["sell_protection", "buy_protection"])
def test_pricing_and_signed_risk_match_core(ctx, ccy, day_count, dc, frequency, months, accrual, fx, side):
    t = terms(ccy, day_count=day_count, payment_frequency=frequency, pay_accrued_on_default=accrual, side=side)
    local = with_discount(ctx, ccy, BD)
    x = report(ccy)
    x["x"] = {}  # product_kind and reference_entity suffice
    bp = positions.plan(local, position_inputs(local, x, [t]), cds_curves={KEY: curve(ccy)}, second_order=False)
    got = bp.direct[PID].valuation
    assert got is not None, bp.direct[PID].detail
    _, disc = discount(ccy, BD)
    market_conv = CdsConvention.isda_standard(local.calendar)
    conv = replace(market_conv, accrual_day_count=dc, payment_frequency_months=months,
                   accrual_on_default=market_conv.accrual_on_default if accrual else AccrualOnDefault.NONE)
    core_side = CdsSide.SELLER if side == "sell_protection" else CdsSide.BUYER
    contract = CdsContract(TID, NAME, ccy, core_side, NOTIONAL, 0.007, START, END, conv)
    quotes = tuple(CdsSpreadQuote(standard_cds_maturity(BD, months, roll="semiannual"), bp / 10_000)
                   for months, bp in zip((12, 36, 60), SPREADS.values()))
    credit = CdsCurveRequest(BD, quotes, 0.25, market_conv)
    hazard = bootstrap_hazard_curve(credit, disc)
    pv = value_cds(contract, valuation_date=BD, discount_curve=disc, hazard_curve=hazard, recovery_rate=0.25)
    engine = CdsRiskEngine(CdsRiskRequest(contract, BD, disc, credit))
    risk = engine.risk()
    assert got.pv_rub == pytest.approx(pv.pv * fx)
    assert got.accrued_rub == pytest.approx(pv.accrued_premium * fx)
    assert got.dv01_rub == pytest.approx(risk.dv01 * fx)
    assert got.spread01_rub == pytest.approx(risk.cs01 * fx)
    assert got.buckets == pytest.approx({b.bucket: b.dv01 * fx for b in risk.buckets})
    assert (got.spread01_rub < 0) == (core_side is CdsSide.SELLER)
    assert (engine.reprice(CdsScenario(spread_shift_bp=1)).total_pnl < 0) == (core_side is CdsSide.SELLER)
    assert bp.cds_curve_revs == {KEY: 7}
    assert got.extra["reported_pv_rub"] == REPORT_MV
    assert got.extra["pv_diff_rub"] == pytest.approx(got.pv_rub - REPORT_MV)
    assert got.fx_delta == ({} if ccy == "RUB" else {ccy: pytest.approx(pv.pv)})
    published = tdm_run.direct_row(local, BOOK, PID, bp.direct[PID], x, None)
    assert published["instr_type"] == "CDS" and published["ccy"] == ccy
    assert published["cs01"] == pytest.approx(risk.cs01 * fx)
    assert published["accrued"] == pytest.approx(pv.accrued_premium * fx)
    assert published["notional"] == (NOTIONAL if core_side is CdsSide.SELLER else -NOTIONAL)


def test_prices_cds_on_built_curve_of_fx_implied_request(ctx):
    cid = implied_curve_id("CNY")
    assert any(c.curve_id == cid and not isinstance(c.curve, DiscountCurve) for c in ctx.core_market.curves)
    x = report("CNY")
    bp = positions.plan(ctx, position_inputs(ctx, x, [terms("CNY")]), cds_curves={KEY: curve("CNY")}, second_order=False)
    v = bp.direct[PID].valuation
    assert v is not None and v.discount_curve_id == cid
    assert v.spread01_rub < 0 and "CNY" in v.fx_delta


@pytest.mark.parametrize("gap,detail", [
    ("terms", "нет условий сделки"), ("curve", "нет котировок C22"),
    ("ccy", "задана в USD, а сделка в RUB"), ("discount", "нет кривой дисконтирования для RUB"),
    ("empty", "кривая без точек"), ("recovery", "некорректные условия сделки или параметры кривой C22"),
    ("frequency", "payment_frequency"), ("day_count", "day_count"),
])
def test_missing_or_invalid_inputs_never_use_report_mv(ctx, gap, detail):
    ts = [] if gap == "terms" else [terms()]
    if gap == "frequency":
        ts[0]["payment_frequency"] = "2M"
    if gap == "day_count":
        ts[0]["day_count"] = "30/360"
    c = curve("USD" if gap == "ccy" else "RUB", {} if gap == "empty" else None,
              recovery=1.0 if gap == "recovery" else 0.25)
    if gap == "discount":
        ctx = replace(ctx, core_market=replace(ctx.core_market, curves=()))
    x = report()
    bp = positions.plan(ctx, position_inputs(ctx, x, ts), cds_curves={} if gap == "curve" else {KEY: c})
    outcome = bp.direct[PID]
    assert outcome.valuation is None
    why = coverage.why_of(f"{outcome.reason}: {outcome.detail}", x, None)
    assert why["short"] == "нет данных"
    assert why["detail"].startswith("нет данных для оценки CDS: ") and detail in why["detail"]


def test_cds_terms_110_and_older_adapters():
    source = decl("trade_terms", ["book_id"])
    scopes = [{"book_id": BOOK}]
    accepted = check_read(source, HIT, result([terms(pay_accrued_on_default=False)], scopes),
                          books=BOOKS, contract="private/1.10")
    assert accepted.status == "accepted" and accepted.issues == []
    assert accepted.units[0]["rows"][0]["pay_accrued_on_default"] is False
    invalid = check_read(source, HIT, result([terms(pay_accrued_on_default="false")], scopes),
                         books=BOOKS, contract="private/1.10")
    assert invalid.status == "rejected"
    for minor in range(2, 10):
        assert check_read(source, HIT, result([fwd()], scopes), books=BOOKS, contract=f"private/1.{minor}").status == "accepted"


def test_forward_start_act_act_alias_has_no_accrued_and_matches_isda(ctx):
    local = with_discount(ctx, "RUB", BD)
    x = report()
    t = terms(start_date="2027-03-20")
    v, alias = [positions.plan(local, position_inputs(local, x, [{**t, "day_count": dc}]),
                               cds_curves={KEY: curve()}, second_order=False).direct[PID].valuation
                for dc in ("ACT/ACT-ISDA", "ACT/ACT")]
    assert v is not None and alias is not None
    assert v.accrued_rub == alias.accrued_rub == 0
    assert v.pv_rub != 0 and v.pv_rub == pytest.approx(alias.pv_rub)
    assert v.spread01_rub == pytest.approx(alias.spread01_rub)


def test_multi_trade_position_only_needs_c22_for_live_trades(ctx):
    local = with_discount(ctx, "RUB", BD)
    x = report()
    ts = [terms(), terms(trade_id="CDS-T2", coupon_bp="80", side="buy_protection"),
          terms(trade_id="CDS-T3", maturity_date=BD.isoformat(), reference_entity="Synthetic Settled Name")]

    def plan(trades):
        return positions.plan(local, position_inputs(local, x, trades), cds_curves={KEY: curve()},
                              second_order=False).direct[PID].valuation

    combined = plan(ts)
    parts = [plan([t]) for t in ts[:2]]
    assert all(parts) and combined is not None
    for field in ("pv_rub", "dv01_rub", "spread01_rub", "accrued_rub"):
        assert getattr(combined, field) == pytest.approx(sum(getattr(p, field) for p in parts))
    assert combined.extra["cds"]["trades"][-1]["settled"] is True
    assert combined.extra["reported_pv_rub"] == REPORT_MV


def test_settled_cds_without_quotes_or_discount_curve(ctx):
    local = with_discount(ctx, "RUB", BD)
    local.core_market.curves = ()
    x = report()
    bp = positions.plan(local, position_inputs(local, x, [terms(maturity_date=BD.isoformat())]),
                        cds_curves={}, second_order=False)
    v = bp.direct[PID].valuation
    assert v is not None and v.pv_rub == v.dv01_rub == v.spread01_rub == 0
    assert v.extra["core"]["curves"] == []
    assert "curve_rev_id" not in v.extra["cds"]["trades"][0]


@pytest.mark.parametrize("extra", [
    {"x_digital_recovery": "0.25"}, {"x_digital_recovery": 0}, {"x_stub_type": "LongFront"},
    {"x_stub_type": "ShortBack"},
])
def test_refuses_digital_recovery_and_nonstandard_stub(ctx, extra):
    x = report()
    o = positions.plan(ctx, position_inputs(ctx, x, [terms(**extra)]), cds_curves={},
                       second_order=False).direct[PID]
    assert o.valuation is None and o.reason == "cds_no_data"
    assert next(iter(extra)) in o.detail


@pytest.mark.parametrize("ccy,cid,kind", [("CNY", XCCY_CURVE_ID, "xccy"), ("EUR", "EUR AAA", "proxy")])
@pytest.mark.parametrize("begin_at_end", [False, True])
def test_alternate_discount_and_carried_credit_inputs_are_visible(ctx, ccy, cid, kind, begin_at_end):
    local = with_discount(ctx, ccy, BD)
    # Give the desk calendar an explicit weekday holiday. Foreign CDS uses the core weekday calendar instead.
    local.calendar = BusinessCalendar(holidays=frozenset({BD + timedelta(days=1)}), version="fixture-holidays")
    local.core_market.curves = ()
    local.proxy_curves = True
    local.fx.curves[cid] = ForeignCurve(cid, ccy, kind, "fixture", cid, discount(ccy, BD)[1])
    extra = {"x_stub_type": "ShortFront", "x_calendar": "TEST_CAL", "x_rolling": "Following",
             "x_schedule_adjustment": "Adjusted", "x_protection_start_date": "2024-01-05", "x_begin_at_end": begin_at_end}
    x = report(ccy)
    carried = replace(curve(ccy), valid_from=BD - timedelta(days=3))
    bp = positions.plan(local, position_inputs(local, x, [terms(ccy, **extra)]), cds_curves={KEY: carried},
                        second_order=False)
    o = bp.direct[PID]
    v = o.valuation
    assert v is not None and v.discount_curve_id == cid
    expected = positions.plan(with_discount(ctx, ccy, BD), position_inputs(local, x, [terms(ccy)]),
                              cds_curves={KEY: carried}, second_order=False).direct[PID].valuation
    assert expected is not None and v.pv_rub == pytest.approx(expected.pv_rub)
    assert v.extra["core"]["curve_fallback_from"] == [implied_curve_id(ccy)]
    assumptions = " ".join(v.assumptions)
    assert all(s in assumptions for s in ("weekends-only", "semiannual IMM", cid, "12.10.2026, 3 дн."))
    assert all(s in assumptions for s in extra if s not in ("x_stub_type", "x_begin_at_end"))
    assert ("x_begin_at_end" in assumptions) is begin_at_end
    fact = tdm_run.direct_row(local, BOOK, PID, o, x, None)
    used = {item["id"]: item for item in provenance._used({}, fact, {})}
    assert used[cid]["state"] == "replaced"
    assert used[f"manual_cds:{carried.rev_id}"]["state"] == "stale"


def test_isda_eur_single_quote_golden(ctx):
    # Public Markit/QuantLib example in core docs/cds.md. Discount factors reproduced by core tests/test_cds.py
    # from the published deposit and swap rates (test_isda_reconciliation_record_with_negative_rates).
    on = date(2021, 7, 26)
    nodes = (
        ("2021-08-30", 1.00054473254594), ("2021-10-28", 1.00142332053730), ("2022-01-28", 1.00269092437529),
        ("2022-07-28", 1.00505485837463), ("2023-07-28", 1.00974244257381), ("2024-07-29", 1.01342831947320),
        ("2025-07-28", 1.01616822064184), ("2026-07-28", 1.01785019178873), ("2027-07-28", 1.01808968813037),
        ("2028-07-28", 1.01685143035422), ("2029-07-30", 1.01431726821542), ("2030-07-29", 1.01043334197881),
        ("2031-07-28", 1.00549994980937), ("2033-07-28", 0.993122793729523), ("2036-07-28", 0.971725164174028),
        ("2041-07-29", 0.941738812166278), ("2051-07-28", 0.917938902180870),
    )
    local = with_discount(ctx, "EUR", on)
    disc = DiscountCurve(on, tuple((date.fromisoformat(d), df) for d, df in nodes))
    local.core_market.curves = (CurveEntry(implied_curve_id("EUR"), "isda-golden", disc),)
    t = terms("EUR", trade_date=on.isoformat(), start_date="2021-06-21", maturity_date="2026-06-20",
              day_count="ACT/360", side="buy_protection", notional_ccy="1000000", coupon_bp="100")
    c = replace(curve("EUR", {"5Y": 67.13}, recovery=0.4), valid_from=on)
    o = positions.plan(local, position_inputs(local, report("EUR"), [t]), cds_curves={KEY: c},
                       second_order=False).direct[PID]
    assert o.valuation is not None, o.detail
    fact = o.valuation.extra["cds"]["trades"][0]
    assert fact["clean_pv_ccy"] == pytest.approx(-16070.7, abs=0.05)
    assert fact["accrued_ccy"] == pytest.approx(-1000.0)
    assert fact["par_spread_bp"] == pytest.approx(67.13)


def test_pnl_uses_cds_horizon_premium_carry_and_fx(ctx):
    prev = with_discount(ctx, "USD", BD)
    on = BD + timedelta(days=7)
    cur = with_discount(ctx, "USD", on)
    cur.market = SimpleNamespace(fx=lambda _: {"cbr": 93.0}, future=lambda _: None)
    # The horizon crosses the final coupon. Core values the trade as settled once step-in is past maturity.
    t = terms("USD", maturity_date=(BD + timedelta(days=5)).isoformat(), day_count="ACT/360")
    x = report("USD")
    inputs, prev_inputs = position_inputs(cur, x, [t]), position_inputs(prev, x, [t])
    curves = {KEY: curve("USD")}
    bp = positions.plan(cur, inputs, cds_curves={}, second_order=False)
    p = dayrun.attach_direct_pnl(prev, cur, bp, inputs, prev_inputs, cds_curves=curves)
    v, old = bp.direct[PID].valuation, p.direct[PID].valuation
    assert v is not None and old is not None
    assert v.pv_rub == 0
    h = old.extra["horizon"]
    assert h["received_ccy"] > 0 and h["received_rub"] > 0
    pnl = bp.direct[PID].pnl
    assert pnl is not None
    assert pnl.carry == pytest.approx(h["carry_rub"])
    assert pnl.rolldown == pytest.approx(h["rolldown_rub"])
    assert pnl.total == pytest.approx(v.pv_rub - old.pv_rub + h["received_rub"])


@pytest.mark.parametrize("shift,changed", [(1, False), (-1, False), (1, True)])
def test_daily_credit_pnl_uses_prior_quote_cs01(ctx, shift, changed):
    prev, cur = (with_discount(ctx, "RUB", d) for d in (BD, BD + timedelta(days=1)))
    x, ts = report(), [terms()]
    inputs, prev_inputs = position_inputs(cur, x, ts), position_inputs(prev, x, ts)
    bumped = {k: v + shift for k, v in SPREADS.items()}
    if changed:
        bumped.pop("3Y")
        bumped["2Y"] = 135
    today = replace(curve(spreads=bumped, recovery=0.30 if changed else 0.25),
                    rev_id=8, valid_from=cur.business_date)
    bp = positions.plan(cur, inputs, cds_curves={KEY: today}, second_order=False)
    old = dayrun.attach_direct_pnl(prev, cur, bp, inputs, prev_inputs, cds_curves={KEY: curve()})
    pv, pnl = old.direct[PID].valuation, bp.direct[PID].pnl
    assert pv is not None and pnl is not None
    cs = pv.extra["cds"]["trades"][0]["cs01_by_quote_rub"]
    assert pnl.spread == pytest.approx(shift * sum(cs[k] for k in SPREADS if k in bumped))
    assert pnl.spread * shift < 0
    assert pnl.explained == pytest.approx(pnl.total)
    if changed:
        detail = pnl.other_reason["detail"]
        assert "изменился состав теноров C22" in detail and "изменилась доля возмещения C22" in detail
        assert abs(pnl.other) > 0
    else:
        # Isolate the quote shock on D. Even with unchanged quotes, rolling the hazard nodes by one day
        # and recalibrating to the same IMM pillar dates leave a small time residual.
        unchanged = positions.plan(cur, inputs, cds_curves={KEY: curve()}, second_order=False).direct[PID].valuation
        actual = bp.direct[PID].valuation
        assert unchanged is not None and actual is not None
        actual_credit = actual.pv_rub - unchanged.pv_rub
        assert pnl.spread == pytest.approx(actual_credit, rel=0.002)
        assert abs(pnl.other) < abs(pnl.spread) * 0.03


def test_fx_implied_built_curve_moves_are_in_rates(ctx):
    cid = implied_curve_id("CNY")
    request = next(e for e in ctx.core_market.curves if e.curve_id == cid)
    prev, cur = (with_discount(ctx, "CNY", d) for d in (BD, BD + timedelta(days=1)))
    for local, rate in ((prev, 0.04), (cur, 0.041)):
        local.core_market.curves = (request,)
        local.fx.curves[cid] = replace(ctx.fx.curves[cid], curve=DiscountCurve.flat(local.business_date, rate))
    x, ts = report("CNY"), [terms("CNY")]
    inputs, prev_inputs = position_inputs(cur, x, ts), position_inputs(prev, x, ts)
    curves = {KEY: curve("CNY")}
    bp = positions.plan(cur, inputs, cds_curves=curves, second_order=False)
    old = dayrun.attach_direct_pnl(prev, cur, bp, inputs, prev_inputs, cds_curves=curves)
    pv, pnl = old.direct[PID].valuation, bp.direct[PID].pnl
    assert pv is not None and pnl is not None
    assert pnl.rates == pytest.approx(10 * pv.dv01_rub)
    assert pnl.rates != 0 and pnl.spread == 0


def test_eod_publishes_and_recalculates_after_c22_revision(tmp_path, store_url):
    settings = Settings(home=tmp_path, snapshot_cache=FIX, store_url=store_url)
    x = report()
    a = {"valid_date": BD, "source": "synthetic", "priority": None, "report_date": BD, "report_ts": None,
         "seq": None, "generated_at": None, "file_mtime": datetime.now(UTC), "file_name": "synthetic.csv",
         "sha256": "0" * 64, "adapter_fingerprint": "fp", "unit_hash": "u"}
    with store.connect(store_url) as con:
        con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, %s, %s, 1, now())",
                    (a["sha256"], a["file_name"], a["file_name"]))
        store.store_inbox_set(con, book_id=BOOK,
                              rows=[{**x, "x_product_group": "cds", "x_issuer": NAME, "mv_rub": str(REPORT_MV),
                                     "position_id": PID, "book_id": BOOK, "_src": "L1"}], **a)
        store.store_dataset_set(con, dataset="trade_terms", scope={"book_id": BOOK}, rows=[terms()], **a)
    rid = dayrun.eod_run(settings, BD.isoformat(), out=lambda _: None)
    with store.connect(store_url) as con:
        run = store.default_runs(con, BD)[BD]
        assert run["manual_cds"] == {BD.isoformat(): {KEY: None}}
        assert con.execute("SELECT pv_rub, dv01_rub, cs01_rub, fact FROM run_position WHERE run_id = %s",
                           (rid,)).fetchone() == (None, None, None, None)
        parallel = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'derivatives_parallel'",
                               (rid,)).fetchone()[0]["rows"][0]
        assert parallel["product"] == "cds" and parallel["used"] == "none"
        assert parallel["imported_pv_rub"] == REPORT_MV
        con.row_factory = dict_row
        api_run = con.execute("SELECT * FROM run WHERE run_id = %s", (rid,)).fetchone()
        shown = concepts_run.assemble(con, api_run)["positions"][0]
        assert shown["notValued"] and shown["mvRub"] is None and shown["extra"]["why"]["short"] == "нет данных"
        assert coverage.unvalued(con, api_run, False)["groups"][0]["kind"] == "CDS"
        con.row_factory = tuple_row
        m.put_curve(con, BD, "Unrelated Synthetic Name", SPREADS, ccy="RUB")
        assert backfill.late_why(con, BD, run) is None
        first, _ = m.put_curve(con, BD, NAME, SPREADS, ccy="RUB")
        assert backfill.plan(con, settings, BD, BD).todo[0].why == "manual CDS curves changed after the run"
    with store.connect(store_url) as con:
        ap = Autopilot(settings_loader=lambda: settings, store_url=store_url, today=lambda: BD)
        ran: list[str] = []
        assert ap._eod(settings, con, store_url, [], ran)[0] == "ok" and ran
        run = store.default_runs(con, BD)[BD]
        rid = run["run_id"]
        assert run["manual_cds"] == {BD.isoformat(): {KEY: first.rev_id}}
        assert backfill.late_why(con, BD, run) is None
        con.row_factory = dict_row
        api_run = con.execute("SELECT * FROM run WHERE run_id = %s", (rid,)).fetchone()
        p = con.execute("SELECT pv_rub, cs01_rub, fact FROM run_position WHERE run_id = %s", (rid,)).fetchone()
        assert p["pv_rub"] is not None and p["pv_rub"] != REPORT_MV and p["cs01_rub"] < 0
        assert p["fact"]["extra"]["cds"]["trades"][0]["curve_rev_id"] == first.rev_id
        items, _ = concepts.risk_items(con, api_run)
        assert concepts.instr_kind(items[0]["fact"]) == "CDS"
        origin = provenance.provenance(con, api_run, PID)
        assert any(s["key"] == "trade_terms" and s["state"] == "present" for s in origin["sources"])
        assert any(i["id"] == f"manual_cds:{first.rev_id}" for i in origin["market"]["items"])
        con.row_factory = tuple_row
        revised, _ = m.put_curve(con, BD, NAME, {k: v + 10 for k, v in SPREADS.items()}, ccy="RUB")
        assert revised.rev_id != first.rev_id
        assert backfill.late_why(con, BD, run) == "manual CDS curves changed after the run"
    rid = dayrun.eod_run(settings, BD.isoformat(), out=lambda _: None)
    with store.connect(store_url) as con:
        assert store.default_runs(con, BD)[BD]["manual_cds"] == {BD.isoformat(): {KEY: revised.rev_id}}
        assert con.execute("SELECT pv_rub FROM run_position WHERE run_id = %s", (rid,)).fetchone()[0] < p["pv_rub"]
        m.put_curve(con, BD, NAME, None)
        assert backfill.late_why(con, BD, store.default_runs(con, BD)[BD]) == "manual CDS curves changed after the run"
