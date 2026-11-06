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


def _scan(con, tmp_path: Path, report: bool = True, policy: str | None = None, books: str = BOOKS,
          ocp: str = OCP, positions: str = POSITIONS, cash: str = CASH, more: tuple = ()) -> None:
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    for name, text in (("books.csv", books), ("entities.csv", ENTITIES), ("funding_rules.csv", RULES),
                       *((("ocp_policy.csv", policy),) if policy else ())):
        _csv(cfg / name, text)
    files = [("positions_20261016.csv", positions), ("trade_terms_20261015.csv", TERMS),
             ("cash_balances_20261015.csv", cash)] + ([("ocp_20261015.csv", ocp)] if report else []) + list(more)
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


# the old methodology, given explicitly by the adapter (TDM#567: the engine's default is report_base_plus_day on every date)
OLD_BASIS = """policy_id;valid_from;active;published
old;;1;report_plus_outside
"""


def test_ocp_end_to_end_inbox_run_api(store_url, ctx, tmp_path):
    with store.connect(store_url) as con:
        _scan(con, tmp_path, policy=OLD_BASIS)
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
    assert any(m.startswith("ОВП: USD 1,050,000 (отчёт 150,000, из позиций 900,000)") and "ОВП из позиций: USD" in m
               for m in log)
    assert [w for w in warnings if w.startswith("ОВП: брейк D1 USD")]

    # the published OCP (TDM#416): D1 = its report (the default policy: the report holds every component, nothing
    # added), X1 outside every report unit = derived; the gaps say X1
    pub = o["published"]
    assert pub["basis"] == "report_plus_outside"
    lines = {(x["book_id"], x["ccy"]): x for x in pub["by_book"]}
    assert lines[("D1", "USD")]["source"] == "report" and lines[("D1", "USD")]["amount"] == 150_000
    assert lines[("D1", "USD")]["components"] == [] and lines[("D1", "USD")]["reported"] == 150_000
    assert lines[("X1", "USD")]["source"] == "derived" and lines[("X1", "USD")]["amount"] == 900_000
    usd = pub["by_ccy"]["USD"]
    assert usd["total"] == 1_050_000 and usd["by_source"] == {"report": 150_000, "derived": 900_000}
    assert usd["rate"] and usd["total_rub"] == pytest.approx(usd["total"] * usd["rate"])
    assert [(g["code"], g["items"]) for g in o["gaps"]] == [("books_outside_report", ["X1"])]
    assert o["policy"]["source"]["published"] == "ocp_policy:old" and o["policy"]["source"]["report_components"] \
        == "provisional" and "published" not in o["policy"]["provisional"]
    assert lin["ocp"]["published"] == {"USD": 1_050_000}
    # the FX delta of the positions does not change: the derived OCP is still the sum of the positions' lines
    assert o["by_ccy"]["USD"]["total"] == pytest.approx(1_050_000 + fwd + 900_000 - 900_000)

    # window.DESK, in tdm's shape (run_ocp_transfer, prototypes/data.js): the source book only, the amount moved
    # signed as the OCP before the transfer; the receiving book's mirror deal has no row
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert desk["OCP_TRANSFERS"] == [
        {"from": "D1", "to": "X1", "ccy": "USD", "amount": 900_000.0, "before": pytest.approx(t["before"]),
         "rate": 81.25, "valueDate": "2026-10-16"}]
    assert all(x["to"] in desk["BOOKS"] for x in desk["OCP_TRANSFERS"])  # the removed Desk Monitor named D.BOOKS[t.to]
    dp = {p["id"]: p for p in desk["positions"]}
    assert dp["D1|XFER"].get("ocpTransfer") is True and not dp["D1|USD"].get("ocpTransfer")


@pytest.mark.parametrize("active", ["1", "0"])  # an OCP-only book is added as inactive: still counted (TDM#556)
def test_a_book_only_the_report_knows_carries_its_reference_in_the_block(store_url, ctx, tmp_path, active):
    """TDM#556: a book of the OCP report without positions (an old book that still holds an OCP) is not among the run's
    reference books (those of its positions), so the block carries its row in force (``books``): the pages group and
    filter its OCP by the entity and book type the owner set. A book with positions is not repeated there."""
    from desk.api.concepts_run import ocp_status

    books = BOOKS + f"OLD;Old book;Legacy;HTM;ENT1;;;{active};\n"
    with store.connect(store_url) as con:
        _scan(con, tmp_path, books=books, ocp=OCP + "ENT1;OLD;EUR;2000000;;\n")
        rid, _ = _run(con, ctx)
        block, = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'ocp'", (rid,)).fetchone()
        ref, = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'books'", (rid,)).fetchone()
    assert "OLD" not in {b["book_id"] for b in ref}
    old = {(x["book_id"], x["ccy"]): x for x in block["published"]["by_book"]}[("OLD", "EUR")]
    assert old["source"] == "report" and old["amount"] == 2_000_000
    assert block["books"] == {"OLD": {"name": "Old book", "desk": "Legacy", "book_type": "HTM", "entity": "ENT1"}}
    o = TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/ocp").json()
    assert o["books"] == block["books"]
    assert ocp_status(block)["books"] == {"OLD": {"name": "Old book", "entity": "ENT1", "desk": "Legacy", "type": "HTM",
                                                  "ocpOnly": True}}
    # a block before TDM#556: no books, the API's default
    assert ocp_out(rid, {k: v for k, v in block.items() if k != "books"}).books == {}
    assert ocp_run.report_only_books([{"book_id": None, "books": ["OLD", "T1", "NONE"]}],
                                     {"OLD": {"name": "Old book", "entity": "ENT1"}}, [{"book_id": "T1"}]) == {
        "OLD": {"name": "Old book", "desk": "—", "book_type": None, "entity": "ENT1"}}


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


# the adapter's ocp_policy (private/1.5, TDM#180), an invented composition: the report holds the cash, the futures,
# the options and the transfers, not the FX legs; published = report + forward
POLICY = """policy_id;valid_from;active;report_components;published;amount_basis;include_ndf;cash_from_balances;settle_window_bd;tolerance_ccy;report_max_age_days
firm;;1;cash,futures,options,transfers;report_plus_outside;spot_equivalent;0;;;;
"""


def test_adapter_policy_explains_the_break_and_publishes_report_plus_outside(store_url, ctx, tmp_path):
    with store.connect(store_url) as con:
        _scan(con, tmp_path, policy=POLICY)
        rid, warnings = _run(con, ctx)
    o = TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/ocp").json()
    d1 = {(x["book_id"], x["ccy"]): x for x in o["reconciliation"]}[("D1", "USD")]
    fwd = d1["components"]["fx_legs"]
    assert d1["break"] == pytest.approx(0) and not d1["over"] and d1["not_in_report"] == {"fx_legs": fwd}
    assert o["totals"]["breaks"] == 0 and not [w for w in warnings if "брейк" in w]
    assert o["policy"]["source"]["report_components"] == "ocp_policy:firm"
    assert o["policy"]["source"]["published"] == "ocp_policy:firm"
    assert o["policy"]["source"]["amount_basis"] == "ocp_policy:firm" and o["policy"]["amount_basis"] == "spot_equivalent"
    assert o["policy"]["source"]["include_ndf"] == "ocp_policy:firm"
    assert "report_components" not in o["policy"]["provisional"] and "amount_basis" not in o["policy"]["provisional"]
    assert "transfer_rule" in o["policy"]["provisional"]
    line = {(x["book_id"], x["ccy"]): x for x in o["published"]["by_book"]}[("D1", "USD")]
    assert line["source"] == "report" and line["amount"] == pytest.approx(150_000 + fwd)
    assert line["components"] == ["fx_legs"] and line["outside"] == {"fx_legs": fwd}
    gaps = {g["code"]: g for g in o["gaps"]}
    assert gaps["outside_without_terms"]["items"] == ["D1|FWD"]  # the forward has no trade terms: its notional


def test_stale_report_is_not_published(store_url, ctx, tmp_path):
    """The report of the 14th carried into the run of the 15th: reconciled, but the published OCP is derived."""
    with store.connect(store_url) as con:
        _scan(con, tmp_path, report=False, policy=OLD_BASIS)
        inbox = tmp_path / "in"
        _csv(inbox / "ocp_20261014.csv", OCP)
        s = Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                     private_adapter=None, private_adapter_config=tmp_path / "cfg", inbox_poll_seconds=0,
                     inbox_lookback_days=0)
        assert scan.scan(con, s).ok
        rid, _ = _run(con, ctx)
    o = TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/ocp").json()
    assert o["state"] == "computed" and o["reconciliation"]
    line = {(x["book_id"], x["ccy"]): x for x in o["published"]["by_book"]}[("D1", "USD")]
    assert line["source"] == "derived_stale_report" and line["amount"] == pytest.approx(
        {(x["book_id"], x["ccy"]): x for x in o["reconciliation"]}[("D1", "USD")]["derived"])
    assert {g["code"] for g in o["gaps"]} >= {"stale_report"}


def _boom(*a, **kw):
    """An SQL error inside a block's computation (TDM#215)."""
    con = a[0]
    con.execute("SELECT * FROM no_such_table")


def test_sql_error_in_a_block_fails_only_the_block(store_url, ctx, tmp_path, monkeypatch):
    from desk.publish import funding_run, fx_liability

    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        for mod in (ocp_run, funding_run, fx_liability):
            monkeypatch.setattr(mod, "_compute", _boom)
        with con.transaction():  # a caller's transaction: an aborted one would fail the run's own INSERTs
            rid, _ = _run(con, ctx)
        status, lin = con.execute("SELECT status, lineage FROM run WHERE run_id = %s", (rid,)).fetchone()
        log = [m for (m,) in con.execute("SELECT message FROM run_log WHERE run_id = %s ORDER BY seq", (rid,))]
    assert status == "complete" and lin["ocp"]["status"] == "failed" and lin["funding"]["status"] == "failed"
    assert any("ОВП не посчитано" in m and "UndefinedTable" in m for m in log)
    o = TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/ocp").json()
    assert o["state"] == "failed" and o["published"]["by_book"] == [] and o["gaps"] == []


def test_rates_of_every_currency_of_the_ocp(store_url, ctx, tmp_path):
    """TDM#602: a currency only the OCP report holds (JPY and KZT, which the CBR quotes per 100 units; GEL, which MOEX
    does not trade) gets its rate in the run: per unit from the snapshot (MOEX, else the CBR's with ``rate_source``
    ``CBR``), so the published OCP has its rouble equivalent; a currency the snapshot has no rate of stays without."""
    report = OCP + "ENT1;D1;JPY;10000000;;\nENT1;D1;KZT;5000000;;\nENT1;D1;GEL;100000;;\nENT1;D1;XAU;10;;\n"
    with store.connect(store_url) as con:
        _scan(con, tmp_path, ocp=report)
        rid, _ = _run(con, ctx)
        block, = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'ocp'", (rid,)).fetchone()
        fx, = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'fx'", (rid,)).fetchone()
    rates = {r["ccy"]: r for r in fx}
    jpy, kzt, gel = (ctx.market.fx(c) for c in ("JPY", "KZT", "GEL"))
    assert rates["JPY"]["rate"] == pytest.approx(jpy["moex_19"]) and rates["JPY"]["rate_cbr"] == pytest.approx(0.529852)
    assert rates["KZT"]["rate"] == pytest.approx(kzt["moex_19"]) and rates["KZT"]["rate_cbr"] == pytest.approx(0.1872)
    assert (rates["GEL"]["rate"], rates["GEL"]["rate_source"]) == (pytest.approx(gel["cbr"]), "CBR")
    assert "XAU" not in rates  # no rate in the snapshot: none invented
    pub = block["published"]["by_ccy"]
    for c in ("JPY", "KZT", "GEL"):
        assert pub[c]["rate"] == rates[c]["rate"] and pub[c]["total_rub"] == pytest.approx(pub[c]["total"] * pub[c]["rate"])
    assert pub["JPY"]["total_rub"] == pytest.approx(10_000_000 * 0.53033)  # per unit, not per 100
    assert pub["XAU"]["rate"] is None and pub["XAU"]["total_rub"] is None
    # the OCP history carries them as the day's rates
    h = TestClient(create_app(store_url)).get(f"/api/v1/history/ocp?run_id={rid}&from=2026-10-15&to=2026-10-15").json()
    day, = h["days"]
    assert {"JPY", "KZT", "GEL"} <= set(day["fx"]) and day["fx"]["GEL"]["cbr"] == pytest.approx(gel["cbr"])


def test_cnh_is_cny_in_every_dataset(store_url, ctx, tmp_path):
    """TDM#601: positions, the OCP report, cash balances and the day's trades given in CNH are CNY in the run (the
    setting ``ccy_aliases``, default CNH=CNY): one currency CNY with its rate, no CNH line or rate; the report's code
    kept with the row (``extra.reported.ccy_reported``, ``_ccy_reported``) and said in the OCP notes."""
    from desk.inbox.schema import CCY_REPORTED

    positions = POSITIONS + "D1;D1|CNH;fx;CNH;;CNH;;5000000;;\nD1;D1|CNY;fx;CNY;;CNY;;1000000;;\n"
    trades = ("trade_id;trade_version;status;trade_date;book_id;position_id;asset_class;source_instrument_id;side;"
              "amount_ccy;ccy;settle_date\nTR-1;1;live;2026-10-15;D1;D1|CNH;fx;CNHRUB_TOM;buy;100000;CNH;2026-10-16\n")
    with store.connect(store_url) as con:
        _scan(con, tmp_path, policy=OLD_BASIS, positions=positions, cash=CASH + "ENT1;D1;CNH;MAIN;20000\n",
              ocp=OCP + "ENT1;D1;CNH;6000000;;\n", more=(("trades_20261015_1100.csv", trades),))
        day = store.dataset_rows(con, "trades", DATE).rows
        rid, _ = _run(con, ctx)
        block, = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'ocp'", (rid,)).fetchone()
        fx, = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'fx'", (rid,)).fetchone()
    assert [(t["ccy"], t[CCY_REPORTED]) for t in day] == [("CNY", {"ccy": "CNH"})]
    rates = {r["ccy"]: r for r in fx}
    assert "CNH" not in rates and rates["CNY"]["rate"]
    pub = block["published"]["by_ccy"]
    assert "CNH" not in pub and pub["CNY"]["rate"] == rates["CNY"]["rate"] and pub["CNY"]["total_rub"] is not None
    assert "CNH" not in {x["ccy"] for x in block["lines"]} and "CNH" not in block["by_ccy"]
    rec = {(x["book_id"], x["ccy"]): x for x in block["reconciliation"]}
    assert rec[("D1", "CNY")]["reported"] == 6_000_000  # the report's CNH line is the book's CNY
    assert any(n.startswith("отчёт ОВП: CNH взята как CNY") for n in block["notes"])
    pos = {p["position_id"]: p["fact"] for p in
           TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/positions").json()["positions"]}
    assert pos["D1|CNH"]["ccy"] == pos["D1|CNY"]["ccy"] == "CNY"
    assert pos["D1|CNH"]["extra"]["reported"]["ccy_reported"] == "CNH"
    assert "ccy_reported" not in pos["D1|CNY"]["extra"]["reported"]


def test_currency_aliases_setting(tmp_path, monkeypatch):
    """TDM#601: the setting's text, both directions of a rate's fallback, «нет» for none."""
    from desk.inbox import schema
    from desk.settings import load

    assert schema.parse_ccy_aliases("cnh=CNY; XYZ = usd, RUB=USD, bad, CNY=CNY") == {"CNH": "CNY", "XYZ": "USD"}
    assert schema.parse_ccy_aliases("нет") == {} and schema.parse_ccy_aliases(None) == {}
    env = {"DESK_HOME": str(tmp_path)}
    try:
        assert load(env).ccy_aliases == "CNH=CNY" and schema.ccy_aliases() == {"CNH": "CNY"}
        assert schema.norm_ccy("cnh") == "CNY" and schema.code_ccy("cnh") == "CNH" and schema.norm_ccy("RUR") == "RUB"
        assert schema.ccy_partners("CNY") == ["CNH"] and schema.ccy_partners("CNH") == ["CNY"]
        row = {"base_ccy": "CNH", "quote_ccy": "RUB"}
        assert schema.alias_row(row, ("base_ccy", "quote_ccy")) == {"base_ccy": "CNH"}
        assert row == {"base_ccy": "CNY", "quote_ccy": "RUB", schema.CCY_REPORTED: {"base_ccy": "CNH"}}
        (tmp_path / "settings.json").write_text('{"ccy_aliases": "нет"}', encoding="utf-8")
        assert load(env).ccy_aliases == "нет" and schema.ccy_aliases() == {} and schema.norm_ccy("CNH") == "CNH"
    finally:
        schema.set_ccy_aliases(schema.DEFAULT_CCY_ALIASES)


def test_a_rate_of_one_code_stands_in_for_the_other(ctx):
    """TDM#601: the snapshot has no CNH rate: CNH takes CNY's (and a code with neither stays without)."""
    assert ctx.market.fx("CNH") == ctx.market.fx("CNY") and ctx.market.fx("CNY")["cbr"]
    assert not any(ctx.market.fx("XAU").values())
