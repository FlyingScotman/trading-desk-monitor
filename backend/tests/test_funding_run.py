"""Wave B1: the funding engine in the runs (``desk.publish.funding_run``), end to end on synthetic data: the reference
adapter's config (books, entities, funding rules, the private/1.3 ``funding_policy``) and inbox files (positions of two
days, cash balances) scanned into the store, two runs published (the second with a flash PnL), the funding read back
through ``/api/v1/runs/{id}/funding``, ``/pnl`` and window.DESK, and checked against a hand calculation. Plus the
contract table (``rules.reference.apply_funding_policy``, the validation rules), "not configured" and a failure."""

from __future__ import annotations

import copy
import dataclasses
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_slice_s2 import prev_outcome, shifted_prev

from desk import store
from desk.api import create_app
from desk.inbox import scan
from desk.inbox.validate import check_reference_tables
from desk.marketdata.adapter import MarketView
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare, value
from desk.publish import desk_run, funding_run
from desk.rules.funding import BaseDate, FundingPolicy, HtmBase, Periodicity, RepoRate, WriteOffDate
from desk.rules.reference import apply_funding_policy
from desk.settings import Settings
from desk.valuation.positions import merge, plan

FIX = Path(__file__).parent / "fixtures"
SNAPS = FIX / "mdhub_v1"
DATE = date(2026, 10, 15)
PREV = date(2026, 10, 14)
BOM = "﻿"

BOOKS = """book_id;name;desk;book_type;entity;ocp_to;trader;active;valid_from
T1;Bond trading;Rates;TRADING;ENT1;;;1;
H1;Banking book;Rates;HTM;ENT1;;;1;
D1;Derivatives;Rates;DERIVS;ENT1;;;1;
F1;Futures;Rates;FUTURES;ENT1;;;1;
C1;Other entity;Rates;TRADING;ENT2;;;1;
"""
ENTITIES = """entity;name;funding_scheme;pnl_sweep;active;valid_from
ENT1;Entity one;by_book_type;;1;
ENT2;Entity two;cash_balance;monthly;1;
"""
RULES = """rule_id;valid_from;active;book_type;entity;book_id;ccy;instrument_id;basis;rate_kind;index_code;spread_bp;fixed_rate_pct;day_count
trading_rub;;1;TRADING;;;RUB;;mv_clean;index_spread;KEY_RATE;100;;ACT/365
htm;;1;HTM;;;;;cost;contract;;;;
derivs_rub;;1;DERIVS;;;RUB;;cash;index_spread;RUONIA;0;;ACT/365
futures;;1;FUTURES;;;;;none;none;;;;
ent2_cash;;1;;ENT2;;RUB;;cash;index_spread;KEY_RATE;100;;ACT/365
"""
POLICY = """policy_id;valid_from;active;book_type;pnl_sweep;htm_base;mv_base_date;cash_base_date;new_positions;closed_positions;fx_date;out_of_currency;entity_cash_book_type;repo_rate;repo_contract_from_term_days;repo_security_book_type;write_off_date
firm;;1;;;cash_spent;previous_business_day;previous_business_day;next_period;full_period;as_of;security_ccy;;contract;;;last_calendar_day
trading;;1;TRADING;monthly;;;;;;;;;;;;
"""
POS_HEAD = ("book_id;position_id;asset_class;source_instrument_id;isin;ccy;quantity;face_amount_ccy;amount_ccy;"
            "purchase_date;purchase_price_pct;contract_rate_pct;purchase_accrued_ccy\n")
HELD = "T1;T1|26238;bond;SEC-26238;RU000A1038V6;RUB;10000;;;;;;\n"
HTM = "H1;H1|29008;bond;SEC-29008;RU000A0JV4P3;RUB;20000;;;2026-03-02;95.5;14.25;123456.78\n"
OTHER = "C1;C1|26238;bond;SEC-26238;RU000A1038V6;RUB;1000;;;;;;\nD1;D1|USD;fx;USD;;USD;;;1000000;;;;\n"
DAY1 = POS_HEAD + HELD + "T1;T1|JXQ44;bond;SEC-JXQ44;RU000A0JXQ44;RUB;;5000000;;;;;\n" + HTM + OTHER
DAY2 = POS_HEAD + HELD + "T1;T1|XR1;bond;SEC-XR1;RU000A109XR1;RUB;;3000000;;;;;\n" + HTM + OTHER
CASH_HEAD = "entity;book_id;ccy;account_id;balance_ccy\n"
CASH1 = CASH_HEAD + "ENT1;D1;RUB;MAIN;-100000000\nENT2;C1;RUB;MAIN;50000000\nENT1;T1;RUB;MAIN;7000000\n"
CASH2 = CASH_HEAD + "ENT1;D1;RUB;MAIN;-120000000\nENT2;C1;RUB;MAIN;55000000\nENT1;T1;RUB;MAIN;6000000\n"


def _csv(path: Path, text: str) -> None:
    path.write_bytes((BOM + text.replace("\n", "\r\n")).encode())


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """A settings folder of its own: the funding settings (#237) are the defaults unless a test writes them."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DESK_HOME", str(home))
    return home


def _funding_settings(home: Path, funding: dict) -> None:
    from desk.settings import update_settings_json
    update_settings_json({"funding": funding}, home)


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


def _scan(con, tmp_path: Path, policy: bool = True, rules: bool = True,
          cash: tuple[tuple[str, str], ...] = (("cash_balances_20261014.csv", CASH1),
                                               ("cash_balances_20261015.csv", CASH2))) -> None:
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    for name, text in (("books.csv", BOOKS), ("entities.csv", ENTITIES), ("funding_rules.csv", RULES if rules else None),
                       ("funding_policy.csv", POLICY if policy else None)):
        if text:
            _csv(cfg / name, text)
    # positions are start_of_day: the file of the 15th is the book of the 14th (and the 16th's of the 15th)
    for name, text in (("positions_20261015.csv", DAY1), ("positions_20261016.csv", DAY2), *cash):
        _csv(inbox / name, text)
    s = Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive", private_adapter=None,
                 private_adapter_config=cfg, inbox_poll_seconds=0, inbox_lookback_days=0)
    r = scan.scan(con, s)
    assert r.ok and r.files == {"accepted": 2 + len(cash)}, r.text()


def _runs(con, ctx) -> tuple[int, int, list]:
    """The run of the 14th (no previous market) and of the 15th (previous market: the S2 synthetic one, flash PnL of the
    exact bonds), both valued on the fixture's market as test_api does."""
    early_market = copy.copy(ctx.market)
    early_market.business_date = PREV
    early = dataclasses.replace(ctx, market=early_market)
    inp1 = store.positions_for(con, PREV)
    bp1 = plan(ctx, inp1)
    pos1 = merge(bp1, value(ctx, bp1.requests, position_ids=bp1.position_ids))
    rid1, st, _ = desk_run.publish(con, early, pos1, prev=None, inputs=inp1)
    assert st == "complete"
    inp2 = store.positions_for(con, DATE)
    bp2 = plan(ctx, inp2)
    cur = value(ctx, bp2.requests, position_ids=bp2.position_ids)
    prev = shifted_prev(ctx, PREV)
    attach_pnl(prev, [prev_outcome(prev, o) if o.valuation and o.valuation.quality == "exact"
                      else Outcome(o.secid, o.terms, None, reason="no_quote") for o in cur], ctx, cur)
    view = MarketView(ctx.market.snapshot)
    view.business_date = PREV
    pos2 = merge(bp2, cur)
    rid2, st, warnings = desk_run.publish(con, ctx, pos2, prev=view, inputs=inp2)
    assert st == "complete"
    return rid1, rid2, warnings


def test_funding_end_to_end_inbox_run_api(store_url, ctx, tmp_path):
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        rid1, rid2, warnings = _runs(con, ctx)
    c = TestClient(create_app(store_url))
    first = c.get(f"/api/v1/runs/{rid1}/funding").json()
    # the 14th: no earlier run, so every position is new (funded from the next period) but the HTM lot (funded from
    # its purchase date); no cash of the 13th
    assert first["state"] == "computed" and first["period"] == {"start": "2026-10-13", "end": "2026-10-14", "days": 1}
    assert [x["ref"] for x in first["lines"]] == ["H1|29008"]
    assert [(p["kind"], p["ref"]) for p in first["problems"]] == [("CASH", "*")]

    f = c.get(f"/api/v1/runs/{rid2}/funding").json()
    assert f["state"] == "computed" and f["period"] == {"start": "2026-10-14", "end": "2026-10-15", "days": 1}
    assert f["problems"] == [] and "РЕПО" in " ".join(f["notes"])
    assert f["policy"]["source"]["htm_base"] == "funding_policy:firm" and f["policy"]["htm_base"] == "cash_spent"
    assert "day_count" not in f["provisional"] and "htm_base" not in f["provisional"]
    lines = {x["ref"]: x for x in f["lines"]}
    assert set(lines) == {"T1|26238", "T1|JXQ44", "H1|29008", "D1|RUB", "C1|RUB"}

    # hand calculation: one calendar day, ACT/365, the previous business day's base (DEC01 default, here explicit)
    kr = ctx.market.latest_fixing("KEY_RATE")[1] / 100
    ruo = ctx.market.latest_fixing("RUONIA")[1] / 100
    prev_facts = {p["position_id"]: p["fact"] for p in c.get(f"/api/v1/runs/{rid1}/positions").json()["positions"]}

    def clean(pid: str) -> float:
        x = prev_facts[pid]
        return x["mv"] - x["extra"]["quantity_bonds"] * x["accrued"]

    held = lines["T1|26238"]
    assert (held["kind"], held["base"], held["rate_expr"]) == ("POS", "mv_clean", "KEY_RATE + 100 bp")
    assert held["interest_rub"] == pytest.approx(-clean("T1|26238") * (kr + 0.01) / 365)
    closed = lines["T1|JXQ44"]  # sold on the 15th: funded on the 14th's MV for the period, a book-level line
    assert closed["interest_rub"] == pytest.approx(-clean("T1|JXQ44") * (kr + 0.01) / 365)
    assert not closed["in_position"] and "закрыта" in closed["note"]
    htm = lines["H1|29008"]  # the lot: face x purchase price at the contract rate; the purchase accrued paid back
    face = prev_facts["H1|29008"]["notional"]
    assert (htm["kind"], htm["base"], htm["rate_pct"]) == ("HTM", "cost", pytest.approx(14.25))
    assert htm["interest_rub"] == pytest.approx(-face * 0.955 * 0.1425 / 365)  # by the first coupon after March
    assert lines["D1|RUB"]["interest_rub"] == pytest.approx(-100_000_000 * ruo / 365)  # the 14th's balance
    ent2 = lines["C1|RUB"]  # an entity funded on its whole cash balance: its bond is not funded by MV
    assert (ent2["kind"], ent2["interest_rub"]) == ("ENTITY_CASH", pytest.approx(50_000_000 * (kr + 0.01) / 365))
    assert "T1|RUB" not in lines  # a market-value book's cash is not funded

    books = {b["book_id"]: b for b in f["books"]}
    assert books["T1"]["total"] == pytest.approx(held["interest_rub"] + closed["interest_rub"])
    assert books["T1"]["write_off"]["periodicity"] == "monthly" and books["T1"]["write_off"]["last"] == "2026-09-30"
    assert books["T1"]["write_off"]["since_write_off"] == pytest.approx(books["T1"]["total"])
    assert books["C1"]["write_off"]["periodicity"] == "monthly"  # the entity's own sweep
    assert books["D1"]["write_off"]["periodicity"] is None and books["D1"]["write_off"]["since_write_off"] is None
    assert f["entities"] == pytest.approx({"ENT1": books["T1"]["total"] + books["H1"]["total"] + books["D1"]["total"],
                                           "ENT2": ent2["interest_rub"]})
    t = f["totals"]
    assert t["total"] == pytest.approx(sum(x["interest_rub"] for x in f["lines"]))
    assert t["total"] == pytest.approx(t["in_positions"] + t["book_level"])

    # per position: in the flash PnL (its own funding component: the total net of funding), deferred, not funded
    pos = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid2}/positions").json()["positions"]}
    st = {k: p["fact"]["extra"]["funding"]["state"] for k, p in pos.items() if p["fact"]}
    assert st["T1|XR1"] == "deferred" and st["C1|26238"] == "not_funded" and st["D1|USD"] == "not_funded"
    assert st["T1|26238"] == st["H1|29008"] == "funded"
    pnl = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid2}/pnl").json()["positions"]}
    with_pnl = [k for k in ("T1|26238", "H1|29008") if pnl[k]["components"]]
    assert with_pnl == ["T1|26238"]  # the exact bond has a flash PnL; the HTM floater has none (book-level line)
    for k in with_pnl:
        assert pnl[k]["components"]["funding"] == pytest.approx(lines[k]["interest_rub"]) and lines[k]["in_position"]
        assert pnl[k]["total"] == pytest.approx(pnl[k]["extra"]["total"] + lines[k]["interest_rub"])  # net of funding
    for k in ("T1|26238", "H1|29008"):
        if k not in with_pnl:  # funded, but without a flash PnL: a book-level line, not in a PnL
            assert not lines[k]["in_position"] and "без флэш-PnL" in lines[k]["note"]
    assert t["in_positions"] == pytest.approx(sum(lines[k]["interest_rub"] for k in with_pnl))
    tot = c.get(f"/api/v1/runs/{rid2}/pnl").json()["totals"]["_total"]
    assert tot["funding"] == pytest.approx(t["in_positions"])
    with store.connect(store_url) as con:
        run_tot, lin = con.execute("SELECT totals, lineage FROM run WHERE run_id = %s", (rid2,)).fetchone()
        log = [m for (m,) in con.execute("SELECT message FROM run_log WHERE run_id = %s ORDER BY seq", (rid2,))]
    assert run_tot["_total"]["pnl"]["funding"] == pytest.approx(t["in_positions"])
    assert lin["funding"]["status"] == "computed" and lin["funding"]["problems"] == 0
    assert any(m.startswith("фондирование 2026-10-14 -> 2026-10-15") for m in log)

    # window.DESK: the funding lines, the book-level sums, the rules, the status; a position's pnl.funding
    desk = c.get(f"/api/v1/compat/desk?run_id={rid2}").json()
    fs = desk["fundingStatus"]
    assert fs["state"] == "computed" and fs["total"] == pytest.approx(t["total"]) and fs["problems"] == []
    assert fs["byBook"]["T1"]["writeOff"]["periodicity"] == "monthly"
    assert {x["id"] for x in desk["funding"]} == {x["key"] for x in f["lines"]}
    assert desk["fundingPnl"]["carry"] == pytest.approx(t["book_level"])
    assert desk["fundingPnlByBook"]["T1"]["carry"] == pytest.approx(closed["interest_rub"] + sum(
        lines[k]["interest_rub"] for k in ("T1|26238",) if k not in with_pnl))
    assert all(x["fxDelta"] == {} for x in desk["funding"])  # OQ-04 open: no funding liability in the FX delta
    assert {r["note"] for r in desk["FUNDING_RULES"] if r["source"] == "adapter"} == {
        "trading_rub", "htm", "derivs_rub", "futures", "ent2_cash"}
    # the market default rate is in the status, not in the rules; every line here has an adapter rule (the HTM lot:
    # its contract rate)
    assert not [r for r in desk["FUNDING_RULES"] if r["source"] == "default"] and fs["defaultRate"]["enabled"]
    assert f["default_rate"]["lines"] == 0 and not any(x["default_rate"] for x in f["lines"])
    assert held["rule_source"] == "adapter" and held["rule_id"] == "trading_rub"
    assert (htm["rule_source"], htm["rule_id"]) == ("adapter", "htm")  # the adapter's contract rule
    dp = {p["id"]: p for p in desk["positions"]}
    for k in with_pnl:
        assert dp[k]["pnl"]["funding"] == pytest.approx(lines[k]["interest_rub"])
        assert dp[k]["pnl"]["explained"] == pytest.approx(pnl[k]["total"])
    assert dp["C1|26238"]["extra"]["funding"]["state"] == "not_funded"
    assert not [w for w in warnings if w.startswith("фондирование")]


def test_not_configured_is_said_and_not_zero(store_url, ctx, tmp_path, _home):
    """No rule anywhere and the market default rate switched off in the settings: "not configured", never 0."""
    _funding_settings(_home, {"default_rate": {"kind": "none"}, "default_rate_by_ccy": {}})
    with store.connect(store_url) as con:
        _scan(con, tmp_path, policy=False, rules=False)
        _, rid, warnings = _runs(con, ctx)
    c = TestClient(create_app(store_url))
    f = c.get(f"/api/v1/runs/{rid}/funding").json()
    assert f["state"] == "not_configured" and f["totals"] is None and f["lines"] == [] and f["books"] == []
    assert f["text"] == funding_run.STATUS_TEXT["not_configured"]
    assert any(w.startswith("фондирование не настроено") for w in warnings)
    pnl = [p for p in c.get(f"/api/v1/runs/{rid}/pnl").json()["positions"] if p["components"]]
    assert pnl and all("funding" not in p["components"] for p in pnl)  # absent, not 0
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert desk["fundingStatus"]["state"] == "not_configured" and desk["fundingStatus"]["total"] is None
    assert all(p["pnl"]["funding"] is None for p in desk["positions"])
    assert desk["funding"] == [] and desk["FUNDING_RULES"] == []


def test_default_rate_without_adapter_rules(store_url, ctx, tmp_path):
    """#237: the adapter gives no funding rule, the settings nothing: funding at the market default rate (RUB RUONIA),
    named so on the line, the position and window.DESK (C01), the TRADING base clean (DEC01)."""
    with store.connect(store_url) as con:
        _scan(con, tmp_path, policy=False, rules=False)
        rid1, rid2, warnings = _runs(con, ctx)
    c = TestClient(create_app(store_url))
    f = c.get(f"/api/v1/runs/{rid2}/funding").json()
    assert f["state"] == "computed" and f["policy"]["source"]["base_by_book_type[TRADING]"] == "DEC01"
    lines = {x["ref"]: x for x in f["lines"]}
    ruo = ctx.market.latest_fixing("RUONIA")[1] / 100
    prev_facts = {p["position_id"]: p["fact"] for p in c.get(f"/api/v1/runs/{rid1}/positions").json()["positions"]}
    x = prev_facts["T1|26238"]
    held = lines["T1|26238"]
    assert held["rate_expr"] == "RUONIA (рыночная ставка по умолчанию)" and held["base"] == "mv_clean"
    assert (held["rule_source"], held["rule_id"], held["default_rate"]) == ("default", "default:RUB", True)
    assert held["interest_rub"] == pytest.approx(-(x["mv"] - x["extra"]["quantity_bonds"] * x["accrued"]) * ruo / 365)
    assert lines["D1|RUB"]["default_rate"] and lines["D1|RUB"]["interest_rub"] == pytest.approx(-100_000_000 * ruo / 365)
    # the HTM lot keeps its contract rate (the adapter's htm_base is not given: a problem, never a guess)
    assert ("HTM", "H1|29008") in {(p["kind"], p["ref"]) for p in f["problems"]}
    d = f["default_rate"]
    assert d["enabled"] and d["label"] == "рыночная ставка по умолчанию" and d["by_ccy"]["RUB"]["kind"] == "ruonia"
    assert d["lines"] == sum(1 for x in f["lines"] if x["default_rate"]) >= 2 and d["positions"] >= 1
    pos = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid2}/positions").json()["positions"]}
    st = pos["T1|26238"]["fact"]["extra"]["funding"]
    assert st["state"] == "funded" and st["default_rate"] and st["rule_source"] == "default"
    assert st["lines"][0]["rate_expr"].endswith("(рыночная ставка по умолчанию)")
    desk = c.get(f"/api/v1/compat/desk?run_id={rid2}").json()
    assert desk["fundingStatus"]["state"] == "computed"
    assert desk["fundingStatus"]["defaultRate"]["by_ccy"]["RUB"]["expr"] == "RUONIA (рыночная ставка по умолчанию)"
    assert {x["id"]: x["defaultRate"] for x in desk["funding"]}["POS|T1|26238"] is True
    assert desk["FUNDING_RULES"] == []  # no rule: the default rate is the status's defaultRate
    with store.connect(store_url) as con:
        log = [m for (m,) in con.execute("SELECT message FROM run_log WHERE run_id = %s ORDER BY seq", (rid2,))]
    assert any("по рыночной ставке по умолчанию" in m for m in log)
    assert not any(w.startswith("фондирование не настроено") for w in warnings)


def test_settings_rules_in_the_run_precedence(store_url, ctx, tmp_path, _home):
    """#237: a settings rule applies where the adapter has none, yields to the adapter's rule, and an override rule
    wins over it; the line says which rule applied."""
    _funding_settings(_home, {"rules": [
        {"rule_id": "s_trading", "book_type": "TRADING", "ccy": "RUB", "rate_kind": "fixed", "fixed_rate_pct": 9},
        {"rule_id": "s_t1", "book_id": "T1", "override": True, "rate_kind": "fixed", "fixed_rate_pct": 12},
        {"rule_id": "s_derivs", "book_type": "DERIVS", "ccy": "RUB", "rate_kind": "fixed", "fixed_rate_pct": 7},
    ], "default_rate": {"kind": "none"}, "default_rate_by_ccy": {}})
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        _, rid2, _ = _runs(con, ctx)
    f = TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid2}/funding").json()
    lines = {x["ref"]: x for x in f["lines"]}
    held = lines["T1|26238"]  # override: over the adapter's trading_rub
    assert (held["rule_id"], held["rule_source"], held["rate_pct"]) == ("s_t1", "settings_override", pytest.approx(12))
    d1 = lines["D1|RUB"]  # the adapter's derivs_rub holds against the settings' s_derivs
    assert (d1["rule_id"], d1["rule_source"]) == ("derivs_rub", "adapter")
    assert f["default_rate"]["enabled"] is False and f["default_rate"]["lines"] == 0
    desk = TestClient(create_app(store_url)).get(f"/api/v1/compat/desk?run_id={rid2}").json()
    src = {r["note"]: r["source"] for r in desk["FUNDING_RULES"]}
    assert (src["s_t1"], src["s_trading"], src["trading_rub"]) == ("settings_override", "settings", "adapter")
    assert {x["id"]: x["ruleSource"] for x in desk["funding"]}["POS|T1|26238"] == "settings_override"


def test_rules_without_policy_take_the_provisional_conventions(store_url, ctx, tmp_path):
    """A 1.2 adapter (rules, no funding_policy): the run funds by the engine's provisional conventions and lists them;
    an HTM lot is a problem (no htm_base), never a guess."""
    with store.connect(store_url) as con:
        _scan(con, tmp_path, policy=False)
        _, rid, _ = _runs(con, ctx)
    f = TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/funding").json()
    assert f["state"] == "computed" and {"htm_base", "mv_base_date", "repo.rate"} <= set(f["provisional"])
    assert ("HTM", "H1|29008") in {(p["kind"], p["ref"]) for p in f["problems"]}
    assert {b["book_id"]: b["write_off"]["periodicity"] for b in f["books"]}["T1"] is None


def test_a_failure_never_fails_the_run(store_url, ctx, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(funding_run, "_compute", boom)
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        _, rid, warnings = _runs(con, ctx)
    f = TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/funding").json()
    assert f["state"] == "failed" and "RuntimeError: synthetic" in f["text"]
    assert any("RuntimeError: synthetic" in w for w in warnings)


def test_a_run_before_b1_is_not_computed(store_url, ctx):
    from desk.api.app import funding_out
    from desk.api.compat import funding_view
    assert funding_out(1, None).state == "not_computed"
    v = funding_view(None, {"RUB": 1.0})
    assert v["status"]["state"] == "not_computed" and v["lines"] == [] and v["pnl"] == {"carry": 0.0, "fx": 0.0,
                                                                                         "fxCbr": 0.0}


# ---- the contract table funding_policy (private/1.3) -----------------------------------------------------------


def _ref(*policy: dict) -> tuple[dict, list[dict]]:
    books = [{"_src": "L2", "book_id": "T1", "name": "T", "desk": "D", "book_type": "TRADING", "entity": "E"}]
    return check_reference_tables({"books": books, "funding_policy": [{"_src": f"L{i}", **p}
                                                                      for i, p in enumerate(policy, 2)]})


def test_funding_policy_rows_validate():
    tables, issues = _ref({"policy_id": "a", "htm_base": "cash_spent", "repo_contract_from_term_days": "30"},
                          {"policy_id": "b", "book_type": "TRADING", "pnl_sweep": "monthly"})
    assert issues == [] and tables["funding_policy"][0]["repo_contract_from_term_days"] == 30
    _, issues = _ref({"policy_id": "a", "book_type": "HTM", "htm_base": "cash_spent"},
                     {"policy_id": "b", "pnl_sweep": "monthly"}, {"policy_id": "c", "htm_base": "dirty"})
    assert [(i["severity"], i["code"]) for i in issues] == [("error", "missing_required")] * 2 + [("error", "bad_value")]
    _, issues = _ref({"policy_id": "a", "htm_base": "cash_spent"}, {"policy_id": "b", "htm_base": "clean_cost"})
    assert [(i["severity"], i["code"]) for i in issues] == [("warning", "ambiguous_policy")]


def test_apply_funding_policy():
    rows = [{"policy_id": "firm", "htm_base": "clean_cost", "mv_base_date": "as_of", "repo_rate": "security",
             "repo_contract_from_term_days": 30, "repo_security_book_type": "HTM", "write_off_date": "last_business_day",
             "entity_cash_book_type": "DERIVS"},
            {"policy_id": "t", "book_type": "TRADING", "pnl_sweep": "quarterly"},
            {"policy_id": "old", "htm_base": "cash_spent", "active": False}]
    s = apply_funding_policy(rows)
    p = s.policy
    assert (p.htm_base, p.mv_base_date, p.repo.rate, p.repo.contract_rate_from_term_days, p.repo.security_rate_book_type) \
        == (HtmBase.CLEAN_COST, BaseDate.AS_OF, RepoRate.SECURITY, 30, "HTM")
    assert p.write_off.date_rule is WriteOffDate.LAST_BUSINESS_DAY and p.entity_cash_rate_book_type == "DERIVS"
    assert p.write_off.by_book_type == {"TRADING": Periodicity.QUARTERLY} and s.conflicts == ()
    assert s.source["htm_base"] == "funding_policy:firm" and s.source["write_off.by_book_type[TRADING]"] == \
        "funding_policy:t"
    assert p.cash_base_date is FundingPolicy().cash_base_date  # not given: the provisional default
    clash = apply_funding_policy([{"policy_id": "a", "htm_base": "clean_cost"}, {"policy_id": "b", "htm_base": "cash_spent"},
                                  {"policy_id": "c", "book_type": "HTM", "pnl_sweep": "daily"},
                                  {"policy_id": "d", "book_type": "HTM", "pnl_sweep": "none"}])
    assert clash.policy.htm_base is None and "HTM" not in clash.policy.write_off.by_book_type
    assert len(clash.conflicts) == 2 and "htm_base" in clash.conflicts[0]


def test_period_start(store_url, ctx):
    view = MarketView(ctx.market.snapshot)
    view.business_date = PREV
    with store.connect(store_url) as con:
        assert funding_run.period_start(con, DATE, view) == PREV
        assert funding_run.period_start(con, date(2026, 10, 19), None) == date(2026, 10, 16)  # Monday: the Friday
        assert funding_run.period_start(con, DATE, None) == DATE - timedelta(days=1)


# ---- B3 cash (#61): a history file, balances by ledger -----------------------------------------------------------

CASH_HISTORY = """_report_date;entity;book_id;ccy;account_id;balance_ccy;ledger
2026-10-14;ENT1;D1;RUB;MAIN;-60000000;S1
2026-10-14;ENT1;D1;RUB;MARGIN;-40000000;S1
2026-10-14;ENT1;D1;RUB;MAIN;-100000000;S2
2026-10-14;ENT2;C1;RUB;MAIN;50000000;S1
2026-10-14;ENT2;C1;RUB;MAIN;49000000;S2
2026-10-14;ENT1;T1;RUB;MAIN;7000000;S1
;ENT1;D1;RUB;MAIN;-120000000;S1
;ENT1;D1;RUB;MAIN;-120000000;S2
;ENT2;C1;RUB;MAIN;55000000;S1
;ENT2;C1;RUB;MAIN;55000000;S2
"""


def test_cash_history_and_ledgers_fund_the_cash_books(store_url, ctx, tmp_path):
    """One multi-date file of the 15th carries the 14th's balances too (08 §6.1): the previous-day base of the 15th's
    period. Two ledgers that agree (D1: S1's two accounts sum to S2's one) fund the book; two that disagree (C1 of the
    entity funded on its whole cash) are a problem, not funded, and not reported a second time as missing cash."""
    with store.connect(store_url) as con:
        _scan(con, tmp_path, cash=(("cash_balances_20261015.csv", CASH_HISTORY),))
        _, rid2, _ = _runs(con, ctx)
    f = TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid2}/funding").json()
    assert f["state"] == "computed"
    lines = {x["ref"]: x for x in f["lines"]}
    ruo = ctx.market.latest_fixing("RUONIA")[1] / 100
    assert lines["D1|RUB"]["body_ccy"] == pytest.approx(-100_000_000)  # the 14th's, from the history rows
    assert lines["D1|RUB"]["interest_rub"] == pytest.approx(-100_000_000 * ruo / 365)
    assert "C1|RUB" not in lines and "T1|RUB" not in lines
    probs = [(p["kind"], p["ref"], p["reason"]) for p in f["problems"]]
    assert len(probs) == 1 and probs[0][:2] == ("CASH", "C1|RUB")
    assert "расхождение" in probs[0][2] and "S1, S2" in probs[0][2] and "1,000,000.00 RUB" in probs[0][2]
    assert {b["book_id"]: b["problems"] for b in f["books"]}["C1"] == 1


def test_summary_funding_its_own_factor_carry_without_it():
    """TDM#168: the pages' carry is the coupon income and the roll-down without the funding, the funding its own
    factor; the factors still sum to the day's total (the PnL net of funding). Funding not computed (a run before
    B1, not configured, a problem) is None, not 0 (B1), in a position's factors and in the run's sum."""
    from desk.publish import summary

    q = {"carry": 1000.0, "rolldown": 50.0, "funding": -400.0, "rates": 300.0, "convexity": 5.0, "spread": -20.0,
         "fx_gross": 10.0, "fx_funding": 0.0, "new_trades": 7.0, "other": 1.0}
    p = summary.pnl_factors(q)
    assert list(p) == ["carry", "funding", "rates", "spread", "fx", "trading", "other", "total"]
    assert p["carry"] == pytest.approx(1050.0) and p["funding"] == pytest.approx(-400.0)
    assert p["total"] == pytest.approx(sum(q.values()))  # the factors sum to the total, net of funding
    assert p["total"] == pytest.approx(sum(v for k, v in p.items() if k != "total"))
    before = summary.pnl_factors({k: v for k, v in q.items() if k != "funding"})  # a run before B1
    assert before["funding"] is None and before["total"] == pytest.approx(p["total"] + 400.0)
    assert before["total"] == pytest.approx(sum(v for k, v in before.items() if k != "total" and v is not None))
    assert summary.pnl_factors({**q, "funding": None})["funding"] is None  # not configured / a problem: null
    assert summary.pnl_factors({**q, "funding": 0.0})["funding"] == 0.0  # deferred / not funded: a real 0

    def row(pid, pnl):
        return {"position_id": pid, "book_id": "B", "pv_rub": 1e6, "risk": {}, "cs01": 0.0, "ofz": None,
                "instr_type": "BOND", "segment": "Корп", "asset_class": None, "coupon_type": "fix", "ccy": "RUB",
                "notional": 1e6, "notional_rub": 1e6, "price": 100.0, "spread_bp": 0.0, "dv01": None, "dm_bp": None,
                "pnl": pnl, "ofz_basis": None, "quality": "exact"}

    no_fund = {k: v for k, v in q.items() if k != "funding"}
    s = summary.build([row("A", no_fund), row("B", no_fund)])  # no position's funding computed: the run's is null
    assert s["pnl"]["funding"] is None and s["pnl"]["carry"] == pytest.approx(2100.0)
    assert s["pnl"]["total"] == pytest.approx(2 * before["total"])
    s = summary.build([row("A", q), row("B", no_fund)])  # one computed: the sum of those that have it
    assert s["pnl"]["funding"] == pytest.approx(-400.0)
    assert s["pnl"]["total"] == pytest.approx(sum(v for k, v in s["pnl"].items() if k != "total"))
    assert summary.VERSION >= 6  # summaries of older versions are rebuilt with the funding factor
