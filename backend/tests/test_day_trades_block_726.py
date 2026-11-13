"""TDM#726: the card's block «Сделки за день» reads the position's ``trade`` (``concepts.trade_of``): per side the number of
trades, the quantity and the average price weighted by it, and the net. Synthetic trades only; the same recorded trades
(``pnl.extra.trades``) the C20 screen lists for the position, no other source."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_concepts import desk  # noqa: F401 - fixture
from test_pnl_period import seed_period
from test_pnl_screens_new_trades import TRADES, more

from desk.api import concepts, create_app

# ten bonds of 1000 in the position after the trades: face 10 000, the face of one bond 1000
POS = [10000.0, 0, 0, 0, 0, 0, 99.0]
TWO_BUYS_AND_A_SALE = [
    {"trade_id": "b1", "side": "buy", "quantity": 3.0, "price_pct": 99.0},
    {"trade_id": "b2", "side": "buy", "quantity": 1.0, "price_pct": 100.0},
    {"trade_id": "s1", "side": "sell", "quantity": 2.0, "price_pct": 101.5},
]


def test_two_purchases_at_different_prices_and_a_sale():
    t = concepts.trade_of({"trades": TWO_BUYS_AND_A_SALE, "quantity": 10.0, "start": 8.0}, POS)
    buy, sell, net = t["buy"], t["sell"], t["net"]
    assert buy["n"] == 2 and buy["bonds"] == pytest.approx(4.0) and buy["face"] == pytest.approx(4000.0)
    assert buy["avg"] == pytest.approx((3 * 99.0 + 1 * 100.0) / 4)   # weighted by the quantity, not the mean 99.5
    assert sell["n"] == 1 and sell["bonds"] == pytest.approx(2.0) and sell["face"] == pytest.approx(2000.0)
    assert sell["avg"] == pytest.approx(101.5)
    assert net == {"bonds": pytest.approx(2.0), "face": pytest.approx(2000.0)}
    assert t["n"] == 3 and t["qty"] == pytest.approx(2000.0)   # what the pages read before is as it was
    assert (buy["counted"], buy["priced"], sell["counted"], sell["priced"]) == (2, 2, 1, 1)


def test_a_sale_to_zero_has_no_face_and_a_net_of_the_bonds():
    t = concepts.trade_of({"trades": [{"side": "sell", "quantity": 9.0, "price_pct": 98.0}], "quantity": 0.0,
                           "start": 9.0}, [0.0, 0, 0, 0, 0, 0, 99.0])
    assert "buy" not in t and t["sell"]["bonds"] == pytest.approx(9.0) and t["sell"]["face"] is None
    assert t["sell"]["avg"] == pytest.approx(98.0)
    assert t["net"] == {"bonds": pytest.approx(-9.0), "face": None}


def test_a_trade_without_a_price_or_a_quantity_is_counted_but_not_in_the_average():
    rows = [{"side": "buy", "quantity": 2.0, "price_pct": 99.0}, {"side": "buy", "quantity": 6.0, "price_pct": None},
            {"side": "buy", "quantity": None, "price_pct": 50.0}]
    b = concepts.trade_of({"trades": rows, "quantity": 10.0, "start": 2.0}, POS)["buy"]
    assert b["n"] == 3 and b["counted"] == 2 and b["priced"] == 1
    assert b["bonds"] == pytest.approx(8.0) and b["avg"] == pytest.approx(99.0)   # the unpriced 6 are not at 0 or at 50
    none = concepts.trade_of({"trades": [{"side": "buy"}], "quantity": 5.0, "start": 0.0}, POS)
    assert none["buy"]["avg"] is None and none["buy"]["bonds"] == 0.0 and none["buy"]["counted"] == 0


def test_the_block_is_in_the_pages_data_for_a_position_with_recorded_trades(store_url):
    ids = seed_period(store_url, more=more)
    client = TestClient(create_app(store_url))
    js = client.get(f"/concepts-data.js?run_id={ids[-1]}")
    assert js.status_code == 200, js.text
    p = json.loads(js.text[len("window.DESK_EXTRAS = "):-2])["positions"]
    a1 = p["A1"]["trade"]   # TRADES: bought 3 at 99, sold 3 at 101
    assert a1["buy"]["n"] == 1 and a1["buy"]["bonds"] == pytest.approx(3.0) and a1["buy"]["avg"] == pytest.approx(99.0)
    assert a1["sell"]["n"] == 1 and a1["sell"]["avg"] == pytest.approx(101.0)
    assert a1["net"]["bonds"] == pytest.approx(0.0) and a1["n"] == len(TRADES)
    t1 = p["T1"]["trade"]   # opened today: only a purchase, no sale key
    assert t1["new"] and t1["buy"]["bonds"] == pytest.approx(5.0) and "sell" not in t1
    assert "trade" not in p["B1"]   # no recorded trade, no block
