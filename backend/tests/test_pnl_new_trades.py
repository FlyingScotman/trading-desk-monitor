"""The PnL of a day with trades (``desk.pnl.new_trades``, TDM#557): the start-of-day position plus each trade from its
price to today's valuation. Per-trade arithmetic on stubs; the whole path (autopilot, store, lineage, screens) on the
synthetic fixtures of the intraday tests. Synthetic data only."""

from __future__ import annotations

import math
from datetime import date
from types import SimpleNamespace

import pytest
import test_slice_s3
from test_autopilot import cfg, put, steps
from test_intraday import FRI, at, publish

from desk import store, trades
from desk.autopilot import Autopilot
from desk.inbox import scan
from desk.pnl import new_trades as nt
from desk.pnl.flash import NoPnl
from desk.valuation import positions as pv1


@pytest.fixture(autouse=True)
def _fresh_process():
    scan._handles.clear()
    scan._last_seen.clear()
    yield
    scan._handles.clear()
    scan._last_seen.clear()


server, stub = test_slice_s3.server, test_slice_s3.stub  # the stub hub's fixtures
ISIN = {"SEC-29008": "RU000A0JV4P3", "SEC-26238": "RU000A1038V6"}
HEAD = ("trade_id;trade_version;status;trade_date;trade_ts;book_id;position_id;asset_class;source_instrument_id;"
        "side;quantity;face_amount_ccy;price_pct;accrued_ccy;ccy;instrument_id;isin\n")


def trade_line(tid, side, qty, price="", acc="", pid="B1|SEC-26238", instr="SEC-26238", hhmm="10:30", ccy="RUB",
               face="", status="live", iid=None):
    """``iid``: the ``instrument_id`` column (default the instrument; "" leaves it to the adapter, which looks the bond
    up by ISIN as it does for a position)."""
    iid = instr if iid is None else iid
    return (f"{tid};1;{status};2026-10-16;2026-10-16T{hhmm}:00;B1;{pid};bond;{instr};{side};{qty};{face};{price};{acc};"
            f"{ccy};{iid};{ISIN.get(instr, '')}\n")


# ---------- one trade against a valuation (stubs, no database) ----------

@pytest.fixture
def stub_face(monkeypatch):
    monkeypatch.setattr(pv1, "face_on", lambda t, d: 1000.0)


def _ctx():
    return SimpleNamespace(static=SimpleNamespace(bonds={"S": SimpleNamespace(
        face_unit=None, currency="RUB", initial_face_value=1000.0)}), business_date=date(2026, 10, 16))


def _ref(**kw):
    base = {"dirty_rub": 650.0, "accrued_rub": 10.0, "face_rub": 1000.0}
    return SimpleNamespace(**{**base, **kw})


def _t(**kw):
    return {"trade_id": "T", "trade_version": 1, "side": "buy", "quantity": "100", "price_pct": "64", "ccy": "RUB",
            "accrued_ccy": "900", **kw}


TERMS = SimpleNamespace(face_unit=None, currency="RUB")


def test_purchase_is_priced_from_the_trade_to_the_valuation(stub_face):
    r = nt.trade_pnl(_ctx(), "S", TERMS, _ref(), _t())
    assert r["flag"] is None and r["quantity"] == 100.0
    assert r["cost_rub"] == pytest.approx(100 * 0.64 * 1000 + 900)
    assert r["pnl_rub"] == pytest.approx(100 * 650 - (64_000 + 900))


def test_sale_is_priced_from_the_valuation_to_the_trade(stub_face):
    r = nt.trade_pnl(_ctx(), "S", TERMS, _ref(), _t(side="sell", price_pct="66"))
    assert r["pnl_rub"] == pytest.approx((66_000 + 900) - 100 * 650)


def test_no_accrued_takes_the_valuations_and_says_so(stub_face):
    r = nt.trade_pnl(_ctx(), "S", TERMS, _ref(), _t(accrued_ccy=None))
    assert r["flag"] == "accrued_assumed" and r["pnl_rub"] == pytest.approx(100 * 650 - (64_000 + 1000))


def test_no_price_is_zero_and_flagged(stub_face):
    r = nt.trade_pnl(_ctx(), "S", TERMS, _ref(), _t(price_pct=None))
    assert r["flag"] == "no_trade_price" and r["pnl_rub"] == 0.0 and r["quantity"] == 100.0


def test_other_currency_is_zero_and_flagged():
    r = nt.trade_pnl(_ctx(), "S", TERMS, _ref(), _t(ccy="USD"))
    assert r["flag"] == "units_mismatch" and r["pnl_rub"] == 0.0


def test_foreign_bond_accrued_is_taken_at_the_valuations_implied_rate(monkeypatch, stub_face):
    """A bond in USD whole: the cost is n x price % x face_rub, the accrued in USD at face_rub / face (90), not at the
    market rate (95), so that no PnL of the rate move appears."""
    monkeypatch.setattr(nt, "fx_rate", lambda ctx, ccy: (95.0, "test"))
    terms = SimpleNamespace(face_unit="USD", currency="USD")
    ref = _ref(face_rub=90_000.0, dirty_rub=60_000.0)
    r = nt.trade_pnl(_ctx(), "S", terms, ref, _t(ccy="USD", accrued_ccy="10", price_pct="64"))
    assert r["cost_rub"] == pytest.approx(100 * 0.64 * 90_000 + 10 * 90)
    assert r["pnl_rub"] == pytest.approx(100 * 60_000 - r["cost_rub"])
    # no face to imply the rate from: the market rate; no market rate either: flagged
    monkeypatch.setattr(pv1, "face_on", lambda t, d: 0.0)
    assert nt.trade_pnl(_ctx(), "S", terms, ref, _t(ccy="USD", accrued_ccy="10"))["cost_rub"] == pytest.approx(
        100 * 0.64 * 90_000 + 10 * 95)
    monkeypatch.setattr(nt, "fx_rate", lambda ctx, ccy: None)
    assert nt.trade_pnl(_ctx(), "S", terms, ref, _t(ccy="USD", accrued_ccy="10"))["flag"] == "no_fx_rate"


def test_a_replacement_bond_traded_in_rub_is_priced_by_its_face_percent(stub_face):
    """A face in USD, the trade settled in RUB: price_pct is a percent of the face whatever the settlement currency,
    the accrued is in RUB as it is. A third currency is still a mismatch."""
    terms = SimpleNamespace(face_unit="USD", currency="USD")
    ref = _ref(face_rub=90_000.0, dirty_rub=60_000.0)
    r = nt.trade_pnl(_ctx(), "S", terms, ref, _t(ccy="RUB", accrued_ccy="900", price_pct="64"))
    assert r["flag"] is None and r["cost_rub"] == pytest.approx(100 * 0.64 * 90_000 + 900)
    assert r["pnl_rub"] == pytest.approx(100 * 60_000 - r["cost_rub"])
    assert nt.trade_pnl(_ctx(), "S", terms, ref, _t(ccy="EUR"))["flag"] == "units_mismatch"
    assert nt.trade_pnl(_ctx(), "S", TERMS, _ref(), _t(ccy="USD"))["flag"] == "units_mismatch"   # a RUB bond, USD trade


def test_a_face_amount_is_the_number_of_bonds_by_the_initial_face(stub_face):
    r = nt.trade_pnl(_ctx(), "S", TERMS, _ref(), _t(quantity=None, face_amount_ccy="50000"))
    assert r["quantity"] == pytest.approx(50.0)


def test_no_valuation_is_zero_and_flagged(stub_face):
    assert nt.trade_pnl(_ctx(), "S", TERMS, None, _t())["flag"] == "no_valuation"
    assert nt.trade_pnl(_ctx(), "S", TERMS, _ref(dirty_rub=math.nan), _t())["flag"] == "no_valuation"


def _days(monkeypatch, by_date):
    def rows(con, ds, on):
        r = by_date.get(on.isoformat(), [])
        return store.DatasetInputs(ds, on, r, [{"set_id": 9}] if r else [])
    monkeypatch.setattr(store, "dataset_rows", rows)
    monkeypatch.setattr(trades, "_trade_sets", lambda con, lo, hi: sorted(
        (date.fromisoformat(k), str(t["book_id"]), 1) for k, v in by_date.items() for t in v
        if lo <= date.fromisoformat(k) < hi))


def _report(inputs, qty, valid="2026-11-03", **kw):
    """The report of the day: T1|SEC1 at ``qty``, the books valid ``valid``."""
    rows = [(p, b, s, qty if p == "T1|SEC1" else q) for p, b, s, q in inputs.rows]
    return store.PositionInputs(**{**inputs.__dict__, "rows": rows, "private_inputs_hash": "report",
                                   "valid_dates": {"T1": date.fromisoformat(valid), "T2": date.fromisoformat(valid)},
                                   **kw})


def test_a_report_of_the_day_keeps_its_quantity_and_the_trades_give_the_pnl(monkeypatch):
    """TDM#557: the report valid the day has the positions; the start of the day is the earlier set; the trades are
    used for the PnL; start + trades = the report: no gap."""
    import test_trades_intraday as tti

    day = date(2026, 11, 3)
    _days(monkeypatch, {"2026-11-03": [tti._trade()]})
    monkeypatch.setattr(store, "positions_for", lambda con, d, books=None: tti._inputs())
    out, summary = trades.apply_intraday(None, day, _report(tti._inputs(), 1200.0))
    assert {r[0]: r[3] for r in out.rows}["T1|SEC1"] == 1200.0 and summary["gaps"] == {}
    assert summary["rows"]["T1|SEC1"] == {"mode": "qty", "start": 1000.0, "final": 1200.0, "new": False,
                                          "final_fold": 1200.0, "report": True, "members": {"T1|SEC1": 1200.0},
                                          "bond": "SEC1", "book": "T1"}
    assert [t["trade_id"] for t in summary["trades"]] == ["TR1"] and summary["applied"] == 1
    assert {r[0]: r[3] for r in summary["start_inputs"].rows}["T1|SEC1"] == 1000.0
    assert out.private_inputs_hash not in ("report", "h0")   # the trades are in the hash


def test_a_report_that_differs_is_kept_and_flagged(monkeypatch):
    import test_trades_intraday as tti

    day = date(2026, 11, 3)
    _days(monkeypatch, {"2026-11-03": [tti._trade()]})
    monkeypatch.setattr(store, "positions_for", lambda con, d, books=None: tti._inputs())
    rep = _report(tti._inputs(), 1150.0)
    rep.extra["T1|SEC1"]["x"] = {"keep": 1}
    out, summary = trades.apply_intraday(None, day, rep)
    assert {r[0]: r[3] for r in out.rows}["T1|SEC1"] == 1150.0
    assert summary["gaps"] == {"T1|SEC1": pytest.approx(-50.0)}
    assert out.extra["T1|SEC1"]["x"] == {"keep": 1, "trades_report_gap": pytest.approx(-50.0)}
    assert summary["rows"]["T1|SEC1"]["final"] == 1150.0 and summary["rows"]["T1|SEC1"]["start"] == 1000.0


def test_a_traded_row_the_report_no_longer_lists_is_kept_at_zero(monkeypatch):
    import test_trades_intraday as tti

    day = date(2026, 11, 3)
    _days(monkeypatch, {"2026-11-03": [tti._trade(side="sell", quantity="1000")]})
    monkeypatch.setattr(store, "positions_for", lambda con, d, books=None: tti._inputs())
    rep = _report(tti._inputs(), 0.0)
    rep = store.PositionInputs(**{**rep.__dict__, "rows": [r for r in rep.rows if r[0] != "T1|SEC1"]})
    out, summary = trades.apply_intraday(None, day, rep)
    row = next(r for r in out.rows if r[0] == "T1|SEC1")
    assert row[3] == 0.0 and out.extra["T1|SEC1"]["mv_rub"] == 0 and summary["gaps"] == {}
    assert summary["rows"]["T1|SEC1"]["start"] == 1000.0 and summary["rows"]["T1|SEC1"]["final"] == 0.0


def test_the_trades_of_the_days_a_report_is_late_are_in_the_start_of_the_day(monkeypatch):
    """TDM#557: no report for the 2nd: the 3rd's book is the set of the 1st + the 2nd's trades + the 3rd's; only the
    3rd's trades are the day's PnL; when the report of the 2nd comes the 2nd's trades are not applied again."""
    import test_trades_intraday as tti

    day = date(2026, 11, 3)
    _days(monkeypatch, {"2026-11-02": [tti._trade(trade_id="Y1", trade_date="2026-11-02", quantity="100")],
                        "2026-11-03": [tti._trade(trade_id="T1", quantity="200")]})
    old = store.PositionInputs(**{**tti._inputs().__dict__, "valid_dates": {"T1": date(2026, 11, 1),
                                                                            "T2": date(2026, 11, 1)}})
    out, summary = trades.apply_intraday(None, day, old)
    assert {r[0]: r[3] for r in out.rows}["T1|SEC1"] == 1300.0 and summary["history"] == 1
    assert [t["trade_id"] for t in summary["trades"]] == ["T1"] and summary["applied"] == 1
    assert {r[0]: r[3] for r in summary["start_inputs"].rows}["T1|SEC1"] == 1100.0
    assert summary["rows"]["T1|SEC1"]["start"] == 1100.0
    base = trades.apply_intraday(None, day, tti._inputs())[0]   # report of the 1st... valid the 2nd: no history
    assert {r[0]: r[3] for r in base.rows}["T1|SEC1"] == 1200.0
    assert base.private_inputs_hash != out.private_inputs_hash
    only = {**old.__dict__}
    _days(monkeypatch, {"2026-11-02": [tti._trade(trade_id="Y1", trade_date="2026-11-02", quantity="100")]})
    same, s2 = trades.apply_intraday(None, day, store.PositionInputs(**only))
    assert s2["history"] == 1 and s2["applied"] == 0 and {r[0]: r[3] for r in same.rows}["T1|SEC1"] == 1100.0


# ---------- the whole path ----------

PID = "B1|SEC-26238"
ROW = ("SELECT p.quantity, p.pv_rub, p.pnl, p.no_pnl FROM run_position p WHERE p.run_id = %s AND p.position_id = %s")


class Day:
    """The intraday run of 16.10 on the stub hub, before and after files of trades are dropped."""

    def __init__(self, hub, tmp_path, store_url, lots=None):
        self.s = cfg(tmp_path, store_url, hub)
        self.url, self.tmp, self.hub = store_url, tmp_path, hub
        if lots is None:
            put(tmp_path, "positions_20261016.csv")
        else:
            self.positions("positions_20261016.csv", lots)
        self.clock = {"t": at(FRI, 10, 6)}
        self.ap = Autopilot(settings_loader=lambda: self.s, now=lambda: self.clock["t"])
        publish(hub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)
        self.ap.run_cycle()
        self.base = self.row() if lots is None else None

    def positions(self, name, lots):
        """A positions file of book B1 with a row per lot: [(position_id, instrument, isin, quantity)]."""
        (self.tmp / "in").mkdir(exist_ok=True)
        head = ("book_id;position_id;asset_class;source_instrument_id;source_instrument_name;isin;ccy;quantity;"
                "face_amount_ccy\n")
        (self.tmp / "in" / name).write_text(head + "".join(
            f"B1;{pid};bond;{ins};;{isin};RUB;{q:.0f};{q * 1000:.0f}\n" for pid, ins, isin, q in lots), encoding="utf-8")

    def latest_id(self, rtype="INTRADAY", on=FRI):
        with store.connect(self.url) as con:
            return con.execute("SELECT max(run_id) FROM run WHERE run_type = %s AND business_date = %s"
                               " AND status = 'complete'", (rtype, on)).fetchone()[0]

    def row(self, pid=PID, rid=None):
        with store.connect(self.url) as con:
            q, pv, pnl, no = con.execute(ROW, (rid or self.latest_id(), pid)).fetchone()
        return {"q": q, "pv": pv, "pnl": pnl, "no_pnl": no}

    def drop(self, *lines, hhmm=(10, 12), name="trades_20261016_1100.csv"):
        (self.tmp / "in" / name).write_text(HEAD + "".join(lines), encoding="utf-8")
        self.clock["t"] = at(FRI, *hhmm)
        return steps(self.ap.run_cycle())

    def cycle(self, hh, mm):
        self.clock["t"] = at(FRI, hh, mm)
        return steps(self.ap.run_cycle())


@pytest.fixture
def day(stub, tmp_path, store_url):
    return Day(stub, tmp_path, store_url)


def test_a_sale_to_zero_keeps_the_days_move_up_to_the_sale(day):
    b = day.base
    assert b["q"] == 12000 and b["pnl"]["new_trades"] == 0.0 and b["pnl"]["extra"]["total"] != 0
    dirty_now = b["pv"] / 12000
    price, acc = 61.0, 50_000.0
    by = day.drop(trade_line("S1", "sell", 12000, price, acc))
    assert by["intraday"][0] == "ok" and "private inputs changed" in by["intraday"][1], by["intraday"]
    r = day.row()
    assert r["q"] == 0 and r["pv"] == 0
    cost = 12000 * price / 100 * 1000 + acc
    expected = cost - 12000 * dirty_now
    p = r["pnl"]
    assert p["new_trades"] == pytest.approx(expected)
    assert p["extra"]["start_quantity"] == 12000
    # the start-of-day position: the same components as without trades; the total adds the sale's PnL
    for k in ("carry", "rolldown", "rates", "spread", "other"):
        assert p[k] == pytest.approx(b["pnl"][k])
    assert p["extra"]["total"] == pytest.approx(b["pnl"]["extra"]["total"] + expected)
    [t] = p["extra"]["trades"]
    assert t["trade_id"] == "S1" and t["flag"] is None and t["pnl_rub"] == pytest.approx(expected)
    assert t["cost_rub"] == pytest.approx(cost)


def test_a_purchase_is_measured_from_its_price(day):
    b = day.base
    dirty_now = b["pv"] / 12000
    day.drop(trade_line("B9", "buy", 1000, 60.0, 20_000.0))
    r = day.row()
    assert r["q"] == 13000
    expected = 1000 * dirty_now - (1000 * 0.60 * 1000 + 20_000.0)
    assert r["pnl"]["new_trades"] == pytest.approx(expected)
    # the day's move of the start-of-day position is on 12000 bonds, not on the 13000 held now
    for k in ("carry", "rolldown", "rates", "spread", "other"):
        assert r["pnl"][k] == pytest.approx(b["pnl"][k])
    assert r["pnl"]["extra"]["total"] == pytest.approx(b["pnl"]["extra"]["total"] + expected)


def test_a_partial_sale_and_a_later_purchase_in_one_row(day):
    b = day.base
    d = b["pv"] / 12000
    day.drop(trade_line("S1", "sell", 2000, 62.0, 10_000.0, hhmm="10:30"),
             trade_line("B1", "buy", 500, 60.5, 5_000.0, hhmm="11:00"))
    r = day.row()
    assert r["q"] == 10500
    sale = (2000 * 0.62 * 1000 + 10_000.0) - 2000 * d
    buy = 500 * d - (500 * 0.605 * 1000 + 5_000.0)
    assert r["pnl"]["new_trades"] == pytest.approx(sale + buy)
    assert {t["trade_id"] for t in r["pnl"]["extra"]["trades"]} == {"S1", "B1"}


def test_a_new_position_has_only_its_trades(day):
    b = day.base
    day.drop(trade_line("N1", "buy", 100, 59.0, 1_000.0, pid="B1|NEW", instr="SEC-29008", hhmm="10:30"))
    with store.connect(day.url) as con:
        rid = day.latest_id()
        row = con.execute(ROW, (rid, "B1|NEW")).fetchone()
    assert row is not None
    q, pv, pnl, no = row
    assert q == 100 and no is None
    assert all(pnl[k] == 0 for k in ("carry", "rolldown", "rates", "spread", "other", "fx_gross"))
    assert pnl["new_trades"] == pytest.approx(pv - (100 * 0.59 * 1000 + 1_000.0))
    assert pnl["extra"]["start_quantity"] == 0 and b["q"] == 12000


def test_a_trade_without_a_price_changes_the_quantity_not_the_pnl(day):
    b = day.base
    day.drop(trade_line("S1", "sell", 1000))
    r = day.row()
    assert r["q"] == 11000 and r["pnl"]["new_trades"] == 0.0
    assert r["pnl"]["extra"]["trades"][0]["flag"] == "no_trade_price"
    assert r["pnl"]["extra"]["total"] == pytest.approx(b["pnl"]["extra"]["total"])


def test_a_trade_without_accrued_uses_the_valuations_and_says_so(day):
    day.drop(trade_line("B1", "buy", 1000, 60.0))
    t = day.row()["pnl"]["extra"]["trades"][0]
    assert t["flag"] == "accrued_assumed" and t["pnl_rub"] != 0


def test_a_currency_other_than_the_bonds_is_zero(day):
    day.drop(trade_line("B1", "buy", 1000, 60.0, 1.0, ccy="USD"))
    r = day.row()
    assert r["pnl"]["new_trades"] == 0.0 and r["pnl"]["extra"]["trades"][0]["flag"] == "units_mismatch"
    assert r["q"] == 13000   # a quantity is the number of bonds whatever the trade's currency says


def test_the_book_and_the_run_hold_the_start_pnl_plus_the_new_trades_once(day):
    with store.connect(day.url) as con:
        rid0 = day.latest_id()
        t0 = con.execute("SELECT totals FROM run WHERE run_id = %s", (rid0,)).fetchone()[0]
    day.drop(trade_line("S1", "sell", 2000, 62.0, 10_000.0), trade_line("B1", "buy", 500, 60.5, 5_000.0, hhmm="11:00"))
    rid = day.latest_id()
    with store.connect(day.url) as con:
        t1 = con.execute("SELECT totals FROM run WHERE run_id = %s", (rid,)).fetchone()[0]
        rows = con.execute("SELECT pnl FROM run_position WHERE run_id = %s AND pnl IS NOT NULL AND book_id = 'B1'",
                           (rid,)).fetchall()
    nts = sum(p[0]["new_trades"] for p in rows)
    assert nts != 0
    assert t1["B1"]["pnl"]["new_trades"] == pytest.approx(nts)
    assert t1["B1"]["pnl"]["total"] - t0["B1"]["pnl"]["total"] == pytest.approx(nts)
    assert t1["_total"]["pnl"]["total"] - t0["_total"]["pnl"]["total"] == pytest.approx(nts)
    from desk.publish import summary as sm
    f = sm.pnl_factors({k: v for k, v in rows[0][0].items() if k != "extra"})
    assert f["trading"] == pytest.approx(rows[0][0]["new_trades"])


def test_the_same_trades_again_make_no_new_run_and_a_new_trade_does(day):
    day.drop(trade_line("S1", "sell", 2000, 62.0, 10_000.0))
    rid = day.latest_id()
    by = day.cycle(10, 14)
    assert by["intraday"][1].startswith("up to date") and day.latest_id() == rid
    by = day.drop(trade_line("S1", "sell", 2000, 62.0, 10_000.0), trade_line("S2", "sell", 1000, 62.5, 5_000.0,
                                                                              hhmm="10:50"),
                  hhmm=(10, 20), name="trades_20261016_1105.csv")
    assert by["intraday"][0] == "ok" and day.latest_id() > rid
    r = day.row()
    assert r["q"] == 9000 and {t["trade_id"] for t in r["pnl"]["extra"]["trades"]} == {"S1", "S2"}   # no double count


# ---------- the closes ----------

MON = date(2026, 10, 19)


def lineage(url, rid):
    with store.connect(url) as con:
        return con.execute("SELECT lineage->'intraday_trades' FROM run WHERE run_id = %s", (rid,)).fetchone()[0]


def test_the_evening_preliminary_close_has_the_days_trades(day):
    day.drop(trade_line("S1", "sell", 1000, 62.0, 5_000.0))
    publish(day.hub, FRI, "eod_preliminary", "2026-10-16T16:45:00Z", 3)
    by = day.cycle(19, 50)
    assert by["intraday"] == ("ok", "16.10 · закрытие предв.: run 4 (new cut)"), by["intraday"]
    rid = day.latest_id("EOD")
    r = day.row(rid=rid)
    assert r["q"] == 11000 and r["pnl"]["new_trades"] != 0 and r["pnl"]["extra"]["start_quantity"] == 12000
    assert lineage(day.url, rid)["applied"] == 1
    assert day.cycle(20, 40)["intraday"][1].startswith("up to date")   # the next cycle: the same inputs
    assert day.latest_id("EOD") == rid
    from desk import recompute
    assert recompute.current_day(day.s, day.url, FRI)["runs"] == []
    # a trade that comes after the close run: recompute makes the preliminary close again, once
    (day.tmp / "in" / "trades_20261016_2100.csv").write_text(
        HEAD + trade_line("S1", "sell", 1000, 62.0, 5_000.0) + trade_line("S2", "sell", 500, 62.5, 2_000.0, hhmm="20:50"),
        encoding="utf-8")
    with store.connect(day.url) as con:
        scan.scan(con, day.s)
    first = recompute.current_day(day.s, day.url, FRI)
    assert [x["business_date"] for x in first["runs"]] == ["2026-10-16"] and first["failed"] == []
    assert day.row(rid=day.latest_id("EOD"))["q"] == 10500
    assert recompute.current_day(day.s, day.url, FRI)["runs"] == []


def test_the_final_close_has_the_trades_until_the_report_of_the_day_comes(day):
    day.drop(trade_line("S1", "sell", 1000, 62.0, 5_000.0))
    publish(day.hub, FRI, "eod_preliminary", "2026-10-16T16:45:00Z", 3)
    day.cycle(19, 50)
    publish(day.hub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    day.clock["t"] = at(MON, 8, 30)
    by = steps(day.ap.run_cycle())
    assert by["eod"][0] == "ok" and "2026-10-16 run" in by["eod"][1], by["eod"]
    final = day.latest_id("EOD")
    r = day.row(rid=final)
    assert r["q"] == 11000 and r["pnl"]["new_trades"] != 0
    assert lineage(day.url, final)["applied"] == 1
    day.clock["t"] = at(MON, 8, 40)
    assert steps(day.ap.run_cycle())["eod"][1].startswith("up to date")   # no endless reruns
    assert day.latest_id("EOD") == final
    # the report as of the end of 16.10 (the start of 19.10): the positions are the report's, the trades still give the
    # PnL (they are the day's, and the day's PnL is not the report's); the difference is flagged, not hidden
    nt_before = r["pnl"]["new_trades"]
    src = day.tmp / "in" / "positions_20261016.csv"
    (day.tmp / "in" / "positions_20261019.csv").write_text(src.read_text(encoding="utf-8-sig"), encoding="utf-8")
    day.clock["t"] = at(MON, 8, 50)
    by = steps(day.ap.run_cycle())
    assert by["eod"][0] == "ok" and "positions changed" in by["eod"][1], by["eod"]
    again = day.latest_id("EOD")
    assert again > final
    r = day.row(rid=again)
    assert r["q"] == 12000 and r["pnl"]["new_trades"] == pytest.approx(nt_before) and nt_before != 0
    assert r["pnl"]["extra"]["start_quantity"] == 12000
    lin = lineage(day.url, again)
    assert lin["applied"] == 1 and lin["gaps"] == {"B1|SU26238RMFS4": 1000.0}   # report 12000 against 12000 - 1000 sold
    day.clock["t"] = at(MON, 9, 0)
    assert steps(day.ap.run_cycle())["eod"][1].startswith("up to date") and day.latest_id("EOD") == again
    with store.connect(day.url) as con:
        assert {x["trade_id"]: x["intraday"] for x in trades.view(con, FRI)["rows"]} == {"S1": "applied"}


def test_a_report_that_matches_the_start_plus_the_trades_has_no_gap(day):
    day.drop(trade_line("S1", "sell", 1000, 62.0, 5_000.0))
    publish(day.hub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    day.clock["t"] = at(MON, 8, 30)
    day.ap.run_cycle()
    before = day.row(rid=day.latest_id("EOD"))
    (day.tmp / "in" / "positions_20261019.csv").write_text(
        (day.tmp / "in" / "positions_20261016.csv").read_text(encoding="utf-8-sig").replace(";12000;12000000;",
                                                                                         ";11000;11000000;"),
        encoding="utf-8")
    day.clock["t"] = at(MON, 8, 50)
    day.ap.run_cycle()
    rid = day.latest_id("EOD")
    r = day.row(rid=rid)
    assert r["q"] == 11000 and r["pnl"]["new_trades"] == pytest.approx(before["pnl"]["new_trades"]) != 0
    assert lineage(day.url, rid)["gaps"] == {}
    for k in ("carry", "rolldown", "rates", "spread", "other"):
        assert r["pnl"][k] == pytest.approx(before["pnl"][k])


def test_a_report_a_day_late_the_next_days_book_holds_both_days_trades(day, monkeypatch):
    """No report valid 16.10 yet on Monday: Monday's book is the set of 15.10 + Friday's trades + Monday's; once the
    report of Friday comes Friday's trades are not applied again."""
    day.drop(trade_line("S1", "sell", 1000, 62.0, 5_000.0))
    publish(day.hub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    publish(day.hub, MON, "intraday", "2026-10-19T07:00:00Z", 4)
    (day.tmp / "in" / "trades_20261019_1100.csv").write_text(
        HEAD + trade_line("M1", "sell", 500, 62.0, 2_000.0).replace("2026-10-16", "2026-10-19"), encoding="utf-8")
    day.clock["t"] = at(MON, 10, 6)
    by = steps(day.ap.run_cycle())
    assert by["intraday"][0] == "ok", by["intraday"]
    rid = day.latest_id(on=MON)
    r = day.row(rid=rid)
    assert r["q"] == 10500                       # 12000 - 1000 (Friday) - 500 (Monday)
    assert r["pnl"]["extra"]["start_quantity"] == 11000   # Friday's trade is in the start of Monday
    assert [t["trade_id"] for t in r["pnl"]["extra"]["trades"]] == ["M1"]
    lin = lineage(day.url, rid)
    assert lin["applied"] == 1 and lin["history"] == 1
    day.clock["t"] = at(MON, 10, 8)
    assert steps(day.ap.run_cycle())["intraday"][1].startswith("up to date")
    # the report of Friday: the 11000 of the report; Friday's trade is not applied again
    (day.tmp / "in" / "positions_20261019.csv").write_text(
        (day.tmp / "in" / "positions_20261016.csv").read_text(encoding="utf-8-sig").replace(";12000;12000000;",
                                                                                         ";11000;11000000;"),
        encoding="utf-8")
    day.clock["t"] = at(MON, 10, 9)
    day.ap.run_cycle()
    r = day.row(rid=day.latest_id(on=MON))
    assert r["q"] == 10500
    assert lineage(day.url, day.latest_id(on=MON))["history"] == 0


def test_a_start_of_day_without_pnl_keeps_no_pnl_and_the_trades_beside_it(day, monkeypatch):
    real = nt.portfolio_pnl

    def unpriced(prev, ctx, start, pids, carried):
        out = real(prev, ctx, start, pids, carried)
        for o in out.values():
            o.pnl, o.no_pnl = None, NoPnl("not_priced_prev", "test: no price on the previous close")
        return out

    monkeypatch.setattr(nt, "portfolio_pnl", unpriced)
    day.drop(trade_line("S1", "sell", 1000, 62.0, 5_000.0))
    r = day.row()
    assert r["q"] == 11000 and r["pnl"] is None
    assert r["no_pnl"]["reason"] == "not_priced_prev" and r["no_pnl"]["new_trades"] != 0
    assert r["no_pnl"]["trades"][0]["trade_id"] == "S1" and r["no_pnl"]["start_quantity"] == 12000


# ---------- a report with a row per lot ----------

def _lot_inputs(rows, valid):
    import test_trades_intraday as tti

    base = tti._inputs()
    return store.PositionInputs(**{**base.__dict__, "rows": rows, "valid_dates": {"T1": valid, "T2": valid},
                                   "private_inputs_hash": "report", "extra": {}})


def _lots_prev(monkeypatch, rows, extra=None, h="prev"):
    import test_trades_intraday as tti

    prev = store.PositionInputs(**{**tti._inputs().__dict__, "rows": rows, "extra": extra or {},
                                   "private_inputs_hash": h})
    monkeypatch.setattr(store, "positions_for", lambda con, d, books=None: prev)


DAY = date(2026, 11, 3)


def test_a_purchase_of_a_bond_held_is_a_new_lot_of_the_report(monkeypatch):
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|T9", instrument_id="SEC1", quantity="500")]})
    _lots_prev(monkeypatch, [("T1|L1", "T1", "SEC1", 1000.0)])
    out, summary = trades.apply_intraday(None, DAY, _lot_inputs(
        [("T1|L1", "T1", "SEC1", 1000.0), ("T1|T9", "T1", "SEC1", 500.0)], DAY))
    assert summary["gaps"] == {}                               # by bond, not by position id
    assert {r[0]: r[3] for r in out.rows} == {"T1|L1": 1000.0, "T1|T9": 500.0}   # the report's quantities
    c = summary["rows"]["T1|L1"]                                 # the lot of the start of the day keeps the trades
    assert (c["start"], c["final"], c["new"], sorted(c["members"])) == (1000.0, 1500.0, False, ["T1|L1", "T1|T9"])
    assert [t["row"] for t in summary["trades"]] == ["T1|L1"]
    assert {r[0]: r[3] for r in summary["start_inputs"].rows} == {"T1|L1": 1000.0}


def test_a_part_sale_with_two_lots_the_report_takes_first_in_first_out(monkeypatch):
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|L2", side="sell", quantity="700")]})
    _lots_prev(monkeypatch, [("T1|L1", "T1", "SEC1", 1000.0), ("T1|L2", "T1", "SEC1", 500.0)])
    out, summary = trades.apply_intraday(None, DAY, _lot_inputs(
        [("T1|L1", "T1", "SEC1", 300.0), ("T1|L2", "T1", "SEC1", 500.0)], DAY))
    assert summary["gaps"] == {}
    c = summary["rows"]["T1|L1"]                  # the lot of the start of the day with the smallest id keeps the trades
    assert (c["start"], c["final"]) == (1500.0, 800.0) and c["members"] == {"T1|L1": 300.0, "T1|L2": 500.0}
    assert {r[0]: r[3] for r in summary["start_inputs"].rows if r[1] == "T1"} == {"T1|L1": 1500.0}   # both lots, one row
    assert {r[0]: r[3] for r in out.rows if r[1] == "T1"} == {"T1|L1": 300.0, "T1|L2": 500.0}


def test_a_new_bond_with_another_position_id_in_the_report(monkeypatch):
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|X1", instrument_id="SEC2", quantity="10")]})
    _lots_prev(monkeypatch, [("T1|L1", "T1", "SEC1", 1000.0), ("T2|SEC2", "T2", "SEC2", 5.0)])
    out, summary = trades.apply_intraday(None, DAY, _lot_inputs(
        [("T1|L1", "T1", "SEC1", 1000.0), ("T1|Y1", "T1", "SEC2", 10.0)], DAY))
    assert summary["gaps"] == {}
    c = summary["rows"]["T1|Y1"]
    assert (c["start"], c["final"], c["new"]) == (0.0, 10.0, True)
    assert [t["row"] for t in summary["trades"]] == ["T1|Y1"] and "T1|X1" not in {r[0] for r in out.rows}


def test_a_book_with_a_report_and_no_earlier_set(monkeypatch):
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(quantity="200", instrument_id="SEC1")]})
    monkeypatch.setattr(store, "positions_for", lambda con, d, books=None: store.PositionInputs(
        [], [], {}, datetime_now(), "none"))
    out, summary = trades.apply_intraday(None, DAY, _lot_inputs([("T1|SEC1", "T1", "SEC1", 200.0)], DAY))
    c = summary["rows"]["T1|SEC1"]
    assert (c["start"], c["final"], c["new"]) == (0.0, 200.0, True) and summary["gaps"] == {}
    assert {r[0]: r[3] for r in out.rows} == {"T1|SEC1": 200.0}


def datetime_now():
    from datetime import UTC, datetime

    return datetime(2026, 11, 3, tzinfo=UTC)


def test_the_set_the_start_of_the_day_comes_from_is_in_the_inputs_hash(monkeypatch):
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(quantity="200")]})
    hashes = []
    for h in ("prev-v1", "prev-v1", "prev-v2"):   # the report of the day before corrected: another hash
        _lots_prev(monkeypatch, [("T1|SEC1", "T1", "SEC1", 1000.0)], h=h)
        hashes.append(trades.apply_intraday(None, DAY, _lot_inputs([("T1|SEC1", "T1", "SEC1", 1200.0)], DAY))[0]
                      .private_inputs_hash)
    assert hashes[0] == hashes[1] != hashes[2]


def test_trades_older_than_the_window_are_said_to_be_dropped(monkeypatch):
    import test_trades_intraday as tti

    old = tti._inputs()
    old = store.PositionInputs(**{**old.__dict__, "valid_dates": {"T1": date(2026, 10, 20), "T2": date(2026, 10, 20)}})
    _days(monkeypatch, {"2026-10-22": [tti._trade(trade_id="O1", trade_date="2026-10-22")],
                        "2026-10-29": [tti._trade(trade_id="N1", trade_date="2026-10-29", quantity="100")]})
    out, summary = trades.apply_intraday(None, DAY, old)
    assert summary["history_cut"] == {"T1": {"report": "2026-10-20", "dropped": 1}}
    assert summary["history"] == 1 and {r[0]: r[3] for r in out.rows}["T1|SEC1"] == 1100.0


def _book_sum(day, rid, pids):
    with store.connect(day.url) as con:
        rows = con.execute("SELECT position_id, quantity, pnl FROM run_position WHERE run_id = %s AND position_id"
                           " = ANY(%s)", (rid, pids)).fetchall()
    return sum((p["extra"]["total"] if p else 0.0) for _, _, p in rows), sum(q for _, q, _ in rows)


def _lot_scenario(stub, tmp_path, store_url, start, trade, report, pids_carried, pids_report, buy=True):
    """The same trades on the same final market, before the report of the day (carried book: the fold) and after it
    (the report's rows per lot): the PnL of the bond and the book must be the same, whatever the lots."""
    d = Day(stub, tmp_path, store_url, lots=start)
    d.drop(trade_line(*trade[0], **trade[1]))
    publish(d.hub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    d.clock["t"] = at(MON, 8, 30)
    d.ap.run_cycle()
    carried = d.latest_id("EOD")
    before = _book_sum(d, carried, pids_carried)
    d.positions("positions_20261019.csv", report)
    d.clock["t"] = at(MON, 8, 50)
    d.ap.run_cycle()
    rid = d.latest_id("EOD")
    assert rid > carried
    after = _book_sum(d, rid, pids_report)
    assert after[0] == pytest.approx(before[0]) and after[0] != 0
    assert after[1] == pytest.approx(before[1])      # the quantities are the report's
    with store.connect(d.url) as con:
        assert con.execute("SELECT count(*) FROM run_position WHERE run_id = %s AND pnl->'extra'->'trades' IS NOT NULL",
                           (rid,)).fetchone()[0] == 1
    assert lineage(d.url, rid)["gaps"] == {}
    return d, rid


S = "SEC-26238"
LOT_ISIN = "RU000A1038V6"


def test_lots_a_purchase_of_a_bond_held_as_a_new_lot_has_the_same_pnl(stub, tmp_path, store_url):
    _lot_scenario(stub, tmp_path, store_url, [("B1|L1", S, LOT_ISIN, 12000)], (("B9", "buy", 500, 60.0, 20_000.0),
                                                                          {"pid": "B1|T9", "iid": ""}),
                  [("B1|L1", S, LOT_ISIN, 12000), ("B1|T9", S, LOT_ISIN, 500)], ["B1|L1"], ["B1|L1", "B1|T9"])


def test_lots_a_part_sale_with_two_lots_the_report_takes_fifo_has_the_same_pnl(stub, tmp_path, store_url):
    d, rid = _lot_scenario(stub, tmp_path, store_url, [("B1|L1", S, LOT_ISIN, 12000), ("B1|L2", S, LOT_ISIN, 3000)],
                           (("S1", "sell", 4000, 62.0, 10_000.0), {"pid": "B1|L2", "iid": ""}),
                           [("B1|L1", S, LOT_ISIN, 8000), ("B1|L2", S, LOT_ISIN, 3000)], ["B1|L1", "B1|L2"],
                           ["B1|L1", "B1|L2"])
    with store.connect(d.url) as con:   # the bond's PnL by the lots' shares of the report: 8000 / 3000
        rows = {r[0]: r for r in con.execute(
            "SELECT position_id, pv_rub, pnl->'extra', pnl->>'new_trades', pnl->>'carry', pnl->>'rates'"
            " FROM run_position WHERE run_id = %s AND position_id IN ('B1|L1', 'B1|L2')", (rid,))}
    l1, l2 = rows["B1|L1"], rows["B1|L2"]
    assert l1[2]["start_quantity"] == 15000 and "pnl_on_row" not in l1[2] and l1[2]["trades"][0]["trade_id"] == "S1"
    assert l2[2]["pnl_on_row"] == "B1|L1" and "trades" not in l2[2] and "start_quantity" not in l2[2]
    for i in (3, 4, 5):   # the new trades, the carry and the rates are split 8000 : 3000
        assert float(l1[i]) / float(l2[i]) == pytest.approx(8000 / 3000)
    assert l2[2]["total"] / l1[2]["total"] == pytest.approx(3000 / 8000)
    assert l2[2]["total"] / l2[1] == pytest.approx(l1[2]["total"] / l1[1])   # the PnL per unit of PV is the same


def test_lots_a_new_bond_with_another_position_id_has_the_same_pnl(stub, tmp_path, store_url):
    new = ("N1", "buy", 100, 59.0, 1_000.0)
    d = Day(stub, tmp_path, store_url, lots=[("B1|L1", S, LOT_ISIN, 12000)])
    d.drop(trade_line(*new, pid="B1|N1", instr="SEC-29008", iid=""))
    publish(d.hub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    d.clock["t"] = at(MON, 8, 30)
    d.ap.run_cycle()
    carried = d.latest_id("EOD")
    before = _book_sum(d, carried, ["B1|N1", "B1|L1"])
    d.positions("positions_20261019.csv", [("B1|L1", S, LOT_ISIN, 12000), ("B1|N2", "SEC-29008", "RU000A0JV4P3", 100)])
    d.clock["t"] = at(MON, 8, 50)
    d.ap.run_cycle()
    rid = d.latest_id("EOD")
    after = _book_sum(d, rid, ["B1|N2", "B1|L1"])
    assert after[0] == pytest.approx(before[0]) and after[1] == pytest.approx(before[1])
    assert lineage(d.url, rid)["gaps"] == {}
    with store.connect(d.url) as con:
        assert con.execute("SELECT count(*) FROM run_position WHERE run_id = %s AND position_id = 'B1|N1'",
                           (rid,)).fetchone()[0] == 0


def test_the_carried_book_with_two_lots_keeps_the_pnl_of_both(stub, tmp_path, store_url):
    """No report of the day: the sale is folded on one lot (it may go negative), the bond's PnL is the same as the
    sum of the lots' start-of-day PnL plus the trade's."""
    d = Day(stub, tmp_path, store_url, lots=[("B1|L1", S, LOT_ISIN, 12000), ("B1|L2", S, LOT_ISIN, 3000)])
    base = _book_sum(d, d.latest_id(), ["B1|L1", "B1|L2"])
    d.drop(trade_line("S1", "sell", 4000, 62.0, 10_000.0, pid="B1|L2", iid=""))
    after = _book_sum(d, d.latest_id(), ["B1|L1", "B1|L2"])
    assert after[1] == 11000
    with store.connect(d.url) as con:
        nt_ = con.execute("SELECT sum((pnl->>'new_trades')::float8) FROM run_position WHERE run_id = %s",
                          (d.latest_id(),)).fetchone()[0]
    assert nt_ != 0 and after[0] == pytest.approx(base[0] + nt_)


def test_a_corrected_report_of_the_day_before_makes_the_run_of_the_day_again_once(stub, tmp_path, store_url):
    d = Day(stub, tmp_path, store_url, lots=[("B1|L1", S, LOT_ISIN, 12000)])
    d.drop(trade_line("S1", "sell", 1000, 62.0, 5_000.0, pid="B1|L1"))
    publish(d.hub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    d.clock["t"] = at(MON, 8, 30)
    d.ap.run_cycle()
    d.positions("positions_20261019.csv", [("B1|L1", S, LOT_ISIN, 11000)])
    d.clock["t"] = at(MON, 8, 50)
    d.ap.run_cycle()
    final = d.latest_id("EOD")
    d.positions("positions_20261016_2.csv", [("B1|L1", S, LOT_ISIN, 12500)])   # the report of 15.10 corrected
    d.clock["t"] = at(MON, 9, 10)
    by = steps(d.ap.run_cycle())
    assert by["eod"][0] == "ok" and "private inputs changed" in by["eod"][1], by["eod"]
    again = d.latest_id("EOD")
    assert again > final
    d.clock["t"] = at(MON, 9, 20)
    assert steps(d.ap.run_cycle())["eod"][1].startswith("up to date") and d.latest_id("EOD") == again


def test_split_pnl_adds_up_to_the_bonds_pnl_to_the_last_digit():
    from datetime import date as d_

    from desk.valuation.fallback import SimplePnl

    p = SimplePnl(secid="S", quantity=1500.0, prev_date=d_(2026, 10, 15), as_of=d_(2026, 10, 16),
                  prev_valuation_date=d_(2026, 10, 15), valuation_date=d_(2026, 10, 16), z0_bp=10.0, z1_bp=11.0,
                  v0=1.5e6, v_carry=1.5e6, v_roll=1.5e6, v_rates=1.5e6, v_spread=1.5e6, v1=1.49e6, received_rub=7.0,
                  carry=100.0, rolldown=3.3, rates=-41.7, spread=0.1, other=-0.3, total=-12345.678, identity_error=1e-6,
                  rates_linear=-40.0, price_basis=("a", "b"), new_trades=-5000.5, start_quantity=1000.0,
                  trades=[{"trade_id": "T"}], rates_linear_curves={"RUB OFZ": {"1Y": 3.0, "2Y": -7.0}},
                  prev_inputs={"pv_rub": 1.5e6, "quantity": 1500.0, "clean_pct": 51.45, "risk": {"RUB OFZ": {"1Y": 2.0}}},
                  curve_moves_bp={"RUB OFZ": {"1Y": 5.0}})
    ws = [0.5, 0.3, 0.2]
    a, b, c = nt.split_pnl(p, ws)
    for f in ("carry", "total", "new_trades", "v0", "rates", "identity_error"):
        assert math.fsum(getattr(x, f) for x in (a, b, c)) == pytest.approx(getattr(p, f), abs=1e-12)
    assert b.total == pytest.approx(p.total * 0.3) and c.new_trades == pytest.approx(p.new_trades * 0.2)
    assert math.fsum(x.rates_linear_curves["RUB OFZ"]["2Y"] for x in (a, b, c)) == pytest.approx(-7.0)
    assert [x.prev_inputs["pv_rub"] for x in (b, c)] == [pytest.approx(0.45e6), pytest.approx(0.3e6)]
    assert b.prev_inputs["clean_pct"] == 51.45 and b.curve_moves_bp == p.curve_moves_bp   # per bond: the same
    assert b.z0_bp == 10.0                                                                # bp are not amounts


def test_lots_in_different_units_are_said_and_their_trades_are_not_in_the_pnl(monkeypatch):
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|L2", instrument_id="SEC1", quantity="100")]})
    _lots_prev(monkeypatch, [("T1|L1", "T1", "SEC1", 1000.0)])
    # the report keeps the second lot as a face amount
    rep = _lot_inputs([("T1|L1", "T1", "SEC1", 1000.0), ("T1|L2", "T1", "SEC1", None)], DAY)
    rep.extra["T1|L2"] = {"asset_class": "bond", "ccy": "RUB", "face_amount_ccy": 100_000.0}
    out, summary = trades.apply_intraday(None, DAY, rep)
    assert summary["mixed_units"] == ["T1|SEC1"] and summary["trades"] == [] and summary["rows"] == {}
    assert summary["skipped"] == {"mixed_units": 1} and summary["skipped_ids"] == {"TR1": "mixed_units"}
    assert summary["applied"] == 0 and summary["versions"] == {}
    assert out.extra["T1|L2"]["x"] == {"trades_mixed_units": True}
    assert trades._intraday_state(tti._trade(), DAY, {"run_type": "EOD", "versions": {},
                                                      "skipped_ids": summary["skipped_ids"]}, True) == "mixed_units"


def test_a_stale_book_with_no_trades_in_the_window_costs_one_read(monkeypatch):
    import test_trades_intraday as tti

    calls = []

    def rows(con, ds, on):
        calls.append(on)
        return store.DatasetInputs(ds, on, [], [])

    monkeypatch.setattr(store, "dataset_rows", rows)
    asked = []
    monkeypatch.setattr(trades, "_trade_sets", lambda con, lo, hi: asked.append((lo, hi)) or [])
    old = store.PositionInputs(**{**tti._inputs().__dict__, "valid_dates": {"T1": date(2026, 9, 1),
                                                                            "T2": date(2026, 9, 1)}})
    out, summary = trades.apply_intraday(None, DAY, old)
    assert out is old and calls == [DAY] and len(asked) == 1 and summary["history_cut"] == {}


def test_a_closed_book_with_an_old_empty_set_sets_no_window_and_costs_no_reads(monkeypatch):
    """A book that holds no positions (an old empty set) does not widen the window of the trades of the days before:
    other books' trades of past days are neither read nor looked up."""
    import test_trades_intraday as tti

    base = tti._inputs()
    rows = [r for r in base.rows if r[1] == "T1"]   # T2 is closed: no rows
    inp = store.PositionInputs(**{**base.__dict__, "rows": rows, "valid_dates": {"T1": date(2026, 11, 2),
                                                                                "T2": date(2026, 9, 1)}})
    calls, asked = [], []
    monkeypatch.setattr(store, "dataset_rows", lambda con, ds, on: calls.append(on) or store.DatasetInputs(
        ds, on, [], []))
    monkeypatch.setattr(trades, "_trade_sets", lambda con, lo, hi: asked.append((lo, hi)) or [])
    out, summary = trades.apply_intraday(None, DAY, inp)
    assert out is inp and calls == [DAY] and asked == [] and summary["history_cut"] == {}


def test_trades_older_than_the_window_are_counted_by_one_aggregate_without_reading_rows(monkeypatch):
    import test_trades_intraday as tti

    read = []
    monkeypatch.setattr(store, "dataset_rows", lambda con, ds, on: read.append(on) or store.DatasetInputs(
        ds, on, [], []))
    monkeypatch.setattr(trades, "_trade_sets", lambda con, lo, hi: [(date(2026, 10, 22), "T1", 3),
                                                                    (date(2026, 10, 23), "T2", 1)])
    old = store.PositionInputs(**{**tti._inputs().__dict__, "valid_dates": {"T1": date(2026, 10, 20),
                                                                            "T2": date(2026, 10, 23)}})
    _, summary = trades.apply_intraday(None, DAY, old)
    assert summary["history_cut"] == {"T1": {"report": "2026-10-20", "dropped": 3}}   # T2's report is after its date
    assert read == [DAY]                                                              # the past dates are not read


def _lot_outcome(vq, core=True, pnl=None):
    from desk.orchestrator import Outcome

    v = SimpleNamespace(quantity=vq, valuation_date=date(2026, 10, 16), z_spread_bp=1.0, price_basis="p")
    return Outcome("S", SimpleNamespace() if core else None, None, valuation=v, pnl=pnl)


def _attach_case(monkeypatch, core):
    """Two lots of one bond in a book with the report (8000 / 3000): which of them are valued with terms."""
    prev = SimpleNamespace(business_date=date(2026, 10, 15))
    ctx = SimpleNamespace(business_date=date(2026, 10, 16))
    start_o = _lot_outcome(11000.0)
    start_o.pnl = nt._opened_today(prev, ctx, start_o, 0.0, [])
    start_o.pnl = nt.replace(start_o.pnl, total=-1000.0, carry=-1000.0)
    monkeypatch.setattr(nt, "portfolio_pnl", lambda *a: {"L1": start_o})
    monkeypatch.setattr(nt, "trade_pnl", lambda *a: {"trade_id": "S", "pnl_rub": 50.0, "flag": None})
    lots = {"L1": _lot_outcome(8000.0, core["L1"]), "L2": _lot_outcome(3000.0, core["L2"])}
    for o in lots.values():
        o.pnl = "regular flash"
    bp = SimpleNamespace(position_ids=list(lots))
    summary = {"trades": [{"row": "L1", "trade_id": "S"}],
               "rows": {"L1": {"start": 11000.0, "book": "B1", "bond": "S", "members": {"L1": 8000.0, "L2": 3000.0}}}}
    n = nt.attach(prev, ctx, SimpleNamespace(), bp, list(lots.values()), summary, None)
    return lots, n


def test_lots_valued_in_different_ways_are_not_split_and_say_so(monkeypatch):
    lots, n = _attach_case(monkeypatch, {"L1": True, "L2": False})
    assert n["mixed_valuation"] == ["B1|S"] and n["rows"] == {"L1": "trades_mixed_valuation",
                                                              "L2": "trades_mixed_valuation"}
    assert lots["L1"].pnl.total == pytest.approx(-950.0) and lots["L1"].pnl.new_trades == 50.0   # the whole, no shares
    assert lots["L2"].pnl == "regular flash"            # the lot at the report's figure keeps its own PnL


def test_the_lot_that_carries_the_pnl_is_one_valued_with_terms(monkeypatch):
    lots, n = _attach_case(monkeypatch, {"L1": False, "L2": True})
    assert n["mixed_valuation"] == ["B1|S"]
    assert lots["L2"].pnl.total == pytest.approx(-950.0) and lots["L1"].pnl == "regular flash"


def test_no_lot_valued_with_terms_is_said_not_lost_silently(monkeypatch):
    lots, n = _attach_case(monkeypatch, {"L1": False, "L2": False})
    assert n["lost"] == ["B1|S"] and set(n["rows"].values()) == {"trades_not_in_pnl"}
    assert all(o.pnl == "regular flash" for o in lots.values())


def test_lots_all_valued_with_terms_are_split(monkeypatch):
    lots, n = _attach_case(monkeypatch, {"L1": True, "L2": True})
    assert n["mixed_valuation"] == [] and n["lost"] == []
    assert lots["L1"].pnl.total + lots["L2"].pnl.total == pytest.approx(-950.0)
    assert lots["L2"].pnl.pnl_on_row == "L1" and lots["L2"].pnl.total == pytest.approx(-950.0 * 3000 / 11000)
