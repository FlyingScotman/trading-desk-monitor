"""The published OCP by day (TDM#446, ``api.ocp_history``): the saved runs' ``ocp`` blocks, synthetic books only."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from desk import store
from desk.api import create_app
from desk.api.ocp_history import DEFAULT_DAYS, period


def line(book, ccy, amount, source="report", entity="ENT1", books=None, valid=None):
    return {"entity": entity, "book_id": book, "books": books or ([book] if book else []), "ccy": ccy,
            "amount": amount, "source": source, "components": [], "outside": {},
            "reported": amount if source == "report" else None, "valid_date": valid}


def block(status, lines, basis="report_plus_outside", reconciliation=None, max_age=None):
    b = {"version": 2, "status": status, "text": "synthetic", "published": {"basis": basis, "by_ccy": {},
         "by_book": lines}, "lines": [{"position_id": "P" + str(i)} for i in range(50)]}
    if reconciliation is not None:
        b["reconciliation"] = reconciliation
    if max_age is not None:
        b["policy"] = {"report_max_age_days": max_age}
    return b


def rec(ccy, rate, valid="2026-10-12", book="B1", amount=1.0, rub=None):
    """A comparison of the block's reconciliation: the report's figures and rate of the currency (``reported``,
    ``reported_rub``, ``reported_rate``) for a book's scope, or an entity's (``book`` None)."""
    return {"level": "book" if book else "entity", "entity": "ENT1", "book_id": book, "ccy": ccy, "reported": amount,
            "reported_rub": rub, "reported_rate": rate, "valid_date": valid, "components": {}, "break": 0.0,
            "over": False}


def insert(con, day, *, cut="eod_final", run_type="EOD", ocp=None, fx=None, books=None, status="complete"):
    rid = con.execute(
        "INSERT INTO run(run_type,business_date,prev_date,cut_type,market_snapshot_id,static_snapshot_id,"
        "private_inputs_hash,position_set_ids,positions_known_at,core_version,backend_version,lineage)"
        " VALUES (%s,%s,%s,%s,'synthetic-market','synthetic-static','synthetic','{}',now(),'test','test','{}')"
        " RETURNING run_id", (run_type, day, date.fromisoformat(day) - timedelta(days=1), cut)).fetchone()[0]
    if ocp is not None:
        con.execute("INSERT INTO run_reference VALUES (%s,'ocp',%s)", (rid, Jsonb(ocp)))
    con.execute("INSERT INTO run_reference VALUES (%s,'fx',%s)", (rid, Jsonb(fx if fx is not None else [
        {"ccy": "USD", "rate": 80.0, "rate_source": "moex_1900", "rate_cbr": 79.5},
        {"ccy": "CNY", "rate": 11.0, "rate_source": "moex_1900", "rate_cbr": None}])))
    con.execute("INSERT INTO run_reference VALUES (%s,'books',%s)", (rid, Jsonb(books if books is not None else [
        {"book_id": "B1", "name": "Book one", "entity": "ENT1", "desk": "FX"}])))
    con.execute("UPDATE run SET status=%s, completed_at=now() WHERE run_id=%s", (status, rid))
    return rid


@pytest.fixture
def seeded(store_url):
    """Five dates: a v2 block, a preliminary close replaced by the final one, a run before B2, a v1 block, a
    no_report day, and an intraday anchor; an incomplete run on a date of its own."""
    ids = {}
    with store.connect(store_url) as con:
        ids["d1"] = insert(con, "2026-10-12", ocp=block("computed", [
            line("B1", "USD", 1.5e6), line(None, "CNY", 20e6, books=["B2", "B3"]),
            line("B4", "EUR", -2e6, source="derived", entity="ENT2")], reconciliation=[
            rec("USD", 80.25), rec("GBP", 101.5), rec("GBP", 99.0, valid="2026-10-09", book="B2"),
            rec("EUR", 95.0, valid="2026-10-09"), rec("CNY", None, amount=1e6, rub=11.2e6),
            rec("JPY", 55.0, amount=1e6, rub=0.55e6), rec("CHF", 100.0, amount=-1e6, rub=-101e6),
            rec("KZT", None, amount=-1e6, rub=0.18e6), rec("SGD", 60.0, amount=-1e6, rub=60.5e6),
            rec("HKD", 10.5, book=None), rec("HKD", 10.4, book="B3"), rec("HKD", 10.3, book="B2"),
            rec("AED", -22.0, amount=1e6, rub=0.0), rec("RUB", 1.0), rec("TRY", 0.0)]),
            books=[{"book_id": "B1", "name": "Old name", "entity": "ENT1", "desk": "FX"},
                   {"book_id": "B9", "name": "Gone", "entity": "ENT1", "desk": "Credit"}])
        insert(con, "2026-10-13", cut="eod_preliminary", ocp=block("computed", [line("B1", "USD", 9e9)]))
        ids["d2"] = insert(con, "2026-10-13", ocp=block("computed", [line("B1", "USD", 2e6)], basis="derived",
                                                        reconciliation=[rec("GBP", 101.0, valid="2026-10-13")]), fx=[
            {"ccy": "USD", "rate": 81.0, "rate_source": "cbr"}, {"ccy": "CNY", "rate": 11.2, "rate_source": "cbr"}])
        ids["d3"] = insert(con, "2026-10-14")  # before B2: no block
        ids["d4"] = insert(con, "2026-10-15", ocp={"version": 1, "status": "computed", "text": "v1"})
        insert(con, "2026-10-16", ocp=block("computed", [line("B1", "USD", 7e9)]), status="failed")
        ids["d5"] = insert(con, "2026-10-19", ocp=block("no_report", [
            line("B1", "USD", 3e6, source="derived", valid=None)]), fx=[])
        ids["anchor"] = insert(con, "2026-10-20", cut="intraday", run_type="INTRADAY",
                               ocp=block("computed", [line("B1", "USD", 4e6, valid="2026-10-19")], max_age=5,
                                         reconciliation=[rec("EUR", 95.5, valid="2026-10-16"),
                                                         rec("GBP", 100.0, valid="2026-10-14")]))
    return TestClient(create_app(store_url)), ids


def test_days_are_the_saved_published_ocp(seeded):
    client, ids = seeded
    r = client.get("/api/v1/history/ocp", params={"run_id": ids["anchor"], "from": "2026-10-01"})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    d = r.json()
    assert d["runId"] == ids["anchor"] and d["asOf"] == "2026-10-20" and d["from"] == "2026-10-01"
    days = {x["date"]: x for x in d["days"]}
    assert list(days) == ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-19", "2026-10-20"]
    # the default run of a date: the final close, not the preliminary one; a failed run is no day
    assert days["2026-10-13"]["runId"] == ids["d2"] and days["2026-10-13"]["lines"][0]["amount"] == 2e6
    d1 = days["2026-10-12"]
    assert d1["state"] == "computed" and d1["basis"] == "report_plus_outside" and d1["runType"] == "EOD"
    # a report part by entity stays a part: no book, its books as the report gives them, nothing spread
    assert d1["lines"] == [
        {"entity": "ENT1", "book": "B1", "books": ["B1"], "ccy": "USD", "amount": 1.5e6, "source": "report",
         "reported": 1.5e6, "validDate": None},
        {"entity": "ENT1", "book": None, "books": ["B2", "B3"], "ccy": "CNY", "amount": 20e6, "source": "report",
         "reported": 20e6, "validDate": None},
        {"entity": "ENT2", "book": "B4", "books": ["B4"], "ccy": "EUR", "amount": -2e6, "source": "derived",
         "reported": None, "validDate": None}]
    # rates: market and CBR; a currency without its CBR rate: none, never the market one (USD stays at the CBR
    # rate: the equivalent would mix two sources)
    assert d1["fx"] == {"USD": {"market": 80.0, "cbr": 79.5, "source": "moex_1900"},
                        "CNY": {"market": 11.0, "cbr": None, "source": "moex_1900"}}
    # a run with no CBR rate at all (before R26) valued on the CBR rate: its rates for both, as the pages' FX_CBR
    assert days["2026-10-13"]["fx"] == {"USD": {"market": 81.0, "cbr": 81.0, "source": "cbr"},
                                        "CNY": {"market": 11.2, "cbr": 11.2, "source": "cbr"}}
    # the report's own rates, ₽ per unit: its rate_rub_per_unit (USD; CHF, SGD: their scale agrees with
    # |ocp_rub| / |ocp_ccy|, ocp_rub signed or not), without one the implied rate (CNY; KZT, short, ocp_rub unsigned);
    # the newer unit's (GBP); at a tie a book's scope before an entity's, then the first book (HKD). Left out: a unit
    # older than the policy's age (EUR of 09.10 under 0 days), a rate quoted per 100 units (JPY: 55 against 0.55 ₽ by
    # the report's own figures), a negative rate without figures (AED), none, zero, RUB
    assert d1["reportFx"] == {"CHF": 100.0, "CNY": 11.2, "GBP": 101.5, "HKD": 10.3, "KZT": 0.18, "SGD": 60.0,
                              "USD": 80.25}
    # published from positions only: no report rate; a block without them: {}
    assert days["2026-10-13"]["reportFx"] == {} and days["2026-10-14"]["reportFx"] == {}
    # the run's report_max_age_days: a unit 4 days old is published, one 6 days old is not
    assert days["2026-10-20"]["reportFx"] == {"EUR": 95.5}
    # before B2, a v1 block: a day without lines, its state says why
    assert days["2026-10-14"]["state"] == "not_computed" and days["2026-10-14"]["lines"] == []
    assert days["2026-10-15"]["state"] == "computed" and days["2026-10-15"]["basis"] is None
    assert days["2026-10-15"]["lines"] == []
    assert days["2026-10-19"]["state"] == "no_report" and days["2026-10-19"]["fx"] == {}
    assert days["2026-10-19"]["lines"][0]["source"] == "derived"
    # the anchor on its date, though intraday
    assert days["2026-10-20"]["runId"] == ids["anchor"] and days["2026-10-20"]["runType"] == "INTRADAY"
    assert days["2026-10-20"]["lines"][0]["validDate"] == "2026-10-19"
    # the books of the period: the latest run's reference wins, a book gone from it is kept
    assert d["books"]["B1"] == {"name": "Book one", "entity": "ENT1", "desk": "FX"}
    assert d["books"]["B9"]["name"] == "Gone"


def test_period_defaults_and_limits(seeded):
    client, ids = seeded
    d = client.get("/api/v1/history/ocp", params={"run_id": ids["d2"]}).json()
    assert d["to"] == "2026-10-13" and d["from"] == str(date(2026, 10, 13) - timedelta(days=DEFAULT_DAYS))
    assert [x["date"] for x in d["days"]] == ["2026-10-12", "2026-10-13"]
    d = client.get("/api/v1/history/ocp", params={"run_id": ids["anchor"], "from": "2026-10-13",
                                                   "to": "2026-10-15"}).json()
    assert [x["date"] for x in d["days"]] == ["2026-10-13", "2026-10-14", "2026-10-15"]
    # without a run: the newest run shown (the intraday anchor)
    assert client.get("/api/v1/history/ocp").json()["runId"] == ids["anchor"]
    for params in ({"run_id": ids["d2"], "to": "2026-10-14"}, {"run_id": ids["d2"], "from": "2026-10-14"},
                   {"run_id": ids["d2"], "from": "2024-01-01"}):
        r = client.get("/api/v1/history/ocp", params=params)
        assert r.status_code == 422 and r.json()["code"] == "invalid_parameter", params
    r = client.get("/api/v1/history/ocp", params={"run_id": 999999})
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_period_rule():
    anchor = {"business_date": date(2026, 10, 20)}
    assert period(anchor, None, None) == (date(2026, 10, 20) - timedelta(days=DEFAULT_DAYS), date(2026, 10, 20))
    assert period(anchor, date(2026, 10, 1), date(2026, 10, 2)) == (date(2026, 10, 1), date(2026, 10, 2))
    with pytest.raises(ValueError):
        period(anchor, None, date(2026, 10, 21))
