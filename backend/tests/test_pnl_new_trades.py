"""The PnL of a day with trades (``desk.pnl.new_trades``, TDM#557): the start-of-day position plus each trade from its
price to today's valuation. Per-trade arithmetic on stubs; the whole path (autopilot, store, lineage, screens) on the
synthetic fixtures of the intraday tests. Synthetic data only."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
import test_slice_s3
from test_autopilot import cfg, put, steps
from test_intraday import FRI, at, publish

from desk import dayrun, store, trades
from desk.autopilot import Autopilot
from desk.inbox import scan
from desk.pnl import new_trades as nt
from desk.pnl.flash import NoPnl
from desk.pnl.reasons import NO_PNL_TEXT, assumption_text
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
                                          "members_start": {"T1|SEC1": 1000.0},
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


def test_a_start_of_day_without_pnl_is_taken_as_0_and_the_trades_are_in_the_total(day, monkeypatch):
    """TDM#576: no PnL of the start of the day is not «нет PnL» for the position: it is taken as 0 on a said assumption, and the PnL of its trades is its PnL."""
    real = nt.portfolio_pnl

    def unpriced(prev, ctx, start, pids, carried):
        out = real(prev, ctx, start, pids, carried)
        for o in out.values():
            o.pnl, o.no_pnl = None, NoPnl("not_priced_prev", "test: no price on the previous close")
        return out

    monkeypatch.setattr(nt, "portfolio_pnl", unpriced)
    day.drop(trade_line("S1", "sell", 1000, 62.0, 5_000.0))
    r = day.row()
    assert r["q"] == 11000 and r["no_pnl"] is None
    assert r["pnl"]["new_trades"] != 0 and r["pnl"]["extra"]["total"] == pytest.approx(r["pnl"]["new_trades"])
    assert r["pnl"]["extra"]["trades"][0]["trade_id"] == "S1" and r["pnl"]["extra"]["start_quantity"] == 12000
    a = r["pnl"]["extra"]["assumption"]
    assert a["code"] == "start_unvalued" and a["detail"] == NO_PNL_TEXT["not_priced_prev"]
    assert assumption_text(a).startswith("PnL позиции на начало дня не посчитан") and "сделки дня учтены" in assumption_text(a)


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


def _legacy_hash(inputs_hash, applied, hist=()):
    """The hash ``apply_intraday`` made of the same trades before the mark ``trades.HELD_MARK`` (TDM#721)."""
    import hashlib
    import json

    canon = json.dumps([inputs_hash, list(applied), list(hist), None, {}], sort_keys=True, default=str,
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def test_the_inputs_hash_changes_only_for_dates_with_applied_trades(monkeypatch):
    """TDM#721: the rows hold the ids of their trades and the OCP counts a bond once, so the dates with applied trades
    (of the day, or of the history of a carried book) get another hash and are made again by the autopilot, ``recompute``
    and the backfill, and no other date is: a date without an applied trade keeps the hash of its inputs, whatever
    trades it has (none, cancelled, not bonds, of another day)."""
    import test_trades_intraday as tti

    settings = SimpleNamespace(intraday_trades=True, off_report_derivatives="exclude")
    inputs = tti._inputs()   # the hash "h0", the books valid 2026-11-02
    left_out = [tti._trade(status="cancelled"), tti._trade(trade_id="TR5", asset_class="fx"),
                tti._trade(trade_id="TR6", trade_date="2026-11-01")]
    for day_rows in ([], left_out):
        _days(monkeypatch, {"2026-11-03": day_rows})
        out, summary = trades.intraday_inputs(None, DAY, settings, inputs)
        assert out.private_inputs_hash == "h0" and summary["applied"] == 0 and summary["history"] == 0
    # a trade of the day: another hash than the one the same trades made before the mark, and a stable one
    _days(monkeypatch, {"2026-11-03": [tti._trade()]})
    out, _ = trades.intraday_inputs(None, DAY, settings, inputs)
    _, _, res = trades._fold(inputs.rows, inputs.extra, {"T1", "T2"}, DAY, [tti._trade()], keep_flat=True)
    assert out.private_inputs_hash not in ("h0", _legacy_hash("h0", res["applied"]))
    assert trades.intraday_inputs(None, DAY, settings, inputs)[0].private_inputs_hash == out.private_inputs_hash
    # only the history of a carried book (the day has no trade): also another hash
    nxt = date(2026, 11, 4)
    out, summary = trades.intraday_inputs(None, nxt, settings, inputs)
    _, _, past = trades._fold(inputs.rows, inputs.extra, {"T1", "T2"}, DAY, [tti._trade()])
    assert summary["history"] == 1 and summary["applied"] == 0
    assert out.private_inputs_hash not in ("h0", _legacy_hash("h0", [], past["applied"]))
    # the setting off: no trade is applied, the hash of the inputs
    off = SimpleNamespace(intraday_trades=False, off_report_derivatives="exclude")
    assert trades.intraday_inputs(None, DAY, off, inputs)[0].private_inputs_hash == "h0"


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


def _attach_case(monkeypatch, core, direct_l2=False):
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
    if direct_l2:   # L2 is a row at the report's figure of the plan's ``direct``
        from desk.pnl.direct import DirectPnl

        l2 = lots.pop("L2")
        l2.terms, l2.pnl = None, DirectPnl("desk-reported-v1", date(2026, 10, 15), date(2026, 10, 16), -300.0, other=-300.0)
        bp = SimpleNamespace(position_ids=list(lots), direct={"L2": l2})
    summary = {"trades": [{"row": "L1", "trade_id": "S"}],
               "rows": {"L1": {"start": 11000.0, "book": "B1", "bond": "S", "members": {"L1": 8000.0, "L2": 3000.0}}}}
    n = nt.attach(prev, ctx, SimpleNamespace(), bp, list(lots.values()), summary, None)
    if direct_l2:
        n["direct"] = bp.direct
    return lots, n


def test_lots_valued_in_different_ways_are_not_split_and_say_so(monkeypatch):
    lots, n = _attach_case(monkeypatch, {"L1": True, "L2": False})
    assert n["mixed_valuation"] == ["B1|S"] and n["rows"] == {"L1": "trades_mixed_valuation",
                                                              "L2": "trades_mixed_valuation"}
    assert lots["L1"].pnl.total == pytest.approx(-950.0) and lots["L1"].pnl.new_trades == 50.0   # the whole, no shares
    # the lot at the report's figure has none of its own: the bond's start of the day (all its lots) and the trades are
    # on L1, so the day's total is the bond's PnL once (it was twice: -950 + the lot's own, TDM#570)
    assert lots["L2"].pnl.total == 0.0 and "L1" in lots["L2"].pnl.note and lots["L2"].no_pnl is None
    assert lots["L1"].pnl.total + lots["L2"].pnl.total == pytest.approx(-950.0)


def test_a_lot_from_the_direct_plan_of_a_mixed_bond_is_zeroed_too(monkeypatch):
    """The lot at the report's figure sits in the plan's ``direct`` (not in the core outcomes): its PnL by the reports
    (which has the start of the day's share and the trades inside) is replaced by a zero, the bond's PnL is on L1."""
    from desk.pnl.direct import DirectPnl

    lots, n = _attach_case(monkeypatch, {"L1": True, "L2": True}, direct_l2=True)
    assert n["mixed_valuation"] == ["B1|S"] and lots["L1"].pnl.total == pytest.approx(-950.0)
    l2 = n["direct"]["L2"]
    assert isinstance(l2.pnl, DirectPnl) and l2.pnl.total == 0.0 and l2.pnl.other == 0.0 and l2.no_pnl is None
    assert lots["L1"].pnl.total + l2.pnl.total == pytest.approx(-950.0)


def test_the_lot_that_carries_the_pnl_is_one_valued_with_terms(monkeypatch):
    lots, n = _attach_case(monkeypatch, {"L1": False, "L2": True})
    assert n["mixed_valuation"] == ["B1|S"]
    assert lots["L2"].pnl.total == pytest.approx(-950.0)
    assert lots["L1"].pnl.total == 0.0 and "L2" in lots["L1"].pnl.note    # the lot at the report's figure: none of its own


def test_no_lot_valued_with_terms_is_said_not_lost_silently(monkeypatch):
    lots, n = _attach_case(monkeypatch, {"L1": False, "L2": False})
    assert n["lost"] == ["B1|S"] and set(n["rows"].values()) == {"trades_not_in_pnl"} and n["lost_trades"] == 1
    # D23: the lots at the report's figure had PV(D) - PV(D-1) with the purchase at its whole value: no PnL, said why
    for o in lots.values():
        assert o.pnl is None and o.no_pnl.reason == nt.NOT_IN_PNL


def test_lots_all_valued_with_terms_are_split(monkeypatch):
    lots, n = _attach_case(monkeypatch, {"L1": True, "L2": True})
    assert n["mixed_valuation"] == [] and n["lost"] == []
    assert lots["L1"].pnl.total + lots["L2"].pnl.total == pytest.approx(-950.0)
    assert lots["L2"].pnl.pnl_on_row == "L1" and lots["L2"].pnl.total == pytest.approx(-950.0 * 3000 / 11000)


# ---------- portfolios, flat new positions, the lineage's counts (TDM#570) ----------

def _pf(p, **kw):
    return {"asset_class": "bond", "ccy": "RUB", "x": {"x_portfolio": p}, **kw}


def _with_extra(inputs, extra):
    return store.PositionInputs(**{**inputs.__dict__, "extra": extra})


def test_a_lot_sold_out_in_one_portfolio_keeps_its_pnl_there(monkeypatch):
    """D22: one bond in the portfolios A and B, the lot of A sold to zero; the report lists the lot of B only. The PnL of
    the sale stays with A (a row at zero in A), the lot of B is not measured with it."""
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|LA", side="sell", quantity="1000")]})
    _lots_prev(monkeypatch, [("T1|LA", "T1", "SEC1", 1000.0), ("T1|LB", "T1", "SEC1", 500.0)],
               {"T1|LA": _pf("A"), "T1|LB": _pf("B")})
    rep = _with_extra(_lot_inputs([("T1|LB", "T1", "SEC1", 500.0)], DAY), {"T1|LB": _pf("B")})
    out, summary = trades.apply_intraday(None, DAY, rep)
    assert set(summary["rows"]) == {"T1|LA"}                        # the lot of B is no part of the traded bond
    c = summary["rows"]["T1|LA"]
    assert (c["start"], c["final"], c["members"]) == (1000.0, 0.0, {})
    assert [t["row"] for t in summary["trades"]] == ["T1|LA"]
    by = {r[0]: r for r in out.rows}
    assert by["T1|LA"][3] == 0.0 and by["T1|LB"][3] == 500.0
    assert out.extra["T1|LA"]["x"]["x_portfolio"] == "A"             # the closed row is in its own portfolio (D3)
    assert {r[0]: r[3] for r in summary["start_inputs"].rows if r[1] == "T1"} == {"T1|LA": 1000.0, "T1|LB": 500.0}


def test_the_lots_of_a_traded_bond_are_split_inside_their_portfolio(monkeypatch):
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|A1", side="sell", quantity="200")]})
    start = [("T1|A1", "T1", "SEC1", 1000.0), ("T1|A2", "T1", "SEC1", 500.0), ("T1|B1", "T1", "SEC1", 700.0)]
    ext = {"T1|A1": _pf("A"), "T1|A2": _pf("A"), "T1|B1": _pf("B")}
    _lots_prev(monkeypatch, start, ext)
    rep = _with_extra(_lot_inputs([("T1|A1", "T1", "SEC1", 800.0), ("T1|A2", "T1", "SEC1", 500.0),
                                   ("T1|B1", "T1", "SEC1", 700.0)], DAY), ext)
    _, summary = trades.apply_intraday(None, DAY, rep)
    assert set(summary["rows"]) == {"T1|A1"} and summary["gaps"] == {}
    c = summary["rows"]["T1|A1"]
    assert (c["start"], c["final"], sorted(c["members"])) == (1500.0, 1300.0, ["T1|A1", "T1|A2"])   # no lot of B
    assert {r[0]: r[3] for r in summary["start_inputs"].rows if r[1] == "T1"} == {"T1|A1": 1500.0, "T1|B1": 700.0}


def test_a_report_that_puts_a_new_bond_in_another_portfolio_still_finds_it(monkeypatch):
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|X1", instrument_id="SEC2", quantity="10")]})
    _lots_prev(monkeypatch, [("T1|L1", "T1", "SEC1", 1000.0)], {"T1|L1": _pf("A")})
    rep = _with_extra(_lot_inputs([("T1|L1", "T1", "SEC1", 1000.0), ("T1|Y1", "T1", "SEC2", 10.0)], DAY),
                      {"T1|L1": _pf("A"), "T1|Y1": _pf("B")})
    _, summary = trades.apply_intraday(None, DAY, rep)
    c = summary["rows"]["T1|Y1"]
    assert (c["start"], c["final"], c["new"], summary["gaps"]) == (0.0, 10.0, True, {})
    assert [t["row"] for t in summary["trades"]] == ["T1|Y1"]


def test_a_bond_bought_and_sold_the_same_day_with_a_report_is_kept_at_zero(monkeypatch):
    """Item 5 with a report of the day: the bond is neither in the start nor in the report; its row stays at zero and
    carries the PnL of both trades."""
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(trade_id="A", position_id="T1|N", instrument_id="SEC2", quantity="10"),
                                       tti._trade(trade_id="B", position_id="T1|N", instrument_id="SEC2", quantity="10",
                                                  side="sell", trade_ts="2026-11-03T12:00:00")]})
    _lots_prev(monkeypatch, [("T1|L1", "T1", "SEC1", 1000.0)], {"T1|L1": _pf("A")})
    rep = _with_extra(_lot_inputs([("T1|L1", "T1", "SEC1", 1000.0)], DAY), {"T1|L1": _pf("A")})
    out, summary = trades.apply_intraday(None, DAY, rep)
    assert {r[0]: r[3] for r in out.rows if r[1] == "T1"} == {"T1|L1": 1000.0, "T1|N": 0.0}
    assert out.extra["T1|N"]["x"]["x_portfolio"] == "A"
    assert [t["row"] for t in summary["trades"]] == ["T1|N", "T1|N"] and summary["new_positions"] == 0
    assert summary["rows"]["T1|N"]["final"] == 0.0 and summary["rows"]["T1|N"]["start"] == 0.0


def test_a_position_the_trades_open_carries_the_portfolio_of_its_book(monkeypatch):
    """D3: a carried book whose bonds are all in one portfolio: the new position is in it; with two portfolios (or a
    portfolio of the trade) as the trade says, else none."""
    import test_trades_intraday as tti

    ext = {"T1|L1": _pf("A"), "T1|L2": _pf("A"), "T1|FX": {"asset_class": "fx"}}
    inp = store.PositionInputs(**{**tti._inputs().__dict__, "extra": ext, "rows": [
        ("T1|L1", "T1", "SEC1", 1000.0), ("T1|L2", "T1", "SEC1", 5.0), ("T1|FX", "T1", None, None)]})
    new = tti._trade(position_id="T1|N", instrument_id="SEC2", quantity="10")
    monkeypatch.setattr(store, "dataset_rows", lambda con, ds, on: store.DatasetInputs(ds, on, [new], [{"set_id": 9}]))
    out, _ = trades.apply_intraday(None, DAY, inp)
    assert out.extra["T1|N"]["x"] == {"intraday_trade": True, "intraday_trades": ["TR1"], "x_portfolio": "A"}
    mixed = _with_extra(inp, {**ext, "T1|L2": _pf("B")})
    out, _ = trades.apply_intraday(None, DAY, mixed)
    assert "x_portfolio" not in out.extra["T1|N"]["x"]
    monkeypatch.setattr(store, "dataset_rows", lambda con, ds, on: store.DatasetInputs(
        ds, on, [{**new, "x_portfolio": "C"}], [{"set_id": 9}]))
    out, _ = trades.apply_intraday(None, DAY, mixed)
    assert out.extra["T1|N"]["x"]["x_portfolio"] == "C"


def test_a_bond_bought_and_sold_the_same_day_has_the_pnl_of_both_trades(day):
    """Item 5, a carried book: the new bond nets to nothing, its row stays at zero with new_trades = sale - purchase
    (cost of the sale minus cost of the purchase: the valuation cancels), and the day's total holds it."""
    with store.connect(day.url) as con:
        t0 = con.execute("SELECT totals FROM run WHERE run_id = %s", (day.latest_id(),)).fetchone()[0]
    day.drop(trade_line("N1", "buy", 100, 59.0, 1_000.0, pid="B1|NEW", instr="SEC-29008", hhmm="10:30"),
             trade_line("N2", "sell", 100, 60.0, 1_500.0, pid="B1|NEW", instr="SEC-29008", hhmm="11:00"))
    rid = day.latest_id()
    with store.connect(day.url) as con:
        row = con.execute(ROW, (rid, "B1|NEW")).fetchone()
        t1 = con.execute("SELECT totals FROM run WHERE run_id = %s", (rid,)).fetchone()[0]
    assert row is not None
    q, _pv, pnl, no = row
    expected = (100 * 0.60 * 1000 + 1_500.0) - (100 * 0.59 * 1000 + 1_000.0)
    assert q == 0 and no is None and pnl["new_trades"] == pytest.approx(expected)
    assert {t["trade_id"] for t in pnl["extra"]["trades"]} == {"N1", "N2"}
    assert t1["B1"]["pnl"]["total"] - t0["B1"]["pnl"]["total"] == pytest.approx(expected)


def test_the_lineage_counts_the_trades_outside_the_total(day, monkeypatch):
    """Item 6: a start of the day without PnL (taken as 0, its trades in the total: ``assumed``), a trade without a price:
    counted in the lineage."""
    real = nt.portfolio_pnl

    def unpriced(prev, ctx, start, pids, carried):
        out = real(prev, ctx, start, pids, carried)
        for o in out.values():
            o.pnl, o.no_pnl = None, NoPnl("not_priced_prev", "test: no price on the previous close")
        return out

    monkeypatch.setattr(nt, "portfolio_pnl", unpriced)
    day.drop(trade_line("S1", "sell", 1000, 62.0, 5_000.0), trade_line("S2", "sell", 100, hhmm="11:00"))
    r = day.row()
    lin = lineage(day.url, day.latest_id())
    out = lin["outside_total"]
    assert out["no_prev_close"] is False
    assert out["assumed"] == {"positions": 1, "trades": 2} and r["pnl"]["new_trades"] != 0
    assert out["no_pnl"] == {"positions": 0, "trades": 0, "pnl_rub": 0.0}   # nothing left beside the total
    assert out["lost"] == {"bonds": 0, "trades": 0} and out["unpriced"] == {"trades": 1} and lin["unpriced"] == 1


def test_the_lineage_says_when_there_is_no_previous_close(day, monkeypatch):

    monkeypatch.setattr(dayrun, "previous_close", lambda sd, d: None)
    day.drop(trade_line("S1", "sell", 1000, 62.0, 5_000.0))
    lin = lineage(day.url, day.latest_id())
    assert lin["outside_total"] == nt.outside_total(1, no_prev_close=True)   # the same keys as always, zeros where empty
    assert set(lin["outside_total"]) == {"no_prev_close", "no_pnl_run", "trades", "no_pnl", "assumed", "lost",
                                         "unpriced", "unvalued"}


def test_a_purchase_into_one_portfolio_of_a_bond_held_in_two_is_measured_there(monkeypatch):
    """A new lot of the report in the portfolio the trade names joins the lot of that portfolio, not the last lot of the
    bond: no gap, no double count, the other portfolio is not part of the key."""
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|T9", instrument_id="SEC1", quantity="100",
                                                  x_portfolio="A")]})
    ext = {"T1|LA": _pf("A"), "T1|LB": _pf("B")}
    _lots_prev(monkeypatch, [("T1|LA", "T1", "SEC1", 1000.0), ("T1|LB", "T1", "SEC1", 500.0)], ext)
    rep = _with_extra(_lot_inputs([("T1|LA", "T1", "SEC1", 1000.0), ("T1|LB", "T1", "SEC1", 500.0),
                                   ("T1|T9", "T1", "SEC1", 100.0)], DAY), {**ext, "T1|T9": _pf("A")})
    _, summary = trades.apply_intraday(None, DAY, rep)
    assert set(summary["rows"]) == {"T1|LA"} and summary["gaps"] == {}
    c = summary["rows"]["T1|LA"]
    assert (c["start"], c["final"], sorted(c["members"]), c["portfolio"]) == (1000.0, 1100.0, ["T1|LA", "T1|T9"], "A")
    assert c["members_start"] == {"T1|LA": 1000.0}
    assert [t["row"] for t in summary["trades"]] == ["T1|LA"]


def test_a_purchase_that_does_not_name_its_portfolio_takes_the_bond_whole(monkeypatch):
    """The portfolio of the trade is unknown and the bond is in two portfolios: the key falls back to (book, bond) as
    before portfolios, so the new lot of the report is a member and nothing is counted twice."""
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|T9", instrument_id="SEC1", quantity="100")]})
    ext = {"T1|LA": _pf("A"), "T1|LB": _pf("B")}
    _lots_prev(monkeypatch, [("T1|LA", "T1", "SEC1", 1000.0), ("T1|LB", "T1", "SEC1", 500.0)], ext)
    rep = _with_extra(_lot_inputs([("T1|LA", "T1", "SEC1", 1000.0), ("T1|LB", "T1", "SEC1", 500.0),
                                   ("T1|T9", "T1", "SEC1", 100.0)], DAY), {**ext, "T1|T9": _pf("A")})
    _, summary = trades.apply_intraday(None, DAY, rep)
    assert len(summary["rows"]) == 1 and summary["gaps"] == {}
    [(anchor, c)] = summary["rows"].items()
    assert anchor == "T1|LA" and (c["start"], c["final"]) == (1500.0, 1600.0)
    assert sorted(c["members"]) == ["T1|LA", "T1|LB", "T1|T9"] and "portfolio" not in c
    assert [t["row"] for t in summary["trades"]] == ["T1|LA"]


def test_a_purchase_without_portfolios_anywhere_is_as_it_was(monkeypatch):
    import test_trades_intraday as tti

    _days(monkeypatch, {"2026-11-03": [tti._trade(position_id="T1|T9", instrument_id="SEC1", quantity="100")]})
    _lots_prev(monkeypatch, [("T1|LA", "T1", "SEC1", 1000.0), ("T1|LB", "T1", "SEC1", 500.0)])
    _, summary = trades.apply_intraday(None, DAY, _lot_inputs(
        [("T1|LA", "T1", "SEC1", 1000.0), ("T1|LB", "T1", "SEC1", 500.0), ("T1|T9", "T1", "SEC1", 100.0)], DAY))
    [(anchor, c)] = summary["rows"].items()
    assert anchor == "T1|LA" and sorted(c["members"]) == ["T1|LA", "T1|LB", "T1|T9"] and summary["gaps"] == {}


def _report_lots(monkeypatch, quantities, pre=None):
    """Lots of one bond valued at the report's figure (not core): L1 as at the start, T9 new; the trade hit L1."""
    from desk.pnl.direct import DirectPnl

    prev = SimpleNamespace(business_date=date(2026, 10, 15))
    ctx = SimpleNamespace(business_date=date(2026, 10, 16))
    lots = {m: _lot_outcome(q, core=False) for m, q in quantities.items()}
    for o in lots.values():
        o.pnl = DirectPnl("desk-reported-v1", prev.business_date, ctx.business_date, 100.0, other=100.0)
    if pre is not None:
        lots["L1"].pnl, lots["L1"].no_pnl = None, pre
    bp = SimpleNamespace(position_ids=[], direct=lots)
    summary = {"trades": [{"row": "L1", "trade_id": "S"}],
               "rows": {"L1": {"start": 8000.0, "book": "B1", "bond": "S", "members": dict(quantities),
                               "members_start": {"L1": 8000.0}}}}
    monkeypatch.setattr(nt, "portfolio_pnl", lambda *a: {})
    n = nt.attach(prev, ctx, SimpleNamespace(), bp, [], summary, None)
    return lots, n


def test_a_report_lot_the_trades_did_not_touch_keeps_its_pnl(monkeypatch):
    """D23 is for the lots the trades changed: L1 is as at the start of the day (its reports' difference is right), T9
    is new (its purchase is inside)."""
    lots, n = _report_lots(monkeypatch, {"L1": 8000.0, "T9": 500.0})
    assert lots["L1"].pnl.total == 100.0 and lots["L1"].no_pnl is None
    assert lots["T9"].pnl is None and lots["T9"].no_pnl.reason == nt.NOT_IN_PNL
    assert n["rows"] == {"T9": "trades_not_in_pnl"} and n["lost"] == ["B1|S"] and n["lost_trades"] == 1


def test_a_report_lot_with_another_quantity_has_no_pnl_and_an_earlier_reason_stays(monkeypatch):
    lots, _ = _report_lots(monkeypatch, {"L1": 7000.0, "T9": 500.0})
    assert lots["L1"].pnl is None and lots["L1"].no_pnl.reason == nt.NOT_IN_PNL
    assert lots["T9"].no_pnl.reason == nt.NOT_IN_PNL
    # a lot that has no PnL for a more basic reason (a report carried from earlier) keeps that reason
    lots, _ = _report_lots(monkeypatch, {"L1": 7000.0}, pre=NoPnl("report_carried", "test"))
    assert lots["L1"].pnl is None and lots["L1"].no_pnl.reason == "report_carried"


def test_the_trades_of_a_bond_not_valued_at_all_are_counted(monkeypatch):
    monkeypatch.setattr(nt, "portfolio_pnl", lambda *a: {})
    prev = SimpleNamespace(business_date=date(2026, 10, 15))
    ctx = SimpleNamespace(business_date=date(2026, 10, 16))
    o = _lot_outcome(8000.0, core=False)
    o.valuation = None
    bp = SimpleNamespace(position_ids=[], direct={"L1": o})
    summary = {"trades": [{"row": "L1", "trade_id": "S"}],
               "rows": {"L1": {"start": 8000.0, "book": "B1", "bond": "S", "members": {"L1": 7000.0},
                               "members_start": {"L1": 8000.0}}}}
    n = nt.attach(prev, ctx, SimpleNamespace(), bp, [], summary, None)
    assert n["unvalued_trades"] == 1 and n["lost"] == [] and nt.outside_total(1, n)["unvalued"] == {"trades": 1}


def test_lots_beside_a_core_lot_whose_start_has_no_pnl_are_assumed_with_it(monkeypatch):
    """The bond start of the day has no PnL (taken as 0, assumed): the lot at the report figure is covered by it."""
    monkeypatch.setattr(nt, "portfolio_pnl", lambda *a: {"L1": SimpleNamespace(
        pnl=None, no_pnl=NoPnl("not_priced_prev", "t"), valuation=SimpleNamespace(quantity=11000.0))})
    monkeypatch.setattr(nt, "trade_pnl", lambda *a: {"trade_id": "S", "pnl_rub": 50.0, "flag": None})
    prev = SimpleNamespace(business_date=date(2026, 10, 15))
    ctx = SimpleNamespace(business_date=date(2026, 10, 16))
    lots = {"L1": _lot_outcome(8000.0, True), "L2": _lot_outcome(3000.0, False)}
    bp = SimpleNamespace(position_ids=list(lots))
    summary = {"trades": [{"row": "L1", "trade_id": "S"}],
               "rows": {"L1": {"start": 11000.0, "book": "B1", "bond": "S", "members": {"L1": 8000.0, "L2": 3000.0},
                               "members_start": {"L1": 11000.0}}}}
    n = nt.attach(prev, ctx, SimpleNamespace(), bp, list(lots.values()), summary, None)
    # TDM#576: the start of the bond is taken as 0 (assumed), its trades are in the total; the report-figure lot is covered
    assert n["no_pnl"] == 0 and n["assumed"] == 1 and lots["L2"].no_pnl is None and lots["L2"].pnl.total == 0.0
    assert lots["L1"].no_pnl is None and lots["L1"].pnl.total == 50.0 and lots["L1"].pnl.assumption["code"] == "start_unvalued"
    assert nt.outside_total(1, n)["assumed"]["positions"] == 1 and nt.outside_total(1, n)["no_pnl"]["positions"] == 0


def test_the_books_total_is_the_sum_of_its_bonds_with_lots_valued_in_different_ways(stub, tmp_path, store_url,
                                                                                    monkeypatch):
    """Through the store: the same sale on the same market, the report of the day with two lots one of which is taken as
    valued at the report's figure. The bond's PnL is on the first lot once, the second has a zero, the book's total
    is the sum of its positions and equals the PnL of the all-core run (nothing counted twice)."""
    d = Day(stub, tmp_path, store_url, lots=[("B1|L1", S, LOT_ISIN, 12000), ("B1|L2", S, LOT_ISIN, 3000)])
    d.drop(trade_line("S1", "sell", 4000, 62.0, 10_000.0, pid="B1|L2", iid=""))
    publish(d.hub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    d.clock["t"] = at(MON, 8, 30)
    d.ap.run_cycle()
    carried = d.latest_id("EOD")
    before = _book_sum(d, carried, ["B1|L1", "B1|L2"])
    orig = nt._core
    monkeypatch.setattr(nt, "_core", lambda o: orig(o) and o.valuation.quantity != 3000)
    d.positions("positions_20261019.csv", [("B1|L1", S, LOT_ISIN, 8000), ("B1|L2", S, LOT_ISIN, 3000)])
    d.clock["t"] = at(MON, 8, 50)
    d.ap.run_cycle()
    rid = d.latest_id("EOD")
    assert rid > carried
    with store.connect(d.url) as con:
        totals = con.execute("SELECT totals FROM run WHERE run_id = %s", (rid,)).fetchone()[0]
        rows = {r[0]: r[1] for r in con.execute(
            "SELECT position_id, pnl FROM run_position WHERE run_id = %s AND book_id = 'B1'", (rid,))}
    assert lineage(d.url, rid)["mixed_valuation"] == ["B1|SU26238RMFS4"]
    assert rows["B1|L2"]["extra"]["total"] == 0.0 and rows["B1|L1"]["extra"]["total"] != 0
    book = sum(p["extra"]["total"] for p in rows.values() if p)
    t = totals["B1"]["pnl"]
    assert t["total"] - t.get("funding", 0.0) == pytest.approx(book)   # the book's funding cost is its own, not a position's
    assert book == pytest.approx(before[0])


def test_trades_that_changed_no_lot_of_the_report_are_still_counted(monkeypatch):
    """A sale to zero of a lot whose row is gone leaves L1 as it was, and a purchase and sale in one lot leave every
    quantity as at the start: no lot is marked, but the trades are counted in the run (lost) and keep their PnL of none."""
    lots, n = _report_lots(monkeypatch, {"L1": 8000.0})
    assert lots["L1"].pnl.total == 100.0 and lots["L1"].no_pnl is None and n["rows"] == {}
    assert n["lost"] == ["B1|S"] and n["lost_trades"] == 1
    assert nt.outside_total(1, n)["lost"] == {"bonds": 1, "trades": 1}
    lots, n = _report_lots(monkeypatch, {"L1": 8000.0 + 1e-9})   # a rounding below the gap's tolerance is no change
    assert lots["L1"].pnl.total == 100.0 and n["rows"] == {} and n["lost_trades"] == 1


def test_the_any_portfolio_of_a_key_is_no_portfolio_name():
    assert trades.ANY != "*" and trades.ANY not in ("*", "ANY", "any", None)


# ---------- a bond at the report's figure with trades today: the trades are taken off the reports' difference (TDM#576) ----------

def _rtp(lots, ts, was=None):
    """The bond's PnL on its first lot (outcome, DirectPnl), or None."""
    r = nt._report_trades_pnl(lots, ts, was)
    return None if r is None else r[0]


def _report_lot(face_rub=math.nan, clean=math.nan, pv=5_000_000.0, q=5000.0, diff=40_000.0, stub=False):
    from desk.orchestrator import Outcome
    from desk.pnl.direct import REPORTED_METHOD, DirectPnl

    v = SimpleNamespace(quantity=q, face_rub=face_rub, clean_pct=clean, pv_rub=pv, accrued_rub=0.0,
                        extra={"qty_stub": True} if stub else {})
    p = DirectPnl(REPORTED_METHOD, date(2026, 10, 15), date(2026, 10, 16), diff, other=diff, note="reported")
    return Outcome("", None, None, valuation=v, pnl=p)


def test_trades_are_taken_off_the_reports_difference_at_their_cost():
    # 1000 bonds bought at 98 % of a face of 1000: the report grew by 40 000 + the purchase 980 000
    o = _report_lot(face_rub=5_000_000.0, diff=1_020_000.0)
    got = _rtp([o], [{**RUB, "side": "buy", "quantity": 1000, "price_pct": 98.0, "accrued_ccy": 0.0}])
    assert got is not None and got[0] is o
    assert got[1].total == pytest.approx(40_000.0) and got[1].other == pytest.approx(40_000.0)
    assert got[1].assumption == {"code": "report_trades_face", "detail": "номинал выведен из отчётной строки"}
    # a sale is cash in: the report fell by the sold value
    o = _report_lot(face_rub=5_000_000.0, diff=-960_000.0)
    got = _rtp([o], [{**RUB, "side": "sell", "quantity": 1000, "price_pct": 99.0, "accrued_ccy": 10_000.0}])
    assert got[1].total == pytest.approx(-960_000.0 + 1000 * 0.99 * 1000 + 10_000.0)


RUB = {"ccy": "RUB"}


def test_the_face_of_a_bond_of_the_report_comes_from_the_trade_or_the_value_else_no_pnl():
    t = {**RUB, "side": "buy", "quantity": 100, "price_pct": 100.0, "accrued_ccy": 0.0}
    by_trade = _rtp([_report_lot(diff=0.0)], [{**t, "face_amount_ccy": 100_000.0}])
    assert by_trade[1].total == pytest.approx(-100_000.0) and by_trade[1].assumption["detail"] == "номинал выведен из сделки"
    by_value = _rtp([_report_lot(clean=100.0, pv=5_000_000.0, diff=0.0)], [t])
    assert by_value[1].total == pytest.approx(-100 * 1000.0) and "стоимости и цены" in by_value[1].assumption["detail"]
    assert _rtp([_report_lot(diff=0.0)], [t]) is None   # no face anywhere
    assert _rtp([_report_lot(face_rub=5e6)], [{**t, "price_pct": None}]) is None   # no trade price
    assert _rtp([_report_lot(face_rub=5e6)], [{**t, "ccy": "USD"}]) is None   # not a RUB trade
    assert _rtp([_report_lot(face_rub=5e6)], [{**t, "ccy": None}]) is None   # no currency: not taken for RUB
    # a report row without a number of bonds is valued as one bond: its face is the whole position's, not a bond's
    assert _rtp([_report_lot(face_rub=5e6, q=1.0, stub=True)], [t]) is None
    # a position of exactly one bond is a position: its face is a bond's
    one = _rtp([_report_lot(face_rub=1000.0, q=1.0, diff=0.0)], [{**t, "quantity": 1}])
    assert one is not None and one[1].total == pytest.approx(-1000.0)
    lot = _report_lot(face_rub=5e6)
    lot.pnl = None
    assert _rtp([lot], [t]) is None   # no PnL by the reports to take it from
    lot = _report_lot(face_rub=5e6)
    lot.pnl = replace(lot.pnl, assumption={"code": "start_pv_missing"})
    assert _rtp([lot], [t]) is None   # an assumption this does not know


# ---- through the reports' own PnL (``direct._reported``, with its assumptions) and ``nt.attach`` ----

def _reported_bond(monkeypatch, *, prev_pv, pv, quantity, start, final, trades, carried=None, clean=100.0, face=1000.0):
    """One lot of a bond at the report's figure (no terms), its PnL by ``direct._reported`` as the run gives it (D-1's
    report ``prev_pv`` or none; the report of D unchanged since ``carried``), the trades through ``nt.attach``. A second
    lot L2 of the same bond and book, untouched, has its own difference. Returns the lots and the counts."""
    from desk.orchestrator import Outcome
    from desk.pnl import direct

    prev = SimpleNamespace(business_date=date(2026, 10, 15))
    ctx = SimpleNamespace(business_date=date(2026, 10, 16))

    def lot(q, value):
        v = SimpleNamespace(quantity=q, valuation_date=ctx.business_date, pv_rub=value, quality="reported_value",
                            face_rub=face * q, clean_pct=clean, accrued_rub=0.0, z_spread_bp=1.0, price_basis="p")
        return Outcome("", None, None, valuation=v)
    l1, l2 = lot(quantity, pv), lot(2000.0, 2_000_000.0)
    p1 = None if prev_pv is None else lot(start, prev_pv)
    p2 = lot(2000.0, 1_990_000.0)
    l1.pnl = direct._reported(prev, ctx, l1, p1, carried)
    l2.pnl = direct._reported(prev, ctx, l2, p2, None)
    bp = SimpleNamespace(position_ids=[], direct={"L1": l1, "L2": l2})
    summary = {"trades": [{"row": "L1", "trade_id": f"T{i}", **t} for i, t in enumerate(trades)],
               "rows": {"L1": {"start": start, "book": "B1", "bond": "S", "members": {"L1": final, "L2": 2000.0},
                               "members_start": {"L1": start, "L2": 2000.0} if start else {"L2": 2000.0}}}}
    monkeypatch.setattr(nt, "portfolio_pnl", lambda *a: {})
    n = nt.attach(prev, ctx, SimpleNamespace(), bp, [], summary, None)
    return {"L1": l1, "L2": l2}, n


BUY = {"side": "buy", "quantity": 1000, "price_pct": 98.0, "accrued_ccy": 0.0, "ccy": "RUB"}


def test_a_bond_held_yesterday_has_the_trades_taken_off_its_reports_difference(monkeypatch):
    # 8000 bonds at 100 -> 9000 at 100.5, 1000 bought at 98: 8000 x 5 + 1000 x 25 = 65 000; L2 keeps its own 10 000
    lots, n = _reported_bond(monkeypatch, prev_pv=8_000_000.0, pv=9_045_000.0, quantity=9000.0, start=8000.0, final=9000.0,
                             trades=[BUY], clean=100.5)
    assert lots["L1"].pnl.total == pytest.approx(65_000.0) and lots["L1"].no_pnl is None
    assert lots["L1"].pnl.assumption["code"] == "report_trades_face"
    assert lots["L2"].pnl.total == pytest.approx(10_000.0)   # untouched
    book = sum(o.pnl.total for o in lots.values())
    assert book == pytest.approx(75_000.0)   # the sum of the lots is the bond's PnL, once
    out = nt.outside_total(1, n)
    assert out["assumed"] == {"positions": 1, "trades": 1} and out["lost"] == {"bonds": 0, "trades": 0}


def test_a_bond_opened_today_at_the_report_is_its_value_less_the_cost(monkeypatch):
    # no lot yesterday: the difference of the reports would be 0 (the assumption), the PnL is PV(D) - the purchase
    lots, n = _reported_bond(monkeypatch, prev_pv=None, pv=1_005_000.0, quantity=1000.0, start=0.0, final=1000.0,
                             trades=[BUY], clean=100.5)
    assert lots["L1"].no_pnl is None and lots["L1"].pnl.total == pytest.approx(25_000.0)   # not -980 000
    assert "открыта сегодня" in lots["L1"].pnl.assumption["detail"]
    assert nt.outside_total(1, n)["assumed"] == {"positions": 1, "trades": 1}


def test_a_book_whose_report_did_not_change_has_the_trades_at_the_reports_price(monkeypatch):
    # the report holds the old figures and not the purchase: 1000 bought at 98 against the report's 100: +2 % of 1 000 000
    lots, n = _reported_bond(monkeypatch, prev_pv=8_000_000.0, pv=8_000_000.0, quantity=9000.0, start=8000.0, final=9000.0,
                             trades=[BUY], carried=date(2026, 10, 15), clean=100.0)
    assert lots["L1"].pnl.total == pytest.approx(20_000.0) and "отчёт не обновлялся" in lots["L1"].pnl.assumption["detail"]
    assert nt.outside_total(1, n)["assumed"] == {"positions": 1, "trades": 1}


def _bond_lots(monkeypatch, specs, trades, *, anchor, clean=100.0, row=None):
    """A bond of several lots at the report's figure, each by ``direct._reported`` as the run gives it. ``specs``: lot ->
    {pv, q, prev_pv (None: no report of D-1), start (None: held, quantity not known; absent: new today), stub (a report row
    without a number of bonds)}. ``row``: more of the bond's row of ``apply_intraday`` (``final``, ``final_fold``)."""
    from desk.orchestrator import Outcome
    from desk.pnl import direct

    prev = SimpleNamespace(business_date=date(2026, 10, 15))
    ctx = SimpleNamespace(business_date=date(2026, 10, 16))

    def lot(q, value, stub=False, acc=0.0):
        v = SimpleNamespace(quantity=q, valuation_date=ctx.business_date, pv_rub=value, quality="reported_value",
                            face_rub=math.nan if stub else 1000.0 * q, clean_pct=clean, accrued_rub=acc, z_spread_bp=1.0,
                            price_basis="p", extra={"qty_stub": True} if stub else {})
        return Outcome("", None, None, valuation=v)
    lots, members, started = {}, {}, {}
    for name, sp in specs.items():
        o = lot(sp["q"], sp["pv"], sp.get("stub", False), sp.get("acc", 0.0))
        p = None if sp.get("prev_pv") is None else lot(sp["q"], sp["prev_pv"])
        o.pnl = direct._reported(prev, ctx, o, p, None)
        lots[name], members[name] = o, sp["q"]
        if "start" in sp:
            started[name] = sp["start"]
    summary = {"trades": [{"row": anchor, "trade_id": f"T{i}", **t} for i, t in enumerate(trades)],
               "rows": {anchor: {"start": 1.0, "book": "B1", "bond": "S", "members": members, "members_start": started,
                               **(row or {})}}}
    monkeypatch.setattr(nt, "portfolio_pnl", lambda *a: {})
    n = nt.attach(prev, ctx, SimpleNamespace(), SimpleNamespace(position_ids=[], direct=lots), [], summary, None)
    return lots, n


def _new_pair(a, b):
    """Two lots opened today (no report of D-1) at 100.5 % of a face of 1000 each, 1000 bonds bought in each at 98."""
    return {a: {"pv": 1_005_000.0, "q": 1000.0}, b: {"pv": 2_010_000.0, "q": 2000.0}}


@pytest.mark.parametrize("names", [("L3", "L4"), ("L4", "L3")])
def test_two_lots_opened_today_have_the_bond_pnl_once_whatever_their_order(monkeypatch, names):
    a, b = names
    buys = [{**BUY, "quantity": 1000}, {**BUY, "quantity": 2000, "price_pct": 99.0}]
    lots, n = _bond_lots(monkeypatch, _new_pair(a, b), buys, anchor="L3")
    # the values of both lots less the cost of both purchases: 3 015 000 - (980 000 + 1 980 000) = 55 000
    assert math.fsum(o.pnl.total for o in lots.values()) == pytest.approx(55_000.0)
    assert lots["L3"].pnl.total == pytest.approx(55_000.0) and lots["L4"].pnl.total == 0.0   # on the first lot by id
    assert all(o.no_pnl is None for o in lots.values())
    assert nt.outside_total(2, n)["assumed"] == {"positions": 1, "trades": 2} and n["lost"] == []


@pytest.mark.parametrize("names", [("L1", "L3"), ("L3", "L1")])
def test_a_lot_held_yesterday_and_a_lot_opened_today_have_the_bond_pnl_once(monkeypatch, names):
    old, new = names
    specs = {old: {"pv": 7_035_000.0, "q": 7000.0, "prev_pv": 7_000_000.0, "start": 8000.0},
             new: {"pv": 1_005_000.0, "q": 1000.0}}   # L1 sold 1000 (8000 -> 7000), L3 bought 1000 at 98
    trades = [{"side": "sell", "quantity": 1000, "price_pct": 99.0, "accrued_ccy": 0.0, "ccy": "RUB"}, BUY]
    lots, n = _bond_lots(monkeypatch, specs, trades, anchor="L1")
    # the held lot's difference 35 000 and the new lot's value 1 005 000, less the net cost of the trades (bought 980 000,
    # sold 990 000): 1 050 000, wherever the lots stand
    assert math.fsum(o.pnl.total for o in lots.values()) == pytest.approx(35_000.0 + 1_005_000.0 - (980_000.0 - 990_000.0))
    assert sum(1 for o in lots.values() if o.pnl.total != 0.0) == 1   # the bond's PnL is on one lot
    assert nt.outside_total(2, n)["assumed"] == {"positions": 1, "trades": 2}


def test_a_lot_held_with_a_quantity_not_known_is_not_taken_for_a_new_one(monkeypatch):
    # the start report lists the lot but its quantity is not in the inputs (None); no report of D-1 either: lost
    specs = {"L1": {"pv": 1_005_000.0, "q": 1000.0, "start": None}}
    lots, n = _bond_lots(monkeypatch, specs, [BUY], anchor="L1")
    assert lots["L1"].pnl is None and lots["L1"].no_pnl.reason == nt.NOT_IN_PNL
    assert nt.outside_total(1, n)["lost"] == {"bonds": 1, "trades": 1}
    # absent from the start report: new today
    lots, n = _bond_lots(monkeypatch, {"L1": {"pv": 1_005_000.0, "q": 1000.0}}, [BUY], anchor="L1")
    assert lots["L1"].pnl.total == pytest.approx(25_000.0)


def test_a_purchase_missing_from_the_file_of_trades_is_taken_at_the_reports_price(monkeypatch):
    """The report holds 3000 bonds opened today, the file of trades one purchase of 1000: the other 2000 are a trade at the
    report's price, PnL 0, not a profit of their whole cost (the owner's rule: the file was late, not yet fetched or corrected)."""
    specs = {"L3": {"pv": 1_005_000.0, "q": 1000.0}, "L4": {"pv": 2_010_000.0, "q": 2000.0}}
    lots, n = _bond_lots(monkeypatch, specs, [BUY], anchor="L3", clean=100.5,
                         row={"mode": "qty", "final": 3000.0, "final_fold": 1000.0})
    assert math.fsum(o.pnl.total for o in lots.values()) == pytest.approx(25_000.0)   # the arrived purchase only
    assert lots["L3"].pnl.assumption["code"] == "report_trades_gap" and "без сделок в файле" in assumption_text(
        lots["L3"].pnl.assumption)
    assert nt.outside_total(1, n)["assumed"] == {"positions": 1, "trades": 1}
    # without the gap in the row the same report would be a profit of 2 010 000 more
    lots, _ = _bond_lots(monkeypatch, specs, [BUY], anchor="L3", clean=100.5)
    assert math.fsum(o.pnl.total for o in lots.values()) == pytest.approx(3_015_000.0 - 980_000.0)


def test_a_sale_missing_from_the_file_has_the_right_sign(monkeypatch):
    # 8000 at the start of the day, the report 7100: 1000 sold are not in the file, 100 bought are
    specs = {"L1": {"pv": 7_135_500.0, "q": 7100.0, "prev_pv": 8_000_000.0, "start": 8000.0}}
    lots, n = _bond_lots(monkeypatch, specs, [{**BUY, "quantity": 100}], anchor="L1", clean=100.5,
                         row={"mode": "qty", "final": 7100.0, "final_fold": 8100.0})
    # 7000 held x 5 + the 100 bought x 25 + the 1000 sold missing: sold at the report's price after +5 each
    assert lots["L1"].pnl.total == pytest.approx(7000 * 5.0 + 100 * 25.0 + 1000 * 5.0)
    assert nt.outside_total(1, n)["assumed"] == {"positions": 1, "trades": 1}


def test_a_lot_held_without_the_report_of_yesterday_has_its_start_at_todays_price(monkeypatch):
    # 8000 held, 9000 in the report, 1000 bought at 98, no report of D-1: the start part has PnL 0, the bought part 25 000
    specs = {"L1": {"pv": 9_045_000.0, "q": 9000.0, "start": 8000.0}}
    lots, n = _bond_lots(monkeypatch, specs, [BUY], anchor="L1", clean=100.5)
    assert lots["L1"].pnl.total == pytest.approx(25_000.0) and lots["L1"].no_pnl is None
    assert nt.outside_total(1, n)["assumed"] == {"positions": 1, "trades": 1}


@pytest.mark.parametrize("names", [("L3", "L4"), ("L4", "L3")])
def test_the_face_of_a_bond_is_taken_from_the_first_lot_that_tells_it(monkeypatch, names):
    a, b = names
    specs = {a: {"pv": 1_005_000.0, "q": 1.0, "stub": True}, b: {"pv": 1_005_000.0, "q": 1000.0}}
    lots, _ = _bond_lots(monkeypatch, specs, [BUY], anchor="L3", clean=100.5)
    assert math.fsum(o.pnl.total for o in lots.values()) == pytest.approx(2_010_000.0 - 980_000.0)
    assert lots["L4"].pnl.note.endswith("on the lot L3") and "on the lot" not in lots["L3"].pnl.note


def test_a_row_of_the_book_without_a_new_report_has_no_gap(monkeypatch):
    """A row of ``apply_intraday``'s ``changed`` (the book has no report of the day: valid since D-1) has ``final`` and no
    ``final_fold``: that is no gap of the report and the trades, the whole position is not a purchase missing from the file."""
    from desk.orchestrator import Outcome
    from desk.pnl import direct

    prev = SimpleNamespace(business_date=date(2026, 10, 15))
    ctx = SimpleNamespace(business_date=date(2026, 10, 16))

    def lot(q, value):
        v = SimpleNamespace(quantity=q, valuation_date=ctx.business_date, pv_rub=value, quality="reported_value",
                            face_rub=1000.0 * q, clean_pct=100.5, accrued_rub=0.0, z_spread_bp=1.0, price_basis="p", extra={})
        return Outcome("", None, None, valuation=v)
    o = lot(9000.0, 9_045_000.0)
    o.pnl = direct._reported(prev, ctx, o, lot(8000.0, 8_000_000.0), None)
    summary = {"trades": [{"row": "P1", "trade_id": "T", **BUY}],
               "rows": {"P1": {"start": 8000.0, "final": 9000.0, "mode": "qty", "new": False, "book": "B1", "bond": "S"}}}
    monkeypatch.setattr(nt, "portfolio_pnl", lambda *a: {})
    n = nt.attach(prev, ctx, SimpleNamespace(), SimpleNamespace(position_ids=[], direct={"P1": o}), [], summary, None)
    assert o.pnl.total == pytest.approx(65_000.0) and o.pnl.assumption["code"] == "report_trades_face"
    assert nt.outside_total(1, n)["assumed"] == {"positions": 1, "trades": 1}


def test_a_lot_held_and_sold_down_without_the_report_of_yesterday(monkeypatch):
    # 8000 held, 7000 in the report, 1000 sold at 99: the start part at today's price, the sale 15 000 under it
    specs = {"L1": {"pv": 7_035_000.0, "q": 7000.0, "start": 8000.0}}
    sale = {"side": "sell", "quantity": 1000, "price_pct": 99.0, "accrued_ccy": 0.0, "ccy": "RUB"}
    lots, _ = _bond_lots(monkeypatch, specs, [sale], anchor="L1", clean=100.5)
    assert lots["L1"].pnl.total == pytest.approx(-15_000.0)
    assert "старт по сегодняшней цене отчёта" in lots["L1"].pnl.assumption["detail"]
    # no quantity of the lot: nothing to take the start from
    lots, n = _bond_lots(monkeypatch, {"L1": {"pv": 7_035_000.0, "q": 7000.0, "start": None}}, [sale], anchor="L1", clean=100.5)
    assert lots["L1"].pnl is None and nt.outside_total(1, n)["lost"] == {"bonds": 1, "trades": 1}


def test_a_gap_in_face_amounts_carries_its_accrued_and_says_its_size(monkeypatch):
    """A row in face amounts: 3 000 000 in the report, 1 000 000 in the file's trades; its accrued 30 000 in the row. The
    2 000 000 missing are a trade at the report's dirty price: PnL 0 (the arrived purchase only: 25 000)."""
    buy = {**BUY, "accrued_ccy": 10_000.0}
    specs = {"L3": {"pv": 3_045_000.0, "q": 3000.0, "acc": 30_000.0}}
    lots, _ = _bond_lots(monkeypatch, specs, [buy], anchor="L3", clean=100.5,
                         row={"mode": "face", "final": 3_000_000.0, "final_fold": 1_000_000.0})
    a = lots["L3"].pnl.assumption
    assert lots["L3"].pnl.total == pytest.approx(25_000.0) and a["code"] == "report_trades_gap"
    assert "расхождение отчёта и сделок +2,000,000 номинала, сделки из файла учтены" in a["detail"]


def test_only_the_lot_with_the_total_carries_the_gap_code(monkeypatch):
    specs = {"L3": {"pv": 1_005_000.0, "q": 1000.0}, "L4": {"pv": 2_010_000.0, "q": 2000.0}}
    lots, _ = _bond_lots(monkeypatch, specs, [BUY], anchor="L3", clean=100.5,
                         row={"mode": "qty", "final": 3000.0, "final_fold": 1000.0})
    assert lots["L3"].pnl.assumption["code"] == "report_trades_gap" and "+2,000 бумаг" in lots["L3"].pnl.assumption["detail"]
    assert lots["L4"].pnl.assumption["code"] == "report_trades_face"
