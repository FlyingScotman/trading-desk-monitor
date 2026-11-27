"""FX-implied curves from futures of one delivery date and from an intraday cut (TDM#695).

On the hub's contract 1.4 golden snapshot (``fixtures/mdhub_v1_4``) with synthetic rows in the shape of the published
ones (invented tickers' numbers; no real figures):

- the main USD contract and its mini contract fix the same day: one quote per delivery date reaches core (it refuses
  two), the other is in the lineage's ``dropped`` with the reason;
- an intraday cut has no ``futures_prices`` and no ``fx_swaps``, only ``futures_prices_intraday``: the curve is built
  from it at the mid of bid and offer (else the last price), the lineage says so and gives the latest ``source_ts``;
- a currency with nothing to build from says why in the message;
- a preliminary close (``eod_preliminary``) without the exchange's results takes the hub's last intraday quotes at the
  cut by the same rules, by currency; a final close never does (TDM#704).
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from desk.api.ocp_line import method_note, ocp_line
from desk.curves import fx as fxcurves
from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.marketdata import adapter
from desk.marketdata.bundle import Snapshot, SnapshotDir
from desk.orchestrator import market_rates, prepare
from desk.pnl.direct import attach
from desk.pnl.reasons import FX_FUTURE_METHOD
from desk.publish.tdm_run import direct_row
from desk.rules import ocp as O
from desk.scenario.fx_ocp import fx_ocp
from desk.scenario.taylor import risk_payload, run_taylor
from desk.store import PositionInputs
from desk.valuation import positions

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1_4"
DATE = "2026-10-15"
Z6, H7 = date(2026, 12, 17), date(2027, 3, 18)


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


def usd_future(ctx, **kw):
    """A copy of the golden's Si row with ``kw`` over it."""
    row = next(r for r in ctx.market.futures_prices if r["secid"] == "SiZ6")
    return {**copy.deepcopy(row), **kw}


def market_with(ctx, mutate, cut_type=None):
    """The golden market view with ``mutate(data)`` applied to a copy of its rows (not re-hashed: nothing reads the id),
    as a cut of ``cut_type`` (the golden's, an end-of-day one, by default)."""
    snap = ctx.market.snapshot
    doc = copy.deepcopy(snap.doc)
    mutate(doc["data"])
    cut_type = cut_type or snap.cut_type
    return adapter.MarketView(Snapshot(snap.id, snap.kind, cut_type, snap.business_date,
                                       snap.static_snapshot_id, {**doc, "cut_type": cut_type}))


def curves_of(ctx, market, **options):
    """``with_fx`` on ``market``: the FX lineage of the run's curves."""
    base = market.core_market(ctx.static)
    ruonia = next(e for e in ctx.core_market.curves if e.curve_id == RUONIA_OIS_CURVE_ID)
    core = replace(base, curves=(*base.curves, ruonia))
    out, fx = fxcurves.with_fx(market, ctx.static, core, market_rates(market),
                               replace(fxcurves.FxOptions(), **options))
    return out, fx, fx.lineage["curves"]


def intraday(secid, delivery, **kw):
    """A ``futures_prices_intraday`` row of a USD/RUB future in the published field set."""
    return {"asset_code": "Si", "base_ccy": "USD", "quote_ccy": "RUB", "is_fx": True, "contract_type": "dated",
            "secid": secid, "last_del_date": delivery.isoformat(), "trade_date": DATE, "open_position": "1000",
            "vol_today": "10", "bid": None, "offer": None, "last": None, "bid_rub_per_unit": None,
            "offer_rub_per_unit": None, "last_rub_per_unit": None, "source_ts": "2026-10-15T10:00:00+00:00",
            "ts": "2026-10-15T10:00:01+00:00", "labels": [], **kw}


def outright(ctx, fx, ccy, day):
    spot = fx.spots[ccy]
    c = fx.curves[f"{ccy} FX"].curve
    rub = next(e.curve for e in ctx.core_market.curves if e.curve_id == RUONIA_OIS_CURVE_ID)
    return spot.rub_per_unit * (c.discount_factor(day) / c.discount_factor(spot.value_date)) / (
        rub.discount_factor(day) / rub.discount_factor(spot.value_date))


# --- end of day: the main contract and its mini contract on one date -----------------------------------------------


def test_golden_usd_curve_is_unchanged(ctx):
    lin = ctx.fx.lineage["curves"]["USD FX"]
    assert lin["futures"] == ["SiZ6"] and lin["dropped"] == {} and lin["futures_source"] == "eod"
    assert "futures_source_ts" not in lin


def test_mini_contract_on_the_same_date_is_dropped(ctx):
    mini = usd_future(ctx, secid="UMZ6", asset_code="UM", open_position="900", volume="99999", short_name="UM-12.26",
                      settle_rub_per_unit="85.3")  # more volume, less open interest: the open interest decides
    market = market_with(ctx, lambda d: d["futures_prices"].append(mini))
    assert [r["secid"] for r in market.fx_futures("USD")] == ["SiZ6"]
    assert {r["secid"] for r in market.fx_future_quotes("USD", all_contracts=True).rows} == {"SiZ6", "UMZ6"}
    _, fx, lin = curves_of(ctx, market)
    usd = lin["USD FX"]
    assert usd["status"] == "ok" and usd["futures"] == ["SiZ6"] and usd["futures_source"] == "eod"
    assert usd["dropped"] == {"UMZ6": "same delivery date as SiZ6 (larger open interest)"}
    assert outright(ctx, fx, "USD", Z6) == pytest.approx(85.219, abs=1e-9)  # Si's settlement price, not the mini's


def test_the_larger_open_interest_wins_whichever_ticker(ctx):
    mini = usd_future(ctx, secid="UMZ6", open_position="99999999", settle_rub_per_unit="85.3")
    market = market_with(ctx, lambda d: d["futures_prices"].append(mini))
    _, fx, lin = curves_of(ctx, market)
    assert lin["USD FX"]["futures"] == ["UMZ6"]
    assert lin["USD FX"]["dropped"] == {"SiZ6": "same delivery date as UMZ6 (larger open interest)"}
    assert outright(ctx, fx, "USD", Z6) == pytest.approx(85.3, abs=1e-9)


def test_ties_break_on_volume_then_ticker(ctx):
    rows = [usd_future(ctx, secid=s, open_position="5", volume=v) for s, v in (("UMZ6", "7"), ("SiZ6", "7"), ("XxZ6", "8"))]
    market = market_with(ctx, lambda d: d.update(futures_prices=[r for r in rows]))
    assert [r["secid"] for r in market.fx_futures("USD")] == ["XxZ6"]
    quotes = market.fx_future_quotes("USD")
    assert quotes.dropped == {"SiZ6": "same delivery date as XxZ6 (larger volume)",
                              "UMZ6": "same delivery date as XxZ6 (larger volume)"}
    market = market_with(ctx, lambda d: d.update(futures_prices=[r for r in rows[:2]]))
    assert market.fx_future_quotes("USD").dropped == {"UMZ6": "same delivery date as SiZ6 (ticker order)"}


def test_end_of_day_prices_win_over_the_intraday_ones(ctx):
    market = market_with(ctx, lambda d: d.update(futures_prices_intraday=[
        intraday("SiZ6", Z6, last_rub_per_unit="1.0", last="1000")]), "intraday")
    quotes = market.fx_future_quotes("USD")
    assert quotes.source == "eod" and [(r["secid"], r["price"]) for r in quotes.rows] == [("SiZ6", 85.219)]


def test_a_final_close_never_takes_intraday_quotes(ctx):
    def mutate(d):
        intraday_cut(d)
        d["futures_prices_intraday"][0]["trade_date"] = DATE
    market = market_with(ctx, mutate, "eod_final")
    quotes = market.fx_future_quotes("USD")
    assert quotes.rows == [] and quotes.source is None and quotes.dropped == {}
    _, _, lin = curves_of(ctx, market)
    assert lin["USD FX"]["status"] == "missing"
    assert lin["USD FX"]["error"].endswith("no futures in futures_prices (intraday quotes are not used in the final close)")
    assert market_with(ctx, mutate, "intraday").fx_future_quotes("USD").source == "intraday"


# --- an intraday cut ----------------------------------------------------------------------------------------------


def intraday_cut(d):
    d["futures_prices"] = []
    d["fx_swaps"] = []
    d["futures_prices_intraday"] = [
        intraday("SiZ6", Z6, bid_rub_per_unit="85.40", offer_rub_per_unit="85.50", last_rub_per_unit="85.00",
                 open_position="2000", source_ts="2026-10-15T10:05:00+00:00"),
        intraday("UMZ6", Z6, bid_rub_per_unit="85.10", offer_rub_per_unit="85.20", open_position="150",
                 vol_today="999999", source_ts="2026-10-15T10:09:00+00:00"),
        # no bid: the last price
        intraday("SiH7", H7, bid_rub_per_unit="0", offer_rub_per_unit="88.00", last_rub_per_unit="87.60",
                 source_ts="2026-10-15T10:07:00+00:00"),
        intraday("SiM7", date(2027, 6, 17), source_ts="2026-10-15T10:01:00+00:00"),  # never traded: no price
        intraday("SiU6", date(2026, 9, 17), last_rub_per_unit="84", trade_date="2026-10-14"),  # another day
        intraday("USDRUBF", Z6, contract_type="perpetual", last_del_date=None, last_rub_per_unit="83.9"),
        intraday("EuZ6", Z6, base_ccy="EUR", last_rub_per_unit="97.0"),
    ]


def test_intraday_cut_builds_the_curve_from_the_intraday_futures(ctx):
    market = market_with(ctx, intraday_cut, "intraday")
    assert market.futures_prices == [] and market.fx_swap_quotes("USD") == []
    _, fx, lin = curves_of(ctx, market)
    usd = lin["USD FX"]
    assert usd["status"] == "ok" and usd["swaps"] == ["anchor:TOD/TOM"] and usd["futures"] == ["SiZ6", "SiH7"]
    assert usd["futures_source"] == "intraday"
    # the times and the kinds are of the futures that entered the curve (SiZ6 10:05 mid, SiH7 10:07 last), not of the
    # mini contract (10:09) or the others
    assert usd["futures_source_ts"] == "2026-10-15T10:07:00+00:00"
    assert usd["futures_source_ts_oldest"] == "2026-10-15T10:05:00+00:00"
    assert usd["futures_price_kind"] == {"SiZ6": "mid", "SiH7": "last"}
    assert usd["dropped"] == {
        "UMZ6": "same delivery date as SiZ6 (larger open interest)",
        "SiM7": "no bid/offer or last price",
        "SiU6": "trade date 2026-10-14, before the business date"}
    assert outright(ctx, fx, "USD", Z6) == pytest.approx(85.45, abs=1e-9)  # the mid of the bid and the offer
    assert outright(ctx, fx, "USD", H7) == pytest.approx(87.60, abs=1e-9)  # no bid: the last price
    assert lin["CNY FX"]["status"] != "error"  # a currency without data (no CNY row here) is missing, not an error


def test_intraday_rows_without_a_trade_date_are_taken(ctx):
    def mutate(d):
        intraday_cut(d)
        for r in d["futures_prices_intraday"]:
            r["trade_date"] = None
    market = market_with(ctx, mutate, "intraday")
    assert [r["secid"] for r in market.fx_future_quotes("USD").rows] == ["SiU6", "SiZ6", "SiH7"]  # by delivery date


def test_the_evening_session_trades_on_the_next_trading_day(ctx):
    """FORTS's evening session carries the next trading day's date: a row dated after the business date is taken, one
    dated before it is not."""
    rows = [intraday("SiZ6", Z6, last_rub_per_unit="85.5", trade_date="2026-10-16"),  # the next business day (Friday)
            intraday("SiH7", H7, last_rub_per_unit="86.5", trade_date="2026-10-19"),  # the Monday after a weekend
            intraday("SiM7", date(2027, 6, 17), last_rub_per_unit="87.5", trade_date="2026-10-14")]
    market = market_with(ctx, lambda d: d.update(futures_prices=[], futures_prices_intraday=rows), "intraday")
    quotes = market.fx_future_quotes("USD")
    assert [r["secid"] for r in quotes.rows] == ["SiZ6", "SiH7"]
    assert quotes.dropped == {"SiM7": "trade date 2026-10-14, before the business date"}


def test_mid_needs_both_sides_positive(ctx):
    rows = [intraday("SiZ6", Z6, bid_rub_per_unit="85.40", offer_rub_per_unit="85.50", last_rub_per_unit="85.0"),
            intraday("SiH7", H7, bid_rub_per_unit="85.40", offer_rub_per_unit=None, last_rub_per_unit="86.0"),
            intraday("SiM7", date(2027, 6, 17), bid_rub_per_unit="86.0", offer_rub_per_unit="86.2")]
    market = market_with(ctx, lambda d: d.update(futures_prices=[], futures_prices_intraday=rows), "intraday")
    quotes = market.fx_future_quotes("USD")
    assert {r["secid"]: r["price"] for r in quotes.rows} == pytest.approx({"SiZ6": 85.45, "SiH7": 86.0, "SiM7": 86.1})
    assert {r["secid"]: r["price_kind"] for r in quotes.rows} == {"SiZ6": "mid", "SiH7": "last", "SiM7": "mid"}


# --- a preliminary close: the hub's last intraday quotes at the cut (TDM#704) --------------------------------------


def preliminary(d):
    """The hub's preliminary close before the exchange's history is out: no results, no swaps, the intraday quotes."""
    intraday_cut(d)


def test_preliminary_close_builds_the_curve_from_the_intraday_futures(ctx):
    market = market_with(ctx, preliminary, "eod_preliminary")
    quotes = market.fx_future_quotes("USD")
    assert quotes.source == "intraday" and [r["secid"] for r in quotes.rows] == ["SiZ6", "SiH7"]
    _, fx, lin = curves_of(ctx, market)
    usd = lin["USD FX"]
    assert usd["status"] == "ok" and usd["futures"] == ["SiZ6", "SiH7"] and usd["futures_source"] == "intraday"
    assert usd["futures_price_kind"] == {"SiZ6": "mid", "SiH7": "last"}
    assert usd["futures_source_ts"] == "2026-10-15T10:07:00+00:00"
    assert usd["futures_source_ts_oldest"] == "2026-10-15T10:05:00+00:00"
    assert usd["dropped"]["UMZ6"] == "same delivery date as SiZ6 (larger open interest)"
    assert usd["dropped"]["SiU6"] == "trade date 2026-10-14, before the business date"
    assert outright(ctx, fx, "USD", Z6) == pytest.approx(85.45, abs=1e-9)
    from desk.api import provenance as pv
    assert pv._curve_state({"fx": {"curves": lin}}, "USD FX") == (
        "ok", "по внутридневным котировкам фьючерсов, на 15.10 13:07 МСК")


def test_preliminary_close_takes_the_evening_session_too(ctx):
    rows = [intraday("SiZ6", Z6, last_rub_per_unit="85.5", trade_date="2026-10-16"),
            intraday("SiH7", H7, last_rub_per_unit="86.5", trade_date="2026-10-14")]
    market = market_with(ctx, lambda d: d.update(futures_prices=[], futures_prices_intraday=rows), "eod_preliminary")
    quotes = market.fx_future_quotes("USD")
    assert [r["secid"] for r in quotes.rows] == ["SiZ6"]
    assert quotes.dropped == {"SiH7": "trade date 2026-10-14, before the business date"}


def test_preliminary_close_prefers_the_results_of_trading(ctx):
    """Results of the currency in the cut: the intraday quotes are not looked at."""
    market = market_with(ctx, lambda d: d.update(futures_prices_intraday=[
        intraday("SiZ6", Z6, last_rub_per_unit="1.0", last="1000")]), "eod_preliminary")
    quotes = market.fx_future_quotes("USD")
    assert quotes.source == "eod" and [(r["secid"], r["price"]) for r in quotes.rows] == [("SiZ6", 85.219)]


def test_preliminary_close_decides_by_currency(ctx):
    """Results for USD, none for EUR: EUR takes the intraday quotes, USD keeps the results (never mixed)."""
    market = market_with(ctx, lambda d: d.update(futures_prices_intraday=[
        intraday("SiZ6", Z6, last_rub_per_unit="1.0"),
        intraday("EuZ6", Z6, base_ccy="EUR", asset_code="Eu", last_rub_per_unit="97.0")]), "eod_preliminary")
    assert market.fx_future_quotes("USD").source == "eod"
    eur = market.fx_future_quotes("EUR")
    assert eur.source == "intraday" and [(r["secid"], r["price"]) for r in eur.rows] == [("EuZ6", 97.0)]


def test_a_ticker_taken_from_the_intraday_quotes_is_not_also_dropped(ctx):
    """The results of a ticker left out (no settlement price) with its intraday quote taken: the ticker is in the rows
    only, its results' reason is not in ``dropped`` (and the curve's lineage)."""
    def mutate(d):
        d["futures_prices"] = [usd_future(ctx, settle_rub_per_unit=None)]
        d["fx_swaps"] = []
        d["futures_prices_intraday"] = [intraday("SiZ6", Z6, last_rub_per_unit="85.5"),
                                        intraday("SiH7", H7, last_rub_per_unit=None)]
    market = market_with(ctx, mutate, "eod_preliminary")
    assert market._eod_futures("USD")[1] == {"SiZ6": "no settlement price"}
    quotes = market.fx_future_quotes("USD")
    assert quotes.source == "intraday" and [r["secid"] for r in quotes.rows] == ["SiZ6"]
    assert quotes.dropped == {"SiH7": "no bid/offer or last price"}
    _, _, lin = curves_of(ctx, market)
    assert lin["USD FX"]["futures"] == ["SiZ6"] and "SiZ6" not in lin["USD FX"]["dropped"]


@pytest.mark.parametrize("field", ["absent", "empty"])
def test_preliminary_close_without_the_hubs_field_is_as_before(ctx, field):
    """A hub that does not put intraday quotes into the preliminary close (the field absent or empty): no futures, the curve is missing, the message names the sets."""
    def mutate(d):
        d["futures_prices"] = []
        d["fx_swaps"] = []
        if field == "absent":
            d.pop("futures_prices_intraday", None)
        else:
            d["futures_prices_intraday"] = []
    market = market_with(ctx, mutate, "eod_preliminary")
    quotes = market.fx_future_quotes("USD")
    assert quotes.rows == [] and quotes.source is None and quotes.dropped == {}
    _, _, lin = curves_of(ctx, market)
    assert lin["USD FX"]["status"] == "missing"
    assert lin["USD FX"]["error"].endswith("no futures in futures_prices or futures_prices_intraday")


# --- nothing to build from ----------------------------------------------------------------------------------------


def test_the_message_says_why_nothing_is_left(ctx):
    def mutate(d):
        d["futures_prices"] = []
        d["fx_swaps"] = []
        d["futures_prices_intraday"] = [intraday("SiZ6", Z6), intraday("UMZ6", Z6, trade_date="2026-10-14",
                                                                         last_rub_per_unit="85")]
    _, _, lin = curves_of(ctx, market_with(ctx, mutate, "intraday"))
    usd = lin["USD FX"]
    assert usd["status"] == "missing"
    assert usd["error"] == ("2026-10-15: no USD swap point or future in the snapshot; left out: "
                            "SiZ6: no bid/offer or last price; UMZ6: trade date 2026-10-14, before the business date")


def test_the_message_without_any_row_names_the_sets(ctx):
    def mutate(d):
        d["futures_prices"] = []
        d["fx_swaps"] = []
        d["futures_prices_intraday"] = []
    _, _, lin = curves_of(ctx, market_with(ctx, mutate, "intraday"))
    assert lin["USD FX"]["error"].endswith("no futures in futures_prices or futures_prices_intraday")


def test_futures_off_leaves_the_intraday_set_alone(ctx):
    _, _, lin = curves_of(ctx, market_with(ctx, intraday_cut, "intraday"), implied_futures=False)
    assert lin["USD FX"]["status"] == "missing" and lin["USD FX"]["error"].endswith("in the snapshot")


# --- what the position card shows («Откуда данные» → «Рыночные данные», column «Пояснение») ---------------------------


def test_curve_state_says_intraday_and_the_reason_for_a_missing_curve(ctx):
    from desk.api import provenance as pv

    _, _, lin = curves_of(ctx, market_with(ctx, intraday_cut, "intraday"))
    state = pv._curve_state({"fx": {"curves": lin}}, "USD FX")
    assert state == ("ok", "по внутридневным котировкам фьючерсов, на 15.10 13:07 МСК")  # 10:07 UTC
    assert pv._curve_state({"fx": {"curves": {"USD FX": {"status": "ok", "futures_source": "intraday"}}}},
                           "USD FX") == ("ok", "по внутридневным котировкам фьючерсов")  # an old run without the time
    _, _, eod = curves_of(ctx, market_with(ctx, lambda d: None))
    assert pv._curve_state({"fx": {"curves": eod}}, "USD FX") == ("ok", "")  # an end-of-day curve: nothing to add
    missing = {"USD FX": {"status": "missing", "error": "2026-10-15: no USD swap point or future in the snapshot; "
                                                        "left out: SiZ6: no bid/offer or last price"}}
    assert pv._curve_state({"fx": {"curves": missing}}, "USD FX") == (
        "missing", ("кривая не построена: missing. 2026-10-15: no USD swap point or future in the snapshot; "
                    "left out: SiZ6: no bid/offer or last price"))
    assert pv._curve_state({"fx": {"curves": {"USD FX": {"status": "error"}}}}, "USD FX") == (
        "missing", "кривая не построена: error")


LONG = "2026-10-15: no USD swap point or future in the snapshot; left out: " + "; ".join(
    f"Si{c}6: no bid/offer or last price" for c in "ABCDEFGHIJKLMNOP")


def test_a_long_reason_is_cut_for_the_card(ctx):
    from desk.api import provenance as pv

    state, text = pv._curve_state({"fx": {"curves": {"USD FX": {"status": "missing", "error": LONG}}}}, "USD FX")
    assert state == "missing" and len(LONG) > 300
    assert text == "кривая не построена: missing. " + LONG[:300] + "…"
    short = pv._curve_state({"fx": {"curves": {"USD FX": {"status": "missing", "error": "x" * 300}}}}, "USD FX")
    assert short[1].endswith("x" * 300)  # exactly the limit: no ellipsis


def test_a_deal_needing_a_missing_curve_gets_the_reason_on_its_card(ctx):
    """``provenance._needed`` (the card of a derivative the run did not value): the build error of the wanted curve is in
    the row of the curve, with no curve at all and with a fallback standing in."""
    from desk.api import provenance as pv

    err = "2026-10-15: no USD swap point or future in the snapshot; left out: SiZ6: no bid/offer or last price"
    spots = {"USD": "80 MOEX 19:00, value 2026-10-16", "CNY": "11 MOEX 19:00, value 2026-10-16"}
    usd = [{"trade_id": "T1", "product_kind": "fx_forward", "base_ccy": "USD", "quote_ccy": "RUB"}]

    def curve_row(fx, terms):
        needs = pv._needed({"fx": fx}, terms, {}, date(2026, 10, 15), {})
        return next(i for i in needs if i["kind"] == "curve" and i["id"] != "RUB RUONIA")

    row = curve_row({"spots": spots, "curves": {"USD FX": {"status": "missing", "error": err}}}, usd)
    assert (row["id"], row["state"]) == ("USD FX", "missing")
    assert row["text"] == ("в рыночных данных прогона нет ни одной из кривых: USD FX. "
                           f"USD FX: кривая не построена: missing. {err}")
    plain = curve_row({"spots": spots, "curves": {}}, usd)  # the run has no such curve and no reason: the plain phrase
    assert plain["text"] == "в рыночных данных прогона нет ни одной из кривых: USD FX"
    cny = [{"trade_id": "T2", "product_kind": "fx_forward", "base_ccy": "CNY", "quote_ccy": "RUB"}]
    fx = {"spots": spots, "curves": {"CNY FX": {"status": "missing", "error": err}, "CNY XCCY": {"status": "ok"}}}
    row = curve_row(fx, cny)
    assert (row["id"], row["state"]) == ("CNY XCCY", "replaced")
    assert row["text"] == f"вместо CNY FX: её нет в рыночных данных прогона (кривая не построена: missing. {err})"


# --- FX futures positions, variation margin and OCP --------------------------------------------------------------

BOOKS = {"B1": {"entity": "E"}, "B2": {"entity": "E"}}


def future(**fields):
    return {"asset_class": "fx", "ccy": "USD", "x_sec_cur": "RUB", "x_product_group": "fx_future", "value_date": Z6,
            "amount_ccy": 2000.0, "x_quantity": 2.0, **fields}


def inputs(ctx, *rows):
    # The generic quantity differs from x_quantity and must never drive a futures valuation.
    order = [(f"P{i}", "B1" if i < 2 else "B2", None, 77.0) for i in range(len(rows))]
    return PositionInputs(order, [], {b: ctx.business_date for b in BOOKS}, datetime.now(UTC), "0" * 64,
                          {p[0]: row for p, row in zip(order, rows)})


def priced_market(ctx, cut):
    def mutate(data):
        data["futures_prices"] = [
            usd_future(ctx, settle_rub_per_unit="85.25", lot_volume="1000"),
            usd_future(ctx, secid="UMZ6", settle_rub_per_unit="85.75", lot_volume="100", open_position="1"),
        ] if cut == "eod_final" else []
        data["futures_prices_intraday"] = [
            intraday("SiZ6", Z6, lot_volume="1000", last_rub_per_unit="85.25"),
            intraday("UMZ6", Z6, lot_volume="100", open_position="1",
                     bid_rub_per_unit="85.5", offer_rub_per_unit="86"),
        ]

    return market_with(ctx, mutate, cut)


@pytest.mark.parametrize("cut", ["eod_final", "intraday", "eod_preliminary"])
def test_matches_pair_exact_expiry_and_each_lot_and_deduplicates_contracts(ctx, cut):
    market = priced_market(ctx, cut)
    core, fx, _ = curves_of(ctx, market)
    ctx = replace(ctx, market=market, core_market=core, fx=fx)
    inp = inputs(ctx, future(), future(value_date=None, maturity_date=Z6, amount_ccy=-3000, x_quantity=-3),
                 future(amount_ccy=200.01, x_quantity=None, x={"x_quantity": "2", "x_sec_cur": "RUB"}),
                 future(amount_ccy=1000, x_quantity=1, x_sec_cur="RUR", source_instrument_id="synthetic-instrument"))
    plan = positions.plan(ctx, inp)
    assert {p.contract_id for p in plan.core_result.fx_futures} == {"SiZ6", "UMZ6"}
    assert len(plan.core_result.fx_futures) == 4 and not plan.core_result.coverage.rejections
    for pid, _, _, _ in inp.rows:
        row, v = inp.extra[pid], plan.direct[pid].valuation
        assert v.pv_rub == 0 and v.extra["core"]["product"] == "fx_future"
        assert v.extra["ocp_amount"] == {"USD": row["amount_ccy"]}
        p = v.extra["futures"]
        assert p["contract_id"] == ("UMZ6" if pid == "P2" else "SiZ6")
        contracts = float((row.get("x") or {}).get("x_quantity") or row["x_quantity"])
        assert p["contracts"] == contracts and p["lot"] == abs(row["amount_ccy"] / contracts)
        price = 85.75 if pid == "P2" else 85.25
        assert p["futures_price_rub_per_unit"] == price
        spot = ctx.fx.spots["USD"].rub_per_unit
        assert v.fx_delta["USD"] == pytest.approx(row["amount_ccy"] * price / spot)
        assert set(v.curve_buckets) == {"RUB RUONIA", "USD FX"}
        assert v.extra["basis01"]["USD"] != 0
        # Core reports the rates and basis Hessian and a first-order FX factor for futures.
        assert any(a.startswith("IR|RUB RUONIA|") and b.startswith("IR|USD FX|") for a, b, _ in v.hessian)


@pytest.mark.parametrize(("fields", "reason"), [
    ({"amount_ccy": 1000}, "lot_mismatch"),
    ({"value_date": Z6 + timedelta(days=1)}, "no_future_quote"),
    ({"x_quantity": None}, "no_quantity"),
    ({"x_quantity": 0}, "no_quantity"),
    ({"x_quantity": -2}, "sign_mismatch"),
    ({"amount_ccy": -2000}, "sign_mismatch"),
    ({"x_sec_cur": None, "x_settlement_ccy": "RUB"}, "no_future_pair"),
])
def test_unmatched_position_is_excluded_from_ocp_and_scenarios(ctx, fields, reason):
    ctx = replace(ctx, market=priced_market(ctx, "eod_final"))
    x = future(**fields)
    plan = positions.plan(ctx, inputs(ctx, x))
    out = plan.direct["P0"]
    assert plan.core_result is None and out.core_not_valued[0] == reason
    assert "в ОВП не включён" in out.valuation.assumptions[0]
    if reason == "lot_mismatch":
        assert "лот в отчёте 500, лоты FORTS 100, 1000" in out.valuation.assumptions[0]
    fact = direct_row(ctx, "B1", "P0", out, x, None)
    assert fact["fx_delta"] == {} and fact["extra"]["ocp_unconfirmed"]["reason"] == reason
    on = ctx.business_date
    report = {"entity": "E", "book_id": "B1", "ccy": "USD", "ocp_ccy": "100", "_set_id": 1,
              "derivatives_ccy": {"fx_future": "2000"}}
    unit = {"scope": {"entity": "E", "book_id": "B1"}, "set_id": 1, "valid_date": on, "row_count": 1}
    ocp = O.compute_ocp([fact], BOOKS, on, report_rows=[report], report_units=[unit])
    assert not ocp.lines and any(g["code"] == reason and g["items"] == ["P0"] for g in ocp.gaps)
    (line,) = ocp.published
    if reason == "no_future_quote":
        assert line["amount"] == 2100 and line["fallback"] == ["fx_future"]
    else:
        assert line["amount"] == 100 and not line["fallback"]
    rows = [{"position_id": "P0", "book_id": "B1", "quality": "assumed", "secid": None, "pv_rub": 0.0, "fact": fact}]
    payload = risk_payload({"run_id": 1, "business_date": on}, rows, {})
    assert "FX|USD" not in payload["factors"]
    status = {"basis": O.PROVISIONAL_POLICY.published.value, "byCcy": {"USD": {"rate": 90}}, "byBook": [ocp_line(line)]}
    assert fx_ocp(status, payload, rows, published_lines=ocp.published)["by_ccy"]["USD"]["linear"] == line["amount"]


def test_unpriced_mini_is_not_substituted_with_main_contract(ctx):
    def mutate(data):
        data["futures_prices"].append(usd_future(ctx, secid="UMZ6", lot_volume="100", settle_rub_per_unit=None))

    ctx = replace(ctx, market=market_with(ctx, mutate))
    out = positions.plan(ctx, inputs(ctx, future(amount_ccy=200))).direct["P0"]
    assert out.reason == "no_future_quote" and "UMZ6" in out.detail


@pytest.mark.parametrize("days_to_expiry", [-1, 0])
@pytest.mark.parametrize("quote", ["RUB", "CNY"])
def test_expired_futures_have_no_mv_or_ocp(ctx, days_to_expiry, quote):
    expiry = ctx.business_date + timedelta(days=days_to_expiry)
    x = future(value_date=expiry, x_sec_cur=quote, mv_rub=123.0)
    out = positions.plan(ctx, inputs(ctx, x)).direct["P0"]
    assert out.valuation.pv_rub == 0 and not out.valuation.fx_delta
    assert "остатках по счетам" in out.valuation.assumptions[0]
    fact = direct_row(ctx, "B1", "P0", out, x, None)
    assert fact["mv_rub"] == fact["mv"] == 0 and not fact.get("fx_delta")
    assert O.classify(fact, (), BOOKS, ctx.business_date, O.PROVISIONAL_POLICY)[:2] == (None, {})


def test_perpetual_futures_in_the_published_run(ctx, store_url):
    from desk import store
    from desk.api.app import ocp_out
    from desk.publish import desk_run
    from desk.valuation.fallback import fx_rate

    usd, eur = (fx_rate(ctx, c)[0] for c in ("USD", "EUR"))
    inp = inputs(ctx, future(value_date=None, mv_rub=123.0, x_quantity=None, source_instrument_id="USDRUBF"),
                 future(value_date=None, x_sec_cur="USD", ccy="EUR", amount_ccy=-1000, x_quantity=-1),
                 future(value_date=None, amount_ccy=None))
    plan = positions.plan(ctx, inp)
    with store.connect(store_url) as con:
        run_id, _, _ = desk_run.publish(con, ctx, positions.merge(plan, []), prev=None, inputs=inp)
        facts = con.execute("SELECT pv_rub, fact, reason FROM run_position WHERE run_id = %s ORDER BY ord",
                            (run_id,)).fetchall()
        (ocp,) = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'ocp'", (run_id,)).fetchone()
    assert len(facts) == 3
    for mv, fact, reason in facts[:2]:
        assert mv == fact["mv_rub"] == 0 and not reason and fact["maturity"] is None
        assert "бессрочный валютный фьючерс: PV = 0, дельта и ОВП по номиналу (DEC18)" in fact["extra"]["assumptions"]
    a, b, bad = (row[1] for row in facts)
    assert a["fx_delta"] == {"USD": 2000}
    assert b["fx_delta"] == pytest.approx({"EUR": -1000, "USD": 1000 * eur / usd})
    assert bad["mv_rub"] == 0 and bad["notional"] is None and bad["fx_delta"] == {}
    assert all(x["component"] == "futures" and x["basis"] == "nominal" for x in ocp["lines"])
    assert ocp["published"]["by_ccy"]["USD"]["total"] == pytest.approx(2000 + 1000 * eur / usd)
    assert ocp["published"]["by_ccy"]["EUR"]["total"] == -1000
    response = ocp_out(run_id, ocp)
    assert not response.left_out
    assert any(g.code == "no_future_notional" and g.items == ["P2"] for g in response.gaps)

    rows = [{"position_id": f["position_id"], "book_id": f["book_id"], "quality": "assumed", "secid": None,
             "pv_rub": 0.0, "fact": f} for f in (a, b)]
    on = ctx.business_date
    payload = risk_payload({"run_id": run_id, "business_date": on}, rows, {})
    shocked = {r["position_id"]: r["fx"] for r in run_taylor(payload, {"fx": {"EUR": 1, "USD": 2}})["positions"]}
    assert shocked == pytest.approx({"P0": 2000 * usd * 0.02, "P1": 1000 * eur * 0.01})
    status = {"basis": ocp["published"]["basis"], "byCcy": {"USD": {"rate": usd}, "EUR": {"rate": eur}},
              "byBook": response.published_rows}
    projection = fx_ocp(status, payload, rows, published_lines=ocp["published"]["by_book"])
    assert projection["by_ccy"]["EUR"]["linear"] == -1000
    assert projection["by_ccy"]["USD"]["linear"] == pytest.approx(2000 + 1000 * eur / usd)


@pytest.mark.parametrize(("fields", "reason"), [
    ({}, None),
    ({"x_quantity": -2}, "sign_mismatch"),
    ({"amount_ccy": None}, "no_future_notional"),
    ({"x_sec_cur": None}, "no_future_pair"),
    ({"amount_ccy": 0}, None),
])
def test_perpetual_future_replaces_split_report_nominal_only_when_confirmed(ctx, fields, reason):
    x = future(value_date=None, **fields)
    out = positions.plan(ctx, inputs(ctx, x)).direct["P0"]
    on, policy = ctx.business_date, O.PROVISIONAL_POLICY
    rep = {"entity": "E", "book_id": "B1", "ccy": "USD", "ocp_ccy": "100", "_set_id": 1,
           "derivatives_ccy": {"fx_future": "3000"}}
    unit = {"scope": {"entity": "E", "book_id": "B1"}, "set_id": 1, "valid_date": on, "row_count": 1}
    assert out.valuation.pv_rub == 0
    fact = direct_row(ctx, "B1", "P0", out, x, None)
    r = O.compute_ocp([fact], BOOKS, on, report_rows=[rep], report_units=[unit])
    (line,) = r.published
    assert not (line["fallback"] or line["no_pv"] or line["partial_pv"])
    assert method_note(policy.published.value, [ocp_line(line)]) is None
    if reason:
        assert fact["fx_delta"] == {} and fact["extra"]["ocp_unconfirmed"]["reason"] == reason
        assert any(g["code"] == reason for g in r.gaps) and line["amount"] == 100
        if x["amount_ccy"] is None:
            assert fact["notional"] is fact["notional_rub"] is fact["side"] is None
    else:
        assert fact["fx_delta"] == {"USD": x["amount_ccy"]}
        assert line["amount"] == 100 + x["amount_ccy"]
        assert not {"derivatives_unknown_last_flow", "derivatives_partial_pv", "derivatives_nominal_fallback"} & {
            g["code"] for g in r.gaps}


@pytest.mark.parametrize("quote", ["RUB", "CNY"])
def test_perpetual_future_pnl_says_why_variation_margin_is_unknown(ctx, quote):
    x = future(value_date=None, x_sec_cur=quote)
    market = market_with(ctx, lambda data: data.update(futures_prices=[]))
    market.business_date = ctx.business_date - timedelta(days=1)
    prev = replace(ctx, market=market)
    inp = inputs(ctx, x)
    current = positions.plan(ctx, inp).direct
    attach(prev, ctx, current, inp, positions.plan(prev, inputs(prev, x)).direct)
    out = current["P0"]
    assert out.pnl is None and out.no_pnl.reason == "no_perpetual_future_pnl"
    assert "фандинг" in out.no_pnl.detail


@pytest.mark.parametrize(("fields", "code"), [
    ({"amount_ccy": None}, "no_future_notional"),
    ({"x_sec_cur": None}, "no_future_pair"),
    ({"x_quantity": -2}, "sign_mismatch"),
])
def test_perpetual_future_pnl_names_the_unconfirmed_input(ctx, fields, code):
    from desk.pnl.reasons import no_pnl_reason_text

    x = future(value_date=None, **fields)
    inp = inputs(ctx, x)
    current = positions.plan(ctx, inp).direct
    attach(ctx, ctx, current, inp, {})
    out = current["P0"]
    assert out.pnl is None and out.no_pnl.reason == code and out.no_pnl.detail == out.detail
    assert no_pnl_reason_text(code) != code


def test_perpetual_future_without_fx_rate_keeps_the_base_nominal(ctx, monkeypatch):
    monkeypatch.setattr(ctx.market, "fx", lambda c: {})
    x = future(value_date=None, x_sec_cur="RUB", x_quantity=None)
    out = positions.plan(ctx, inputs(ctx, x)).direct["P0"]
    fact = direct_row(ctx, "B1", "P0", out, x, None)
    assert fact["ccy"] == "USD" and fact["mv_rub"] == 0
    assert fact["notional"] == 2000 and fact["notional_rub"] is None
    assert fact["fx_delta"] == {"USD": 2000} and "fx_rate" not in fact["extra"]


def test_carried_split_report_does_not_restore_an_expired_future(ctx):
    on = ctx.business_date
    x = future(value_date=on)
    out = positions.plan(ctx, inputs(ctx, x)).direct["P0"]
    fact = direct_row(ctx, "B1", "P0", out, x, None)
    report = {"entity": "E", "book_id": "B1", "ccy": "USD", "ocp_ccy": "500", "_set_id": 1,
              "derivatives_ccy": {"fx_future": "2000.0000003"}}
    unit = {"scope": {"entity": "E", "book_id": "B1"}, "set_id": 1, "valid_date": on - timedelta(days=1),
            "row_count": 1}
    calculated = O.compute_ocp([fact], BOOKS, on, report_rows=[report], report_units=[unit])
    (line,) = calculated.published
    assert line["amount"] == 500 and line["parts"]["derivatives"] == 0 and not line["fallback"]
    assert "expired_future_report_nominal" not in {g["code"] for g in calculated.gaps}


def test_carried_mixed_report_does_not_restore_the_expired_legs_of_a_cross(ctx):
    on = ctx.business_date
    expired = future(ccy="EUR", x_sec_cur="USD", value_date=on, amount_ccy=-2000)
    live = future(ccy="EUR", x_sec_cur="USD", amount_ccy=1000)
    plan = positions.plan(ctx, inputs(ctx, expired, live))
    facts = [direct_row(ctx, "B1", f"P{i}", plan.direct[f"P{i}"], x, None) for i, x in enumerate((expired, live))]
    report = [{"entity": "E", "book_id": "B1", "ccy": c, "ocp_ccy": "0", "_set_id": 1,
               "derivatives_ccy": {"fx_future": n}} for c, n in [("EUR", "-1000"), ("USD", "1200")]]
    unit = {"scope": {"entity": "E", "book_id": "B1"}, "set_id": 1, "valid_date": on - timedelta(days=1),
            "row_count": 2}
    ocp = O.compute_ocp(facts, BOOKS, on, report_rows=report, report_units=[unit])
    assert {line["ccy"]: line["amount"] for line in ocp.published} == facts[1]["fx_delta"]
    assert all(not line["fallback"] for line in ocp.published)
    gap = next(g for g in ocp.gaps if g["code"] == "expired_future_report_nominal")
    assert gap["items"] == ["книга B1 EUR", "книга B1 USD"]


def test_cross_future_keeps_both_nominal_legs(ctx):
    x = future(ccy="EUR", x_sec_cur="USD")
    out = positions.plan(ctx, inputs(ctx, x)).direct["P0"]
    assert not out.valuation.extra.get("core")
    fact = direct_row(ctx, "B1", "P0", out, x, None)
    comp, amounts, basis, _ = O.classify(fact, (), BOOKS, ctx.business_date, O.PROVISIONAL_POLICY)
    assert comp == "futures" and basis == "notional"
    rate = ctx.market.fx("EUR")["moex_19"] / ctx.market.fx("USD")["moex_19"]
    assert amounts == pytest.approx({"EUR": 2000.0, "USD": -2000 * rate})


def test_variation_margin_uses_previous_signed_nominal_and_core_spot_delta(ctx):
    prev = replace(ctx, market=priced_market(ctx, "eod_final"))
    before = inputs(prev, future(amount_ccy=-2000.0, x_quantity=-2))
    prior = positions.plan(prev, before, second_order=False).direct
    old = prior["P0"].valuation
    market = market_with(ctx, lambda data: None)
    market.business_date = ctx.business_date + timedelta(days=1)
    spot = replace(ctx.fx.spots["USD"], rub_per_unit=old.extra["fx_rates"]["USD"] + 0.5)
    cur = replace(ctx, market=market, fx=replace(ctx.fx, spots={**ctx.fx.spots, "USD": spot}))
    after = inputs(cur, future(amount_ccy=-3000.0, x_quantity=-3))
    # Reuse core's valuation shape and feed new marks and spot for the PnL-only leg of the test.
    extra = {**old.extra, "futures": {**old.extra["futures"], "contracts": -3,
                                       "futures_price_rub_per_unit": 86.75},
             "ocp_amount": {"USD": -3000.0}, "fx_rates": {"USD": old.extra["fx_rates"]["USD"] + 0.5}}
    current = {"P0": replace(prior["P0"], valuation=replace(old, extra=extra))}
    attach(prev, cur, current, after, prior)
    pnl = current["P0"].pnl
    assert pnl.method == FX_FUTURE_METHOD and pnl.total == -3000.0
    assert pnl.fx_gross == pytest.approx(old.fx_delta["USD"] * 0.5)
    assert pnl.other == pytest.approx(pnl.total - pnl.fx_gross)
    assert pnl.rates == pnl.carry == pnl.rolldown == 0 and pnl.explained == pytest.approx(pnl.total)
    assert pnl.as_tdm()["fx_gross"] == pnl.fx_gross and pnl.extra()["total"] == pnl.total
    fact = direct_row(cur, "B1", "P0", current["P0"], after.extra["P0"], None)
    assert fact["extra"]["pnl"]["total"] == pnl.total
    assert fact["extra"]["futures"]["futures_price_rub_per_unit"] == 86.75

    without_price = replace(old, extra={**old.extra, "futures": {"contract_id": "SiZ6"}})
    current["P0"].pnl = None
    attach(prev, cur, current, after, {"P0": replace(prior["P0"], valuation=without_price)})
    assert current["P0"].pnl is None and current["P0"].no_pnl.reason == "no_prev_future_price"
    attach(prev, cur, current, after, {})
    assert current["P0"].no_pnl.reason == "no_prev_future_position"
    unmatched = positions.plan(prev, inputs(prev, future(x_quantity=None))).direct
    attach(prev, cur, current, after, unmatched)
    assert current["P0"].no_pnl.reason == "prev_future_not_matched"


def test_cross_future_expires_with_its_existing_pnl_by_fx_rate(ctx):
    market = market_with(ctx, lambda data: None)
    market.business_date = ctx.business_date - timedelta(days=1)
    eur = ctx.market.fx("EUR")["moex_19"]
    market.fx = lambda ccy: {**ctx.market.fx(ccy), "moex_19": eur - 1} if ccy == "EUR" else ctx.market.fx(ccy)
    prev = replace(ctx, market=market)
    x = future(ccy="EUR", x_sec_cur="USD", value_date=ctx.business_date)
    after = inputs(ctx, x)
    current = positions.plan(ctx, after).direct
    attach(prev, ctx, current, after, positions.plan(prev, inputs(prev, x)).direct)
    out = current["P0"]
    assert out.valuation.pv_rub == 0 and out.no_pnl is None and out.pnl.total == 2000


def test_expiry_day_uses_the_final_mark_without_live_risk(ctx):
    expiry = ctx.fx.spots["USD"].value_date
    assert expiry == ctx.business_date + timedelta(days=1)
    before = usd_future(ctx, last_del_date=expiry.isoformat(), settle_rub_per_unit="85.25")
    prev = replace(ctx, market=market_with(ctx, lambda d: d.update(futures_prices=[before]), "eod_final"))
    x = future(value_date=expiry)
    prior = positions.plan(prev, inputs(prev, x), second_order=False).direct
    assert prior["P0"].core_not_valued[0] == "future_spot_value_date"
    assert prior["P0"].valuation.extra["futures"]["futures_price_rub_per_unit"] == 85.25

    def mutate(data):
        data["futures_prices"] = [{**before, "trade_date": expiry.isoformat(), "settle_rub_per_unit": "86.5"}]

    market = market_with(ctx, mutate, "eod_final")
    market.business_date = expiry
    cur = replace(ctx, market=market)
    after = inputs(cur, x)
    current = positions.plan(cur, after).direct
    attach(prev, cur, current, after, prior)
    out = current["P0"]
    assert out.valuation.pv_rub == 0 and not out.valuation.fx_delta
    assert out.pnl.total == 2000 * (86.5 - 85.25)
    assert out.pnl.other == out.pnl.total and "FX-дельты" in out.pnl.other_reason["detail"]


def test_ocp_publishes_nominal_as_valued_and_scenario_uses_core_delta(ctx):
    ctx = replace(ctx, market=priced_market(ctx, "eod_final"))
    x = future()
    out = positions.plan(ctx, inputs(ctx, x)).direct["P0"]
    fact = direct_row(ctx, "B1", "P0", out, x, None)
    on, policy = ctx.business_date, O.PROVISIONAL_POLICY
    report = {"entity": "E", "book_id": "B1", "ccy": "USD", "ocp_ccy": "0", "_set_id": 1,
              "derivatives_ccy": {"fx_future": "2000"}}
    unit = {"scope": {"entity": "E", "book_id": "B1"}, "set_id": 1, "valid_date": on, "row_count": 1}
    calculated = O.compute_ocp([fact], BOOKS, on, report_rows=[report], report_units=[unit])
    (derivative,) = O.derivative_lines([fact], BOOKS, {}, on)
    assert derivative.basis == "nominal" and derivative.amount == 2000.0 and derivative.valued_by_core
    for basis in O.AmountBasis:
        assert O.classify(fact, (), BOOKS, on, replace(policy, amount_basis=basis))[:3] == (
            "futures", {"USD": 2000.0}, "nominal")
    (line,) = calculated.published
    assert line["parts"]["derivatives"] == 2000.0 and line["amount"] == 2000.0
    assert not line["no_pv"] and not line["fallback"] and not line["partial_pv"]
    assert line["contributions"]["derivatives"]["items"][0]["basis"] == "nominal"
    line = ocp_line(line)
    assert method_note(policy.published.value, [line]) is None
    (check,) = calculated.derivatives_check
    assert check["diff"] == 0 and not check["no_pv"] and not check["without_pv"]

    rows = [{"position_id": "P0", "book_id": "B1", "quality": "exact", "secid": None, "pv_rub": 0.0, "fact": fact}]
    payload = risk_payload({"run_id": 1, "business_date": on}, rows, {})
    rate = ctx.fx.spots["USD"].rub_per_unit - 0.5  # distinct OCP rate, still use core spot for a percentage shock
    status = {"basis": policy.published.value, "byCcy": {"USD": {"rate": rate}}, "byBook": [line]}
    projected = fx_ocp(status, payload, rows, published_lines=calculated.published)
    scope = projected["by_ccy"]["USD"]
    assert scope["amount"] == 2000.0
    shock = run_taylor(payload, {"fx": {"USD": 1}})["positions"][0]["fx"]
    assert scope["linear"] * rate / 100 == pytest.approx(shock)
    assert shock == pytest.approx(2000.0 * 85.25 / 100)
