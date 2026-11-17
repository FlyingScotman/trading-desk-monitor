"""The OCP of a past date with what each figure consists of (TDM#767, ``api.ocp_day``, ``/api/v1/history/ocp/day``): the
date's saved run answers, its terms sum to the figure and equal the history's chart figure of the date, and what an old
run did not save is named. Synthetic books and invented numbers only."""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from desk import store
from desk.api import create_app
from desk.api.ocp_day import FULL, NONE, PARTIAL, line_breakdown
from desk.rules import ocp as O

B = date(2026, 10, 15)
BOOKS = {"D1": {"book_id": "D1", "book_type": "DERIVS", "entity": "ENT1", "ocp_to": None},
         "T1": {"book_id": "T1", "book_type": "TRADING", "entity": "ENT1", "ocp_to": None},
         "Z1": {"book_id": "Z1", "book_type": "DERIVS", "entity": "ENT2", "ocp_to": None}}
POLICY = O.OcpPolicy(report_components=frozenset(set(O.COMPONENTS) - {"options", "ndf"}))
FILE = {"file_name": "ocp_20261015.csv", "inbox_file_sha256": "f15"}
UNITS = [{"scope": {"entity": "ENT1", "book_id": "D1"}, "set_id": 11, "valid_date": B, "row_count": 2, **FILE},
         {"scope": {"entity": "ENT1", "book_id": "T1"}, "set_id": 12, "valid_date": B, "row_count": 1, **FILE}]
REPORT = [
    {"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "100000",
     "derivatives_ccy": {"fx_forward": "500000", "fx_future": "0"}, "_set_id": 11, "_src": "L2"},
    {"entity": "ENT1", "book_id": "T1", "ccy": "USD", "ocp_ccy": "1010000",
     "derivatives_ccy": {"fx_forward": "0", "fx_future": "0"}, "_set_id": 12, "_src": "L4"}]


def core_row(pid, book, delta, product="fx_forward"):
    return {"position_id": pid, "book_id": book, "instr_type": "FX", "fx_delta": delta,
            "extra": {"core": {"product": product}, "ocp_amount": delta, "legs": [{"value_date": "2026-12-16"}]}}


def v3_lines(pv: float) -> list[dict]:
    """The published lines the engine gives for a forward of ``pv`` (block v3), as the store keeps them (JSON)."""
    rows = [core_row("D1|FWD", "D1", {"USD": pv}), core_row("D1|OPT", "D1", {"USD": 40_000.0}, "fx_option_vanilla"),
            core_row("Z1|FWD", "Z1", {"USD": 70_000.0})]
    r = O.compute_ocp(rows, BOOKS, B, report_rows=REPORT, report_units=UNITS, policy=POLICY)
    return json.loads(json.dumps(r.published, default=str))


Z1_STORED = [{"key": "Z1|FWD", "position_id": "Z1|FWD", "book_id": "Z1", "entity": "ENT2", "ccy": "USD",
              "component": "fx_legs", "basis": "spot_equivalent", "amount": 70_000.0, "pv": None, "note": ""}]
"""The run's own lines of the book published from positions (what ``ocp_origin`` reads its items from)."""


def v3_block(pv: float, *, lines=None, origins=None) -> dict:
    by_book = lines if lines is not None else v3_lines(pv)
    return {"version": 3, "status": "computed", "text": "synthetic", "published": {
        "basis": "report_base_plus_day", "by_ccy": {}, "by_book": by_book},
        "parts": [{"key": "base", "name": "база: отчёт ОВП без деривативов"}],
        "lines": Z1_STORED if origins is None else origins}


def v2_block() -> dict:
    return {"version": 2, "status": "computed", "text": "synthetic", "lines": [], "published": {
        "basis": "report_plus_outside", "by_ccy": {}, "by_book": [
            {"entity": "ENT1", "book_id": "D1", "books": ["D1"], "ccy": "USD", "amount": 640_000.0, "source": "report",
             "components": ["options"], "outside": {"options": 40_000.0}, "reported": 600_000.0,
             "valid_date": "2026-10-12"}]}}


def insert(con, day, *, cut="eod_final", run_type="EOD", ocp=None, status="complete"):
    rid = con.execute(
        "INSERT INTO run(run_type,business_date,prev_date,cut_type,market_snapshot_id,static_snapshot_id,"
        "private_inputs_hash,position_set_ids,positions_known_at,core_version,backend_version,lineage)"
        " VALUES (%s,%s,%s,%s,'synthetic-market','synthetic-static','synthetic','{}',now(),'test','test','{}')"
        " RETURNING run_id", (run_type, day, date.fromisoformat(day) - timedelta(days=1), cut)).fetchone()[0]
    if ocp is not None:
        con.execute("INSERT INTO run_reference VALUES (%s,'ocp',%s)", (rid, Jsonb(ocp)))
    con.execute("INSERT INTO run_reference VALUES (%s,'fx',%s)", (rid, Jsonb([
        {"ccy": "USD", "rate": 80.0, "rate_source": "moex_1900", "rate_cbr": 79.5}])))
    con.execute("INSERT INTO run_reference VALUES (%s,'books',%s)", (rid, Jsonb([
        {"book_id": b, "name": "Book " + b, "entity": m["entity"], "desk": "FX"} for b, m in BOOKS.items()])))
    con.execute("UPDATE run SET status=%s, completed_at=now() WHERE run_id=%s", (status, rid))
    return rid


def stripped(lines: list[dict], part: str) -> list[dict]:
    """The lines of a run that saved the parts but not the contributions of ``part`` (an older v3 run)."""
    out = json.loads(json.dumps(lines))
    for x in out:
        (x.get("contributions") or {}).pop(part, None)
    return out


@pytest.fixture
def seeded(store_url):
    ids = {}
    with store.connect(store_url) as con:
        ids["d1"] = insert(con, "2026-10-12", ocp=v3_block(490_000.0))
        insert(con, "2026-10-13", cut="eod_preliminary", ocp=v3_block(9e9))  # replaced by the final close below
        ids["d2"] = insert(con, "2026-10-13", ocp=v3_block(380_000.0))
        ids["old_contrib"] = insert(con, "2026-10-14", ocp=v3_block(0, lines=stripped(v3_lines(250_000.0), "derivatives")))
        ids["v2"] = insert(con, "2026-10-15", ocp=v2_block())
        ids["v1"] = insert(con, "2026-10-16", ocp={"version": 1, "status": "computed", "text": "v1"})
        ids["before_b2"] = insert(con, "2026-10-19")
        ids["failed"] = insert(con, "2026-10-20", ocp={"version": 3, "status": "failed", "text": "boom", "lines": []})
        ids["prelim"] = insert(con, "2026-10-21", cut="eod_preliminary", ocp=v3_block(300_000.0))  # no final close
        ids["anchor"] = insert(con, "2026-10-22", ocp=v3_block(510_000.0))
    return TestClient(create_app(store_url)), ids


def day(client, on, run_id=None, **kw):
    return client.get("/api/v1/history/ocp/day", params={"date": on, "run_id": run_id, **kw})


def test_a_past_date_answers_from_the_run_of_that_date(seeded):
    client, ids = seeded
    r = day(client, "2026-10-13", ids["anchor"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    d = r.json()
    # the date's default run (the final close, not the preliminary one), not the page's run
    assert d["date"] == "2026-10-13" and d["runId"] == ids["d2"] and d["anchorRunId"] == ids["anchor"]
    assert d["cutType"] == "eod_final" and d["runType"] == "EOD"
    assert d["asOf"] == "2026-10-22" and d["basis"] == "report_base_plus_day" and d["state"] == "computed"
    by = {(x["book"], x["ccy"]): x for x in d["lines"]}
    usd = by[("D1", "USD")]
    # the figure and its terms are that run's: forward 380k + base 100k + options outside 40k
    assert usd["amount"] == 520_000 and usd["baseKind"] == "without_derivatives" and usd["setId"] == 11
    rows = {x["key"]: x for x in usd["breakdown"]["rows"]}
    assert rows["derivatives"]["amount"] == 380_000 and rows["base"]["amount"] == 100_000
    assert rows["base"]["setId"] == 11 and rows["base"]["date"] == "2026-10-15"
    assert rows["base"]["file"] == "ocp_20261015.csv"
    assert usd["breakdown"]["status"] == FULL and usd["breakdown"]["absent"] == []
    # the other date answers its own run
    other = day(client, "2026-10-12", ids["anchor"]).json()
    assert other["runId"] == ids["d1"]
    assert {(x["book"], x["ccy"]): x for x in other["lines"]}[("D1", "USD")]["amount"] == 630_000


def test_the_anchor_on_its_own_date_and_as_of(seeded):
    client, ids = seeded
    d = day(client, "2026-10-22", ids["anchor"]).json()
    assert d["runId"] == ids["anchor"] and d["lines"]
    by_as_of = client.get("/api/v1/history/ocp/day", params={"date": "2026-10-12", "as_of": "2026-10-22"}).json()
    assert by_as_of["runId"] == ids["d1"] and by_as_of["anchorRunId"] == ids["anchor"]


def test_terms_sum_to_the_figure_and_the_figure_is_the_charts(seeded):
    """Every line of every date: its rows sum to its amount, and the amount is the history's (the chart's) own."""
    client, ids = seeded
    hist = client.get("/api/v1/history/ocp", params={"run_id": ids["anchor"], "from": "2026-10-01"}).json()
    seen = 0
    for h in hist["days"]:
        d = day(client, h["date"], ids["anchor"]).json()
        assert d["runId"] == h["runId"] and d["state"] == h["state"] and d["basis"] == h["basis"]
        assert [(x["book"], x["ccy"], x["amount"]) for x in d["lines"]] == \
            [(x["book"], x["ccy"], x["amount"]) for x in h["lines"]], h["date"]
        assert d["fx"] == h["fx"] and d["reportFx"] == h["reportFx"] and d["methodNote"] == h["methodNote"]
        for x in d["lines"]:
            b = x["breakdown"]
            assert b["rows"] and b["matches"] is True, (h["date"], x["book"], x["ccy"], b)
            assert math.isclose(sum(r["amount"] for r in b["rows"]), x["amount"], abs_tol=0.01)
            assert math.isclose(b["sum"], x["amount"], abs_tol=0.01)
            seen += 1
        assert d["check"]["sumsMatch"] is True and d["check"]["lines"] == len(d["lines"])
    assert seen >= 8


def test_a_run_that_did_not_save_the_contributions_of_a_part_says_so(seeded):
    client, ids = seeded
    d = day(client, "2026-10-14", ids["anchor"]).json()
    assert d["runId"] == ids["old_contrib"]
    usd = {(x["book"], x["ccy"]): x for x in d["lines"]}[("D1", "USD")]
    b = usd["breakdown"]
    # the figure and its parts stay; the missing contributions are named, not rebuilt
    assert usd["amount"] == 390_000 and b["status"] == PARTIAL and b["matches"] is True
    assert {r["key"]: r["amount"] for r in b["rows"]}["derivatives"] == 250_000
    assert [(a["code"], a["part"]) for a in b["absent"]] == [("no_contributions", "derivatives")]
    assert "прогон не сохранил" in b["absent"][0]["reason"]
    # the lines whose derivatives part is zero are complete
    clean = {(x["book"], x["ccy"]): x for x in d["lines"]}[("T1", "USD")]
    assert clean["breakdown"]["status"] == FULL
    assert d["check"]["partial"] >= 1 and d["check"]["sumsMatch"] is True


def test_a_run_of_the_old_methodology_names_what_it_lacks(seeded):
    client, ids = seeded
    d = day(client, "2026-10-15", ids["anchor"]).json()
    assert d["runId"] == ids["v2"] and d["basis"] == "report_plus_outside"
    assert d["methodNote"].startswith("другая методика")
    x = d["lines"][0]
    b = x["breakdown"]
    # the terms the run has (the report whole, outside the report) sum to the figure; the new ones are named absent
    assert [r["key"] for r in b["rows"]] == ["report", "outside:options"] and b["matches"] is True
    assert b["status"] == PARTIAL and [a["code"] for a in b["absent"]] == ["old_methodology"]
    assert "прежней методике" in b["absent"][0]["reason"]
    assert "baseKind" not in x and "parts" not in x


def test_a_date_without_the_published_ocp_has_no_lines_and_the_reason(seeded):
    client, ids = seeded
    for on, rid, code in (("2026-10-16", ids["v1"], "no_published_ocp"), ("2026-10-19", ids["before_b2"], "not_computed"),
                          ("2026-10-20", ids["failed"], "failed")):
        d = day(client, on, ids["anchor"]).json()
        assert d["runId"] == rid and d["lines"] == [], on
        assert d["absent"]["code"] == code and d["absent"]["reason"], on
    assert day(client, "2026-10-12", ids["anchor"]).json()["absent"] is None


def test_derived_line_items_from_the_runs_own_lines(store_url):
    """A line published from positions: its items are those the run saved (nothing recomputed); without them the
    absence is named."""
    derived = {"entity": "ENT2", "book_id": "Z1", "books": ["Z1"], "ccy": "EUR", "amount": -2e6, "source": "derived",
               "components": [], "outside": {}, "reported": None, "valid_date": None}
    stored = [{"key": "Z1|P1", "position_id": "Z1|P1", "book_id": "Z1", "entity": "ENT2", "ccy": "EUR",
               "component": "fx_legs", "basis": "spot_equivalent", "amount": -1.5e6, "pv": None, "note": ""},
              {"key": "Z1|P2", "position_id": "Z1|P2", "book_id": "Z1", "entity": "ENT2", "ccy": "EUR",
               "component": "fx_legs", "basis": "spot_equivalent", "amount": -0.5e6, "pv": None, "note": ""}]
    with store.connect(store_url) as con:
        ok = insert(con, "2026-10-12", ocp=v3_block(0, lines=[derived], origins=stored))
        bad = insert(con, "2026-10-13", ocp=v3_block(0, lines=[derived], origins=[]))
        mismatch = insert(con, "2026-10-14", ocp=v3_block(0, lines=[{**derived, "amount": -9e6}], origins=stored))
        anchor = insert(con, "2026-10-15", ocp=v3_block(0, lines=[], origins=[]))
    client = TestClient(create_app(store_url))
    b = day(client, "2026-10-12", anchor).json()["lines"][0]["breakdown"]
    assert b["status"] == FULL and b["matches"] is True and b["sum"] == -2e6
    assert [(r["key"], r["amount"], r["items"]) for r in b["rows"]] == [("position", -2e6, 2)]
    b = day(client, "2026-10-13", anchor).json()["lines"][0]["breakdown"]
    assert b["status"] == NONE and b["rows"] == [] and b["absent"][0]["code"] == "no_origin"
    b = day(client, "2026-10-14", anchor).json()["lines"][0]["breakdown"]
    assert b["status"] == PARTIAL and {a["code"] for a in b["absent"]} == {"origin_mismatch", "sum_mismatch"}
    assert ok and bad and mismatch


def test_a_date_without_a_run_and_a_date_after_the_run(seeded):
    client, ids = seeded
    r = day(client, "2026-10-17", ids["anchor"])  # a Saturday: no run
    assert r.status_code == 404 and r.json()["details"]["reason"] == "no_run_on_date"
    assert day(client, "2026-10-30", ids["anchor"]).status_code == 422  # after the run's date
    assert client.get("/api/v1/history/ocp/day", params={"run_id": ids["anchor"]}).status_code == 422  # no date
    assert day(client, "2026-10-12", 999999).status_code == 404


def test_breakdown_never_calls_a_sum_that_does_not_match_complete():
    x = {"source": "report", "amount": 100.0, "parts": {"base": 60.0, "derivatives": 30.0},
         "contributions": {"base": {"items": [{"kind": "report", "amount": 60.0}], "rest": None},
                           "derivatives": {"items": [{"kind": "position", "amount": 25.0}],
                                           "rest": {"count": 2, "amount": 1.0}}}}
    b = line_breakdown(x)
    assert b["status"] == PARTIAL and b["matches"] is False and b["sum"] == 90.0
    assert {a["code"] for a in b["absent"]} == {"contributions_mismatch", "sum_mismatch"}
    # a line without any term source
    assert line_breakdown({"source": "?", "amount": 1.0})["status"] == NONE
    # the engine's own lines are complete
    for y in v3_lines(123_000.0):
        if y["source"] == O.REPORT:
            assert line_breakdown(y)["status"] == FULL, y


def test_the_day_says_which_cut_its_run_is(seeded, store_url):
    """A figure is labelled by its run's cut: a date with only the preliminary close, the page's own intraday run on
    its date, a final close."""
    client, ids = seeded
    assert day(client, "2026-10-21", ids["anchor"]).json()["cutType"] == "eod_preliminary"
    assert day(client, "2026-10-22", ids["anchor"]).json()["cutType"] == "eod_final"
    with store.connect(store_url) as con:
        intraday = insert(con, "2026-10-23", cut="intraday", run_type="INTRADAY", ocp=v3_block(400_000.0))
    d = day(client, "2026-10-23", intraday).json()
    assert d["runId"] == intraday and d["cutType"] == "intraday" and d["runType"] == "INTRADAY"
    # the history carries the same cut per day
    hist = client.get("/api/v1/history/ocp", params={"run_id": intraday, "from": "2026-10-20"}).json()
    assert {x["date"]: x["cutType"] for x in hist["days"]}["2026-10-21"] == "eod_preliminary"
    assert hist["days"][-1]["cutType"] == "intraday"


def test_derived_kinds_are_totals_of_all_items_not_of_the_largest_listed(store_url):
    """More items than ``MAX_ITEMS``: the «position» row is every position, the cash row its balance; the cash
    accounts are not read for the day."""
    n = 45
    stored = [{"key": f"Z1|P{i:02d}", "position_id": f"Z1|P{i:02d}", "book_id": "Z1", "entity": "ENT2", "ccy": "EUR",
               "component": "fx_legs", "basis": "spot_equivalent", "amount": 1000.0 + i, "pv": None, "note": ""}
              for i in range(n)]
    stored.append({"key": "CASH|Z1|EUR|ACC1", "position_id": None, "book_id": "Z1", "entity": "ENT2", "ccy": "EUR",
                   "component": "cash", "basis": "balance", "amount": 500.0, "pv": None, "note": ""})
    total = sum(1000.0 + i for i in range(n)) + 500.0
    derived = {"entity": "ENT2", "book_id": "Z1", "books": ["Z1"], "ccy": "EUR", "amount": total, "source": "derived",
               "components": [], "outside": {}, "reported": None, "valid_date": None}
    with store.connect(store_url) as con:
        rid = insert(con, "2026-10-12", ocp=v3_block(0, lines=[derived], origins=stored))
        anchor = insert(con, "2026-10-13", ocp=v3_block(0, lines=[], origins=[]))
    client = TestClient(create_app(store_url))
    b = day(client, "2026-10-12", anchor).json()["lines"][0]["breakdown"]
    rows = {r["key"]: r for r in b["rows"]}
    assert rows["position"]["items"] == n and rows["position"]["amount"] == sum(1000.0 + i for i in range(n))
    assert rows["cash"]["items"] == 1 and rows["cash"]["amount"] == 500.0
    assert b["status"] == FULL and b["matches"] is True and rid
