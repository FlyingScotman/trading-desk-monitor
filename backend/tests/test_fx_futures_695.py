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
from datetime import date
from pathlib import Path

import pytest

from desk.curves import fx as fxcurves
from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.marketdata import adapter
from desk.marketdata.bundle import Snapshot, SnapshotDir
from desk.orchestrator import market_rates, prepare

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
    assert market.future("UMZ6")["asset_code"] == "UM"  # a position's ticker lookup still finds the mini contract
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
