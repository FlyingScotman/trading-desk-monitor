"""S13a (TDM#525): the ``trades`` dataset of the private contract (private/1.6, 08 §10): schema and validation, storage
of cumulative exports and deltas with versions, cancellations and late bookings, ``GET /api/v1/trades`` behind the
monitors' screen C20 «Сделки дня», the reference adapter's ``trades_*.csv`` and the conformance kit. Synthetic data
only (``fixtures/private/v1/datasets``)."""

from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import daystatus, store
from desk.api import create_app
from desk.inbox import conformance, reference, scan
from desk.inbox.loader import load_adapter
from desk.inbox.schema import validate_row
from desk.inbox.validate import check_hit, check_read
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
DS = FIX / "private" / "v1" / "datasets"
DS_CONFIG, SNAPS = DS / "config", FIX / "mdhub_v1"
D = date.fromisoformat
BOOKS = [{"book_id": b, "name": b, "desk": "Rates", "book_type": "TRADING", "entity": "ENT1", "active": True}
         for b in ("T1", "D1")]
DECL = {"source": "s", "dataset": "trades", "time_basis": "intraday", "completeness": "full_snapshot",
        "scope": ["book_id"], "covers": "listed"}
DELTA = {"source": "s", "dataset": "trades", "time_basis": "intraday", "completeness": "increment", "scope": ["book_id"]}
HIT = {"source": "s", "report_date": "2026-10-20", "date_from": "name", "report_ts": "2026-10-20T15:00:00+03:00",
       "seq": None}
CUMULATIVE = ("trades_20261020_1100.csv", "trades_20261020_1500.csv")
DELTAS = ("trades_delta_20261021_1000.csv", "trades_delta_20261021_1200.csv")


def trade(**kw) -> dict:
    return {"_src": "L2", "trade_id": "TR-1", "trade_version": "1", "status": "live", "trade_date": "2026-10-20",
            "trade_ts": "2026-10-20T10:15:00", "book_id": "T1", "position_id": "T1|SEC-1", "asset_class": "bond",
            "source_instrument_id": "SEC-1", "side": "buy", "quantity": 1000, "price_pct": "98.5", "ccy": "rub", **kw}


def read(rows: list[dict], scopes: list[dict] | None = None) -> dict:
    return {"rows": rows, "scopes": [{"book_id": "T1"}] if scopes is None else scopes, "issues": [], "controls": {},
            "meta": {}}


def codes(c) -> list[tuple[str, str]]:
    return sorted((i["severity"], i["code"]) for i in c.issues)


# ---- schema and validation ------------------------------------------------------------------------------------


def test_a_good_row_is_normalised_and_late_bookings_and_cancellations_are_valid() -> None:
    norm, errs, _ = validate_row("trades", trade(x_desk_ref="A1"))
    assert errs == [] and norm["trade_version"] == 1 and norm["ccy"] == "RUB" and norm["quantity"] == "1000"
    assert norm["x_desk_ref"] == "A1" and norm["price_pct"] == "98.5"
    for ts in ("2026-10-20T10:15", "2026-10-20T10:15:00.123+03:00", "2026-10-20T07:15:00Z"):
        assert validate_row("trades", trade(trade_ts=ts))[1] == [], ts
    c = check_read(DECL, HIT, read([trade(), trade(trade_id="TR-2", status="cancelled"),
                                    trade(trade_id="TR-3", trade_date="2026-10-17"),  # a late booking: kept
                                    trade(trade_id="TR-1", trade_version=2, quantity=1200)]), books=BOOKS)
    assert c.status == "accepted" and codes(c) == [] and len(c.units[0]["rows"]) == 4


@pytest.mark.parametrize("bad,code", [
    ({"status": "open"}, "bad_value"), ({"side": "long"}, "bad_value"), ({"asset_class": "bonds"}, "bad_value"),
    ({"trade_ts": "2026-10-20 10:15"}, "bad_date"), ({"trade_ts": "2026-13-20T10:15:00"}, "bad_date"),
    ({"trade_date": "20.10.2026"}, "bad_date"), ({"trade_version": "v2"}, "bad_number"),
    ({"price_pct": "98,5"}, "bad_number"), ({"position_id": None}, "missing_required"),
    ({"source_instrument_id": ""}, "bad_value"), ({"notional": 1}, "unknown_field"),
    ({"quantity": -1000}, "bad_value"), ({"amount_ccy": "-5"}, "bad_value"), ({"face_amount_ccy": -1}, "bad_value"),
])
def test_a_bad_row_rejects_its_book(bad: dict, code: str) -> None:
    c = check_read(DECL, HIT, read([trade(**bad), trade(trade_id="TR-9", book_id="D1", position_id="D1|SEC-1")],
                                   scopes=[{"book_id": "T1"}, {"book_id": "D1"}]), books=BOOKS)
    assert c.status == "partially_accepted", bad
    assert {u["scope"]["book_id"]: u["status"] for u in c.units} == {"T1": "rejected", "D1": "accepted"}
    assert ("error", code) in codes(c), codes(c)
    assert all("98" not in (i["message"] or "") and "SEC" not in (i["message"] or "") for i in c.issues)  # no values


def test_versions_are_the_key_and_intraday_needs_a_cut() -> None:
    dup = check_read(DECL, HIT, read([trade(), trade(_src="L3")]), books=BOOKS)
    assert dup.status == "rejected" and ("error", "duplicate_key") in codes(dup)
    assert check_read(DECL, HIT, read([trade(), trade(trade_version=2)]), books=BOOKS).status == "accepted"
    assert check_hit(DECL, {**HIT, "report_ts": None}) == "bad_hit: report_ts is required for intraday"
    unknown = check_read(DECL, HIT, read([trade(book_id="X9", position_id="X9|1")], scopes=[{"book_id": "X9"}]),
                         books=BOOKS)
    assert unknown.status == "rejected" and ("error", "unknown_book") in codes(unknown)
    # deltas: no scopes, the units come from the rows; a multi-date file puts each row on its own date
    c = check_read(DELTA, HIT, {"rows": [trade(), trade(trade_id="TR-2", _report_date="2026-10-19")]}, books=BOOKS)
    assert c.status == "accepted" and sorted(u["report_date"] for u in c.units) == ["2026-10-19", "2026-10-20"]


def test_the_reference_adapter_matches_trades_with_their_cut() -> None:
    m = reference.match("/in/trades_20261020_1500.csv", None)
    assert m == {"source": "trades_csv", "report_date": "2026-10-20", "date_from": "name",
                 "report_ts": "2026-10-20T15:00:00+03:00", "seq": None}
    assert reference.match("/in/trades_20261020.csv", None)["report_ts"] == "2026-10-20T23:59:59+03:00"
    assert reference.match("/in/trades_delta_20261021_1000.csv", None)["source"] == "trades_delta_csv"
    assert reference.match("/in/trades_20261020_2460.csv", None) is None  # not a time: not ours
    assert reference.match("/in/positions_20261020_2.csv", None)["seq"] == 2  # the others keep seq
    h = load_adapter(None, str(DS_CONFIG), None)
    by = {s["source"]: s for s in h.describe["sources"]}
    assert h.ok and by["trades_csv"]["completeness"] == "full_snapshot" and by["trades_csv"]["covers"] == "listed"
    assert by["trades_delta_csv"]["completeness"] == "increment" and "covers" not in by["trades_delta_csv"]
    assert {by[s]["dataset"] for s in ("trades_csv", "trades_delta_csv")} == {"trades"}


def test_the_kit_accepts_trades_sources(tmp_path: Path) -> None:
    for n in (*CUMULATIVE, *DELTAS):
        shutil.copy(DS / n, tmp_path / n)
    rep = conformance.check(None, [str(tmp_path)], stub_ctx=True, config_dir=str(DS_CONFIG))
    assert rep.ok, rep.text()
    full, delta = rep.groups[("trades_csv", "2026-10-20")], rep.groups[("trades_delta_csv", "2026-10-21")]
    assert full.statuses == {"accepted": 2} and full.units == {"accepted": 4} and full.rows == 10 and full.issues == {}
    assert delta.statuses == {"accepted": 2} and delta.units == {"accepted": 2} and delta.rows == 5
    assert "trades_csv 2026-10-20 -> 2026-10-20" in rep.text() and "TR-1" not in rep.text()


# ---- the store, the API and the day status ------------------------------------------------------------------


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


def _inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "in"
    inbox.mkdir()
    return inbox


def _now(con) -> datetime:
    return con.execute("SELECT now()").fetchone()[0]


def _current(con, day: str, **where) -> dict[str, tuple]:
    rows = store.dataset_rows(con, "trades", D(day), scope=where or None).rows
    return {r["trade_id"]: (r["book_id"], r.get("trade_version"), r["status"], r.get("quantity") or r.get("amount_ccy"))
            for r in rows}


def test_cumulative_exports_replace_the_books_trades_by_cut(con, tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    s = _settings(tmp_path, inbox)
    shutil.copy(DS / CUMULATIVE[0], inbox / CUMULATIVE[0])
    r = scan.scan(con, s)
    assert r.ok and r.files == {"accepted": 1} and r.units == {"accepted": 2}, r.text()
    first = _current(con, "2026-10-20")
    assert first == {"TR-10": ("T1", 1, "live", "1000"), "TR-11": ("T1", 1, "live", "500"),
                     "TR-15": ("T1", 1, "live", "100"), "TR-12": ("D1", 1, "live", "1000000")}
    at_11 = _now(con)
    shutil.copy(DS / CUMULATIVE[1], inbox / CUMULATIVE[1])
    r = scan.scan(con, s)
    assert r.files == {"accepted": 1} and r.revisions == 2, r.text()
    # the 15:00 export replaces T1's and D1's trades: TR-10 amended (v2 current), TR-11 cancelled (kept), TR-15 gone,
    # TR-13 a late booking of the day before, TR-14 new
    now = _current(con, "2026-10-20")
    assert now == {"TR-10": ("T1", 2, "live", "1200"), "TR-11": ("T1", 2, "cancelled", "500"),
                   "TR-13": ("T1", 1, "live", "300"), "TR-12": ("D1", 1, "live", "1000000"),
                   "TR-14": ("D1", 1, "live", "5000000")}
    rows = store.dataset_rows(con, "trades", D("2026-10-20")).rows
    assert next(r for r in rows if r["trade_id"] == "TR-13")["trade_date"] == "2026-10-19"
    assert next(r for r in rows if r["trade_id"] == "TR-10")["x"] == {"x_desk_ref": "A1"}
    assert {r["_src"] for r in rows if r["trade_id"] == "TR-10"} == {"L3"}  # the v2 line of the 15:00 file
    assert _current(con, "2026-10-20", book_id="D1") == {k: v for k, v in now.items() if v[0] == "D1"}
    # as known at 11:00, and a flow is never carried forward
    assert {r["trade_id"] for r in store.dataset_rows(con, "trades", D("2026-10-20"), known_at=at_11).rows} == set(first)
    assert store.dataset_rows(con, "trades", D("2026-10-21")).rows == []
    # every stored version is in the typed view
    view = con.execute("SELECT trade_id, trade_version, status, completeness, report_ts FROM trade_row"
                       " WHERE trade_id = 'TR-10' ORDER BY set_id, trade_version").fetchall()
    assert [(t, v, st, c) for t, v, st, c, _ in view] == [("TR-10", 1, "live", "full_snapshot")] * 2 + [
        ("TR-10", 2, "live", "full_snapshot")]
    assert view[-1][4].astimezone(store.MSK).hour == 15


def test_an_earlier_cut_arriving_late_is_superseded(con, tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    s = _settings(tmp_path, inbox)
    shutil.copy(DS / CUMULATIVE[1], inbox / CUMULATIVE[1])
    assert scan.scan(con, s).files == {"accepted": 1}
    shutil.copy(DS / CUMULATIVE[0], inbox / CUMULATIVE[0])  # the 11:00 export, read after the 15:00 one
    r = scan.scan(con, s)
    assert r.files == {"superseded": 1} and r.revisions == 0, r.text()
    assert _current(con, "2026-10-20")["TR-10"] == ("T1", 2, "live", "1200") and "TR-15" not in _current(con, "2026-10-20")


def test_deltas_add_up_by_version_in_any_order(con, tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    s = _settings(tmp_path, inbox)
    shutil.copy(DS / DELTAS[1], inbox / DELTAS[1])  # the 12:00 delta first
    assert scan.scan(con, s).files == {"accepted": 1}
    shutil.copy(DS / DELTAS[0], inbox / DELTAS[0])  # then the 10:00 one: still stored, it adds
    r = scan.scan(con, s)
    assert r.files == {"accepted": 1} and r.revisions == 1, r.text()
    assert _current(con, "2026-10-21") == {"TR-20": ("C1", 2, "live", "2500"), "TR-21": ("C1", 2, "cancelled", "700"),
                                           "TR-22": ("C1", 1, "live", "100")}
    t = store.dataset_rows(con, "trades", D("2026-10-21"))
    assert sorted(u["completeness"] for u in t.units) == ["increment", "increment"]
    # the same delta again under another cut (a blank line more: another file): equal rows, nothing new
    (inbox / "trades_delta_20261021_1001.csv").write_bytes((DS / DELTAS[0]).read_bytes() + b"\r\n")
    r = scan.scan(con, s)
    assert r.revisions == 0 and r.units == {"accepted": 1}, r.text()
    # a cumulative export of the same book is another source of the unit, without priorities: scope_conflict (08
    # §6.6, one source per unit), the deltas' trades stay
    (inbox / "trades_20261021_1300.csv").write_bytes((DS / DELTAS[0]).read_bytes() + b"\r\n\r\n")
    r = scan.scan(con, s)
    assert r.files == {"rejected": 1} and r.units == {"rejected": 1}, r.text()
    assert con.execute("SELECT code FROM inbox_issue ORDER BY issue_id DESC LIMIT 1").fetchone()[0] == "scope_conflict"
    assert _current(con, "2026-10-21")["TR-20"][1] == 2


def test_the_trades_api_and_the_c20_page(con, store_url, tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    for n in (*CUMULATIVE, *DELTAS):
        shutil.copy(DS / n, inbox / n)
    assert scan.scan(con, _settings(tmp_path, inbox)).ok
    c = TestClient(create_app(store_url))
    v = c.get("/api/v1/trades", params={"date": "2026-10-20"}).json()
    assert v["date"] == "2026-10-20" and v["summary"] == {"trades": 5, "live": 4, "cancelled": 1, "late": 1,
                                                          "books": 2, "versions": 2}
    assert [b["book_id"] for b in v["books"]] == ["D1", "T1"] and v["books"][1]["cancelled"] == 1
    tr13 = next(r for r in v["rows"] if r["trade_id"] == "TR-13")
    assert tr13["late"] is True and tr13["quantity"] == 300.0 and tr13["price_pct"] == 98.3 and tr13["src"] == "L5"
    assert {f["file_name"] for f in v["files"]} == {"trades_20261020_1500.csv"}
    assert [d["date"] for d in v["days"]] == ["2026-10-20"]
    t1 = c.get("/api/v1/trades", params={"date": "2026-10-20", "book_id": "T1"}).json()
    assert {r["book_id"] for r in t1["rows"]} == {"T1"} and t1["summary"]["trades"] == 3
    latest = c.get("/api/v1/trades").json()  # the latest date with trades
    assert latest["date"] == "2026-10-21" and latest["summary"]["trades"] == 3 and latest["files"][0]["completeness"] \
        == "increment"
    assert c.get("/api/v1/trades", params={"date": "20.10.2026"}).status_code == 400
    assert c.get("/api/v1/trades", params={"days": 0}).status_code == 422
    assert c.get("/api/v1/trades", params={"date": "2026-10-01"}).json()["rows"] == []
    assert c.get("/concepts/c20-trades.html").status_code == 200
    # the day status lists the trades in force
    inputs = {i["dataset"]: i for i in daystatus.day(con, _settings(tmp_path, inbox), D("2026-10-20"))["inputs"]}
    assert inputs["trades"]["name"] == "Сделки дня" and inputs["trades"]["stored_rows"] == 5
    assert inputs["trades"]["stored_sets"] == 2 and inputs["trades"]["state"] != "missing"


def test_no_trade_is_carried_or_lost_between_dates(con, tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    for n in CUMULATIVE:
        shutil.copy(DS / n, inbox / n)
    assert scan.scan(con, _settings(tmp_path, inbox)).ok
    day = D("2026-10-20")
    assert all(store.dataset_rows(con, "trades", day + timedelta(days=k)).rows == [] for k in (-1, 1, 2))
