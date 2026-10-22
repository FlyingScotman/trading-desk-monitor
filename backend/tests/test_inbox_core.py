"""desk.inbox core (S7a part A): contract schemas, validator, loader and runner, time semantics, reference
adapter, conformance kit. No store: everything here is standard library."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from desk.inbox import conformance, reference, runner
from desk.inbox import schema as schema_module
from desk.inbox.loader import check_describe, fingerprint, load_adapter
from desk.inbox.schema import schema_fields, validate_row
from desk.inbox.spi import AdapterFailure
from desk.inbox.static_index import (
    CalendarUnavailable,
    Instruments,
    build_index,
    business_calendar,
    covering,
    static_snapshots,
)
from desk.inbox.timesem import report_date_suspicious, revision_key, valid_date
from desk.inbox.validate import check_read, check_reference, unit_hash

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / "fixtures"
PRIV, CONFIG, SNAPS, ADAPTERS = FIX / "private" / "v1", FIX / "private" / "v1" / "config", FIX / "mdhub_v1", FIX / "adapters"
STATIC_ID = "4f173275220b492c558acad982351bac01fe73baea6d50dcb0fcc38400953c4a"
POS = {"source": "positions_csv", "dataset": "positions", "time_basis": "start_of_day",
       "completeness": "full_snapshot", "scope": ["book_id"], "covers": "listed"}
HIT = {"source": "positions_csv", "report_date": "2026-10-20", "date_from": "name", "report_ts": None, "seq": None}
BOOKS = [{"book_id": b, "name": b, "desk": "Rates", "book_type": "TRADING", "entity": "ENT1", "active": a}
         for b, a in (("B1", True), ("B2", True), ("B3", True), ("B9", False))]


def bond(book: str, sec: str, qty="100", **kw) -> dict:
    return {"book_id": book, "position_id": f"{book}|{sec}", "asset_class": "bond", "source_instrument_id": sec,
            "ccy": "RUB", "quantity": qty, "_src": "R1", **kw}


def read(rows, scopes=None, **kw) -> dict:
    return {"rows": rows, "scopes": scopes if scopes is not None else [{"book_id": b} for b in
            dict.fromkeys(r["book_id"] for r in rows)], "issues": [], "controls": {}, "meta": {}, **kw}


# ---- B1: the contract files ------------------------------------------------------------------------------


def _doc_table(heading: str) -> list[tuple[str, bool]]:
    """(field, required) from the 08 §10 table under ``heading``."""
    text = (REPO / "docs/architecture/08_PRIVATE_ADAPTER_INTERFACE.md").read_text(encoding="utf-8")
    block = text.split(heading, 1)[1].split("\n### ", 1)[0]
    out = []
    for line in block.splitlines():
        if line.startswith("| `"):
            cell = line.split("|")[1]
            out += [(f, "●" in cell) for f in re.findall(r"`([a-z0-9_]+)`", cell)]
    return out


def test_schema_fields_equal_the_08_tables() -> None:
    pos = _doc_table("### `positions`")
    assert schema_fields("positions") == ([f for f, _ in pos], [f for f, r in pos if r])
    text = (REPO / "docs/architecture/08_PRIVATE_ADAPTER_INTERFACE.md").read_text(encoding="utf-8")
    line = text.split("### `books`", 1)[1].split("\n\n")[1].split(": ", 1)[1].split(". ", 1)[0]
    line = re.sub(r"\(`[A-Z][^)]*\)", "", line)  # the book_type enum
    names, required = re.findall(r"`([a-z0-9_]+)`", line), re.findall(r"`([a-z0-9_]+)` ●", line)
    fields, req = schema_fields("books")
    assert sorted(fields) == sorted(names) and sorted(req) == sorted(required)
    for name in ("positions", "books"):  # the CSV templates: header only, BOM, ;
        raw = (REPO / f"contract/private/v1/{name}.csv").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\n") == 1
        assert raw[3:].decode().strip().split(";") == schema_fields(name)[0]


def test_validate_row_normalises_amounts_and_rejects_bad_values() -> None:
    norm, errs, _ = validate_row("positions", bond("B1", "S", qty=0.1, face_amount_ccy="1500.50",
                                                   mv_rub=-3, isin=None, x_raw="R 12", purchase_date="2026-01-31"))
    assert errs == []
    assert norm["quantity"] == "0.1" and norm["face_amount_ccy"] == "1500.5" and norm["mv_rub"] == "-3"
    assert "isin" not in norm and norm["x_raw"] == "R 12"  # null == absent; x_ kept as is
    bad = {"quantity": "12,5", "face_amount_ccy": "1e5", "mv_rub": float("nan"), "amount_ccy": True,
           "isin": "", "value_date": "2026-02-30", "asset_class": "swap", "mystery": 1}
    _, errs, _ = validate_row("positions", {**bond("B1", "S"), **bad})
    got = {(e.code, e.field) for e in errs}
    assert got == {("bad_number", "quantity"), ("bad_number", "face_amount_ccy"), ("bad_number", "mv_rub"),
                   ("bad_number", "amount_ccy"), ("bad_value", "isin"), ("bad_date", "value_date"),
                   ("bad_value", "asset_class"), ("unknown_field", None)}
    _, errs, _ = validate_row("positions", {"_src": "R1"})
    assert {e.field for e in errs if e.code == "missing_required"} == set(schema_fields("positions")[1])
    norm, errs, dropped = validate_row("positions", {**bond("B1", "S"), "new_field": "x"}, allow_newer=True)
    assert errs == [] and dropped == 1 and "new_field" not in norm
    norm, errs, _ = validate_row("books", {**BOOKS[0], "_src": "L2", "active": None})
    assert errs == [] and norm["active"] is True  # default


# ---- static data and time semantics ----------------------------------------------------------------------


def test_instruments_index_and_lookup(tmp_path: Path) -> None:
    doc = build_index(SNAPS, tmp_path / "idx.json")
    assert [s["id"] for s in doc["snapshots"]] == [STATIC_ID]
    inst = Instruments.from_file(tmp_path / "idx.json")
    rec = inst.lookup("isin", "RU000A1038V6", on="2026-10-20")
    assert rec == {"instrument_id": "SU26238RMFS4", "isin": "RU000A1038V6", "regnum": rec["regnum"],
                   "moex_secid": "SU26238RMFS4", "name": rec["name"], "currency": "RUB", "kind": "ofz_pd",
                   "initial_face": "1000"}
    assert inst.lookup("regnum", rec["regnum"])["instrument_id"] == "SU26238RMFS4"
    assert inst.lookup("moex_secid", "RU000A109XR1")["initial_face"] == "1000"
    assert inst.lookup("isin", "RU000ZZZZZZ0") is None and inst.snapshot_id == STATIC_ID
    with pytest.raises(ValueError):
        inst.lookup("cusip", "x")
    assert Instruments(None).lookup("isin", "RU000A1038V6") is None
    refs = static_snapshots(SNAPS)
    assert covering(refs, "2020-01-01").id == covering(refs, None).id == STATIC_ID


def test_business_calendar_follows_ru_production() -> None:
    cal = business_calendar(SNAPS, date(2026, 10, 20))
    assert cal.snapshot_id == STATIC_ID and cal.version.startswith("RU_PRODUCTION@")
    assert cal.prev_business_day(date(2026, 10, 19)) == date(2026, 10, 16)
    assert cal.prev_business_day(date(2026, 11, 5)) == date(2026, 11, 3)    # 4.11 holiday
    assert not cal.is_business_day(date(2026, 3, 9))                       # transferred holiday, MOEX trades
    assert cal.is_business_day(date(2027, 2, 20))                          # working Saturday
    with pytest.raises(CalendarUnavailable):
        business_calendar(SNAPS, date(2028, 1, 10))
    with pytest.raises(CalendarUnavailable):
        cal.prev_business_day(date(2026, 1, 1))                            # before the range: never guessed


def test_time_semantics() -> None:
    cal = business_calendar(SNAPS, date(2026, 10, 20))
    assert valid_date("2026-10-17", "start_of_day", cal) == date(2026, 10, 16)  # Saturday report
    assert valid_date("2026-10-19", "start_of_day", cal) == date(2026, 10, 16)
    assert valid_date("2026-10-18", "end_of_day", None) == date(2026, 10, 18)    # never shifted
    assert valid_date("2026-10-18", "intraday", None) == date(2026, 10, 18)
    with pytest.raises(CalendarUnavailable):
        valid_date("2026-10-19", "start_of_day", None)
    assert not report_date_suspicious("2026-10-20", "start_of_day", "2026-10-20T06:42:00+03:00")
    assert report_date_suspicious("2026-10-20", "start_of_day", "2026-10-19T22:00:00")       # Moscow, day before
    assert not report_date_suspicious("2026-10-20", "start_of_day", "2026-10-19T22:00:00Z")  # 01:00 MSK
    assert report_date_suspicious("2026-10-17", "end_of_day", "2026-10-16T21:00:00+03:00")
    assert not report_date_suspicious("2026-10-17", "end_of_day", "2026-10-20T07:00:00+03:00")
    assert not report_date_suspicious("2026-10-17", "end_of_day", None)
    original = revision_key("2026-10-16", None, None, None, 2000.0)
    correction = revision_key("2026-10-16", None, 2, None, 1000.0)  # arrived first, still wins
    assert correction > original > revision_key("2026-10-15", None, 9, None, 3000.0)
    assert revision_key("2026-10-16", "2026-10-16T13:00:00", None, None, None) > \
        revision_key("2026-10-16", "2026-10-16T12:00:00+03:00", 5, None, None)


# ---- B2: loader and runner -------------------------------------------------------------------------------


def test_reference_adapter_loads_with_a_fingerprint(tmp_path: Path) -> None:
    h = load_adapter(None, str(CONFIG), None)
    assert h.ok and h.name == "desk_reference" and h.source("positions_csv")["covers"] == "listed"
    assert h.fingerprint == fingerprint(None, str(CONFIG)) != fingerprint(None, None)
    assert load_adapter("desk.inbox.reference", str(CONFIG), h.fingerprint).ok
    bad = load_adapter(None, str(CONFIG), "0" * 64)
    assert bad.error.startswith("fingerprint_mismatch") and bad.fingerprint == h.fingerprint
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "books.csv").write_bytes((CONFIG / "books.csv").read_bytes())
    fp = fingerprint(None, str(cfg))
    (cfg / "books.csv").write_bytes((CONFIG / "books.csv").read_bytes() + b"B4;x;x;HTM;ENT1;;;1;\r\n")
    assert fingerprint(None, str(cfg)) != fp  # new mappings: new fingerprint
    assert load_adapter(None, str(tmp_path / "missing"), None).error.startswith("config_dir_missing")


@pytest.mark.parametrize(("name", "error"), [
    ("import_error", "import_error: ModuleNotFoundError"),
    ("wrong_spi", "unsupported_spi"),
    ("json", "name_collision"),
    ("no_such_adapter", "not_found"),
])
def test_broken_adapters_leave_ingestion_off_with_the_reason(name: str, error: str) -> None:
    h = load_adapter(str(ADAPTERS / name), None, None)
    assert not h.ok and h.error.startswith(error)
    with pytest.raises(AdapterFailure):
        runner.call_read(h, "x", {}, None, 5)
    assert json.dumps({"ok": 1})  # the parent's json was never shadowed


def test_check_describe_rules() -> None:
    d = {"name": "a", "version": "1", "spi": 1, "contract": "private/1.4", "sources": [dict(POS)]}
    assert check_describe(d, 1, True) is None
    assert check_describe(d, 1, False).startswith("missing_reference")
    assert check_describe({**d, "contract": "private/2.0"}, 1, True).startswith("unsupported_contract")
    assert check_describe(d, 2, True).startswith("spi_mismatch")
    for bad in ({"covers": None}, {"scope": ["isin"], "covers": "all_active_books"}, {"dataset": "fx_rates"},
                {"time_basis": "eod"}, {"priority": "1"}, {"datasets": ["positions"]}, {"scope": []}):
        assert check_describe({**d, "sources": [{**POS, **bad}]}, 1, True).startswith("bad_source"), bad
    multi = {"source": "oc", "datasets": ["ocp", "cash_balances"], "time_basis": "start_of_day",
             "completeness": "full_snapshot", "scope": {"ocp": ["entity"], "cash_balances": ["entity", "book_id"]},
             "covers": "listed"}
    assert check_describe({**d, "sources": [multi]}, 1, True) is None


def test_runner_isolates_the_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = load_adapter(str(ADAPTERS / "good_adapter"), str(CONFIG), None)
    assert good.ok and good.name == "good_adapter"
    files = [str(PRIV / "positions_20261014.csv"), str(PRIV / "README.md")]
    hits = runner.call_match(good, files, None, 10)
    assert hits[files[1]] is None and hits[files[0]]["report_date"] == "2026-10-14"

    h = load_adapter(str(ADAPTERS / "match_raises"), None, None)
    out = runner.call_match(h, files, None, 10)
    err = out[files[0]]["_error"]
    assert err["type"] == "ValueError" and err["kind"] == "failed" and err["frames"][-1][0] == "__init__.py"
    assert out[files[1]] is None and "4242" not in json.dumps(out)  # the message never leaves the child

    h = load_adapter(str(ADAPTERS / "read_hangs"), None, None)
    with pytest.raises(AdapterFailure) as e:
        runner.call_read(h, files[0], {}, None, 1)
    assert e.value.kind == "timed_out"

    spy = tmp_path / "spy"
    spy.mkdir()
    (spy / "__init__.py").write_text(
        "import os, sys\nSPI_VERSION = 1\n"
        "def describe():\n    return {}\n"
        "def match(path, ctx):\n    print('noise on stdout')\n"
        "    return {'env': sorted(os.environ), 'path': sys.path, 'spi': ctx.spi, 'cfg': ctx.config_dir,\n"
        "            'lookup': ctx.instruments.lookup('isin', 'RU000A1038V6'), 'bad': float('nan') if path == 'nan' else 0}\n"
        "def read(path, hit, ctx):\n    return {'when': __import__('datetime').date.today()}\n", encoding="utf-8")
    handle = load_adapter(str(spy), None, None)  # describe() {} fails the declaration check
    assert handle.error.startswith("bad_describe")
    handle = type(handle)(str(spy), "spy", "0", handle.fingerprint, {}, None, "/cfg")
    monkeypatch.setenv("DESK_STORE_URL_FOR_TEST", "postgresql://secret")
    monkeypatch.setenv("DESK_PRIVATE_ADAPTER_X", "1")
    hit = runner.call_match(handle, ["a", "nan"], None, 10)
    env = set(hit["a"]["env"])
    assert "DESK_PRIVATE_ADAPTER_X" in env and not any(k.startswith(("DESK_STORE", "DESK_HUB")) for k in env)
    assert env <= {"DESK_PRIVATE_ADAPTER_X", "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "__CF_USER_TEXT_ENCODING",
                   "LC_CTYPE"}
    assert hit["a"]["spi"] == 1 and hit["a"]["cfg"] == "/cfg" and hit["a"]["lookup"] is None  # stub ctx
    assert hit["nan"]["_error"]["type"] == "ValueError"  # NaN is not plain data
    with pytest.raises(AdapterFailure) as e:
        runner.call_read(handle, "a", {}, None, 10)  # a date object is not plain data either
    assert e.value.kind == "failed" and e.value.type == "TypeError"


def test_child_env_is_stripped() -> None:
    env = {"PATH": "/bin", "DESK_STORE_URL": "postgresql://u:p@h/d", "DESK_HUB_TOKEN": "t", "HOME": "/h",
           "DESK_PRIVATE_ADAPTER": "x", "DESK_PRIVATE_ADAPTER_CONFIG": "c", "SystemRoot": "C:\\W", "PYTHONPATH": "/p"}
    assert runner.child_env(env) == {"PATH": "/bin", "DESK_PRIVATE_ADAPTER": "x",
                                     "DESK_PRIVATE_ADAPTER_CONFIG": "c", "SystemRoot": "C:\\W"}


# ---- B3: the reference adapter ---------------------------------------------------------------------------


def test_reference_adapter_reads_canonical_csv(tmp_path: Path) -> None:
    h = load_adapter(None, str(CONFIG), None)
    books, issues = check_reference(runner.call_reference(h, None, 10))
    assert issues == [] and [(b["book_id"], b["active"]) for b in books] == \
        [("B1", True), ("B2", True), ("B3", True), ("B9", False)]
    files = sorted(str(p) for p in PRIV.glob("*.csv"))
    hits = runner.call_match(h, files + [str(CONFIG / "books.csv")], None, 10)
    assert hits[str(CONFIG / "books.csv")] is None
    assert hits[str(PRIV / "positions_20261016_2.csv")]["seq"] == 2
    idx = tmp_path / "idx.json"
    build_index(SNAPS, idx)
    raw = runner.call_read(h, files[0], hits[files[0]], idx, 10)
    by_pos = {r["position_id"]: r for r in raw["rows"]}
    assert by_pos["B1|SEC-26238"]["instrument_id"] == "SU26238RMFS4" and by_pos["B1|SEC-26238"]["_src"] == "L2"
    assert "instrument_id" not in by_pos["B3|SEC-UNMAPPED"] and by_pos["B1|USD"]["x_desk_note"] == "spot"
    c = check_read(POS, hits[files[0]], raw, books=books)
    assert c.status == "accepted" and [u["scope"]["book_id"] for u in c.units] == ["B1", "B2", "B3"]

    marker = str(PRIV / "positions_20261016.csv")
    c = check_read(POS, hits[marker], runner.call_read(h, marker, hits[marker], None, 10), books=books)
    assert {u["scope"]["book_id"]: len(u["rows"]) for u in c.units} == {"B1": 3, "B2": 2, "B3": 0}

    bad = str(PRIV / "positions_20261015.csv")
    c = check_read(POS, hits[bad], runner.call_read(h, bad, hits[bad], None, 10), books=books)
    assert c.status == "partially_accepted"
    assert {u["scope"]["book_id"]: u["status"] for u in c.units} == {"B1": "accepted", "B2": "rejected"}
    assert [(i["code"], i["scope"], i["src"], i["count"]) for i in c.issues] == \
        [("bad_number", {"book_id": "B2"}, "L5", 1)]

    empty = tmp_path / "positions_20261020.csv"
    empty.write_bytes((REPO / "contract/private/v1/positions.csv").read_bytes())
    hit = reference.match(str(empty), None)
    c = check_read(POS, hit, runner.call_read(h, str(empty), hit, None, 10), books=books)
    assert c.units == [] and [i["code"] for i in c.issues] == ["empty_report"]
    assert reference.match("positions_20261340.csv", None) is None
    assert runner.call_reference(load_adapter(None, None, None), None, 10) == {"books": []}


# ---- B6 part: check_read -----------------------------------------------------------------------------------


def test_check_read_units_and_file_errors() -> None:
    ok = read([bond("B1", "S1"), bond("B2", "S2")])
    c = check_read(POS, HIT, ok, books=BOOKS)
    assert c.status == "accepted" and c.controls_status == "no_controls" and c.file_error is None

    def file_error(result, decl=POS, hit=HIT, books=BOOKS):
        return check_read(decl, hit, result, books=books).file_error

    assert file_error({**ok, "scopes": None}) == "missing_scopes"
    assert file_error(read([bond("B1", "S1")], scopes=[{"book_id": "B2"}])) == "row_outside_scopes"
    assert file_error(read([{**bond("B1", "S1"), "_page": "Sheet1"}])) == "reserved_key"
    assert file_error(read([{**bond("B1", "S1"), "_report_date": "2026-10-19"}])) == "bad_row"
    assert file_error(read([], scopes=[{"book_id": "B1", "entity": "E"}])) == "bad_scopes"
    assert file_error({**ok, "extra": 1}) == "bad_result_shape"
    assert file_error({**ok, "issues": [{"severity": "error", "code": "Bad Code"}]}) == "bad_issue"
    assert file_error(ok, hit={**HIT, "report_date": "20.10.2026"}) == "bad_hit"
    assert file_error(ok, decl={**POS, "dataset": "trades"}) == "dataset_not_supported"
    assert file_error({**ok, "issues": [{"severity": "error", "code": "unexpected_layout"}]}) == "unexpected_layout"
    increment = {**POS, "completeness": "increment", "covers": None}
    assert check_read(increment, HIT, {"rows": [bond("B1", "S1")]}, books=BOOKS).status == "accepted"


def test_check_read_unit_scoped_errors() -> None:
    rows = [bond("B1", "S1"), bond("B1", "S1"),                                   # duplicate key
            {**bond("B2", "S2"), "quantity": None},                               # bond without a size
            {**bond("B3", "USD"), "asset_class": "fx"},                           # fx without amount_ccy
            bond("B7", "S7"), bond("B9", "S9", face_amount_ccy="100000")]          # unknown book; inactive book is known
    c = check_read(POS, HIT, read(rows, issues=[{"severity": "warning", "code": "unmapped_instrument",
                                                 "scope": {"book_id": "B9"}, "src": "R9"}]), books=BOOKS)
    status = {u["scope"]["book_id"]: u["status"] for u in c.units}
    assert status == {"B1": "rejected", "B2": "rejected", "B3": "rejected", "B7": "rejected", "B9": "accepted"}
    assert c.status == "partially_accepted"
    assert {(i["code"], i["scope"]["book_id"]) for i in c.issues if i["severity"] == "error"} == \
        {("duplicate_key", "B1"), ("missing_required", "B2"), ("missing_required", "B3"), ("unknown_book", "B7")}
    assert all(u["rows"] == [] for u in c.units if u["status"] == "rejected")
    adapter = read([bond("B1", "S1"), bond("B2", "S2")],
                   issues=[{"severity": "error", "code": "unknown_book", "scope": {"book_id": "B2"}, "message": "m"}])
    c = check_read(POS, HIT, adapter, books=None)  # no reference: no book checks
    assert [u["status"] for u in c.units] == ["accepted", "rejected"]
    assert c.issues[0]["origin"] == "adapter"


def test_all_active_books_closes_missing_books() -> None:
    decl = {**POS, "covers": "all_active_books"}
    c = check_read(decl, HIT, read([bond("B1", "S1"), {**bond("B2", "S2"), "quantity": "x"}]), books=BOOKS)
    status = {u["scope"]["book_id"]: (u["status"], len(u["rows"])) for u in c.units}
    assert status == {"B1": ("accepted", 1), "B2": ("rejected", 0), "B3": ("accepted", 0)}  # B9 inactive
    assert [(i["code"], i["scope"]) for i in c.issues if i["severity"] == "warning"] == \
        [("book_closed_by_omission", {"book_id": "B3"})]
    assert {u["scope"]["book_id"] for u in check_read(decl, HIT, read([bond("B1", "S1")]), books=None).units} == {"B1"}


def test_controls_are_resummed_with_tolerance() -> None:
    rows = [bond("B1", "S1", face_amount_ccy="1000000000.004"), bond("B1", "S2", face_amount_ccy=0.5)]

    def controls(c):
        return check_read(POS, HIT, read(rows, controls={"positions": c}), books=BOOKS)
    c = controls({"row_count": 2, "sums": {"face_amount_ccy": "1000000000.5"}})
    assert c.controls_status == "ok" and c.status == "accepted"
    assert controls({"sums": {"face_amount_ccy": "1000000010.4"}}).controls_status == "ok"  # within 1e-8 x total
    c = controls({"sums": {"face_amount_ccy": "1000000011"}})
    assert c.file_error == "control_mismatch" and c.controls_status == "mismatch" and c.status == "rejected"
    assert controls({"row_count": 3}).file_error == "control_mismatch"
    assert controls({"sums": {"book_id": "1"}}).file_error == "bad_controls"
    assert check_read(POS, HIT, read(rows, controls={"trades": {}}), books=BOOKS).file_error == "bad_controls"


def test_newer_minor_fields_are_dropped_with_a_warning() -> None:
    result = read([{**bond("B1", "S1"), "future_field": "1"}])
    assert check_read(POS, HIT, result, books=BOOKS).status == "rejected"  # unknown in 1.3
    c = check_read(POS, HIT, result, books=BOOKS, contract="private/1.4")
    assert c.status == "accepted" and [i["code"] for i in c.issues] == ["field_from_newer_contract"]
    assert "future_field" not in c.units[0]["rows"][0]


def test_contract_1_1_fields_accepted_and_1_0_adapters_too() -> None:
    """1.1 adds maturity_date, risk_dv01_rub, risk_cs01_rub: kept without a warning; a 1.0 adapter is accepted."""
    otc = {"_src": "R9", "book_id": "B1", "position_id": "B1|IRS-1", "asset_class": "otc",
           "source_instrument_id": "IRS-1", "ccy": "RUB", "maturity_date": "2031-01-10", "x_kind": "IRS"}
    rows = [{**bond("B1", "S1"), "maturity_date": "2030-06-01"}, otc,
            {**otc, "position_id": "B1|IRS-2", "mv_rub": "1.5", "risk_dv01_rub": "-2", "risk_cs01_rub": "0.5"}]
    for contract in ("private/1.1", "private/1.0"):
        c = check_read(POS, HIT, read(rows), books=BOOKS, contract=contract)
        assert c.status == "accepted" and c.issues == [], (contract, c.issues)
        got = {r["position_id"]: r for r in c.units[0]["rows"]}
        assert got["B1|IRS-1"]["maturity_date"] == "2031-01-10" and got["B1|IRS-1"]["x_kind"] == "IRS"
        assert got["B1|IRS-2"]["risk_dv01_rub"] == "-2"
    d = {"name": "a", "version": "1", "spi": 1, "sources": [dict(POS)]}
    assert check_describe({**d, "contract": "private/1.1"}, 1, True) is None
    assert check_describe({**d, "contract": "private/1.0"}, 1, True) is None


def test_unit_hash_ignores_src_x_and_order() -> None:
    a = validate_row("positions", bond("B1", "S1", qty="10.0", x_note="a"))[0]
    b = validate_row("positions", {**bond("B1", "S1", qty=10), "_src": "R99", "x_note": "b"})[0]
    c = validate_row("positions", bond("B1", "S2"))[0]
    assert unit_hash([a, c]) == unit_hash([c, b])
    assert unit_hash([a]) != unit_hash([validate_row("positions", bond("B1", "S1", qty="11"))[0]])


def test_check_reference_drops_bad_and_repeated_books() -> None:
    raw = {"books": [{**BOOKS[0], "_src": "L2"}, {**BOOKS[1], "_src": "L3", "book_type": "BANKING"},
                     {**BOOKS[2], "_src": "L4"}, {**BOOKS[2], "_src": "L5", "name": "other"}]}
    books, issues = check_reference(raw)
    assert [b["book_id"] for b in books] == ["B1"]
    assert sorted(i["code"] for i in issues) == ["bad_value", "duplicate_key"]
    assert check_reference({"rows": []})[0] is None


# ---- B6: the conformance kit -----------------------------------------------------------------------------


def test_kit_passes_the_reference_and_a_path_adapter() -> None:
    rep = conformance.check(None, [str(PRIV)], snapshot_dir=str(SNAPS), config_dir=str(CONFIG))
    assert rep.ok, rep.text()
    g = rep.groups[("positions_csv", "2026-10-15")]
    assert g.valid_date == "2026-10-14" and g.statuses == {"partially_accepted": 1}
    assert g.issues == {("error", "bad_number"): 1}
    assert rep.groups[("positions_csv", "2026-10-17")].valid_date == "2026-10-16"   # Saturday
    assert rep.groups[("positions_csv", "2026-10-14")].unmapped == 1
    assert rep.notes["same_valid_date"] == 1 and rep.instruments == STATIC_ID

    rep = conformance.check(str(ADAPTERS / "good_adapter"), [str(PRIV)], stub_ctx=True, config_dir=str(CONFIG))
    assert rep.ok and rep.instruments == "stub"
    g = rep.groups[("positions_daily", "2026-10-15")]
    assert g.issues[("warning", "book_closed_by_omission")] == 1 and g.valid_date is None
    assert rep.groups[("positions_daily", "2026-10-14")].unmapped == 5  # stub: nothing resolves

    j = rep.to_json()
    text = json.dumps(j) + rep.text()
    assert j["sources"][0]["files"] == "<10" and "positions_2026" not in text and "SEC-" not in text
    assert "B1" not in text and "12500" not in text


def test_kit_fails_broken_adapters(tmp_path: Path) -> None:
    rep = conformance.check(str(ADAPTERS / "json"), [str(PRIV)])
    assert not rep.ok and rep.load_error.startswith("name_collision") and "result: FAIL" in rep.text()
    rep = conformance.check(str(ADAPTERS / "match_raises"), [str(PRIV)])
    assert not rep.ok and rep.problems["match_failed"] == 6 and "4242" not in rep.text()
    rep = conformance.check(str(ADAPTERS / "read_hangs"), [str(PRIV / "config")], read_timeout_s=1)
    assert not rep.ok and rep.problems == {"read_timed_out": 1}

    flaky = tmp_path / "flaky"
    flaky.mkdir()
    src = (ADAPTERS / "good_adapter" / "__init__.py").read_text(encoding="utf-8")
    (flaky / "parse.py").write_text((ADAPTERS / "good_adapter" / "parse.py").read_text(encoding="utf-8"))
    (flaky / "__init__.py").write_text(src.replace(
        'rows.append({**rec, "_src": f"R{line}"})',
        'rows.append({**rec, "_src": f"R{line}", "x_t": 1, "quantity": str(__import__("time").time_ns())})'))
    rep = conformance.check(str(flaky), [str(PRIV)], stub_ctx=True, config_dir=str(CONFIG))
    assert rep.problems["nondeterministic"] == 6

    assert conformance.main(["--dir", str(PRIV), "--config", str(CONFIG), "--json"]) == 0
    assert conformance.main(["--adapter", str(ADAPTERS / "wrong_spi"), "--dir", str(PRIV)]) == 1


def test_kit_imports_only_stdlib_and_desk_inbox() -> None:
    src = str(Path(conformance.__file__).resolve().parents[2])
    code = (f"import sys; sys.path[:0] = [{src!r}]\n"
            "import desk.inbox.conformance, desk.inbox.runner, desk.inbox.schema, desk.inbox.validate\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('psycopg', 'rub_fixed_income', 'fastapi',\n"
            "       'pydantic', 'numpy', 'pgserver', 'uvicorn') or (m.startswith('desk.') and\n"
            "       not m.startswith('desk.inbox'))]\n"
            "assert not bad, bad\n")
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


# ---- review fixes (S7a) ----------------------------------------------------------------------------------


def test_adapter_error_for_an_unlisted_scope_rejects_that_unit() -> None:
    err = [{"severity": "error", "code": "unexpected_layout", "scope": {"book_id": "B3"}}]
    for covers in ("all_active_books", "listed"):
        c = check_read({**POS, "covers": covers}, HIT, read([bond("B1", "S1")], issues=err), books=BOOKS)
        status = {u["scope"]["book_id"]: u["status"] for u in c.units}
        expect = {"B1": "accepted", "B2": "accepted", "B3": "rejected"} if covers == "all_active_books" \
            else {"B1": "accepted", "B3": "rejected"}
        assert status == expect, covers  # B3 keeps its previous revision, never closed by omission
        closed = [i["scope"]["book_id"] for i in c.issues if i["code"] == "book_closed_by_omission"]
        assert closed == (["B2"] if covers == "all_active_books" else [])
    # a scope that cannot be a unit (empty key, missing scope field) rejects the file, never ignored
    for scope in ({"": "x"}, {"entity": "ENT1"}):
        c = check_read(POS, HIT, read([bond("B1", "S1")], issues=[{**err[0], "scope": scope}]), books=BOOKS)
        assert c.file_error == "unexpected_layout"


def test_schemas_are_package_data_equal_to_the_published_copy() -> None:
    pkg = Path(schema_module.__file__).parent / "schemas" / "v1"
    published = sorted(p.name for p in (REPO / "contract/private/v1").glob("*.schema.json"))
    assert published == sorted(p.name for p in pkg.glob("*.schema.json")) == [
        f"{n}.schema.json" for n in ("books", "cash_balances", "entities", "funding_policy", "funding_rules", "ocp", "pnl_official",
                                     "positions", "trade_terms")]
    for name in published:
        assert (pkg / name).read_bytes() == (REPO / "contract/private/v1" / name).read_bytes(), name
    assert validate_row("trades", {"x": 1}) == ({}, [schema_module.RowError("dataset_not_supported", None)], 0)
    assert schema_module.load_schema(["positions"]) is None and schema_module.load_schema("../x") is None


def test_amounts_and_currency_codes_are_strict_and_normalised() -> None:
    _, errs, _ = validate_row("positions", bond("B1", "S", qty="123\n"))
    assert [(e.code, e.field) for e in errs] == [("bad_number", "quantity")]
    assert validate_row("positions", bond("B1", "S", ccy="rur"))[0]["ccy"] == "RUB"
    assert validate_row("positions", bond("B1", "S", ccy="usd"))[0]["ccy"] == "USD"


def test_unhashable_values_never_crash_the_checks() -> None:
    d = {"name": "a", "version": "1", "spi": 1, "contract": "private/1.0", "sources": [dict(POS)]}
    for bad in ({"time_basis": ["start_of_day"]}, {"dataset": ["positions"]}, {"covers": {"a": 1}},
                {"completeness": []}, {"datasets": [["positions"]], "dataset": None}):
        src = {k: v for k, v in {**POS, **bad}.items() if v is not None}
        assert check_describe({**d, "sources": [src]}, 1, True).startswith("bad_source"), bad
    bad_issue = read([bond("B1", "S1")], issues=[{"severity": ["error"], "code": "x_y"}])
    assert check_read(POS, HIT, bad_issue, books=BOOKS).file_error == "bad_issue"
    rows = [{**bond("B1", "S1"), "position_id": ["B1", "S1"]}, {**bond("B1", "S2"), "position_id": {"a": 1}}]
    c = check_read(POS, HIT, read(rows), books=BOOKS)
    assert c.file_error is None and [u["status"] for u in c.units] == ["rejected"]


def _static(folder: Path, business_date: str, valid_from: str, valid_to: str) -> None:
    import gzip
    import hashlib
    raw = json.dumps({"kind": "static", "business_date": business_date, "data": {"instruments": [], "calendars": [
        {"calendar": "RU_PRODUCTION", "fingerprint": business_date * 2, "valid_from": valid_from,
         "valid_to": valid_to, "holidays": ["2027-01-01", "2027-01-04", "2027-01-05", "2027-01-06", "2027-01-07",
                                            "2027-01-08"], "working_weekends": []}]}}).encode()
    (folder / f"{hashlib.sha256(raw).hexdigest()}.json.gz").write_bytes(gzip.compress(raw))


def test_business_calendar_covers_the_day_before(tmp_path: Path) -> None:
    _static(tmp_path, "2026-12-20", "2026-01-01", "2027-12-31")
    _static(tmp_path, "2027-01-05", "2027-01-01", "2028-12-31")  # newer, but starts on 1 January
    cal = business_calendar(tmp_path, date(2027, 1, 11))
    assert cal.valid_from == date(2026, 1, 1) and valid_date("2027-01-11", "start_of_day", cal) == date(2026, 12, 31)
    assert business_calendar(tmp_path, date(2027, 6, 1)).valid_from == date(2027, 1, 1)  # the newest when it can
    with pytest.raises(CalendarUnavailable):
        business_calendar(tmp_path, date(2028, 6, 1).replace(year=2029))


def _spy(tmp_path: Path, name: str, body: str) -> object:
    d = tmp_path / name
    d.mkdir()
    (d / "__init__.py").write_text("import os, sys, time, subprocess\nSPI_VERSION = 1\n"
                                   "def describe():\n    return {}\n" + body, encoding="utf-8")
    h = load_adapter(str(d), None, None)
    return type(h)(str(d), name, "0", h.fingerprint, {}, None, None)


def test_match_runs_in_chunks_and_fd1_cannot_corrupt_the_reply(tmp_path: Path) -> None:
    h = _spy(tmp_path, "chunky", "def match(path, ctx):\n    os.write(1, b'garbage')\n    return {'pid': os.getpid()}\n")
    paths = [f"p{i}" for i in range(runner.MATCH_CHUNK + 10)]
    out = runner.call_match(h, paths, None, 10)
    assert len(out) == len(paths) and len({v["pid"] for v in out.values()}) == 2


def test_timeout_kills_the_process_tree(tmp_path: Path) -> None:
    import os
    import time
    pidfile = tmp_path / "grandchild.pid"
    h = _spy(tmp_path, "forker", "def match(path, ctx):\n    return None\ndef read(path, hit, ctx):\n"
             "    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
             f"    open({str(pidfile)!r}, 'w').write(str(p.pid))\n    time.sleep(60)\n")
    t0 = time.monotonic()
    with pytest.raises(AdapterFailure) as e:
        runner.call_read(h, "x", {}, None, 2)
    assert e.value.kind == "timed_out" and time.monotonic() - t0 < 2 + runner.KILL_WAIT_S + 2
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the adapter's own child survived the timeout")


def test_module_adapter_is_fingerprinted_from_what_the_child_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = runner.call_describe("desk.inbox.reference", None, 10)
    assert resp["result"]["location"] == {"file": reference.__file__, "path": []}
    fp = fingerprint("desk.inbox.reference", str(CONFIG), resp["result"]["location"])
    # the parent's own resolution is not used: it may see another sys.path than the child
    monkeypatch.setattr("importlib.machinery.PathFinder.find_spec", lambda *a, **k: None)
    h = load_adapter("desk.inbox.reference", str(CONFIG), fp)
    assert h.ok and h.fingerprint == fp


def test_kit_survives_odd_hits_and_reports_names_only(tmp_path: Path) -> None:
    odd = tmp_path / "odd_adapter"
    odd.mkdir()
    (odd / "parse.py").write_text((ADAPTERS / "good_adapter" / "parse.py").read_text(encoding="utf-8"))
    (odd / "__init__.py").write_text((ADAPTERS / "good_adapter" / "__init__.py").read_text(encoding="utf-8")
                                     .replace("    return hit(path)", "    return True if 'README' in path else hit(path)"))
    rep = conformance.check(str(odd), [str(PRIV)], stub_ctx=True, config_dir=str(CONFIG))
    assert rep.problems == {"bad_hit": 1} and rep.to_json()["adapter"] == "odd_adapter"
    assert str(tmp_path) not in json.dumps(rep.to_json())


def test_kit_notional_per_currency_and_quantity_only() -> None:
    rep = conformance.check(None, [str(PRIV)], snapshot_dir=str(SNAPS), config_dir=str(CONFIG))
    g = rep.groups[("positions_csv", "2026-10-14")]
    # 10m + 5m + 20000 bonds x initial face 1000 (by quantity, resolved) + 3m; the unmapped bond has a quantity only
    assert g.notional == {"RUB": 38_000_000} and g.quantity_only == 1 and g.unmapped == 1
    assert rep.to_json()["sources"][0]["unmapped_share_pct"] == {"RUB": 0.0}
    assert "quantity only <10" in rep.text()


def test_reference_says_unreadable_book_lines(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "books.csv").write_bytes((CONFIG / "books.csv").read_bytes() + b"B4;x;x;HTM;ENT1;;;1;;extra\r\n")
    books, issues = check_reference(runner.call_reference(load_adapter(None, str(cfg), None), None, 10))
    assert [b["book_id"] for b in books] == ["B1", "B2", "B3", "B9"]
    assert [(i["code"], i["src"], i["origin"]) for i in issues] == [("unexpected_layout", "L6", "adapter")]
    assert check_reference({"books": [], "issues": [{"severity": "bad"}]})[0] is None
