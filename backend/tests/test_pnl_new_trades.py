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
               face="", status="live"):
    return (f"{tid};1;{status};2026-10-16;2026-10-16T{hhmm}:00;B1;{pid};bond;{instr};{side};{qty};{face};{price};{acc};"
            f"{ccy};{instr};{ISIN.get(instr, '')}\n")


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


def test_foreign_bond_accrued_is_taken_at_todays_rate(monkeypatch, stub_face):
    monkeypatch.setattr(nt, "fx_rate", lambda ctx, ccy: (90.0, "test"))
    terms = SimpleNamespace(face_unit="USD", currency="USD")
    r = nt.trade_pnl(_ctx(), "S", terms, _ref(face_rub=90_000.0, dirty_rub=60_000.0), _t(ccy="USD", accrued_ccy="10",
                                                                                     price_pct="64"))
    assert r["cost_rub"] == pytest.approx(100 * 0.64 * 90_000 + 10 * 90)
    monkeypatch.setattr(nt, "fx_rate", lambda ctx, ccy: None)
    assert nt.trade_pnl(_ctx(), "S", terms, _ref(), _t(ccy="USD"))["flag"] == "no_fx_rate"


def test_a_face_amount_is_the_number_of_bonds_by_the_initial_face(stub_face):
    r = nt.trade_pnl(_ctx(), "S", TERMS, _ref(), _t(quantity=None, face_amount_ccy="50000"))
    assert r["quantity"] == pytest.approx(50.0)


def test_no_valuation_is_zero_and_flagged(stub_face):
    assert nt.trade_pnl(_ctx(), "S", TERMS, None, _t())["flag"] == "no_valuation"
    assert nt.trade_pnl(_ctx(), "S", TERMS, _ref(dirty_rub=math.nan), _t())["flag"] == "no_valuation"


def test_in_positions_when_the_book_report_is_as_of_the_day(monkeypatch):
    import test_trades_intraday as tti

    rows = [tti._trade()]
    monkeypatch.setattr(store, "dataset_rows", lambda con, ds, on: store.DatasetInputs(ds, on, rows, [{"set_id": 9}]))
    inputs = tti._inputs()
    same = store.PositionInputs(**{**inputs.__dict__, "valid_dates": {"T1": date(2026, 11, 3), "T2": date(2026, 11, 3)}})
    out, summary = trades.apply_intraday(None, date(2026, 11, 3), same)
    assert summary["skipped"] == {"in_positions": 1} and summary["applied"] == 0 and out is same
    assert trades.effect(rows[0], date(2026, 11, 3)) is None


# ---------- the whole path ----------

PID = "B1|SEC-26238"
ROW = ("SELECT p.quantity, p.pv_rub, p.pnl, p.no_pnl FROM run_position p WHERE p.run_id = %s AND p.position_id = %s")


class Day:
    """The intraday run of 16.10 on the stub hub, before and after files of trades are dropped."""

    def __init__(self, hub, tmp_path, store_url):
        self.s = cfg(tmp_path, store_url, hub)
        self.url, self.tmp, self.hub = store_url, tmp_path, hub
        put(tmp_path, "positions_20261016.csv")
        self.clock = {"t": at(FRI, 10, 6)}
        self.ap = Autopilot(settings_loader=lambda: self.s, now=lambda: self.clock["t"])
        publish(hub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)
        self.ap.run_cycle()
        self.base = self.row()

    def latest_id(self, rtype="INTRADAY"):
        with store.connect(self.url) as con:
            return con.execute("SELECT max(run_id) FROM run WHERE run_type = %s AND business_date = %s"
                               " AND status = 'complete'", (rtype, FRI)).fetchone()[0]

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
    # the report as of the end of 16.10 (the start of 19.10): the trades are in it, the close is made once more
    src = day.tmp / "in" / "positions_20261016.csv"
    (day.tmp / "in" / "positions_20261019.csv").write_text(src.read_text(encoding="utf-8-sig"), encoding="utf-8")
    day.clock["t"] = at(MON, 8, 50)
    by = steps(day.ap.run_cycle())
    assert by["eod"][0] == "ok" and "positions changed" in by["eod"][1], by["eod"]
    again = day.latest_id("EOD")
    assert again > final
    r = day.row(rid=again)
    assert r["q"] == 12000 and r["pnl"]["new_trades"] == 0.0   # the report's quantity, the trades not counted twice
    assert lineage(day.url, again)["skipped"] == {"in_positions": 1}
    day.clock["t"] = at(MON, 9, 0)
    assert steps(day.ap.run_cycle())["eod"][1].startswith("up to date") and day.latest_id("EOD") == again
    with store.connect(day.url) as con:
        from desk import trades as tr
        assert {x["trade_id"]: x["intraday"] for x in tr.view(con, FRI)["rows"]} == {"S1": "in_positions"}
