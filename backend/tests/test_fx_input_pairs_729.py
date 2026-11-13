"""FX-implied curves from the hub's synchronous input groups (contract 1.9 ``fx_input_pairs``, TDM#729).

On the contract 1.4 golden snapshot (``fixtures/mdhub_v1_4``) with synthetic groups in the published shape (invented
prices; no real figures): the group of a currency is one spot observation and, in the same minute, the best bid and
offer of four quarterly futures. Only their points (``mid(future) - mid(spot)``) enter the curve, added to the run's own
spot; the exchange's swap points stay. With the group taken no future of ``futures_prices`` enters (TDM#733): the curve
ends at the group's last point and goes flat-forward beyond it; every future left out has its reason in the lineage.
Without a taken group (no dataset, a group not usable) the curve is exactly as it was, the far futures included.
"""

from __future__ import annotations

import copy
import math
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from rub_fixed_income import InvalidTerms

from desk.curves import fx as fxcurves
from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.marketdata import adapter
from desk.marketdata.bundle import Snapshot, SnapshotDir
from desk.orchestrator import market_rates, prepare

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1_4"
DATE = "2026-10-15"
Z6, H7, M7, U7, Z7 = date(2026, 12, 17), date(2027, 3, 18), date(2027, 6, 17), date(2027, 9, 16), date(2027, 12, 16)
H8 = date(2028, 3, 16)
SPOT_TS = "2026-10-15T15:59:00Z"  # 18:59 Moscow time
GOLDEN_SWAPS = ["anchor:TOD/TOM", "USDRUB_TOM1D:TOM/SPT"]
SPOT_MID = Decimal("83.75")
# the synthetic futures: (ticker, last delivery date, bid, offer); the mids give the points 1.46, 3.17, 4.88, 6.58
NODES = [("SiZ6", Z6, "85.20", "85.22"), ("SiH7", H7, "86.90", "86.94"), ("SiM7", M7, "88.60", "88.66"),
         ("SiU7", U7, "90.30", "90.36")]
POINTS = {"SiZ6": 1.46, "SiH7": 3.17, "SiM7": 4.88, "SiU7": 6.58}
LEFT_OUT = "future not after the last fx_input_pairs point"
PAST_U7 = "future past the last fx_input_pairs point (2027-09-16): the curve ends there, flat-forward beyond"
# CNY: the spot is a LAST of 11.95 (the group's, not the run's 12.5141); the points are 0.16, 0.66, 1.16, 1.66
CNY_NODES = [("CRZ6", Z6, "12.10", "12.12"), ("CRH7", H7, "12.60", "12.62"), ("CRM7", M7, "13.10", "13.12"),
             ("CRU7", U7, "13.60", "13.62")]
CNY_POINTS = {"CRZ6": 0.16, "CRH7": 0.66, "CRM7": 1.16, "CRU7": 1.66}


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


# --- the synthetic hub rows -----------------------------------------------------------------------------------------


def node(position, secid, delivery, bid, offer, **kw):
    """A ``fx_input_node`` of the published field set: a clean one (no reasons, 20 s before the spot clock)."""
    return {"position": position, "secid": secid, "asset_code": secid[:2], "first_trade_date": "2025-12-18",
            "last_trade_date": delivery.isoformat(), "last_del_date": delivery.isoformat(), "underlying_value_date": None,
            "definition_source": "moex_apim", "definition_run_id": 1, "definition_capture_sha256": "d" * 64,
            "lot_volume": "1000", "min_step": "1", "step_price": "1", "bid_rub_per_unit": bid,
            "offer_rub_per_unit": offer, "update_time": "18:58:40", "update_ts": "2026-10-15T15:58:40Z",
            "source_ts": "2026-10-15T15:58:50Z", "captured_at": "2026-10-15T15:59:05Z", "capture_sha256": "c" * 64,
            "update_skew_seconds": "20", "source_skew_seconds": "10", "reasons": [], **kw}


def group(ccy="USD", nodes=NODES, spot=None, **kw):
    """A ``fx_input_group``: NTPro indicative USD/EUR spot 83.70 / 83.80 at 18:59 Moscow time (mid 83.75), or a CNY LAST."""
    if ccy == "CNY":
        base = {"source": "moex_iss", "instrument": "CNYRUB_TOM", "quote_kind": "last", "bid": None, "offer": None,
                "last": "11.9500", "settle_date": "2026-10-16", "clock_semantics": "moex_last_trade_time",
                "source_variant": "iss_public"}
    else:
        base = {"source": "ntpro_public", "instrument": f"{ccy}/RUB_TOM", "quote_kind": "indicative_bid_offer",
                "bid": "83.70", "offer": "83.80", "last": None, "settle_date": None,
                "clock_semantics": "ntpro_creation_datetime", "source_variant": "public_indicative"}
    spot_row = {"currency": ccy, "quote_ts": SPOT_TS, "update_time": None, "update_ts": None, "source_ts": None,
                "trade_date": None, "trade_time": None, "reasons": ["spot_value_date_unknown"],
                "received_at": "2026-10-15T15:59:01Z", "captured_at": "2026-10-15T15:59:01Z",
                "capture_sha256": "a" * 64, **base, **(spot or {})}
    return {"run_id": 77, "currency": ccy, "known_at": "2026-10-15T16:10:00Z", "future_source": "moex_apim",
            "future_source_variant": "apim_marketdata", "spot": spot_row,
            "nodes": [node(i, *n) for i, n in enumerate(nodes, 1)],
            "coverage": {"expected_nodes": 4, "observed_nodes": 4, "spot_observed": True, "all_four_observed": True},
            "content_sha256": "b" * 64, "spot_age_seconds_at_cut": "60", **kw}


def dataset(rows, stale=()):
    return {"name": "fx_input_pairs", "rows": len(rows), "coverage": {"groups": len(rows), "complete_four": len(rows)},
            "stale": bool(stale), "stale_reasons": list(stale), "sha256": "e" * 64}


def view(ctx, rows=None, cut_type=None, stale=(), mutate=None, doc_mutate=None):
    """The golden market view as a cut of ``cut_type`` (the golden's end-of-day one by default) with the dataset
    ``fx_input_pairs`` of ``rows`` (None: the snapshot has no such dataset, as a hub before 1.9), ``mutate(data)`` over
    the golden's rows (not re-hashed: nothing reads the id)."""
    snap = ctx.market.snapshot
    doc = copy.deepcopy(snap.doc)
    if rows is not None:
        doc["data"]["fx_input_pairs"] = copy.deepcopy(rows)
        doc["datasets"].append(dataset(rows, stale))
    if mutate:
        mutate(doc["data"])
    if doc_mutate:
        doc_mutate(doc)
    cut_type = cut_type or snap.cut_type
    return adapter.MarketView(Snapshot(snap.id, snap.kind, cut_type, snap.business_date, snap.static_snapshot_id,
                                       {**doc, "cut_type": cut_type}))


def curves_of(ctx, market, **options):
    base = market.core_market(ctx.static)
    ruonia = next(e for e in ctx.core_market.curves if e.curve_id == RUONIA_OIS_CURVE_ID)
    core = replace(base, curves=(*base.curves, ruonia))
    out, fx = fxcurves.with_fx(market, ctx.static, core, market_rates(market), replace(fxcurves.FxOptions(), **options))
    return out, fx, fx.lineage["curves"]


def forward(ctx, fx, ccy, day):
    """The curve's forward on ``day``: the run's spot carried by the foreign and the RUB curves (covered parity)."""
    spot = fx.spots[ccy]
    c = fx.curves[f"{ccy} FX"].curve
    rub = next(e.curve for e in ctx.core_market.curves if e.curve_id == RUONIA_OIS_CURVE_ID)
    return spot.rub_per_unit * (c.discount_factor(day) / c.discount_factor(spot.value_date)) / (
        rub.discount_factor(day) / rub.discount_factor(spot.value_date))


def usd_future(ctx, **kw):
    row = next(r for r in ctx.market.futures_prices if r["secid"] == "SiZ6")
    return {**copy.deepcopy(row), **kw}


def intraday(secid, delivery, bid, offer, ccy="USD", **kw):
    return {"asset_code": secid[:2], "base_ccy": ccy, "quote_ccy": "RUB", "is_fx": True, "contract_type": "dated",
            "secid": secid, "last_del_date": delivery.isoformat(), "trade_date": DATE, "open_position": "1000",
            "vol_today": "10", "bid": None, "offer": None, "last": None, "bid_rub_per_unit": bid,
            "offer_rub_per_unit": offer, "last_rub_per_unit": None, "source_ts": "2026-10-15T15:58:50+00:00",
            "ts": "2026-10-15T15:58:51+00:00", "labels": [], **kw}


def intraday_cut(d):
    """An intraday cut: no results, no exchange swaps; the quotes of the four quarterly contracts and of a farther one."""
    d["futures_prices"], d["fx_swaps"] = [], []
    d["futures_prices_intraday"] = [intraday(s, dt, b, o) for s, dt, b, o in NODES] + [
        intraday("SiZ7", Z7, "92.00", "92.10")]


# --- a full USD group at the end of the day -------------------------------------------------------------------------


def test_the_golden_has_no_dataset_and_its_curve_is_as_before(ctx):
    assert ctx.market.has_fx_input_pairs is False and ctx.market.fx_input_pairs == []
    assert ctx.market.fx_input_group("USD") == adapter.FxInputPick(None, adapter.NO_FX_INPUT_DATASET)
    lin = ctx.fx.lineage["curves"]["USD FX"]
    assert lin["swaps"] == GOLDEN_SWAPS and lin["futures"] == ["SiZ6"] and lin["dropped"] == {}
    assert "fx_input_group" not in lin and "points_source" not in lin  # a hub before 1.9: nothing added


def test_a_full_usd_group_gives_the_points_to_the_run_spot(ctx):
    def more_futures(d):  # the exchange's results: the Dec 2026 contract (a group's date), Mar 2027, and a farther one
        # (TDM#733: the farther one is not taken, the curve ends at the group's last point)
        d["futures_prices"] += [usd_future(ctx, secid="SiH7", last_del_date=H7.isoformat(), settle_rub_per_unit="86.95"),
                                usd_future(ctx, secid="SiZ7", last_del_date=Z7.isoformat(), settle_rub_per_unit="92.05")]
    market = view(ctx, [group()], mutate=more_futures)
    _, fx, lin = curves_of(ctx, market)
    usd = lin["USD FX"]
    assert usd["status"] == "ok" and usd["points_source"] == "fx_input_pairs"
    # the exchange's TOM/SPT swap stays; the group's four points follow it; the nearer futures do not repeat them
    assert usd["swaps"] == [*GOLDEN_SWAPS, *(f"fx_input_pairs:{n[0]}" for n in NODES)]
    assert usd["futures"] == [] and "futures_source" not in usd  # none of the results' futures enters with a group
    assert usd["dropped"] == {"SiZ6": LEFT_OUT, "SiH7": LEFT_OUT, "SiZ7": PAST_U7}
    assert usd["anchor"].startswith("assumed: no TOD/TOM swap")
    spot = fx.spots["USD"].rub_per_unit
    assert spot == pytest.approx(83.76)  # the run's rate is the golden's: the group's 83.75 is not used as a level
    for secid, delivery, _, _ in NODES:
        assert forward(ctx, fx, "USD", delivery) == pytest.approx(spot + POINTS[secid], rel=1e-9)
    assert usd["last_node"] == U7.isoformat()  # the curve ends at the group's last point
    assert abs(forward(ctx, fx, "USD", Z7) - 92.05) > 0.05  # the results' price of the farther contract is not on it


def test_the_lineage_names_the_group(ctx):
    _, _, lin = curves_of(ctx, view(ctx, [group()]))
    assert lin["USD FX"]["fx_input_group"] == {
        "status": "used", "run_id": 77, "content_sha256": "b" * 64, "known_at": "2026-10-15T16:10:00+00:00",
        "spot_source": "ntpro_public", "spot_quote_ts": "2026-10-15T15:59:00+00:00", "spot_mid": "83.75",
        "spot_age_seconds_at_cut": "60", "nodes_used": ["SiZ6", "SiH7", "SiM7", "SiU7"], "nodes_dropped": {}}


def test_the_future_of_a_group_date_is_not_taken_twice(ctx):
    """The results' SiZ6 (a date of the group) is out, the group's point stays: one quote per date reaches core."""
    _, fx, lin = curves_of(ctx, view(ctx, [group()]))
    usd = lin["USD FX"]
    assert usd["futures"] == [] and usd["dropped"] == {"SiZ6": "future not after the last fx_input_pairs point"}
    assert forward(ctx, fx, "USD", Z6) == pytest.approx(83.76 + 1.46, rel=1e-9)  # not the settlement price 85.219


def test_a_eur_group_builds_the_curve_of_a_currency_without_swaps_and_futures(ctx):
    assert ctx.fx.lineage["curves"]["EUR FX"]["status"] == "missing"  # the golden has neither for EUR
    _, fx, lin = curves_of(ctx, view(ctx, [group("EUR")]))
    eur = lin["EUR FX"]
    assert eur["status"] == "ok" and eur["swaps"] == ["anchor:TOD/TOM", *(f"fx_input_pairs:{n[0]}" for n in NODES)]
    assert eur["fx_input_group"]["spot_source"] == "ntpro_public" and eur["futures"] == []
    spot = fx.spots["EUR"].rub_per_unit
    assert forward(ctx, fx, "EUR", H7) == pytest.approx(spot + 3.17, rel=1e-9)


# --- past the group: no futures, the curve ends at the group's last point (TDM#733) ----------------------------------


def results(ctx, *rows):
    """``mutate`` for ``view``: the exchange's results of the USD contracts ``rows`` (ticker, delivery, settlement price)."""
    def add(d):
        d["futures_prices"] += [usd_future(ctx, secid=s, last_del_date=dt.isoformat(), settle_rub_per_unit=p)
                                for s, dt, p in rows]
    return add


FAR_RESULTS = (("SiH7", H7, "86.95"), ("SiM7", M7, "88.65"), ("SiU7", U7, "90.35"), ("SiZ7", Z7, "92.05"),
               ("SiH8", H8, "93.70"))
"""The results' prices of the four contracts of the group's dates and of two farther ones."""


def log_rate(curve, a, b):
    """The curve's forward (continuously compounded, per day) between two dates, from its discount factors."""
    return math.log(curve.discount_factor(a) / curve.discount_factor(b)) / (b - a).days


def test_past_the_last_point_of_the_group_the_curve_goes_flat_forward(ctx):
    _, fx, lin = curves_of(ctx, view(ctx, [group()], mutate=results(ctx, *FAR_RESULTS)))
    usd, curve = lin["USD FX"], fx.curves["USD FX"].curve
    # no node past the group's last date; the curve ends there
    assert curve.nodes[-1][0] == U7 and all(d <= U7 for d, _ in curve.nodes) and usd["last_node"] == U7.isoformat()
    assert usd["swaps"] == [*GOLDEN_SWAPS, *(f"fx_input_pairs:{n[0]}" for n in NODES)] and usd["futures"] == []
    # the forward beyond is the last segment's, whatever the span (flat-forward from the last two nodes)
    last = log_rate(curve, M7, U7)
    assert last != pytest.approx(log_rate(curve, H7, M7), rel=1e-3)  # the segments differ: the check is not trivial
    for a, b in [(U7, Z7), (Z7, H8), (U7, H8), (U7, date(2030, 1, 1))]:
        assert log_rate(curve, a, b) == pytest.approx(last, rel=1e-9)
    # the far contracts' settlement prices are not on the curve
    assert abs(forward(ctx, fx, "USD", Z7) - 92.05) > 0.05 and abs(forward(ctx, fx, "USD", H8) - 93.70) > 0.05
    # the curve's lineage names the rule and every future left out has its reason
    assert usd["extrapolation"] == (f"flat-forward beyond {U7.isoformat()} (the forward between the last two nodes holds): "
                                    "futures past the last fx_input_pairs point are not used (TDM#733)")
    assert usd["dropped"] == {"SiZ6": LEFT_OUT, "SiH7": LEFT_OUT, "SiM7": LEFT_OUT, "SiU7": LEFT_OUT,
                              "SiZ7": PAST_U7, "SiH8": PAST_U7}
    assert "futures_source" not in usd and usd["fx_input_group"]["status"] == "used"


def test_a_group_that_ends_earlier_ends_the_curve_earlier(ctx):
    """The fourth future of the group is not usable: the curve ends at the third; the results' price of the fourth
    contract is not used in its place (a settlement price beside synchronous points is what the rule leaves out)."""
    g = with_node(4, bid_rub_per_unit="88.0", offer_rub_per_unit="89.0")  # a spread of 1.1%
    _, fx, lin = curves_of(ctx, view(ctx, [g], mutate=results(ctx, *FAR_RESULTS)))
    usd, curve = lin["USD FX"], fx.curves["USD FX"].curve
    assert usd["fx_input_group"]["nodes_dropped"] == {"SiU7": "spread 1.13% above 0.50%"}
    assert curve.nodes[-1][0] == M7 and usd["last_node"] == M7.isoformat() and usd["futures"] == []
    past_m7 = "future past the last fx_input_pairs point (2027-06-17): the curve ends there, flat-forward beyond"
    assert usd["dropped"] == {"SiZ6": LEFT_OUT, "SiH7": LEFT_OUT, "SiM7": LEFT_OUT, "SiU7": past_m7, "SiZ7": past_m7,
                              "SiH8": past_m7}
    assert log_rate(curve, M7, Z7) == pytest.approx(log_rate(curve, H7, M7), rel=1e-9)
    assert usd["extrapolation"].startswith(f"flat-forward beyond {M7.isoformat()} ")


@pytest.mark.parametrize("ccy, nodes, far", [
    ("USD", NODES, [("SiZ7", Z7, "92.00", "92.10"), ("SiH8", H8, "93.60", "93.80")]),
    ("EUR", NODES, [("EuZ7", Z7, "92.00", "92.10")]),
    ("CNY", CNY_NODES, [("CRZ7", Z7, "14.10", "14.12")])])
def test_every_currency_with_a_group_ends_its_curve_at_the_groups_last_point(ctx, ccy, nodes, far):
    def cut(d):
        intraday_cut(d)
        d["futures_prices_intraday"] = [intraday(s, dt, b, o, ccy) for s, dt, b, o in (*nodes, *far)]
    market = view(ctx, [group(ccy, nodes=nodes)], "intraday", mutate=cut)
    assert [q["secid"] for q in market.fx_future_quotes(ccy).rows] == [n[0] for n in (*nodes, *far)]
    _, fx, lin = curves_of(ctx, market)
    info, curve = lin[f"{ccy} FX"], fx.curves[f"{ccy} FX"].curve
    assert info["status"] == "ok" and info["points_source"] == "fx_input_pairs" and info["futures"] == []
    assert curve.nodes[-1][0] == U7 and info["last_node"] == U7.isoformat()
    assert {s: info["dropped"][s] for s, *_ in far} == {s: PAST_U7 for s, *_ in far}
    assert log_rate(curve, U7, Z7) == pytest.approx(log_rate(curve, M7, U7), rel=1e-9)


def test_a_group_that_core_cannot_build_leaves_the_far_futures_in_the_curve(ctx):
    """The fallback to the curve without the group's points is the curve as before: the far futures stay."""
    g = group(spot={"bid": "500.0", "offer": "500.2"})
    _, fx, lin = curves_of(ctx, view(ctx, [g], mutate=results(ctx, *FAR_RESULTS)))
    usd = lin["USD FX"]
    assert usd["fx_input_group"]["status"] == "not_used" and "points_source" not in usd and "extrapolation" not in usd
    assert usd["futures"] == ["SiZ6", "SiH7", "SiM7", "SiU7", "SiZ7", "SiH8"] and usd["dropped"] == {}
    assert fx.curves["USD FX"].curve.nodes[-1][0] == H8
    assert forward(ctx, fx, "USD", H8) == pytest.approx(93.70, rel=1e-9)


# --- without a group: the curve exactly as before, the far futures included ------------------------------------------


NO_GROUP_USD_NODES = [  # the curve of the snapshot without the dataset and the results of FAR_RESULTS (but SiH8), before TDM#733
    (date(2026, 10, 16), 1.0), (date(2026, 10, 19), 0.9993449073089175), (Z6, 0.9937025733253848),
    (H7, 0.9799070520762855), (M7, 0.9662602018156633), (U7, 0.9530085851762622), (Z7, 0.9387197773266288)]


def curve_and_lineage(ctx, market, ccy="USD"):
    """The curve's nodes (date, discount factor) and its lineage without ``fx_input_group``."""
    _, fx, lin = curves_of(ctx, market)
    return fx.curves[f"{ccy} FX"].curve.nodes, {k: v for k, v in lin[f"{ccy} FX"].items() if k != "fx_input_group"}


def test_a_snapshot_without_the_dataset_keeps_the_far_futures_and_its_numbers(ctx):
    reference = results(ctx, *FAR_RESULTS[:4])
    nodes, usd = curve_and_lineage(ctx, view(ctx, mutate=reference))
    assert [d for d, _ in nodes] == [d for d, _ in NO_GROUP_USD_NODES]
    assert [v for _, v in nodes] == pytest.approx([v for _, v in NO_GROUP_USD_NODES], rel=1e-12, abs=0)
    assert usd["futures"] == ["SiZ6", "SiH7", "SiM7", "SiU7", "SiZ7"] and usd["last_node"] == Z7.isoformat()
    assert usd["dropped"] == {} and "extrapolation" not in usd and "points_source" not in usd
    _, fx, _ = curves_of(ctx, view(ctx, mutate=reference))
    assert forward(ctx, fx, "USD", Z7) == pytest.approx(92.05, rel=1e-9)  # the settlement price of the farthest contract


@pytest.mark.parametrize("rows", [
    [],                                           # the dataset without a group of USD
    [group(spot={"bid": None})],                  # a spot that is not usable
    [group(), group(run_id=78)],                  # two groups of the currency
], ids=["no group", "unusable spot", "two groups"])
def test_a_group_that_is_not_taken_leaves_the_curve_bit_for_bit_as_without_the_dataset(ctx, rows):
    expected = curve_and_lineage(ctx, view(ctx, mutate=results(ctx, *FAR_RESULTS[:4])))
    got = curve_and_lineage(ctx, view(ctx, rows, mutate=results(ctx, *FAR_RESULTS[:4])))
    assert got == expected  # nodes (date, discount factor) and the lineage, equal to the last bit
    assert got[1]["futures"][-1] == "SiZ7"


def test_an_intraday_cut_with_an_old_spot_leaves_the_far_futures_in(ctx):
    expected = curve_and_lineage(ctx, view(ctx, cut_type="intraday", mutate=intraday_cut))
    got = curve_and_lineage(ctx, view(ctx, [group(spot_age_seconds_at_cut="3600")], "intraday", mutate=intraday_cut))
    assert got == expected and got[1]["futures"] == ["SiZ6", "SiH7", "SiM7", "SiU7", "SiZ7"]


def test_a_group_with_no_usable_future_leaves_the_far_futures_in(ctx):
    g = group()
    for n in g["nodes"]:
        n["reasons"] = ["future_capture_missing"]
    expected = curve_and_lineage(ctx, view(ctx, mutate=results(ctx, *FAR_RESULTS[:4])))
    assert curve_and_lineage(ctx, view(ctx, [g], mutate=results(ctx, *FAR_RESULTS[:4]))) == expected


def test_cny_with_exchange_swap_points_keeps_its_far_futures(ctx):
    """The group is not taken for CNY while the cut has the exchange's swap points: its futures are as before."""
    def cny_results(d):
        row = next(r for r in ctx.market.futures_prices if r["secid"] == "CRZ6")
        d["futures_prices"] += [{**copy.deepcopy(row), "secid": s, "last_del_date": dt.isoformat(),
                                 "settle_rub_per_unit": p} for s, dt, p in (("CRH7", H7, "12.70"), ("CRZ7", Z7, "13.40"))]
    expected = curve_and_lineage(ctx, view(ctx, mutate=cny_results), "CNY")
    got = curve_and_lineage(ctx, view(ctx, [group("CNY", nodes=CNY_NODES)], mutate=cny_results), "CNY")
    assert got == expected
    assert got[1]["futures"] == ["CRZ6", "CRH7", "CRZ7"] and "extrapolation" not in got[1] and got[0][-1][0] == Z7


def test_the_setting_off_with_a_group_and_far_futures_takes_neither(ctx):
    _, fx, lin = curves_of(ctx, view(ctx, [group()], mutate=results(ctx, *FAR_RESULTS)), implied_futures=False)
    usd = lin["USD FX"]
    assert usd["fx_input_group"] == {"status": "not_used", "reason": "setting fx_implied_futures is off"}
    assert usd["swaps"] == GOLDEN_SWAPS and usd["futures"] == [] and usd["dropped"] == {}
    assert fx.curves["USD FX"].curve.nodes[-1][0] == date(2026, 10, 19) and "extrapolation" not in usd


# --- the futures of a group that are not taken ----------------------------------------------------------------------


def with_node(position, **kw):
    """The group with the ``position``-th future's fields replaced."""
    g = group()
    g["nodes"][position - 1].update(kw)
    return g


@pytest.mark.parametrize("kw, reason", [
    ({"reasons": ["future_clock_unknown", "future_quote_missing"]}, "hub: future_clock_unknown, future_quote_missing"),
    ({"bid_rub_per_unit": "88.0", "offer_rub_per_unit": "89.0"}, "spread 1.13% above 0.50%"),
    ({"update_ts": "2026-10-15T15:59:30Z", "update_skew_seconds": "-30"}, "updated after the spot quote"),
    ({"update_ts": "2026-10-15T15:52:20Z", "update_skew_seconds": "400"}, "update skew 400 s outside 0..300 s"),
    ({"update_skew_seconds": None}, "no update skew"),
    ({"update_ts": None, "update_skew_seconds": None}, "no update time"),
    ({"bid_rub_per_unit": None}, "no positive bid below the offer"),
    ({"bid_rub_per_unit": "88.66", "offer_rub_per_unit": "88.66"}, "no positive bid below the offer"),
    ({"bid_rub_per_unit": "0", "offer_rub_per_unit": "88.66"}, "no positive bid below the offer"),
    ({"secid": None}, "no ticker or delivery date"),
    ({"last_del_date": None}, "no ticker or delivery date"),
    ({"last_del_date": "2026-10-16"}, "delivery 2026-10-16 not after the spot date 2026-10-16"),
])
def test_a_future_that_is_not_usable_is_dropped_with_the_reason(ctx, kw, reason):
    _, fx, lin = curves_of(ctx, view(ctx, [with_node(3, **kw)]))  # the third: SiM7
    usd = lin["USD FX"]
    name = "position 3" if "secid" in kw and kw["secid"] is None else "SiM7"  # a future without a ticker is named by place
    assert usd["fx_input_group"]["nodes_dropped"] == {name: reason}
    assert usd["fx_input_group"]["nodes_used"] == ["SiZ6", "SiH7", "SiU7"]
    assert usd["swaps"] == [*GOLDEN_SWAPS, "fx_input_pairs:SiZ6", "fx_input_pairs:SiH7", "fx_input_pairs:SiU7"]
    spot = fx.spots["USD"].rub_per_unit
    assert forward(ctx, fx, "USD", U7) == pytest.approx(spot + 6.58, rel=1e-9)


def test_a_future_on_the_date_of_an_exchange_swap_leaves_the_exchange_quote(ctx):
    """The TOM/SPT swap fixes 2026-10-19; a group future of that date is dropped, the swap stays."""
    _, _, lin = curves_of(ctx, view(ctx, [with_node(1, last_del_date="2026-10-19", last_trade_date="2026-10-19")]))
    usd = lin["USD FX"]
    assert usd["fx_input_group"]["nodes_dropped"] == {"SiZ6": "date 2026-10-19 already quoted by USDRUB_TOM1D:TOM/SPT"}
    assert "USDRUB_TOM1D:TOM/SPT" in usd["swaps"] and "fx_input_pairs:SiZ6" not in usd["swaps"]
    assert usd["status"] == "ok"


def test_the_points_are_the_mid_of_the_future_less_the_mid_of_the_spot_whatever_the_sides(ctx):
    """A wide but allowed bid and offer: the half-sum of both sides, not one of them."""
    g = with_node(1, bid_rub_per_unit="85.00", offer_rub_per_unit="85.40")  # spread 0.47%, mid 85.20
    _, fx, lin = curves_of(ctx, view(ctx, [g]))
    assert lin["USD FX"]["fx_input_group"]["nodes_dropped"] == {}
    assert forward(ctx, fx, "USD", Z6) == pytest.approx(83.76 + (85.20 - 83.75), rel=1e-9)


def test_no_usable_future_is_no_group(ctx):
    g = group(nodes=NODES)
    for n in g["nodes"]:
        n["reasons"] = ["future_capture_missing"]
    _, _, lin = curves_of(ctx, view(ctx, [g]))
    usd = lin["USD FX"]
    assert usd["swaps"] == GOLDEN_SWAPS and usd["futures"] == ["SiZ6"] and "points_source" not in usd  # as before
    assert usd["fx_input_group"]["status"] == "not_used" and usd["fx_input_group"]["reason"] == "no usable future in the group"
    assert usd["fx_input_group"]["nodes_dropped"] == {s: "hub: future_capture_missing" for s, *_ in NODES}


# --- a spot that is not usable --------------------------------------------------------------------------------------


@pytest.mark.parametrize("spot, reason", [
    ({"bid": None}, "no spot bid and offer (a positive bid, the offer not below it)"),
    ({"offer": None}, "no spot bid and offer (a positive bid, the offer not below it)"),
    ({"bid": "0", "offer": "83.80"}, "no spot bid and offer (a positive bid, the offer not below it)"),
    ({"bid": "83.90", "offer": "83.80"}, "no spot bid and offer (a positive bid, the offer not below it)"),
    ({"quote_ts": None}, "no spot quote time"),
    ({"quote_ts": "2026-10-14T15:59:00Z"}, "spot quoted on 2026-10-14, not on the business date"),
    ({"quote_ts": "2026-10-15T21:30:00Z"}, "spot quoted on 2026-10-16, not on the business date"),  # 00:30 Moscow
])
def test_a_spot_that_is_not_usable_leaves_the_curve_as_before(ctx, spot, reason):
    _, _, lin = curves_of(ctx, view(ctx, [group(spot=spot)]))
    usd = lin["USD FX"]
    assert usd["fx_input_group"] == {"status": "not_used", "reason": reason}
    assert usd["swaps"] == GOLDEN_SWAPS and usd["futures"] == ["SiZ6"] and "points_source" not in usd
    assert usd["dropped"] == {}


def test_the_business_date_is_the_moscow_one(ctx):
    """21:30 UTC of the day before is 00:30 of the business date in Moscow: that spot is on the business date."""
    g = group(spot={"quote_ts": "2026-10-14T21:30:00Z"})
    for n in g["nodes"]:
        n.update(update_ts="2026-10-14T21:29:40Z")
    _, _, lin = curves_of(ctx, view(ctx, [g]))
    assert lin["USD FX"]["fx_input_group"]["status"] == "used"


def test_an_equal_bid_and_offer_of_the_spot_is_usable(ctx):
    _, _, lin = curves_of(ctx, view(ctx, [group(spot={"bid": "83.75", "offer": "83.75"})]))
    assert lin["USD FX"]["fx_input_group"]["spot_mid"] == "83.75"


# --- an intraday cut -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("age, taken", [("60", True), ("1800", True), ("1800.5", False), ("3600", False), (None, False)])
def test_an_intraday_cut_takes_the_group_by_the_age_of_its_spot(ctx, age, taken):
    market = view(ctx, [group(spot_age_seconds_at_cut=age)], "intraday", mutate=intraday_cut)
    _, fx, lin = curves_of(ctx, market)
    usd = lin["USD FX"]
    assert usd["status"] == "ok"
    if taken:
        assert usd["points_source"] == "fx_input_pairs"
        assert usd["swaps"] == ["anchor:TOD/TOM", *(f"fx_input_pairs:{n[0]}" for n in NODES)]
        assert usd["futures"] == [] and "futures_source" not in usd  # the farther one is not taken either (TDM#733)
        assert usd["dropped"] == {**{s: LEFT_OUT for s, *_ in NODES}, "SiZ7": PAST_U7}
        assert forward(ctx, fx, "USD", H7) == pytest.approx(83.76 + 3.17, rel=1e-9)
        assert usd["last_node"] == U7.isoformat()
    else:
        assert "points_source" not in usd and usd["swaps"] == ["anchor:TOD/TOM"]
        assert usd["futures"] == ["SiZ6", "SiH7", "SiM7", "SiU7", "SiZ7"]  # the intraday futures as before
        why = usd["fx_input_group"]["reason"]
        assert why.startswith("intraday cut: the spot is ") and why.endswith("(at most 1800 s)")
        assert ("of an unknown age" in why) if age is None else (f"{age} s old" in why)


@pytest.mark.parametrize("cut", ["eod_preliminary", "eod_final"])
def test_an_end_of_day_cut_does_not_check_the_age(ctx, cut):
    """The day's last minute may be hours old at an end-of-day cut: the group is taken."""
    _, _, lin = curves_of(ctx, view(ctx, [group(spot_age_seconds_at_cut="18000")], cut))
    assert lin["USD FX"]["fx_input_group"]["status"] == "used"
    assert lin["USD FX"]["fx_input_group"]["spot_age_seconds_at_cut"] == "18000"


# --- CNY: the exchange's swap points first ---------------------------------------------------------------------------


def test_cny_with_exchange_swap_points_keeps_them(ctx):
    market = view(ctx, [group("CNY", nodes=CNY_NODES)])
    assert [q["secid"] for q in market.fx_swap_quotes("CNY")] == ["CNYRUBTODTOM", "CNYRUB_TOM1D"]
    _, _, lin = curves_of(ctx, market)
    cny = lin["CNY FX"]
    assert cny["fx_input_group"] == {"status": "not_used", "reason": (
        "CNY has exchange swap points in the cut: CNYRUBTODTOM:TOD/TOM, CNYRUB_TOM1D:TOM/SPT")}
    assert cny["swaps"] == ["CNYRUBTODTOM:TOD/TOM", "CNYRUB_TOM1D:TOM/SPT"] and cny["futures"] == ["CRZ6"]
    assert "points_source" not in cny and cny["dropped"] == {}  # the golden's CNY curve


def test_cny_without_exchange_swap_points_takes_the_group(ctx):
    def cut(d):
        intraday_cut(d)
        d["futures_prices_intraday"] = [intraday(s, dt, b, o, "CNY") for s, dt, b, o in CNY_NODES]
    market = view(ctx, [group("CNY", nodes=CNY_NODES)], "intraday", mutate=cut)
    assert market.fx_swap_quotes("CNY") == []
    _, fx, lin = curves_of(ctx, market)
    cny = lin["CNY FX"]
    assert cny["status"] == "ok" and cny["points_source"] == "fx_input_pairs"
    assert cny["swaps"] == ["anchor:TOD/TOM", *(f"fx_input_pairs:{n[0]}" for n in CNY_NODES)] and cny["futures"] == []
    info = cny["fx_input_group"]
    assert info["spot_source"] == "moex_iss" and info["spot_mid"] == "11.9500"  # a LAST: the mid is the price itself
    spot = fx.spots["CNY"].rub_per_unit
    assert spot == pytest.approx(12.5141)
    for secid, delivery, _, _ in CNY_NODES:
        assert forward(ctx, fx, "CNY", delivery) == pytest.approx(spot + CNY_POINTS[secid], rel=1e-9)


def test_cny_exchange_swaps_that_were_all_dropped_leave_the_group(ctx):
    """"Has exchange swap points" is what ``_swap_quotes`` returns: a CNY swap whose dates are not after each other
    gives none, and the group is taken."""
    def cut(d):
        for r in d["fx_swaps"]:
            if r["base_ccy"] == "CNY":
                r["far_settle_date"] = r["near_settle_date"] or "2026-10-16"
                r["near_settle_date"] = r["far_settle_date"]
    market = view(ctx, [group("CNY", nodes=CNY_NODES)], mutate=cut)
    _, _, lin = curves_of(ctx, market)
    cny = lin["CNY FX"]
    assert cny["fx_input_group"]["status"] == "used" and cny["points_source"] == "fx_input_pairs"
    assert all(why.startswith("far ") for k, why in cny["dropped"].items() if k.startswith("CNYRUB"))


def test_a_last_price_spot_must_be_positive(ctx):
    market = view(ctx, [group("CNY", spot={"last": "0"}, nodes=CNY_NODES)], mutate=lambda d: d.update(fx_swaps=[]))
    _, _, lin = curves_of(ctx, market)
    assert lin["CNY FX"]["fx_input_group"] == {"status": "not_used", "reason": "no positive spot last price"}


# --- no group at all ----------------------------------------------------------------------------------------------


def test_an_empty_dataset_says_the_hubs_reason(ctx):
    market = view(ctx, [], stale=["no_completed_capture"])
    assert market.has_fx_input_pairs and market.fx_input_pairs == []
    _, _, lin = curves_of(ctx, market)
    usd = lin["USD FX"]
    assert usd["fx_input_group"] == {"status": "not_used", "reason": "no USD group in fx_input_pairs (no_completed_capture)"}
    assert usd["swaps"] == GOLDEN_SWAPS and usd["futures"] == ["SiZ6"] and "points_source" not in usd
    assert usd["status"] == "ok"


def test_a_partial_dataset_says_which_currency_is_missing(ctx):
    market = view(ctx, [group("USD")], stale=["group_missing"])
    _, _, lin = curves_of(ctx, market)
    assert lin["USD FX"]["fx_input_group"]["status"] == "used"
    assert lin["EUR FX"]["fx_input_group"] == {"status": "not_used", "reason": "no EUR group in fx_input_pairs (group_missing)"}
    assert lin["EUR FX"]["status"] == "missing"  # as before: no swaps and no futures of EUR in the golden


def test_an_empty_dataset_without_a_reason(ctx):
    _, _, lin = curves_of(ctx, view(ctx, []))
    assert lin["USD FX"]["fx_input_group"] == {"status": "not_used", "reason": "no USD group in fx_input_pairs"}


def test_two_groups_of_a_currency_are_not_guessed(ctx):
    _, _, lin = curves_of(ctx, view(ctx, [group(), group(run_id=78)]))
    assert lin["USD FX"]["fx_input_group"] == {"status": "not_used", "reason": "2 USD groups in fx_input_pairs"}


def test_a_group_that_does_not_parse_is_not_a_crash(ctx):
    g = group()
    del g["spot"]["source"]
    g2 = group("EUR")
    g2["nodes"][0]["bid_rub_per_unit"] = "not a number"
    _, _, lin = curves_of(ctx, view(ctx, [g, g2]))
    usd = lin["USD FX"]
    assert usd["status"] == "ok" and usd["swaps"] == GOLDEN_SWAPS and usd["futures"] == ["SiZ6"]
    assert usd["fx_input_group"]["status"] == "not_used"
    assert usd["fx_input_group"]["reason"].startswith("the USD group of fx_input_pairs cannot be read (KeyError")
    assert lin["EUR FX"]["fx_input_group"]["reason"].startswith("the EUR group of fx_input_pairs cannot be read (")


def test_the_setting_that_turns_futures_off_leaves_the_group_out(ctx):
    _, _, lin = curves_of(ctx, view(ctx, [group()]), implied_futures=False)
    usd = lin["USD FX"]
    assert usd["fx_input_group"] == {"status": "not_used", "reason": "setting fx_implied_futures is off"}
    assert usd["swaps"] == GOLDEN_SWAPS and usd["futures"] == []  # the exchange swap alone, as before


def test_points_that_core_cannot_build_leave_the_curve_as_before(ctx):
    """A group whose spot is far above the run's: the points (about -415) would make the outrights negative, core refuses;
    the curve is built without the group and says so."""
    g = group(spot={"bid": "500.0", "offer": "500.2"})
    _, fx, lin = curves_of(ctx, view(ctx, [g]))
    usd = lin["USD FX"]
    assert usd["status"] == "ok" and usd["swaps"] == GOLDEN_SWAPS and usd["futures"] == ["SiZ6"]
    assert usd["dropped"] == {} and "points_source" not in usd
    info = usd["fx_input_group"]
    assert info["status"] == "not_used"
    assert info["reason"].startswith("the curve with the group's points did not build: ")
    # what the lineage knew of the group stays; only the status and the reason changed
    assert (info["run_id"], info["content_sha256"], info["known_at"], info["spot_source"]) == (
        77, "b" * 64, "2026-10-15T16:10:00+00:00", "ntpro_public")
    assert info["spot_mid"] == "500.1" and info["nodes_used"] == ["SiZ6", "SiH7", "SiM7", "SiU7"]
    assert info["nodes_dropped"] == {}
    assert forward(ctx, fx, "USD", Z6) == pytest.approx(85.219, rel=1e-9)  # the settlement price, as before


def refuse_build(monkeypatch, only_with_group_points):
    """Core refuses to build the implied curve: always, or only when the request holds the group's points."""
    request = fxcurves.FxImpliedCurveRequest
    real = request.build

    def build(self, rub, spot_rate, spot_value_date):
        if not only_with_group_points or any(q.label.startswith("fx_input_pairs:") for q in self.quotes):
            raise InvalidTerms("synthetic: the quotes do not build")
        return real(self, rub, spot_rate, spot_value_date)
    monkeypatch.setattr(request, "build", build)


def test_a_curve_that_does_not_build_even_without_the_group_keeps_the_group_in_its_lineage(ctx, monkeypatch):
    """The curve fails with the group's points and again without them: the status is ``error`` and its lineage still
    has ``fx_input_group`` (what was known of the group, the reason the points were left out)."""
    refuse_build(monkeypatch, only_with_group_points=False)
    _, fx, lin = curves_of(ctx, view(ctx, [group()]))
    usd = lin["USD FX"]
    assert usd["status"] == "error" and usd["error"] == "synthetic: the quotes do not build"
    assert "USD FX" not in fx.curves
    info = usd["fx_input_group"]
    assert info["status"] == "not_used"
    assert info["reason"] == "the curve with the group's points did not build: synthetic: the quotes do not build"
    assert (info["run_id"], info["content_sha256"]) == (77, "b" * 64)
    assert info["nodes_used"] == ["SiZ6", "SiH7", "SiM7", "SiU7"] and info["nodes_dropped"] == {}


def test_a_curve_that_does_not_build_with_a_group_that_was_not_used_says_why(ctx, monkeypatch):
    refuse_build(monkeypatch, only_with_group_points=False)
    _, _, lin = curves_of(ctx, view(ctx, [group(spot={"bid": None})]))
    usd = lin["USD FX"]
    assert usd["status"] == "error" and usd["fx_input_group"] == {
        "status": "not_used", "reason": "no spot bid and offer (a positive bid, the offer not below it)"}


def test_a_curve_that_does_not_build_on_a_snapshot_without_the_dataset_is_as_before(ctx, monkeypatch):
    refuse_build(monkeypatch, only_with_group_points=False)
    _, _, lin = curves_of(ctx, view(ctx))
    assert lin["USD FX"] == {"status": "error", "error": "synthetic: the quotes do not build"}


def test_a_curve_that_builds_without_the_group_points_is_ok_with_the_group_kept(ctx, monkeypatch):
    refuse_build(monkeypatch, only_with_group_points=True)
    _, fx, lin = curves_of(ctx, view(ctx, [group()]))
    usd = lin["USD FX"]
    assert usd["status"] == "ok" and "points_source" not in usd and usd["swaps"] == GOLDEN_SWAPS
    assert usd["fx_input_group"]["status"] == "not_used" and usd["fx_input_group"]["run_id"] == 77
    assert "USD FX" in fx.curves


def test_the_card_of_a_position_reads_a_curve_with_the_group(ctx):
    from desk.api import provenance as pv

    _, _, lin = curves_of(ctx, view(ctx, [group()]))
    assert pv._curve_state({"fx": {"curves": lin}}, "USD FX") == ("ok", "")


def test_the_adapter_reads_the_published_shape_of_a_group():
    g = adapter._fx_input_group(group())
    assert (g.run_id, g.currency, g.spot.quote_kind, g.spot.bid, g.spot.offer) == (77, "USD", "indicative_bid_offer",
                                                                                   Decimal("83.70"), Decimal("83.80"))
    assert [n.secid for n in g.nodes] == [n[0] for n in NODES] and g.nodes[0].update_skew_seconds == Decimal(20)
    assert g.spot_age_seconds_at_cut == Decimal(60) and g.known_at.isoformat() == "2026-10-15T16:10:00+00:00"
    assert g.spot.quote_ts is not None and g.spot.quote_ts.utcoffset().total_seconds() == 0
