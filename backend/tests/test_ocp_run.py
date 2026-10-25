"""Wave B2 (TDM#59): the OCP in the run (``desk.publish.ocp_run``) end to end on synthetic data. The reference
adapter's config (books with an ``ocp_to``, entities, funding rules) and inbox files (positions, cash balances,
trade terms with an OCP transfer deal, the reported ``ocp``) scanned into the store, a run published, the OCP read back
through ``/api/v1/runs/{id}/ocp`` and window.DESK: the derived OCP by component, the break per currency explained by
the components, the transfer, the run log and lineage; a run without a report; a run before B2."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import store
from desk.api import create_app
from desk.api.app import ocp_out
from desk.inbox import scan
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.publish import desk_run, ocp_run
from desk.settings import Settings
from desk.valuation.positions import merge, plan

FIX = Path(__file__).parent / "fixtures"
SNAPS = FIX / "mdhub_v1"
DATE = date(2026, 10, 15)
BOM = "﻿"

BOOKS = """book_id;name;desk;book_type;entity;ocp_to;trader;active;valid_from
T1;Bond trading;Rates;TRADING;ENT1;;;1;
D1;Derivatives;Rates;DERIVS;ENT1;X1;;1;
X1;Transfer book;Rates;DERIVS;ENT1;;;1;
"""
ENTITIES = """entity;name;funding_scheme;pnl_sweep;active;valid_from
ENT1;Entity one;by_book_type;;1;
"""
RULES = """rule_id;valid_from;active;book_type;entity;book_id;ccy;instrument_id;basis;rate_kind;index_code;spread_bp;fixed_rate_pct;day_count
trading_rub;;1;TRADING;;;RUB;;mv_clean;index_spread;KEY_RATE;100;;ACT/365
derivs_rub;;1;DERIVS;;;RUB;;cash;index_spread;RUONIA;0;;ACT/365
"""
# start_of_day: the positions file of the 16th is the book of the 15th
POSITIONS = """book_id;position_id;asset_class;source_instrument_id;isin;ccy;quantity;amount_ccy;value_date;deal_id
T1;T1|26238;bond;SEC-26238;RU000A1038V6;RUB;10000;;;
D1;D1|USD;fx;USD;;USD;;1000000;;
D1;D1|FWD;fx;USD-FWD;;USD;;250000;2026-12-17;
D1;D1|XFER;fx;USD-XFER;;USD;;-900000;2026-10-16;XF-1
X1;X1|XFER;fx;USD-XFER;;USD;;900000;2026-10-16;XF-2
"""
# the transfer deals face each other's book (D1's ocp_to is X1); spot-dated, so the run keeps them at their amount
TERMS = """trade_id;leg_id;trade_version;book_id;entity;position_id;product_kind;source_product;trade_date;value_date;base_ccy;quote_ccy;side;notional_base;notional_quote;contract_rate;ndf;counterparty_id
XF-1;;1;D1;ENT1;D1|XFER;fx_forward;OCPXFER;2026-10-15;2026-10-16;USD;RUB;sell;900000;73125000;81.25;0;X1
XF-2;;1;X1;ENT1;X1|XFER;fx_forward;OCPXFER;2026-10-15;2026-10-16;USD;RUB;buy;900000;73125000;81.25;0;D1
"""
CASH = """entity;book_id;ccy;account_id;balance_ccy
ENT1;D1;USD;MAIN;50000
ENT1;D1;RUB;MAIN;-100000000
"""
# the report: D1 in USD without its forward (a report that leaves out the FX legs), T1 covered without rows
OCP = """entity;book_id;ccy;ocp_ccy;ocp_rub;rate_rub_per_unit
ENT1;D1;USD;150000;;
ENT1;T1;;;;
"""


def _csv(path: Path, text: str) -> None:
    path.write_bytes((BOM + text.replace("\n", "\r\n")).encode())


@pytest.fixture(autouse=True)
def _fresh_process():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(SNAPS), DATE.isoformat())


def _scan(con, tmp_path: Path, report: bool = True) -> None:
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    for name, text in (("books.csv", BOOKS), ("entities.csv", ENTITIES), ("funding_rules.csv", RULES)):
        _csv(cfg / name, text)
    files = [("positions_20261016.csv", POSITIONS), ("trade_terms_20261015.csv", TERMS),
             ("cash_balances_20261015.csv", CASH)] + ([("ocp_20261015.csv", OCP)] if report else [])
    for name, text in files:
        _csv(inbox / name, text)
    s = Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive", private_adapter=None,
                 private_adapter_config=cfg, inbox_poll_seconds=0, inbox_lookback_days=0)
    r = scan.scan(con, s)
    assert r.ok and r.files == {"accepted": len(files)}, r.text()


def _run(con, ctx) -> tuple[int, list[str]]:
    inp = store.positions_for(con, DATE)
    bp = plan(ctx, inp)
    pos = merge(bp, value(ctx, bp.requests, position_ids=bp.position_ids))
    rid, st, warnings = desk_run.publish(con, ctx, pos, prev=None, inputs=inp)
    assert st == "complete"
    return rid, warnings


def test_ocp_end_to_end_inbox_run_api(store_url, ctx, tmp_path):
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        rid, warnings = _run(con, ctx)
        lin, = con.execute("SELECT lineage FROM run WHERE run_id = %s", (rid,)).fetchone()
        log = [m for (m,) in con.execute("SELECT message FROM run_log WHERE run_id = %s ORDER BY seq", (rid,))]
    c = TestClient(create_app(store_url))
    o = c.get(f"/api/v1/runs/{rid}/ocp").json()
    assert o["state"] == "computed" and o["as_of"] == "2026-10-15"
    assert o["policy"]["provisional"] and "report_components" in o["policy"]["provisional"]
    comp = {x["position_id"]: x["component"] for x in o["lines"] if x["position_id"]}
    assert comp["D1|USD"] == "cash" and comp["D1|FWD"] == "fx_legs"
    assert comp["D1|XFER"] == comp["X1|XFER"] == "transfers"
    assert "T1|26238" not in comp  # a RUB bond: no OCP
    cash = [x for x in o["lines"] if x["key"].startswith("CASH|")]
    assert [(x["book_id"], x["ccy"], x["amount"]) for x in cash] == [("D1", "USD", 50_000)]  # RUB balance: no OCP

    # the break of D1 in USD and its explanation: the report holds cash and the transfer, not the forward
    rec = {(x["book_id"], x["ccy"]): x for x in o["reconciliation"]}
    d1 = rec[("D1", "USD")]
    fwd = d1["components"]["fx_legs"]
    assert d1["components"]["cash"] == pytest.approx(1_050_000) and d1["components"]["transfers"] == -900_000
    assert fwd == pytest.approx(250_000)  # a forward without trade terms: its notional
    assert d1["reported"] == 150_000 and d1["break"] == pytest.approx(-fwd) and d1["over"]
    assert d1["hints"] and "PV валютных ног" in d1["hints"][0]
    assert d1["break_rub"] is not None
    assert ("T1", "USD") not in rec  # T1 covered, no currency on either side
    assert [x["book_id"] for x in o["outside_report"]] == ["X1"]  # no report unit covers the transfer book
    assert o["totals"]["breaks"] == 1 and o["report"]["rows"] == 1 and len(o["report"]["units"]) == 2

    # the transfer of D1 to X1: the deal's own rate and value date
    t = {(x["book_id"], x["ccy"]): x for x in o["transfers"]}[("D1", "USD")]
    assert (t["ocp_to"], t["transferred"], t["rate"], t["value_date"]) == ("X1", -900_000, 81.25, "2026-10-16")
    assert t["before"] == pytest.approx(1_050_000 + fwd) and t["after"] == pytest.approx(150_000 + fwd)

    # per position, the run log, lineage
    pos = {p["position_id"]: p["fact"] for p in c.get(f"/api/v1/runs/{rid}/positions").json()["positions"]}
    assert pos["D1|XFER"]["extra"]["ocp_transfer"] is True and pos["D1|XFER"]["extra"]["ocp"]["component"] == "transfers"
    assert pos["D1|USD"]["extra"]["ocp"] == {"component": "cash", "amounts": {"USD": 1_000_000.0}, "basis": "amount"}
    assert lin["ocp"]["status"] == "computed" and lin["ocp"]["breaks"] == 1
    assert any(m.startswith("ОВП из позиций: USD") for m in log)
    assert [w for w in warnings if w.startswith("ОВП: брейк D1 USD")]

    # window.DESK: the transfer and the deal's flag
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert desk["OCP_TRANSFERS"] == [
        {"from": "D1", "to": "X1", "ccy": "USD", "amount": -900_000.0, "before": pytest.approx(t["before"]),
         "rate": 81.25, "valueDate": "2026-10-16"},
        {"from": "X1", "to": "D1", "ccy": "USD", "amount": 900_000.0, "before": 0.0, "rate": 81.25,
         "valueDate": "2026-10-16"}]
    dp = {p["id"]: p for p in desk["positions"]}
    assert dp["D1|XFER"].get("ocpTransfer") is True and not dp["D1|USD"].get("ocpTransfer")


def test_no_report_and_before_b2(store_url, ctx, tmp_path):
    with store.connect(store_url) as con:
        _scan(con, tmp_path, report=False)
        rid, _ = _run(con, ctx)
    c = TestClient(create_app(store_url))
    o = c.get(f"/api/v1/runs/{rid}/ocp").json()
    assert o["state"] == "no_report" and o["text"] == ocp_run.STATUS_TEXT["no_report"]
    assert o["reconciliation"] == [] and o["by_ccy"]["USD"]["total"] != 0  # derived only
    old = ocp_out(7, None).model_dump(by_alias=True)  # a run published before B2 has no block
    assert old["state"] == "not_computed" and old["lines"] == [] and old["transfers"] == []
    assert old["text"] == ocp_run.STATUS_TEXT["not_computed"]
