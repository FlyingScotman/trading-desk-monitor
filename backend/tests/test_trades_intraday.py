"""The day's bond trades in the intraday book (``desk.trades.apply_intraday``, ``intraday_inputs``; TDM#557):
synthetic inputs and trades, no database."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from desk import store, trades
from desk.settings import Settings


def _inputs() -> store.PositionInputs:
    rows = [("T1|SEC1", "T1", "SEC1", 1000.0), ("T1|FX", "T1", None, None)]
    extra = {"T1|SEC1": {"asset_class": "bond", "face_amount_ccy": 1_000_000.0, "mv_rub": 990_000.0},
             "T1|FX": {"asset_class": "fx", "deal_id": "777"}}
    return store.PositionInputs(rows, [1], {"T1": date(2026, 11, 2)}, datetime(2026, 11, 3, tzinfo=UTC), "h0",
                                extra)


def _trade(**kw) -> dict:
    return {"trade_id": "TR1", "status": "live", "trade_date": "2026-11-03", "book_id": "T1",
            "position_id": "T1|SEC1", "asset_class": "bond", "side": "buy", "quantity": "200", "_set_id": 9, **kw}


def test_apply_intraday(monkeypatch):
    day = date(2026, 11, 3)
    rows = [_trade(), _trade(trade_id="TR2", side="sell", quantity="50"),
            _trade(trade_id="TR3", position_id="T1|NEW", instrument_id="SEC2", quantity="10"),
            _trade(trade_id="TR4", status="cancelled"), _trade(trade_id="TR5", asset_class="fx"),
            _trade(trade_id="TR6", trade_date="2026-11-01"), _trade(trade_id="TR7", quantity=None),
            _trade(trade_id="TR8", position_id="T1|NOPE")]
    monkeypatch.setattr(store, "dataset_rows", lambda con, ds, on: store.DatasetInputs(ds, on, rows, [{"set_id": 9}]))
    out, summary = trades.apply_intraday(None, day, _inputs())
    by = {r[0]: r for r in out.rows}
    assert by["T1|SEC1"][3] == 1150.0 and by["T1|NEW"] == ("T1|NEW", "T1", "SEC2", 10.0)
    assert out.extra["T1|SEC1"]["face_amount_ccy"] == pytest.approx(1_150_000.0)
    assert out.extra["T1|SEC1"]["mv_rub"] == pytest.approx(990_000.0 * 1.15)
    assert out.extra["T1|NEW"]["x"] == {"intraday_trade": True}
    assert summary["applied"] == 3 and summary["new_positions"] == 1 and summary["set_ids"] == [9]
    assert summary["skipped"] == {"cancelled": 1, "no_instrument": 1, "no_quantity": 1, "not_bond": 1,
                                  "other_trade_date": 1}
    assert out.private_inputs_hash != "h0" and out.set_ids == [1]
    assert _inputs().rows[0][3] == 1000.0           # the stored inputs are untouched
    monkeypatch.setattr(store, "dataset_rows", lambda con, ds, on: store.DatasetInputs(ds, on, [], []))
    same, summary = trades.apply_intraday(None, day, _inputs())
    assert same.private_inputs_hash == "h0" and summary["applied"] == 0


def test_effect_marks_the_screen():
    day = date(2026, 11, 3)
    assert trades.effect(_trade(), day) is None
    assert trades.effect(_trade(status="cancelled"), day) == "cancelled"
    assert trades.effect(_trade(asset_class="otc"), day) == "not_bond"


def test_intraday_inputs_follows_the_setting(monkeypatch):
    day = date(2026, 11, 3)
    monkeypatch.setattr(store, "dataset_rows",
                        lambda con, ds, on: store.DatasetInputs(ds, on, [_trade()], [{"set_id": 9}]))
    on, summary = trades.intraday_inputs(None, day, Settings(), _inputs())
    assert summary["applied"] == 1 and on.rows[0][3] == 1200.0
    off, none = trades.intraday_inputs(None, day, Settings(intraday_trades=False), _inputs())
    assert none is None and off.private_inputs_hash == "h0" and off.rows[0][3] == 1000.0
    empty = store.PositionInputs([], [], {}, datetime(2026, 11, 3, tzinfo=UTC), "h0", {})
    assert trades.intraday_inputs(None, day, Settings(), empty) == (empty, None)
