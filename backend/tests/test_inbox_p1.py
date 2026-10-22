"""Private contract 1.2 (work package P1): FUTURES books, the ocp, cash_balances, pnl_official and trade_terms
schemas, reference() entities and funding rules, describe() settings tabs, multi-date cash history, the generic
dataset storage and the reference adapter over all of them. Synthetic data only."""

from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from desk import store
from desk.inbox import conformance, scan
from desk.inbox.loader import check_settings_tabs, load_adapter
from desk.inbox.schema import schema_fields, validate_row
from desk.inbox.spi import CONTRACT, DATASET_KEYS, MULTI_DATE_DATASETS, REFERENCE_KINDS
from desk.inbox.validate import check_read, check_reference, check_reference_tables
from desk.settings import Settings

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / "fixtures"
DS = FIX / "private" / "v1" / "datasets"
DS_CONFIG, SNAPS = DS / "config", FIX / "mdhub_v1"
DOC = REPO / "docs/architecture/08_PRIVATE_ADAPTER_INTERFACE.md"
D = date.fromisoformat
NEW = ("ocp", "cash_balances", "pnl_official", "trade_terms", "entities", "funding_rules")
BOOKS = [{"book_id": b, "name": b, "desk": "Rates", "book_type": t, "entity": e, "active": True}
         for b, t, e in (("T1", "TRADING", "ENT1"), ("D1", "DERIVS", "ENT1"), ("F1", "FUTURES", "ENT1"),
                         ("C1", "TRADING", "ENT2"))]
HIT = {"source": "s", "report_date": "2026-10-20", "date_from": "name", "report_ts": None, "seq": None}


def decl(dataset: str, scope: list[str], **kw) -> dict:
    return {"source": "s", "dataset": dataset, "time_basis": "end_of_day", "completeness": "full_snapshot",
            "scope": scope, "covers": "listed", **kw}


def result(rows: list[dict], scopes: list[dict]) -> dict:
    return {"rows": rows, "scopes": scopes, "issues": [], "controls": {}, "meta": {}}


def fwd(**kw) -> dict:
    return {"_src": "L2", "trade_id": "FX-1", "book_id": "D1", "product_kind": "fx_forward", "trade_date": "2026-10-01",
            "value_date": "2026-12-17", "base_ccy": "usd", "quote_ccy": "RUB", "side": "buy",
            "notional_base": "1000000", "notional_quote": 83500000, **kw}


# ---- the contract files ------------------------------------------------------------------------------------


def _doc_table(heading: str) -> list[tuple[str, bool]]:
    text = DOC.read_text(encoding="utf-8")
    block = text.split(heading, 1)[1].split("\n### ", 1)[0]
    out = []
    for line in block.splitlines():
        if line.startswith("| `"):
            cell = line.split("|")[1]
            out += [(f, "●" in cell) for f in re.findall(r"`([a-z0-9_]+)`", cell)]
    return out


@pytest.mark.parametrize("name", NEW)
def test_new_schemas_equal_the_08_tables_and_their_templates(name: str) -> None:
    table = _doc_table(f"### `{name}`")
    assert schema_fields(name) == ([f for f, _ in table], [f for f, r in table if r])
    raw = (REPO / f"contract/private/v1/{name}.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and raw.endswith(b"\r\n") and raw.count(b"\n") == 1
    assert raw[3:].decode().strip().split(";") == schema_fields(name)[0]
    schema = json.loads((REPO / f"contract/private/v1/{name}.schema.json").read_text(encoding="utf-8"))
    assert schema["$id"] == f"private/1.2/{name}" and schema["additionalProperties"] is False


def test_contract_is_1_2_and_keys_are_schema_fields() -> None:
    assert CONTRACT == "private/1.2" and "cash_balances" in MULTI_DATE_DATASETS and "ocp" not in MULTI_DATE_DATASETS
    for ds, keys in DATASET_KEYS.items():
        fields = schema_fields(ds)[0]
        assert not fields or set(keys) <= set(fields), ds  # trades: no schema yet
    assert "## 10 Private input contract v1 (`private/1.2`)" in DOC.read_text(encoding="utf-8")
    for phase in ("A7", "A8", "A9", "A10"):
        assert phase in DOC.read_text(encoding="utf-8").split("## 11 Integration points", 1)[1].split("## 12", 1)[0]


def test_futures_book_type_and_backward_compatible_books() -> None:
    books, issues = check_reference({"books": [{**b, "_src": f"L{i}"} for i, b in enumerate(BOOKS, 2)]})
    assert [b["book_type"] for b in books] == ["TRADING", "DERIVS", "FUTURES", "TRADING"] and issues == []
    _, errs, _ = validate_row("books", {**BOOKS[0], "_src": "L1", "book_type": "BANKING"})
    assert [(e.code, e.field) for e in errs] == [("bad_value", "book_type")]
    # a 1.1 reference without entities or funding rules: tables absent, nothing else asked
    tables, issues = check_reference_tables({"books": [{**BOOKS[0], "_src": "L2"}]})
    assert tables == {"books": [{**BOOKS[0], "_src": "L2"}], "entities": None, "funding_rules": None}
    assert issues == [] and tuple(tables) == REFERENCE_KINDS


# ---- rows -----------------------------------------------------------------------------------------------------


def test_ocp_and_cash_rows_normalise_and_cash_keys_include_the_ledger() -> None:
    norm, errs, _ = validate_row("ocp", {"_src": "L2", "entity": "E", "ccy": "rur", "ocp_ccy": 1.5})
    assert errs == [] and norm == {"_src": "L2", "entity": "E", "ccy": "RUB", "ocp_ccy": "1.5"}
    cash = {"_src": "L2", "entity": "ENT1", "book_id": "D1", "ccy": "RUB", "account_id": "MAIN", "balance_ccy": "-5"}
    d = decl("cash_balances", ["entity", "book_id"])
    two = [{**cash, "ledger": "S1"}, {**cash, "ledger": "S2", "_src": "L3"}]
    c = check_read(d, HIT, result(two, [{"entity": "ENT1", "book_id": "D1"}]), books=BOOKS)
    assert c.status == "accepted" and c.issues == []
    c = check_read(d, HIT, result([cash, {**cash, "_src": "L3"}], [{"entity": "ENT1", "book_id": "D1"}]), books=BOOKS)
    assert c.status == "rejected" and [i["code"] for i in c.issues] == ["duplicate_key"]


def test_cash_history_is_multi_date_and_ocp_is_not() -> None:
    cash = {"_src": "L2", "entity": "ENT1", "book_id": "D1", "ccy": "RUB", "account_id": "MAIN", "balance_ccy": "1"}
    rows = [{**cash, "_report_date": "2026-10-16"}, {**cash, "_src": "L3"}]
    scopes = [{"entity": "ENT1", "book_id": "D1", "_report_date": "2026-10-16"}, {"entity": "ENT1", "book_id": "D1"}]
    c = check_read(decl("cash_balances", ["entity", "book_id"]), HIT, result(rows, scopes), books=BOOKS)
    assert c.status == "accepted" and sorted(u["report_date"] for u in c.units) == ["2026-10-16", "2026-10-20"]
    ocp = {"_src": "L2", "entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "1", "_report_date": "2026-10-16"}
    c = check_read(decl("ocp", ["entity", "book_id"]), HIT, result([ocp], [{"entity": "ENT1", "book_id": "D1"}]),
                   books=BOOKS)
    assert c.file_error == "bad_row"


def test_pnl_official_integers_and_parts_check() -> None:
    line = {"_src": "L2", "entity": "ENT1", "book_id": "T1", "line_id": "K1", "pnl_daily_rub": "100",
            "pnl_pl_daily_rub": "60", "pnl_oci_daily_rub": "40", "trade_version": "3"}
    norm, errs, _ = validate_row("pnl_official", line)
    assert errs == [] and norm["trade_version"] == 3
    for bad in ("3.5", True, 3.0, "x"):
        assert [e.code for e in validate_row("pnl_official", {**line, "trade_version": bad})[1]] == ["bad_number"]
    d = decl("pnl_official", ["entity", "book_id"])
    c = check_read(d, HIT, result([line, {**line, "line_id": "K2", "pnl_daily_rub": "101"}],
                                  [{"entity": "ENT1", "book_id": "T1"}]), books=BOOKS)
    assert c.status == "accepted" and [(i["severity"], i["code"], i["count"]) for i in c.issues] == [
        ("warning", "pnl_parts_mismatch", 1)]
    c = check_read(d, HIT, result([{k: v for k, v in line.items() if k != "pnl_oci_daily_rub"}],
                                  [{"entity": "ENT1", "book_id": "T1"}]), books=BOOKS)
    assert c.status == "rejected" and c.issues[0]["code"] == "missing_required"


def test_trade_terms_fx_rules() -> None:
    norm, errs, _ = validate_row("trade_terms", fwd(ndf="1"))
    assert [(e.code, e.field) for e in errs] == [("bad_value", "ndf")]  # booleans are JSON booleans
    norm, errs, _ = validate_row("trade_terms", fwd())
    assert errs == [] and norm["base_ccy"] == "USD" and norm["ndf"] is False and norm["notional_quote"] == "83500000"
    d, scope = decl("trade_terms", ["book_id"]), [{"book_id": "D1"}]

    def codes(*rows: dict) -> list[tuple[str, str]]:
        return [(i["severity"], i["code"]) for i in check_read(d, HIT, result(list(rows), scope), books=BOOKS).issues]

    assert codes(fwd(), fwd(trade_id="FX-2", ndf=True, settle_ccy="RUB", fixing_date="2027-01-13")) == []
    assert codes(fwd(ndf=True)) == [("warning", "ndf_without_fixing")]
    assert codes({k: v for k, v in fwd().items() if k != "side"}) == [("error", "missing_required")]
    assert codes(fwd(quote_ccy="USD")) == [("error", "bad_value")]
    near = fwd(product_kind="fx_swap", leg_id="near")
    assert codes(near, {**near, "leg_id": "far", "side": "sell"}) == []
    assert codes(near, near) == [("error", "duplicate_key")]
    assert codes(fwd(product_kind="fx_swap")) == [("error", "missing_required")]
    assert codes(fwd(product_kind="irs")) == [("error", "bad_value")]  # later minors


# ---- reference tables and settings tabs ----------------------------------------------------------------------------


def test_reference_entities_and_funding_rules() -> None:
    ent = [{"_src": "L2", "entity": "ENT1", "funding_scheme": "by_book_type"},
           {"_src": "L3", "entity": "ENT2", "funding_scheme": "cash_balance", "pnl_sweep": "monthly"}]
    rule = {"_src": "L2", "rule_id": "r1", "book_type": "TRADING", "ccy": "rub", "basis": "mv_clean",
            "rate_kind": "index_spread", "index_code": "KEY_RATE", "spread_bp": 100}
    rules = [rule, {**rule, "_src": "L3", "valid_from": "2026-10-19", "spread_bp": "150"},
             {"_src": "L4", "rule_id": "fut", "book_type": "FUTURES", "rate_kind": "none"}]
    books = [{**b, "_src": "L1"} for b in BOOKS]
    tables, issues = check_reference_tables({"books": books, "entities": ent, "funding_rules": rules})
    assert issues == [] and [e["active"] for e in tables["entities"]] == [True, True]
    assert [(r["rule_id"], r.get("valid_from"), r.get("spread_bp")) for r in tables["funding_rules"]] == [
        ("r1", None, "100"), ("r1", "2026-10-19", "150"), ("fut", None, None)]
    assert tables["funding_rules"][0]["ccy"] == "RUB"

    bad = [{**rule, "_src": "L5", "index_code": None}, {**rule, "_src": "L6", "rule_id": "fx", "rate_kind": "fixed"},
           {**rule, "_src": "L7"}, {**rule, "_src": "L8", "rule_id": "r2", "entity": "ENT9", "book_id": "X"},
           {**rule, "_src": "L9", "rule_id": "r3", "entity": "ENT9", "book_id": "X"}, {"_src": "L10", "rule_id": "z"}]
    tables, issues = check_reference_tables({"books": books + [{**books[0], "book_id": "T9", "entity": "ENT3"}],
                                             "entities": ent + [{"_src": "L4", "entity": "ENT1",
                                                                 "funding_scheme": "x"}],
                                             "funding_rules": [rule] + bad})
    got = sorted((i["severity"], i["code"], i["count"]) for i in issues)
    assert got == sorted([
        ("error", "missing_required", 1), ("error", "missing_required", 1),  # index_code; fixed_rate_pct
        ("error", "missing_required", 1),                                    # z: rate_kind
        ("error", "duplicate_key", 2),                                       # r1 twice without valid_from
        ("error", "bad_value", 1),                                           # funding_scheme
        ("warning", "unknown_entity", 1), ("warning", "unknown_entity", 1), ("warning", "unknown_entity", 1),
        ("warning", "unknown_book", 1), ("warning", "unknown_book", 1),
        ("warning", "ambiguous_rule", 2)])                                   # r2 and r3: equal selectors
    assert [r["rule_id"] for r in tables["funding_rules"]] == ["r2", "r3"]
    assert [e["entity"] for e in tables["entities"]] == ["ENT1", "ENT2"]
    assert all("ENT9" not in (i["message"] or "") for i in issues)  # no values in messages
    tables, issues = check_reference_tables({"books": books, "entities": {"x": 1}})
    assert tables["entities"] is None and [i["code"] for i in issues] == ["bad_reference"]


def test_settings_tabs_are_checked_and_never_fatal() -> None:
    files = [{"name": "funding.json", "title": "Funding"}, {"name": "entities.json"}]
    form = {"type": "object", "properties": {"rules": {"type": "array"}}}
    desc = {"config_files": files, "settings_tabs": [
        {"id": "funding", "title": "Funding", "file": "funding.json", "schema": form, "help": "rates"},
        {"id": "funding", "file": "entities.json", "schema": form},
        {"id": "Bad id", "file": "entities.json", "schema": form},
        {"id": "prices", "file": "prices.json", "schema": form},
        {"id": "entities", "file": "entities.json", "schema": {"type": "array"}},
        {"id": "entities2", "file": "entities.json", "schema": form, "title": 5}]}
    tabs, warnings = check_settings_tabs(desc)
    assert [(t["id"], t["title"], t["file"]) for t in tabs] == [("funding", "Funding", "funding.json"),
                                                                 ("entities2", "entities2", "entities.json")]
    assert len(warnings) == 5 and all(w.startswith("settings_tabs[") for w in warnings)
    assert check_settings_tabs({}) == ([], []) and check_settings_tabs({"settings_tabs": {}})[1]


def test_reference_adapter_declares_every_dataset_and_reads_the_reference_tables(tmp_path: Path) -> None:
    h = load_adapter(None, str(DS_CONFIG), None)
    assert h.ok and h.describe["contract"] == "private/1.2" and h.describe["sources"][0]["source"] == "positions_csv"
    assert {s["dataset"] for s in h.describe["sources"]} == {"positions", "ocp", "cash_balances", "pnl_official",
                                                            "trade_terms"}
    rep = conformance.check(None, [str(DS)], stub_ctx=True, config_dir=str(DS_CONFIG))
    assert rep.ok, rep.text()
    assert rep.reference == {"books": 4, "entities": 2, "funding_rules": 7}
    g = {k[0]: v for k, v in rep.groups.items() if k[1] == "2026-10-20"}
    assert g["ocp_csv"].statuses == {"accepted": 1} and g["ocp_csv"].units == {"accepted": 2}
    assert g["cash_balances_csv"].units == {"accepted": 3} and g["cash_balances_csv"].rows == 6
    assert g["pnl_official_csv"].issues == {("warning", "pnl_parts_mismatch"): 1}
    assert g["trade_terms_csv"].statuses == {"partially_accepted": 1}
    assert g["trade_terms_csv"].units == {"accepted": 1, "rejected": 1}
    j = rep.to_json()
    assert j["reference"] == {"books": "<10", "entities": "<10", "funding_rules": "<10"}
    assert "reference: books <10" in rep.text() and "ENT1" not in rep.text() and "KEY_RATE" not in rep.text()


# ---- the store ---------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_process():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


@pytest.fixture
def con(store_url):
    with store.connect(store_url) as c:
        yield c


def _settings(tmp_path: Path, inbox: Path) -> Settings:
    return Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                    private_adapter=None, private_adapter_config=DS_CONFIG, inbox_poll_seconds=0,
                    inbox_lookback_days=0)


def test_scan_stores_the_datasets_with_revisions_and_stock_flow_reads(con, tmp_path: Path) -> None:
    inbox = tmp_path / "in"
    inbox.mkdir()
    for n in ("ocp_20261020.csv", "cash_balances_20261020.csv", "pnl_official_20261020.csv",
              "trade_terms_20261020.csv"):
        shutil.copy(DS / n, inbox / n)
    s = _settings(tmp_path, inbox)
    r = scan.scan(con, s)
    assert r.ok and r.files == {"accepted": 3, "partially_accepted": 1}, r.text()
    assert r.units == {"accepted": 8, "rejected": 1} and r.revisions == 8
    assert r.reference_revised == {"entities": 2, "funding_rules": 7} and "reference revised" in r.text()

    ocp = store.dataset_rows(con, "ocp", D("2026-10-20"))
    assert sorted((x["book_id"], x["ccy"], x["ocp_ccy"]) for x in ocp.rows) == [("D1", "CNY", "-2500000.5"),
                                                                                 ("D1", "USD", "1500000")]
    assert sorted(u["row_count"] for u in ocp.units) == [0, 2]  # T1: a covered, empty unit
    assert ocp.rows[0]["x"] == {"x_lines": "2"} or ocp.rows[1]["x"] == {"x_lines": "2"}
    # a stock is carried forward; the next day's snapshot closes D1's CNY and leaves T1 alone (covers = listed)
    assert len(store.dataset_rows(con, "ocp", D("2026-10-25")).rows) == 2
    shutil.copy(DS / "ocp_20261021.csv", inbox / "ocp_20261021.csv")
    assert scan.scan(con, s).revisions == 1
    later = store.dataset_rows(con, "ocp", D("2026-10-21"))
    assert [(x["ccy"], x["ocp_ccy"], x["_valid_date"]) for x in later.rows] == [("USD", "1200000", D("2026-10-21"))]
    assert {json.dumps(u["scope"], sort_keys=True): u["valid_date"] for u in later.units} == {
        '{"book_id": "D1", "entity": "ENT1"}': D("2026-10-21"), '{"book_id": "T1", "entity": "ENT1"}': D("2026-10-20")}
    assert len(store.dataset_rows(con, "ocp", D("2026-10-21"), scope={"book_id": "D1"}).units) == 1

    # multi-date cash: each row's date is a unit of its own
    cash16 = store.dataset_rows(con, "cash_balances", D("2026-10-16"))
    assert sorted((x["ccy"], x["balance_ccy"]) for x in cash16.rows) == [("RUB", "-50000000"), ("USD", "400000")]
    cash20 = store.dataset_rows(con, "cash_balances", D("2026-10-20"))
    assert sorted((x["book_id"], x["ccy"], x.get("ledger")) for x in cash20.rows) == [
        ("C1", "RUB", None), ("D1", "RUB", "S1"), ("D1", "RUB", "S2"), ("D1", "USD", "S1")]
    assert store.dataset_rows(con, "cash_balances", D("2026-10-15")).rows == []
    view = con.execute("SELECT valid_date, balance_ccy FROM cash_balance_row WHERE ledger = 'S2'").fetchall()
    assert [(d, str(b)) for d, b in view] == [(D("2026-10-20"), "-51990000")]

    # a flow is never carried forward
    pnl = store.dataset_rows(con, "pnl_official", D("2026-10-20"))
    assert sorted(x["line_id"] for x in pnl.rows) == ["K1", "K2", "K3"]
    assert {x["line_id"]: x.get("trade_version") for x in pnl.rows} == {"K1": 2, "K2": 1, "K3": None}
    assert store.dataset_rows(con, "pnl_official", D("2026-10-21")).rows == []

    terms = con.execute("SELECT trade_id, leg_id, side, notional_quote, ndf, fixing_date FROM trade_terms_row"
                        " ORDER BY trade_id, leg_id").fetchall()
    assert [(t, leg, side, str(n), ndf) for t, leg, side, n, ndf, _ in terms] == [
        ("FX-1", None, "buy", "83500000", False), ("FX-2", None, "sell", "58000000", True),
        ("SW-1", "far", "buy", "164900000", False), ("SW-1", "near", "sell", "164000000", False)]
    units = con.execute("SELECT status, dataset_set_id IS NOT NULL, set_id IS NULL FROM inbox_unit"
                        " WHERE dataset = 'trade_terms' ORDER BY status").fetchall()
    assert units == [("accepted", True, True), ("rejected", False, True)]

    # the same files again under new names: equal content, no new revisions
    for n in ("ocp_20261020.csv", "pnl_official_20261020.csv"):
        (inbox / n).rename(inbox / n.replace(".csv", "_1.csv"))
    (inbox / "ocp_20261020_1.csv").write_bytes((DS / "ocp_20261020.csv").read_bytes() + b"\r\n")
    r = scan.scan(con, s)
    assert r.revisions == 0 and r.units == {"accepted": 2} and r.copies == {"duplicate": 1}, r.text()
    with pytest.raises(Exception, match="append-only|forbid|not allowed|immutable"):
        con.execute("DELETE FROM dataset_row")


def test_reference_rows_are_bitemporal_with_history(con, tmp_path: Path) -> None:
    rules = [{"rule_id": "r1", "rate_kind": "fixed", "fixed_rate_pct": "5", "active": True},
             {"rule_id": "r1", "valid_from": "2026-10-19", "rate_kind": "fixed", "fixed_rate_pct": "6",
              "active": True},
             {"rule_id": "r2", "valid_from": "2026-11-01", "rate_kind": "none", "active": True}]
    assert store.store_reference(con, "funding_rules", rules, "fp") == ["r1", "r1", "r2"]
    assert store.store_reference(con, "funding_rules", rules, "fp") == []  # unchanged: no revision

    def on(d: str) -> dict:
        return {r["rule_id"]: r.get("fixed_rate_pct") for r in store.current_reference(con, "funding_rules", D(d))}

    assert on("2026-10-01") == {"r1": "5"} and on("2026-10-19") == {"r1": "6"}
    assert on("2026-11-01") == {"r1": "6", "r2": None}
    t = con.execute("SELECT now()").fetchone()[0]
    store.store_reference(con, "funding_rules", [{**rules[1], "fixed_rate_pct": "6.5"}], "fp2")
    assert on("2026-10-20") == {"r1": "6.5"}
    assert store.current_reference(con, "funding_rules", D("2026-10-20"), t)[0]["fixed_rate_pct"] == "6"
    assert store.current_reference(con, "entities", D("2026-10-20")) == []
