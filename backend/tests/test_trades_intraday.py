"""The day's bond trades in the intraday book (``desk.trades.apply_intraday``, ``intraday_inputs``; TDM#557):
synthetic inputs and trades, no database."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from desk import store, trades
from desk.settings import Settings


def _inputs() -> store.PositionInputs:
    rows = [("T1|SEC1", "T1", "SEC1", 1000.0), ("T1|FX", "T1", None, None), ("T2|SEC2", "T2", "SEC2", 5.0),
            ("T1|FACE", "T1", None, None)]
    extra = {"T1|SEC1": {"asset_class": "bond", "ccy": "RUB", "face_amount_ccy": 1_000_000.0, "mv_rub": 990_000.0,
                         "book_value_rub": 980_000.0, "accrued_ccy": 1000.0, "risk_dv01_rub": 50.0},
             "T1|FX": {"asset_class": "fx", "deal_id": "777"},
             "T2|SEC2": {"asset_class": "bond", "ccy": "USD", "face_amount_ccy": 5000.0},
             "T1|FACE": {"asset_class": "bond", "ccy": "USD", "face_amount_ccy": 100.0, "mv_rub": 9000.0,
                         "book_value_rub": 8000.0, "accrued_ccy": 2.0, "x": {"keep": 1}}}
    return store.PositionInputs(rows, [1], {"T1": date(2026, 11, 2), "T2": date(2026, 11, 2)},
                                datetime(2026, 11, 3, tzinfo=UTC), "h0", extra)


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
    assert out.extra["T1|NEW"]["ccy"] == "USD"  # the bond's own currency, not the trade's
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


def _apply(monkeypatch, rows, inputs=None, day=date(2026, 11, 3)):
    monkeypatch.setattr(store, "dataset_rows", lambda con, ds, on: store.DatasetInputs(ds, on, rows, [{"set_id": 9}]))
    return trades.apply_intraday(None, day, inputs or _inputs())


def test_a_book_without_positions_takes_no_trade(monkeypatch):
    out, summary = _apply(monkeypatch, [_trade(book_id="ZZ", position_id="ZZ|SEC9", instrument_id="SEC1")])
    assert summary["skipped"] == {"other_book": 1} and summary["skipped_ids"] == {"TR1": "other_book"}
    assert out.rows == _inputs().rows and summary["applied"] == 0


@pytest.mark.parametrize("order", [(("sell", 1000), ("buy", 500)), (("buy", 500), ("sell", 1000))])
def test_closing_to_zero_and_reopening_does_not_depend_on_the_order(monkeypatch, order):
    rows = [_trade(trade_id=f"X{i}", side=side, quantity=str(q), trade_ts=f"2026-11-03T10:0{i}:00")
            for i, (side, q) in enumerate(order)]
    out, summary = _apply(monkeypatch, rows)
    by = {r[0]: r for r in out.rows}
    assert by["T1|SEC1"][3] == 500.0 and summary["applied"] == 2
    assert out.extra["T1|SEC1"]["face_amount_ccy"] == pytest.approx(500_000.0)
    assert out.extra["T1|SEC1"]["mv_rub"] == pytest.approx(495_000.0)
    assert out.extra["T1|SEC1"]["accrued_ccy"] == pytest.approx(500.0)
    assert out.extra["T1|SEC1"]["x"]["intraday_start_qty"] == 1000.0


def test_a_sale_to_zero_zeroes_the_rows_figures_and_a_purchase_keeps_the_cost_basis(monkeypatch):
    out, _ = _apply(monkeypatch, [_trade(side="sell", quantity="1000")])
    x = out.extra["T1|SEC1"]
    assert {r[0]: r for r in out.rows}["T1|SEC1"][3] == 0.0 and x["mv_rub"] == 0 and x["book_value_rub"] == 0
    out, _ = _apply(monkeypatch, [_trade(quantity="1000")])   # doubled: the cost of the new bonds is not known
    x = out.extra["T1|SEC1"]
    assert x["mv_rub"] == pytest.approx(1_980_000.0) and x["book_value_rub"] == 980_000.0
    assert x["x"]["intraday_cost_basis"] == "stale"


def test_face_only_row_scales_every_figure_and_wants_the_same_currency(monkeypatch):
    t = {"position_id": "T1|FACE", "quantity": None, "face_amount_ccy": "50", "ccy": "USD", "side": "buy"}
    out, summary = _apply(monkeypatch, [_trade(**t)])
    x = out.extra["T1|FACE"]
    assert x["face_amount_ccy"] == 150.0 and x["mv_rub"] == pytest.approx(13_500.0)
    assert x["accrued_ccy"] == pytest.approx(3.0) and x["x"]["keep"] == 1 and x["x"]["intraday_start_face"] == 100.0
    assert summary["applied"] == 1
    out, summary = _apply(monkeypatch, [_trade(**{**t, "ccy": "RUB"}), _trade(trade_id="Q", position_id="T1|FACE",
                                                                              quantity="3")])
    assert summary["skipped"] == {"units_mismatch": 2} and out.extra["T1|FACE"]["face_amount_ccy"] == 100.0
    assert _inputs().extra["T1|FACE"]["x"] == {"keep": 1}   # the stored inputs are untouched


def test_new_position_needs_a_free_id_and_a_known_currency(monkeypatch):
    rows = [_trade(trade_id="A", position_id="T2|SEC2", book_id="T1", instrument_id="SEC2"),
            _trade(trade_id="B", position_id="T1|UNK", instrument_id="UNKNOWN")]
    _, summary = _apply(monkeypatch, rows)
    assert summary["skipped"] == {"no_currency": 1, "position_id_taken": 1}
    # a new row that nets to nothing is not left behind
    out, summary = _apply(monkeypatch, [_trade(trade_id="C", position_id="T1|N", instrument_id="SEC2"),
                                        _trade(trade_id="D", position_id="T1|N", instrument_id="SEC2", side="sell")])
    assert "T1|N" not in {r[0] for r in out.rows} and summary["new_positions"] == 0 and summary["applied"] == 2


def test_a_growing_cut_with_the_same_trade_does_not_count_twice(monkeypatch):
    first = [_trade()]
    again = [_trade(), _trade(trade_id="TR2", quantity="100")]
    a, _ = _apply(monkeypatch, first)
    b, summary = _apply(monkeypatch, again)
    assert {r[0]: r for r in a.rows}["T1|SEC1"][3] == 1200.0 and {r[0]: r for r in b.rows}["T1|SEC1"][3] == 1300.0
    assert summary["trade_ids"] == ["TR1", "TR2"] and a.private_inputs_hash != b.private_inputs_hash


def test_the_screen_says_what_the_run_took_in():
    day = date(2026, 11, 3)
    t = _trade(trade_version=2)
    run = {"run_type": "INTRADAY", "trade_ids": ["TR1"], "versions": {"TR1": 2}, "skipped_ids": {"TR9": "no_instrument"}}
    state = trades._intraday_state
    assert state(t, day, run, True) == "applied"
    assert state(_trade(trade_version=3), day, run, True) == "pending"    # a newer version than the run took
    assert state(_trade(trade_id="TR9"), day, run, True) == "no_instrument"
    assert state(_trade(trade_id="TR5"), day, run, True) == "pending"
    assert state(t, day, None, True) == "pending" and state(t, day, run, False) == "off"
    assert state(t, day, {**run, "run_type": "EOD"}, True) == "eod"        # the preliminary close: no trades
    assert state(_trade(status="cancelled"), day, run, True) == "cancelled"


def test_the_changed_row_says_where_its_day_started(monkeypatch):
    """TDM#557, to be decided: the flash PnL of a changed row is measured on the quantity after the trades; the row says
    from what quantity the day started (``x.intraday_start_qty``)."""
    out, _ = _apply(monkeypatch, [_trade(side="sell", quantity="1000")])
    assert out.extra["T1|SEC1"]["x"]["intraday_start_qty"] == 1000.0
