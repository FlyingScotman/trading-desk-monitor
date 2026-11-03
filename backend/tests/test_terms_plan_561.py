"""TDM#561 (private/1.8): the ``trade_list`` dataset (schema, storage as a stock replaced per book) and
``desk.terms_plan.plan``: which trades' terms to fetch. Synthetic data only."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from desk import store, terms_plan
from desk.inbox import reference, scan
from desk.inbox.loader import load_adapter
from desk.inbox.schema import validate_row
from desk.inbox.validate import check_read
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
DS = FIX / "private" / "v1" / "datasets"
DS_CONFIG, SNAPS = DS / "config", FIX / "mdhub_v1"
D = date.fromisoformat
DECL = {"source": "s", "dataset": "trade_list", "time_basis": "end_of_day", "completeness": "full_snapshot",
        "scope": ["book_id"], "covers": "listed"}
HIT = {"source": "s", "report_date": "2026-10-20", "date_from": "name", "report_ts": None, "seq": None}
BOOKS = [{"book_id": b, "name": b, "desk": "Rates", "book_type": "TRADING", "entity": "ENT1", "active": True}
         for b in ("T1", "D1")]
TERMS = ("trade_id;book_id;product_kind;trade_date;value_date;base_ccy;quote_ccy;side;notional_base;"
         "notional_quote;contract_rate\n")


def item(**kw) -> dict:
    return {"_src": "L2", "trade_id": "TR-1", "trade_version": "2", "book_id": "T1", "status": "live",
            "source_product": "fx_forward", "in_positions": True, **kw}


def codes(c) -> list[tuple[str, str]]:
    return sorted((i["severity"], i["code"]) for i in c.issues)


# ---- the dataset ----------------------------------------------------------------------------------------------


def test_a_row_is_validated_and_a_book_without_trades_is_a_marker() -> None:
    norm, errs, _ = validate_row("trade_list", item(x_ref="A"))
    assert errs == [] and norm["trade_version"] == 2 and norm["in_positions"] is True and norm["x_ref"] == "A"
    assert validate_row("trade_list", {k: v for k, v in item().items() if k not in ("trade_version", "in_positions",
                                                                                  "source_product")})[1] == []
    for bad, code in (({"status": "open"}, "bad_value"), ({"in_positions": "yes"}, "bad_value"),
                      ({"trade_version": "v2"}, "bad_number"), ({"trade_id": None}, "missing_required"),
                      ({"book_id": None}, "missing_required"), ({"price": 1}, "unknown_field")):
        assert code in [e.code for e in validate_row("trade_list", item(**bad))[1]], bad
    c = check_read(DECL, HIT, {"rows": [item(), item(trade_id="TR-2", status="cancelled")], "issues": [],
                               "scopes": [{"book_id": "T1"}, {"book_id": "D1"}], "controls": {}, "meta": {}},
                   books=BOOKS)
    assert c.status == "accepted" and codes(c) == []
    assert {u["scope"]["book_id"]: len(u["rows"]) for u in c.units} == {"T1": 2, "D1": 0}  # D1: listed, no trades
    dup = check_read(DECL, HIT, {"rows": [item(), item(_src="L3")], "issues": [], "scopes": [{"book_id": "T1"}],
                                 "controls": {}, "meta": {}}, books=BOOKS)
    assert dup.status == "rejected" and ("error", "duplicate_key") in codes(dup)


def test_the_reference_adapter_reads_trade_list_files() -> None:
    m = reference.match("/in/trade_list_20261020.csv", None)
    assert m == {"source": "trade_list_csv", "report_date": "2026-10-20", "date_from": "name", "report_ts": None,
                 "seq": None}
    h = load_adapter(None, str(DS_CONFIG), None)
    by = {s["source"]: s for s in h.describe["sources"]}
    assert by["trade_list_csv"]["dataset"] == "trade_list" and by["trade_list_csv"]["covers"] == "listed"
    assert by["trade_list_csv"]["time_basis"] == "end_of_day" and by["trade_list_csv"]["scope"] == ["book_id"]


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


def _put(con, s: Settings, inbox: Path, name: str, text: str) -> None:
    (inbox / name).write_text(text, encoding="utf-8")
    assert scan.scan(con, s).ok


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    p = tmp_path / "in"
    p.mkdir()
    return p


def test_the_fixture_is_stored_as_a_stock_replaced_per_book(con, tmp_path: Path, inbox: Path) -> None:
    s = _settings(tmp_path, inbox)
    shutil.copy(DS / "trade_list_20261020.csv", inbox / "trade_list_20261020.csv")
    r = scan.scan(con, s)
    assert r.ok and r.files == {"accepted": 1}, r.text()
    got = store.dataset_rows(con, "trade_list", D("2026-10-20"))
    assert sorted((x["trade_id"], x["book_id"], x["status"]) for x in got.rows) == [
        ("FX-1", "D1", "live"), ("FX-2", "D1", "live"), ("FX-3", "D1", "cancelled"), ("REPO-1", "T1", "live")]
    assert sorted(u["row_count"] for u in got.units) == [0, 1, 3]  # F1: a marker
    assert next(x for x in got.rows if x["trade_id"] == "FX-1")["trade_version"] == 2
    assert len(store.dataset_rows(con, "trade_list", D("2026-10-27")).rows) == 4  # a stock: carried forward
    assert store.dataset_rows(con, "trade_list", D("2026-10-19")).rows == []
    # the next list of D1 replaces D1's trades only
    _put(con, s, inbox, "trade_list_20261021.csv",
         "trade_id;trade_version;book_id;status\r\nFX-1;3;D1;live\r\n")
    got = store.dataset_rows(con, "trade_list", D("2026-10-21"))
    assert sorted((x["trade_id"], x["book_id"]) for x in got.rows) == [("FX-1", "D1"), ("REPO-1", "T1")]


# ---- the plan -------------------------------------------------------------------------------------------------


def test_plan_new_changed_have_closed_and_cancelled(con, tmp_path: Path, inbox: Path) -> None:
    s = _settings(tmp_path, inbox)
    assert terms_plan.plan(con, D("2026-10-20"), ["D1"]) is None  # no list stored
    terms = ("trade_id;trade_version;book_id;product_kind;trade_date;value_date;base_ccy;quote_ccy;side;"
             "notional_base;notional_quote;contract_rate\n")
    rows = "".join(f"{t};{v};{b};fx_forward;2026-10-01;2026-12-17;USD;RUB;buy;10;800;80\n" for t, v, b in (
        ("SAME", "2", "D1"), ("OLD", "1", "D1"), ("NOVER", "", "D1"), ("GONE", "1", "D1"), ("CANC", "1", "D1"),
        ("OTHER", "1", "T1")))
    _put(con, s, inbox, "trade_terms_20261020.csv", terms + rows)
    assert terms_plan.plan(con, D("2026-10-20"), ["D1"]) is None  # terms but no list
    _put(con, s, inbox, "trade_list_20261020.csv",
         "trade_id;trade_version;book_id;status\r\nSAME;2;D1;live\r\nOLD;2;D1;live\r\nNOVER;5;D1;live\r\n"
         "NEW;1;D1;live\r\nNEWNOVER;;D1;live\r\nCANC;2;D1;cancelled\r\nOUT;1;T1;live\r\n")
    p = terms_plan.plan(con, D("2026-10-20"), ["D1"])
    assert p["new"] == ["NEW", "NEWNOVER"] and p["changed"] == ["OLD"]
    assert p["have"] == ["NOVER", "SAME"]  # an unknown version on either side is no change
    assert p["closed"] == ["CANC", "GONE"]  # absent from the list, or cancelled; OTHER is another book's
    assert p["books"] == ["D1"] and p["live"] == 5
    assert p["counts"] == {"live": 5, "books": 1, "requested": 3, "new": 2, "changed": 1, "have": 2, "closed": 2}
    assert {t["trade_id"]: (t["need"], t["trade_version"]) for t in p["trades"]} == {
        "NEW": (True, 1), "NEWNOVER": (True, None), "NOVER": (False, 5), "OLD": (True, 2), "SAME": (False, 2)}
    assert all(t["book_id"] == "D1" for t in p["trades"])
    both = terms_plan.plan(con, D("2026-10-20"), ["D1", "T1"])
    assert both["books"] == ["D1", "T1"] and both["closed"] == ["CANC", "GONE", "OTHER"] and both["live"] == 6
    assert terms_plan.plan(con, D("2026-10-20"), ["C1"]) is None  # a book without a list
    assert terms_plan.plan(con, D("2026-10-19"), ["D1"]) is None  # before the list
    # a stock carried to the next day is no list of that day: a stale marker, not a plan
    assert terms_plan.plan(con, D("2026-10-21"), ["D1"]) == {"stale": True, "date": "2026-10-21"}
    # a book listed with no trades (a marker only): a plan with no trades, its terms are all closed
    _put(con, s, inbox, "trade_list_20261022.csv", "trade_id;trade_version;book_id;status\r\n;;D1;\r\n")
    _put(con, s, inbox, "trade_terms_20261022.csv", TERMS + "GONE;D1;fx_forward;2026-10-01;2026-12-17;USD;RUB;"
         "buy;10;800;80\n")
    p = terms_plan.plan(con, D("2026-10-22"), ["D1"])
    assert p["trades"] == [] and p["books"] == ["D1"] and p["closed"] == ["GONE"] and p["counts"]["live"] == 0
    # one stale unit among fresh ones: stale as a whole
    _put(con, s, inbox, "trade_list_20261023.csv", "trade_id;trade_version;book_id;status\r\nA;1;T1;live\r\n")
    assert terms_plan.plan(con, D("2026-10-23"), ["D1", "T1"]) == {"stale": True, "date": "2026-10-23"}
