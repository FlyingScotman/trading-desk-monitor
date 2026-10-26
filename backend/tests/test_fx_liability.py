"""The currency funding liability of foreign-currency bonds and their FX delta net of it (OQ-04, DEC01, owner's rule of
26.10.2026; TDM#54, TDM#169), on synthetic rows and the golden fixtures' USD-face bond: the liability (clean MV + the
accrued paid at the purchase), the accrued paid at the purchase from what the data has, the FX delta net of it, the OCP
that subtracts it once, ``fx_funding`` of the flash PnL, the bond currency without ``face_unit``, and a run end to end
(the row, the funding and OCP APIs, window.DESK)."""

from __future__ import annotations

import dataclasses
from datetime import date
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


def terms(*pay_dates, start=None):
    return SimpleNamespace(coupons=tuple(SimpleNamespace(pay_date=d, start=start if i == 0 else None)
                                         for i, d in enumerate(pay_dates)))


def test_engine_liability_is_clean_mv_plus_purchase_accrued():
    p = F.FundingPosition("P", "T1", "TRADING", "ENT1", "USD", 1e6, accrued=2e4, purchase_accrued=5e3)
    (x,), _ = F.funding_liabilities([p], policy=TRADING)
    assert x.amount == pytest.approx(-1_005_000)  # the bond keeps 15 000 of the accrued: earned since the purchase
    dirty = F.FundingPolicy(base_by_book_type={"TRADING": F.FundingBase.MV_DIRTY})
    (y,), _ = F.funding_liabilities([p], policy=dirty)
    assert y.amount == pytest.approx(-1_020_000)  # a dirty base covers the whole accrued
    (z,), _ = F.funding_liabilities([dataclasses.replace(p, purchase_accrued=0.0)], policy=TRADING)
    assert z.amount == -1e6


def test_purchase_accrued_from_what_the_data_has():
    p = F.FundingPosition("P", "T1", "TRADING", "ENT1", "USD", 1e6, accrued=2e4)
    t = terms(date(2026, 12, 1), start=date(2026, 9, 1))
    assert fx_liability.purchase_accrued(p, {"purchase_accrued_ccy": "5000", "purchase_date": "2026-09-20"}, t, ON) \
        == (5000.0, fx_liability.REPORT, "")
    short = dataclasses.replace(p, mv_clean=-1e6, accrued=-2e4)
    assert fx_liability.purchase_accrued(short, {"purchase_accrued_ccy": "5000"}, t, ON)[0] == -5000.0  # as the position
    # a coupon paid after the purchase returned the accrued paid then
    paid = terms(date(2026, 9, 1), date(2027, 3, 1), start=date(2026, 3, 1))
    acc, basis, detail = fx_liability.purchase_accrued(p, {"purchase_accrued_ccy": "5000", "purchase_date": "2026-05-01"},
                                                       paid, ON)
    assert (acc, basis) == (0.0, fx_liability.RETURNED) and "2026-09-01" in detail
    # no accrued in the report, a purchase date in the running period: linear share of today's accrued
    acc, basis, _ = fx_liability.purchase_accrued(p, {"purchase_date": "2026-09-22"}, t, ON)
    assert basis == fx_liability.ESTIMATED and acc == pytest.approx(2e4 * 21 / 44)
    # nothing: 0, said
    assert fx_liability.purchase_accrued(p, {}, t, ON)[:2] == (0.0, fx_liability.UNKNOWN)
    assert fx_liability.purchase_accrued(p, {"purchase_date": "2026-09-22"}, None, ON)[:2] == (0.0, fx_liability.UNKNOWN)


def test_fx_delta_net_of_the_liability():
    rows = [bond_row(), bond_row("T1|NOACC", accrued=None), bond_row("H1|USD", "H1"), bond_row("T1|RUB", ccy="RUB")]
    extra = {"T1|USD": {"purchase_accrued_ccy": "5000", "purchase_date": "2026-09-20"}}
    per = fx_liability.liabilities(rows, BOOKS, extra, {"T1|USD": terms(date(2026, 12, 1), start=date(2026, 9, 1))}, ON,
                                   TRADING, None)
    x = per["T1|USD"]
    assert x["state"] == "computed" and x["ccy"] == "USD"
    assert x["amount_ccy"] == pytest.approx(-1_005_000) and x["clean_mv_ccy"] == pytest.approx(1e6)
    assert x["fx_delta_gross"] == 1_020_000 and x["fx_delta_net"] == pytest.approx(15_000)  # accrued since purchase
    assert x["purchase_accrued_basis"] == "report"
    # accrued unknown: the liability on the dirty MV, said; an HTM book (cost base): no liability, said
    assert per["T1|NOACC"]["fx_delta_net"] == pytest.approx(0) and "НКД неизвестен" in per["T1|NOACC"]["note"]
    assert per["H1|USD"]["state"] == "none" and "cost" in per["H1|USD"]["reason"]
    assert "T1|RUB" not in per  # a RUB bond has no OCP
    # a problem: no base for the book type
    probs = fx_liability.liabilities([bond_row(book="X")], {"X": {"book_id": "X", "book_type": "OTHER"}}, {}, {}, ON,
                                     TRADING, None)
    assert probs["T1|USD"]["state"] == "problem"

    run = fx_liability.RunFxLiability("computed", {"positions": per}, per, {"T1|USD": (-10_050.0, -9_000.0)})
    content = [dict(r, extra=dict(r["extra"]), pnl={"fx_gross": 10_200.0, "fx_funding": 0.0}) for r in rows]
    o = SimpleNamespace(pnl=FlashPnl(*([None] * 6), *([0.0] * 17), price_basis=("", ""), fx_gross=10_200.0))
    ref, log = {"funding": {"status": "computed"}}, [("INFO", "last")]
    run.apply(content, ref, log, {"T1|USD": o})
    assert content[0]["fx_delta"] == {"USD": pytest.approx(15_000)} and content[0]["extra"]["fx_liability"] is x
    assert content[2]["fx_delta"] == {"USD": 1_020_000}  # no liability: the dirty MV stays
    assert content[0]["pnl"]["fx_funding"] == -10_050.0 and content[0]["pnl"]["fx_funding_cbr"] == -9_000.0
    assert o.pnl.as_tdm()["fx_funding"] == -10_050.0 and o.pnl.as_tdm()["fx_funding_cbr"] == -9_000.0
    assert ref["funding"]["fx_liability"]["positions"] is per and log[-1] == ("INFO", "last")


def test_fx_funding_is_the_liability_times_the_rate_move():
    assert fx_liability.fx_funding(-1e6, (80.0, 81.5), (79.9, 81.0)) == (pytest.approx(-1.5e6), pytest.approx(-1.1e6))
    assert fx_liability.fx_funding(-1e6, None, (None, 81.0)) == (None, None)
    # fx_gross + fx_funding: the bond's FX net of its funding (dirty 1 020 000, liability -1 005 000)
    gross = 1_020_000 * (81.5 - 80.0)
    assert gross + fx_liability.fx_funding(-1_005_000, (80.0, 81.5), (None, None))[0] == pytest.approx(15_000 * 1.5)
    assert DirectPnl("m", ON, ON, 0.0, fx_funding=3.0).as_tdm()["fx_funding"] == 3.0


def test_ocp_subtracts_the_liability_once():
    per = fx_liability.liabilities([bond_row()], BOOKS, {"T1|USD": {"purchase_accrued_ccy": "5000"}}, {}, ON,
                                   TRADING, None)
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
POSITIONS = ("book_id;position_id;asset_class;source_instrument_id;isin;ccy;quantity;purchase_date;purchase_accrued_ccy\n"
             f"T1;T1|USD;bond;SEC-USD;{USD_BOND};USD;1000;2026-10-01;1500\n")


def test_run_publishes_the_bond_net_of_its_liability(store_url, ctx, tmp_path):
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    for name, text in CFG.items():
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
    x = fact["extra"]["fx_liability"]
    assert x["state"] == "computed" and x["ccy"] == "USD" and x["purchase_accrued_basis"] == "report"
    assert x["amount_ccy"] == pytest.approx(-(x["clean_mv_ccy"] + 1500))
    assert x["fx_delta_gross"] == pytest.approx(fact["mv"])
    assert fact["fx_delta"]["USD"] == pytest.approx(x["accrued_ccy"] - 1500)  # the accrued earned since the purchase
    assert lin["fx_liability"]["status"] == "computed" and lin["fx_liability"]["positions"] == 1
    assert any("валютный пассив фондирования: 1 из 1" in m and "предварительно" in m for m in log)

    c = TestClient(create_app(store_url))
    fu = c.get(f"/api/v1/runs/{rid}/funding").json()["fx_liability"]
    assert fu["positions"]["T1|USD"]["fx_delta_net"] == pytest.approx(fact["fx_delta"]["USD"])
    assert fu["provisional"] and "по текущей чистой MV" in fu["provisional"][0]
    o = c.get(f"/api/v1/runs/{rid}/ocp").json()
    usd = o["by_ccy"]["USD"]["components"]
    assert usd == {"bonds": pytest.approx(fact["fx_delta"]["USD"])}  # no separate funding component: once
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()
    p = next(x for x in desk["positions"] if x["id"] == "T1|USD")
    assert p["fxDelta"] == {"USD": pytest.approx(fact["fx_delta"]["USD"])}  # the OCP tile sums the net figure


def test_fx_funding_of_the_run_from_the_previous_rows(monkeypatch):
    """``fx_funding`` of a bond with a flash PnL: the liability of the previous default run's row x the rate move (the
    PnL's own rates when it has them, else the markets'), x the CBR move; a bond new on D gets 0 and a note."""
    class View:
        def __init__(self, d, mkt, cbr):
            self.business_date, self._fx, self.fx_rates = d, {"moex_19": mkt, "cbr": cbr}, []

        def fx(self, ccy):
            return self._fx

    prev, cur = View(date(2026, 10, 14), 80.0, 79.0), View(ON, 81.0, 80.5)
    old = bond_row(mv=1_018_000.0, accrued=18_000.0)
    monkeypatch.setattr(fx_liability.funding_run, "_previous_rows", lambda con, d: (7, [old], {}))
    monkeypatch.setattr(fx_liability, "_books", lambda *a: BOOKS)
    rows = [dict(bond_row(), pnl={"fx_gross": 1.0}), dict(bond_row("T1|NEW"), pnl={"fx_gross": 1.0})]
    per = fx_liability.liabilities(rows, BOOKS, {}, {}, ON, TRADING, None)
    inputs = SimpleNamespace(known_at=None, extra={})
    out, notes = fx_liability._fx_funding(None, ON, prev, cur, rows, [], inputs, {}, {}, TRADING, None, per)
    assert out["T1|USD"] == (pytest.approx(-1e6 * 1.0), pytest.approx(-1e6 * 1.5))  # liability of D-1: clean 1 000 000
    assert out["T1|NEW"] == (0.0, None) and any("T1|NEW" in n for n in notes)
    o = SimpleNamespace(pnl=SimpleNamespace(fx_rates=(80.0, 80.4)))
    out, _ = fx_liability._fx_funding(None, ON, prev, cur, rows[:1], [], inputs, {}, {"T1|USD": o}, TRADING, None, per)
    assert out["T1|USD"][0] == pytest.approx(-1e6 * 0.4)  # the rates of the position's fx_gross
