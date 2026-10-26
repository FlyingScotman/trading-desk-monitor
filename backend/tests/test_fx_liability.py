"""The currency OCP liability of foreign-currency bonds and their FX delta net of it (OQ-04, OQ-17, DEC01; TDM#54,
TDM#169, TDM#266), on synthetic rows and the golden fixtures' USD-face bond: the liability (the dirty MV at the last
OCP transfer: the previous business day's, else the purchase cost or today's), the coupons of the day, the FX delta
net of it (the FX PnL since the transfer), the bond book's OCP after the day's transfer (the residual), the OCP that
subtracts the liability once, ``fx_funding`` of the flash PnL, the funding body unchanged (DEC01), the bond currency
without ``face_unit``, and a run end to end (the row, the funding and OCP APIs, window.DESK)."""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from desk import store
from desk.api import create_app
from desk.api.compat import funding_view
from desk.inbox import scan
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.pnl.direct import DirectPnl
from desk.pnl.flash import FlashPnl
from desk.publish import desk_run, fx_liability, tdm_run
from desk.rules import funding as F
from desk.rules import ocp as O
from desk.rules.rate_index import FixingTable, FxTable
from desk.settings import Settings
from desk.valuation.positions import merge, plan

ON = date(2026, 10, 15)
TRADING = F.FundingPolicy(base_by_book_type={"TRADING": F.FundingBase.MV_CLEAN, "HTM": F.FundingBase.COST})
BOOKS = {"T1": {"book_id": "T1", "book_type": "TRADING", "entity": "ENT1"},
         "H1": {"book_id": "H1", "book_type": "HTM", "entity": "ENT1"}}
USD_BOND = "RU000A10BRM5"  # the golden fixtures' USD-face bond (public market data)


def bond_row(pid="T1|USD", book="T1", mv=1_020_000.0, accrued=20_000.0, ccy="USD"):
    """A bond row valued without core (``tdm_run.direct_row`` shape): dirty MV and the report's accrued in currency."""
    return {"position_id": pid, "book_id": book, "instr_type": "BOND", "ccy": ccy, "mv": mv,
            "fx_delta": {ccy: mv} if ccy != "RUB" else {},
            "extra": {"reported": {"accrued_ccy": accrued} if accrued is not None else {}}}


def terms(*pay_dates, start=None, amount=None):
    return SimpleNamespace(coupons=tuple(SimpleNamespace(pay_date=d, start=start if i == 0 else None,
                                                         amount_per_bond=amount)
                                         for i, d in enumerate(pay_dates)))


D0 = date(2026, 10, 14)  # the previous business day of ON


def prev_row(pid="T1|USD", mv=1_010_000.0, qty=None, ccy="USD"):
    """The same position's row in the previous default run (of ``D0``)."""
    return {"position_id": pid, "book_id": "T1", "instr_type": "BOND", "ccy": ccy, "mv": mv,
            "extra": {"quantity_bonds": qty}}


def net(rows, previous=None, coupons=None, extra=None, d0=D0, pol=TRADING):
    return fx_liability.liabilities(rows, BOOKS, extra or {}, ON, pol, None, previous=previous, d0=d0, coupons=coupons)


def test_engine_liability_is_the_dirty_mv_of_the_position_as_given():
    """``funding_liabilities`` takes the position as of its reference (the last OCP transfer): - its dirty MV, on a
    clean or a dirty funding base alike (the base is the funding body's, DEC01, not the OCP liability's)."""
    p = F.FundingPosition("P", "T1", "TRADING", "ENT1", "USD", 1e6, accrued=2e4)
    (x,), _ = F.funding_liabilities([p], policy=TRADING)
    assert x.amount == pytest.approx(-1_020_000)
    dirty = F.FundingPolicy(base_by_book_type={"TRADING": F.FundingBase.MV_DIRTY})
    (y,), _ = F.funding_liabilities([p], policy=dirty)
    assert y.amount == pytest.approx(-1_020_000)
    assert F.funding_liabilities([dataclasses.replace(p, book_type="HTM")], policy=TRADING) == ((), ())  # cost base


def test_reference_from_what_the_store_has():
    row = dict(bond_row(), notional=1_000_000.0, extra={"quantity_bonds": 1000.0, "reported": {"accrued_ccy": 2e4}})
    # the previous business day's row: its dirty MV (the last transfer)
    assert fx_liability.reference(row, prev_row(), D0, True, {}, ON)[:2] == (1_010_000.0, fx_liability.PREV)
    # the quantity changed: the D-1 dirty price on today's quantity, said
    mv, kind, text = fx_liability.reference(row, prev_row(mv=505_000.0, qty=500.0), D0, True, {}, ON)
    assert (mv, kind) == (pytest.approx(1_010_000.0), fx_liability.PREV) and "количество 500 → 1000" in text
    # new on D, bought on D with a purchase price: the purchase cost (clean x face + the accrued paid)
    x = {"purchase_date": ON.isoformat(), "purchase_price_pct": "99.5", "purchase_accrued_ccy": "1500"}
    mv, kind, text = fx_liability.reference(row, None, D0, True, x, ON)
    assert (mv, kind) == (pytest.approx(996_500.0), fx_liability.PURCHASE) and "не было" in text
    short = dict(row, mv=-1_020_000.0, notional=-1_000_000.0)
    assert fx_liability.reference(short, None, D0, True, x, ON)[0] == pytest.approx(-996_500.0)  # as the position
    # bought before D-1, or no price, or no run of D-1, or no previous day: today's dirty MV (OCP 0), with the reason
    old = dict(x, purchase_date="2026-10-01")
    for args, why in (((None, D0, True, old), "позиции на 2026-10-14 не было"),
                      ((None, D0, True, {"purchase_date": ON.isoformat()}), "не было"),
                      ((None, D0, False, old), "нет прогона за 2026-10-14"),
                      ((None, None, False, old), "нет предыдущего рабочего дня"),
                      ((prev_row(ccy="EUR"), D0, True, {}), "в другой валюте")):
        mv, kind, text = fx_liability.reference(row, *args, ON)
        assert (mv, kind) == (1_020_000.0, fx_liability.CURRENT) and why in text and "ОВП 0" in text
    # no previous day at all, bought on D: the purchase cost
    assert fx_liability.reference(row, None, None, False, x, ON)[1] == fx_liability.PURCHASE


def test_price_rise_gives_the_change_in_dirty_mv_as_ocp():
    """A foreign bond whose price rose over the day: its OCP = the change of its dirty MV in the currency (+ the
    coupon of the day); the liability does not follow the MV."""
    per = net([bond_row(mv=1_020_000.0)], {"T1|USD": prev_row(mv=1_010_000.0)})
    x = per["T1|USD"]
    assert x["state"] == "computed" and x["reference"] == fx_liability.PREV and x["reference_date"] == "2026-10-14"
    assert x["amount_ccy"] == pytest.approx(-1_010_000) and x["fx_delta_gross"] == 1_020_000
    assert x["fx_delta_net"] == pytest.approx(10_000)  # the price move and the accrued growth: FX PnL in the OCP
    # a coupon paid on the day: the dirty MV dropped by it, the cash is FX PnL too
    per = net([bond_row(mv=995_000.0)], {"T1|USD": prev_row(mv=1_010_000.0)}, {"T1|USD": 25_000.0})
    assert per["T1|USD"]["fx_delta_net"] == pytest.approx(10_000) and per["T1|USD"]["coupons_ccy"] == 25_000.0
    # unchanged price: OCP 0 (yesterday's transfer took it all)
    assert net([bond_row()], {"T1|USD": prev_row(mv=1_020_000.0)})["T1|USD"]["fx_delta_net"] == pytest.approx(0)
    # no row of D-1 (no run): today's dirty MV, OCP 0, the coupon not counted
    x = net([bond_row()], None, {"T1|USD": 25_000.0})["T1|USD"]
    assert x["reference"] == fx_liability.CURRENT and x["fx_delta_net"] == pytest.approx(0) and x["coupons_ccy"] == 0


def test_coupons_received_on_the_day():
    row = dict(bond_row(), extra={"quantity_bonds": 1000.0})
    fp = SimpleNamespace(fx_rates=(80.0, 81.0), ccy="USD", received_rub=81.0 * 25_000)
    assert fx_liability.coupons_received(row, None, SimpleNamespace(pnl=fp), D0, ON) == (pytest.approx(25_000), "")
    t = terms(ON, date(2027, 4, 15), amount=Decimal(25))
    assert fx_liability.coupons_received(row, t, None, D0, ON) == (pytest.approx(25_000), "")
    assert fx_liability.coupons_received(row, terms(date(2026, 10, 1), amount=Decimal(25)), None, D0, ON) == (0, "")
    amount, why = fx_liability.coupons_received(row, terms(ON), None, D0, ON)
    assert amount == 0 and "2026-10-15 без суммы" in why
    amount, why = fx_liability.coupons_received(bond_row(), t, None, D0, ON)
    assert amount == 0 and "количество бумаг неизвестно" in why
    assert fx_liability.coupons_received(row, t, None, None, ON) == (0.0, "")  # no previous day: no window


def test_fx_delta_net_of_the_liability_and_apply():
    rows = [bond_row(), bond_row("H1|USD", "H1"), bond_row("T1|RUB", ccy="RUB")]
    per = net(rows, {"T1|USD": prev_row(mv=1_005_000.0)})
    x = per["T1|USD"]
    assert x["state"] == "computed" and x["ccy"] == "USD" and x["fx_delta_net"] == pytest.approx(15_000)
    assert x["clean_mv_ccy"] == pytest.approx(1e6) and x["base"] == "mv_clean"  # the funding base: as before
    assert per["H1|USD"]["state"] == "none" and "cost" in per["H1|USD"]["reason"]
    assert "T1|RUB" not in per  # a RUB bond has no OCP
    # a problem: no base for the book type
    probs = fx_liability.liabilities([bond_row(book="X")], {"X": {"book_id": "X", "book_type": "OTHER"}}, {}, ON,
                                     TRADING, None)
    assert probs["T1|USD"]["state"] == "problem"

    run = fx_liability.RunFxLiability("computed", {"positions": per}, per, {"T1|USD": (-10_050.0, -9_000.0)})
    content = [dict(r, extra=dict(r["extra"]), pnl={"fx_gross": 10_200.0, "fx_funding": 0.0}) for r in rows]
    o = SimpleNamespace(pnl=FlashPnl(*([None] * 6), *([0.0] * 17), price_basis=("", ""), fx_gross=10_200.0))
    ref, log = {"funding": {"status": "computed"}}, [("INFO", "last")]
    run.apply(content, ref, log, {"T1|USD": o})
    assert content[0]["fx_delta"] == {"USD": pytest.approx(15_000)} and content[0]["extra"]["fx_liability"] is x
    assert content[1]["fx_delta"] == {"USD": 1_020_000}  # no liability: the dirty MV stays
    assert content[0]["pnl"]["fx_funding"] == -10_050.0 and content[0]["pnl"]["fx_funding_cbr"] == -9_000.0
    assert o.pnl.as_tdm()["fx_funding"] == -10_050.0 and o.pnl.as_tdm()["fx_funding_cbr"] == -9_000.0
    assert ref["funding"]["fx_liability"]["positions"] is per and log[-1] == ("INFO", "last")


def test_fx_funding_is_the_liability_times_the_rate_move():
    assert fx_liability.fx_funding(-1e6, (80.0, 81.5), (79.9, 81.0)) == (pytest.approx(-1.5e6), pytest.approx(-1.1e6))
    assert fx_liability.fx_funding(-1e6, None, (None, 81.0)) == (None, None)
    # fx_gross + fx_funding: the revaluation of yesterday's OCP, which the transfer moved out: 0 when the liability is
    # the D-1 dirty MV the flash PnL's fx_gross revalues
    gross = 1_010_000 * (81.5 - 80.0)
    assert gross + fx_liability.fx_funding(-1_010_000, (80.0, 81.5), (None, None))[0] == pytest.approx(0)
    assert DirectPnl("m", ON, ON, 0.0, fx_funding=3.0).as_tdm()["fx_funding"] == 3.0


def _transfer(pid, book, usd, to):
    row = {"position_id": pid, "book_id": book, "instr_type": "FX", "fx_delta": {"USD": usd},
           "extra": {"reported": {"asset_class": "fx", "value_date": "2026-10-16", "deal_id": pid}}}
    return row, {"trade_id": pid, "book_id": book, "counterparty_id": to, "value_date": "2026-10-16"}


def test_bond_book_ocp_after_the_transfer_is_the_residual():
    """The bond book's OCP = Σ its bonds' FX PnL since the last transfer − the day's transfer, counted once: 0 when
    the transfer matched, the residual when it did not."""
    books = {**BOOKS, "T1": {**BOOKS["T1"], "ocp_to": "X1"},
             "X1": {"book_id": "X1", "book_type": "DERIVS", "entity": "ENT1"}}
    per = net([bond_row(mv=1_020_000.0)], {"T1|USD": prev_row(mv=1_010_000.0)}, {"T1|USD": 2_500.0})
    bond = bond_row(mv=1_020_000.0)
    fx_liability.RunFxLiability("computed", {}, per).apply([bond], {}, [])
    assert bond["fx_delta"]["USD"] == pytest.approx(12_500)  # the day's FX PnL: price, accrued, coupon
    for moved, left in ((12_500.0, 0.0), (12_000.0, 500.0)):
        out, out_t = _transfer("T1|XFER", "T1", -moved, "X1")
        into, into_t = _transfer("X1|XFER", "X1", moved, "T1")
        for pol in (O.PROVISIONAL_POLICY, O.OcpPolicy(bond_funding_liability=True)):
            r = O.compute_ocp([bond, out, into], books, ON, terms=[out_t, into_t], policy=pol,
                              liabilities=[F.FundingLiability("T1|USD", "T1", "ENT1", "USD", per["T1|USD"]["amount_ccy"])])
            t1 = O.derive(r.lines)[("ENT1", "T1", "USD")]
            assert t1 == pytest.approx({"bonds": 12_500, "transfers": -moved})  # the liability and the transfer, once
            assert sum(t1.values()) == pytest.approx(left)
            t = {x["book_id"]: x for x in r.transfers}["T1"]
            assert (t["before"], t["transferred"], t["after"]) == (pytest.approx(12_500), -moved, pytest.approx(left))
            assert O.by_currency(r.lines)["USD"]["transfers"] == 0  # the desk keeps it: moved between books


def test_ocp_subtracts_the_liability_once():
    per = net([bond_row()], {"T1|USD": prev_row(mv=1_005_000.0)})
    row = bond_row()
    fx_liability.RunFxLiability("computed", {}, per).apply([row], {}, [])
    liab = [F.FundingLiability("T1|USD", "T1", "ENT1", "USD", per["T1|USD"]["amount_ccy"])]
    for pol in (O.PROVISIONAL_POLICY, O.OcpPolicy(bond_funding_liability=True)):  # off by default, and even when on
        r = O.compute_ocp([row], BOOKS, ON, liabilities=liab, policy=pol)
        assert O.derive(r.lines) == {("ENT1", "T1", "USD"): {"bonds": pytest.approx(15_000)}}
        assert r.per_position["T1|USD"]["basis"] == "mv_net"
    assert any("второй раз не вычитается" in n for n in r.notes)
    assert not O.PROVISIONAL_POLICY.bond_funding_liability
    assert "bond_funding_liability" not in O.provisional(O.PROVISIONAL_POLICY)  # OQ-04 is decided
    # a row still gross (a run before TDM#54) with the component on: subtracted once, as the funding component
    r = O.compute_ocp([bond_row()], BOOKS, ON, liabilities=liab, policy=O.OcpPolicy(bond_funding_liability=True))
    assert O.derive(r.lines)[("ENT1", "T1", "USD")] == pytest.approx({"bonds": 1_020_000, "funding": -1_005_000})


def test_compat_line_has_the_liability_not_in_its_fx_delta():
    block = {"status": "computed", "text": "", "lines": [
        {"key": "POS|T1|USD", "kind": "POS", "book_id": "T1", "position_id": "T1|USD", "ccy": "USD", "body_ccy": -1e6,
         "rate_pct": 5.0, "rate_expr": "x", "day_basis": "ACT/365", "first_day": "2026-10-15", "interest_rub": -100.0,
         "in_position": True}],
        "fx_liability": {"positions": {"T1|USD": {"state": "computed", "ccy": "USD", "amount_ccy": -1_005_000.0,
                                                  "fx_funding": -50.0}}}}
    v = funding_view(block, {"USD": 80.0})
    line = v["lines"][0]
    assert line["fxDelta"] == {} and line["fxLiability"] == {"USD": -1_005_000.0}  # the position's fxDelta holds it
    assert line["pnl"]["fxAllocated"] == -50.0 and v["by_book"]["T1"]["fxAllocated"] == -50.0
    assert v["pnl"]["fx"] == 0.0  # in the position's PnL, not again at book level


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(Path(__file__).parent / "fixtures" / "mdhub_v1"), ON.isoformat())


def test_foreign_bond_without_face_unit_keeps_its_fx_delta(ctx):
    o = value(ctx, [(USD_BOND, 10.0)], position_ids=["P1"])[0]
    t = o.terms
    assert t.face_unit == "USD"
    for patched in (dataclasses.replace(t, face_unit=None, currency="USD"), t):
        o.terms = patched
        row = tdm_run.run_content(ctx, [("P1", "CORP", o)], None, {}).rows[0]
        assert row["ccy"] == "USD" and row["fx_delta"]["USD"] == pytest.approx(row["mv"]) and row["fx_delta"]["USD"]
    o.terms = dataclasses.replace(t, face_unit=None, currency="SUR")  # RUR / SUR are RUB
    row = tdm_run.run_content(ctx, [("P1", "CORP", o)], None, {}).rows[0]
    assert row["ccy"] == "RUB" and "fx_delta" not in row


# ---------------------------------------------------------------------------------------------- end to end
BOM = "﻿"
CFG = {
    "books.csv": "book_id;name;desk;book_type;entity;ocp_to;trader;active;valid_from\n"
                 "T1;Bond trading;Rates;TRADING;ENT1;;;1;\n",
    "entities.csv": "entity;name;funding_scheme;pnl_sweep;active;valid_from\nENT1;Entity one;by_book_type;;1;\n",
    "funding_rules.csv": "rule_id;valid_from;active;book_type;entity;book_id;ccy;instrument_id;basis;rate_kind;"
                         "index_code;spread_bp;fixed_rate_pct;day_count\n"
                         "trading_rub;;1;TRADING;;;RUB;;mv_clean;index_spread;KEY_RATE;100;;ACT/365\n",
}
POSITIONS = ("book_id;position_id;asset_class;source_instrument_id;isin;ccy;quantity;purchase_date;purchase_price_pct;"
             f"purchase_accrued_ccy\nT1;T1|USD;bond;SEC-USD;{USD_BOND};USD;1000;2026-10-15;99.5;1500\n")
"""Bought on ``ON``; the run has no previous day: the liability is the purchase cost."""


def _cost(fact):
    return fact["notional"] * 99.5 / 100 + 1500


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """A settings folder of its own: the funding settings (#237) are the defaults unless a test writes them."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DESK_HOME", str(home))
    return home


def _publish(store_url, ctx, tmp_path, cfg_files):
    """Scan the config and the USD bond's positions, publish the run of ``ON``: (fact, lineage, log, run id)."""
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    for name, text in cfg_files.items():
        (cfg / name).write_bytes((BOM + text.replace("\n", "\r\n")).encode())
    (inbox / "positions_20261016.csv").write_bytes((BOM + POSITIONS.replace("\n", "\r\n")).encode())
    s = Settings(snapshot_cache=Path(__file__).parent / "fixtures" / "mdhub_v1", inbox_dirs=(inbox,),
                 inbox_archive=tmp_path / "archive", private_adapter=None, private_adapter_config=cfg,
                 inbox_poll_seconds=0, inbox_lookback_days=0)
    with store.connect(store_url) as con:
        assert scan.scan(con, s).ok
        inp = store.positions_for(con, ON)
        bp = plan(ctx, inp)
        pos = merge(bp, value(ctx, bp.requests, position_ids=bp.position_ids))
        rid, st, _ = desk_run.publish(con, ctx, pos, prev=None, inputs=inp)
        assert st == "complete"
        fact, = con.execute("SELECT fact FROM run_position WHERE run_id = %s", (rid,)).fetchone()
        lin, = con.execute("SELECT lineage FROM run WHERE run_id = %s", (rid,)).fetchone()
        log = [m for (m,) in con.execute("SELECT message FROM run_log WHERE run_id = %s ORDER BY seq", (rid,))]
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    return fact, lin, log, rid


def test_run_publishes_the_bond_net_of_its_liability(store_url, ctx, tmp_path):
    fact, lin, log, rid = _publish(store_url, ctx, tmp_path, CFG)
    x = fact["extra"]["fx_liability"]
    assert x["state"] == "computed" and x["ccy"] == "USD" and x["reference"] == fx_liability.PURCHASE
    assert x["amount_ccy"] == pytest.approx(-_cost(fact)) and x["fx_delta_gross"] == pytest.approx(fact["mv"])
    assert fact["fx_delta"]["USD"] == pytest.approx(fact["mv"] - _cost(fact))  # the FX PnL since the purchase
    assert lin["fx_liability"]["status"] == "computed" and lin["fx_liability"]["positions"] == 1
    assert any("валютный ОВП-пассив: 1 из 1" in m and "предварительно: residual" in m for m in log)

    c = TestClient(create_app(store_url))
    fu = c.get(f"/api/v1/runs/{rid}/funding").json()["fx_liability"]
    assert fu["positions"]["T1|USD"]["fx_delta_net"] == pytest.approx(fact["fx_delta"]["USD"])
    assert fu["positions"]["T1|USD"]["reference"] == "purchase_cost"
    assert fu["provisional"] and "residual прошлых дней не накапливается" in fu["provisional"][0]
    assert any("стоимость покупки" in n for n in fu["notes"])
    o = c.get(f"/api/v1/runs/{rid}/ocp").json()
    usd = o["by_ccy"]["USD"]["components"]
    assert usd == {"bonds": pytest.approx(fact["fx_delta"]["USD"])}  # no separate funding component: once
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()
    p = next(x for x in desk["positions"] if x["id"] == "T1|USD")
    assert p["fxDelta"] == {"USD": pytest.approx(fact["fx_delta"]["USD"])}  # the OCP tile sums the net figure


class _View:
    def __init__(self, d, mkt, cbr):
        self.business_date, self._fx, self.fx_rates = d, {"moex_19": mkt, "cbr": cbr}, []

    def fx(self, ccy):
        return self._fx


def test_run_liability_from_the_previous_rows(monkeypatch):
    """The run of D on the previous default run's rows: the liability is the D-1 dirty MV, the OCP the change of the
    dirty MV + the coupon of the flash PnL; ``fx_funding`` that liability x the rate move (the PnL's own rates when it
    has them, else the markets'), x the CBR move; a bond new on D gets the fallback, ``fx_funding`` 0 and a note."""
    prev, cur = _View(D0, 80.0, 79.0), _View(ON, 81.0, 80.5)
    monkeypatch.setattr(fx_liability.funding_run, "_previous_rows",
                        lambda con, d: (7, [prev_row(mv=1_010_000.0)], {}) if d == D0 else (None, [], {}))
    monkeypatch.setattr(fx_liability, "_books", lambda *a: BOOKS)
    monkeypatch.setattr(fx_liability, "resolve_policy",
                        lambda *a: fx_liability._Policy(TRADING, None, "computed", ""))
    rows = [dict(bond_row(mv=1_017_500.0), pnl={"fx_gross": 1.0}), dict(bond_row("T1|NEW"), pnl={"fx_gross": 1.0})]
    o = SimpleNamespace(pnl=SimpleNamespace(fx_rates=(80.0, 80.4), ccy="USD", received_rub=80.4 * 2_500))
    inputs = SimpleNamespace(known_at=None, extra={})
    res = fx_liability.compute(None, ON, prev, cur, rows, [], inputs, {}, {"T1|USD": o})
    x, new = res.per_position["T1|USD"], res.per_position["T1|NEW"]
    assert res.status == "computed" and x["reference"] == fx_liability.PREV
    assert x["amount_ccy"] == pytest.approx(-1_010_000) and x["coupons_ccy"] == pytest.approx(2_500)
    assert x["fx_delta_net"] == pytest.approx(10_000)  # 1 017 500 - 1 010 000 + 2 500
    assert res.pnl["T1|USD"] == (pytest.approx(-1_010_000 * 0.4), pytest.approx(-1_010_000 * 1.5))  # the PnL's rates
    assert new["reference"] == fx_liability.CURRENT and new["fx_delta_net"] == pytest.approx(0)
    assert res.pnl["T1|NEW"] == (0.0, None)
    assert any("T1|NEW" in n and "fx_funding — 0" in n for n in res.block["notes"])
    # without the outcome's rates: the markets'
    res = fx_liability.compute(None, ON, prev, cur, rows[:1], [], inputs, {}, {})
    assert res.pnl["T1|USD"][0] == pytest.approx(-1_010_000 * 1.0)
    # no run of D-1: today's dirty MV, said
    res = fx_liability.compute(None, ON, _View(date(2026, 10, 13), 80.0, 79.0), cur, rows[:1], [], inputs, {}, {})
    assert res.per_position["T1|USD"]["reference"] == fx_liability.CURRENT
    assert any("нет прогона за 2026-10-13" in n for n in res.block["notes"])


def test_purchase_accrued_is_in_the_liability_not_in_the_funding_body():
    """The rate accrues on the clean price of the day (DEC01): the funding body is the clean MV, unchanged by the OCP
    rule (TDM#266). The currency OCP liability is not that body: it is the dirty MV at the last transfer, which no
    longer holds a separate accrued paid at the purchase."""
    rules = F.FundingRules((F.FundingRule(ccy="USD", book_type="TRADING", fixed_rate=0.0365),))
    p = F.FundingPosition("P", "T1", "TRADING", "ENT1", "USD", 1e6, accrued=2e4)
    period = F.AccrualPeriod(date(2026, 10, 14), ON)
    res = F.compute_funding(period, rules, FixingTable({}), FxTable("RUB", {"USD": {ON: 80.0}}), positions=[p],
                            previous_positions=[p], policy=TRADING)
    (line,) = res.lines
    assert line.days[-1].body == -1e6  # clean MV only
    assert line.interest == pytest.approx(-1e6 * 0.0365 / 365)  # interest on the clean MV
    # the price rose over the day: the OCP liability stays at the D-1 dirty MV, the move is in the OCP
    per = net([bond_row(mv=1_030_000.0)], {"T1|USD": prev_row(mv=1_020_000.0)})
    assert per["T1|USD"]["amount_ccy"] == pytest.approx(-1_020_000) and per["T1|USD"]["fx_delta_net"] == 10_000


# ---- #237: the liability takes the run's funding policy, the settings' and the default rate's included ---------------

NO_RULES = {k: v for k, v in CFG.items() if k != "funding_rules.csv"}


def test_liability_without_adapter_rules_at_the_default_rate(store_url, ctx, tmp_path):
    """No adapter rule, the default settings (the market default rate on): funding is configured, the TRADING base is
    DEC01's clean MV, so the bond's liability is computed (the purchase cost: bought on the day), not "not
    configured"."""
    fact, lin, _, _ = _publish(store_url, ctx, tmp_path, NO_RULES)
    x = fact["extra"]["fx_liability"]
    assert lin["fx_liability"]["status"] == "computed" and x["state"] == "computed" and x["base"] == "mv_clean"
    assert x["amount_ccy"] == pytest.approx(-_cost(fact))
    assert fact["fx_delta"]["USD"] == pytest.approx(fact["mv"] - _cost(fact))  # not the whole dirty MV


def test_liability_takes_the_settings_base(store_url, ctx, tmp_path, _home):
    """The TRADING base given by a settings rule only (default rate off): the liability is on it, not "none"."""
    from desk.settings import update_settings_json
    update_settings_json({"funding": {"default_rate": {"kind": "none"}, "default_rate_by_ccy": {}, "rules": [
        {"rule_id": "s_trading", "book_type": "TRADING", "basis": "mv_dirty", "rate_kind": "none"}]}}, _home)
    fact, lin, _, _ = _publish(store_url, ctx, tmp_path, NO_RULES)
    x = fact["extra"]["fx_liability"]
    assert lin["fx_liability"]["status"] == "computed" and x["state"] == "computed" and x["base"] == "mv_dirty"
    assert x["amount_ccy"] == pytest.approx(-_cost(fact))  # the OCP liability does not depend on the funding base


def test_liability_not_configured_only_without_any_rule_and_default(store_url, ctx, tmp_path, _home):
    from desk.settings import update_settings_json
    update_settings_json({"funding": {"default_rate": {"kind": "none"}, "default_rate_by_ccy": {}}}, _home)
    fact, lin, _, _ = _publish(store_url, ctx, tmp_path, NO_RULES)
    assert lin["fx_liability"]["status"] == "not_configured"
    assert fact["extra"]["fx_liability"]["state"] == "not_configured"


def test_cny_bond_liability_from_the_shared_policy(store_url):
    """A CNY bond of a TRADING book, no adapter rule, the default settings (CNY at RUSFARCNY): the shared policy
    (``funding_run.run_policy``) gives DEC01's clean base; without a previous day the liability is today's dirty MV."""
    from desk import funding_settings
    with store.connect(store_url) as con:
        rp = fx_liability.resolve_policy(con, ON, None, funding_settings.from_doc(None))
    assert rp.status == "computed" and rp.policy.base_by_book_type["TRADING"] is F.FundingBase.MV_CLEAN
    per = fx_liability.liabilities([bond_row("T1|CNY", ccy="CNY")], BOOKS, {}, ON, rp.policy, rp.entities)
    x = per["T1|CNY"]
    assert x["state"] == "computed" and x["ccy"] == "CNY" and x["reference"] == fx_liability.CURRENT
    assert x["amount_ccy"] == pytest.approx(-1_020_000.0) and x["clean_mv_ccy"] == pytest.approx(1_000_000.0)
