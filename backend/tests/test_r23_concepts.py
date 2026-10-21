"""R23, the concept pages on real data: the static snapshot's facts (hub static 1.1: issuer groups, sectors, short
and Latin names, issue size, coupon frequency) on the positions and the market, the market's payment calendar by
type and currency with its drill-down, the CBR meetings, the runs' summaries the series read, PnL month and year to
date from the runs with what is partial, and the API's cached bodies. The fixture's static snapshot, given the 1.1
fields; two runs (2026-10-14 on the shifted market, 2026-10-15). Synthetic data only."""

from __future__ import annotations

import gzip
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_market_history import DATE, FIX, FLOATER, MARKET, OFZ, STATIC, USD_FACE, shifted, write

from desk import VALUATION_VERSION, cli, store
from desk.api import concepts, create_app
from desk.marketdata import static_facts
from desk.marketdata.bundle import read_bundle
from desk.publish import summary
from desk.settings import Settings

PREV = "2026-10-14"
RZD = "RU000A0JXQ44"  # a corporate with a scheduled put on 2028-04-18


def static_11() -> dict:
    """The fixture's static snapshot with the static 1.1 fields the hub now publishes (HS1, HS3, HS4, HS7, HS12)."""
    with gzip.open(FIX / f"{STATIC}.json.gz") as f:
        doc = json.load(f)
    doc["schema_version"] = "market_snapshot/1.1"
    for i in doc["data"]["issuers"]:
        i.update({"short_name": {712: 'ОАО "РЖД"', 269: "Банк ВТБ (ПАО)"}.get(i["issuer_id"]),
                  "sector": {712: "transport", 269: "banks", 1228: "sovereign", 1013: "chemicals"}.get(i["issuer_id"]),
                  "group_name": {269: "ВТБ", 712: "РЖД"}.get(i["issuer_id"]), "group_id": None, "country": "RU",
                  "is_state_owned": i["issuer_id"] == 712})
    for i in doc["data"]["instruments"]:
        i["lat_name"] = "Lat " + i["secid"]
        i["coupon_frequency_schedule"] = i.get("coupon_frequency")
    return doc


def cache_11(tmp_path) -> tuple:
    """A snapshot cache of the 1.1 static and two markets naming it (2026-10-14 shifted, 2026-10-15)."""
    cache = tmp_path / "cache"
    sid = write(cache, static_11())
    ids = []
    for d, step in ((PREV, 1), (DATE, 0)):
        doc = shifted(d, step, light=False)
        doc["static_snapshot_id"] = sid
        ids.append(write(cache, doc))
    return cache, sid, ids


@pytest.fixture
def desk(store_url, tmp_path):
    cache, *_ = cache_11(tmp_path)
    settings = Settings(home=tmp_path / "home", snapshot_cache=cache)
    for d, n in ((PREV, 8), (DATE, 10)):
        book = tmp_path / f"book{d}.csv"
        book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},{n}\nP2,OFZ,{FLOATER},5\n"
                        f"P3,CORP,{USD_FACE},3\nP4,CORP,{RZD},4\n", encoding="utf-8")
        lines: list[str] = []
        assert cli.eod_run(settings, d, store_url=store_url, book=str(book), out=lines.append), lines
    return TestClient(create_app(store_url))


def extras(client, q: str = "") -> dict:
    body = client.get("/concepts-data.js" + q).text
    return json.loads(body[len("window.DESK_EXTRAS = "):-2])


# ---- static facts ------------------------------------------------------------------------------------------------


def test_bond_facts_flows_and_calendar_from_the_static_snapshot():
    doc = static_11()
    as_of = date.fromisoformat(DATE)
    facts = static_facts.bond_facts(doc["data"], as_of)
    rzd = facts[RZD]
    assert rzd["issuer"] == "РЖД" and rzd["group"] == "РЖД" and rzd["sector_label"] == "Транспорт"
    assert rzd["lat_name"] == "Lat " + RZD and rzd["issue_size"] == 15e6 and rzd["outstanding"] == 15e9
    assert rzd["type"] == "CORP_FIX" and rzd["market"] == "rub" and rzd["freq_letter"] == "S" and rzd["cpn_pct"]
    assert facts[OFZ]["type"] == "PD" and facts[FLOATER]["type"] == "PK" and facts["SU52005RMFS4"]["type"] == "IN"
    assert facts["RU000A109XR1"]["type"] == "CORP_FLT" and facts[USD_FACE]["market"] == "fx"
    assert facts[USD_FACE]["type"] == "USD"
    fl = static_facts.flows(doc["data"], facts, as_of)
    assert fl and all(DATE < x["pay_date"] <= "2027-11-19" for x in fl)
    put = [x for x in fl if x["kind"] == "put"]
    assert not put  # the scheduled put is in 2028, beyond the horizon
    ofz = [x for x in fl if x["secid"] == OFZ and x["kind"] == "cpn"]
    assert ofz and ofz[0]["amount"] == pytest.approx(ofz[0]["per_bond"] * 1e9)
    flt = [x for x in fl if x["secid"] == "RU000A109XR1" and x["kind"] == "cpn"]
    assert any(x["est"] for x in flt)  # a floater's coupons not fixed yet: the last amount, an estimate
    usd = [x for x in fl if x["secid"] == USD_FACE]
    assert usd and {x["ccy"] for x in usd} == {"USD"} and {x["market"] for x in usd} == {"fx"}
    amort = [x for x in fl if x["kind"] == "amort"]
    assert amort, "the amortizers' redemptions"
    cal = static_facts.calendar(fl, facts, as_of)
    f = cal["fields"]
    rows = [dict(zip(f["rub"], r)) for r in cal["rub"]]
    assert {r["type"] for r in rows} >= {"PD", "PK", "CORP_FIX", "CORP_FLT"}
    assert sum(r["amount"] for r in rows) == pytest.approx(sum(x["amount"] for x in fl
                                                               if x["market"] == "rub" and x["amount"] is not None))
    big = sum(r["bigAmount"] for r in rows)
    assert 0 < big < sum(r["amount"] for r in rows)  # the OFZ and RZD issues are large, the perpetuals not
    fx = [dict(zip(f["fx"], r)) for r in cal["fx"]]
    assert fx and {r["ccy"] for r in fx} == {"USD"} and cal["ccys"] == ["USD"]
    assert cal["coverage"]["with_group"] >= 2 and cal["coverage"]["with_lat_name"] == len(facts)


def test_an_offer_from_two_sources_is_one_event():
    """E2E 21.10.2026: the hub lists a scheduled offer once per source (cbonds and MOEX ISS); the market calendar
    counted it twice, doubling the possible volume of the offers (5.3 trillion RUB of corporate puts in November)."""
    doc = static_11()
    as_of = date.fromisoformat(DATE)
    facts = static_facts.bond_facts(doc["data"], as_of)
    put = {"secid": RZD, "offer_date": "2027-03-01", "status": "scheduled", "type": "put", "source": "cbonds"}
    doc["data"]["offers"] = (doc["data"].get("offers") or []) + [put, {**put, "source": "moex_iss"},
                                                                  {**put, "type": "call", "source": "moex_iss"}]
    fl = [x for x in static_facts.flows(doc["data"], facts, as_of) if x["secid"] == RZD and x["pay_date"] == "2027-03-01"]
    assert sorted(x["kind"] for x in fl) == ["call", "put"]  # a put and a call of one day stay two events


def test_a_foreign_issuers_fx_bond_is_left_out():
    doc = static_11()
    for i in doc["data"]["issuers"]:
        if i["issuer_id"] == 1013:
            i["country"] = "KZ"
    facts = static_facts.bond_facts(doc["data"], date.fromisoformat(DATE))
    assert facts[USD_FACE]["foreign_issuer"]
    assert not [x for x in static_facts.flows(doc["data"], facts, date.fromisoformat(DATE)) if x["secid"] == USD_FACE]


def test_load_static_once_keep_the_newest_and_cbr(store_url, tmp_path):
    cache, sid, ids = cache_11(tmp_path)
    snap = read_bundle(cache / f"{sid}.json.gz")
    with store.connect(store_url) as con:
        assert static_facts.load_static(con, snap) == "loaded"
        assert static_facts.load_static(con, snap) == "cached"
        n = con.execute("SELECT count(*) FROM md_market_flow WHERE snapshot_id = %s", (sid,)).fetchone()[0]
        assert n > 50
        for k in range(static_facts.KEEP + 1):  # newer snapshots push the oldest out
            doc = static_11()
            doc["business_date"] = f"2026-10-{20 + k}"
            static_facts.load_static(con, read_bundle(cache / f"{write(cache, doc)}.json.gz"))
        assert con.execute("SELECT count(*) FROM md_static_summary").fetchone()[0] == static_facts.KEEP
        assert con.execute("SELECT count(*) FROM md_market_flow WHERE snapshot_id = %s", (sid,)).fetchone()[0] == 0
        assert static_facts.load_static(con, snap) == "older"
        market = read_bundle(cache / f"{ids[1]}.json.gz")
        assert static_facts.load_cbr(con, market) == "loaded"
        assert static_facts.load_cbr(con, read_bundle(cache / f"{ids[0]}.json.gz")) == "older"  # an earlier date
        assert con.execute("SELECT count(*) FROM md_cbr_meeting").fetchone()[0] == 76


# ---- the run's facts (VALUATION_VERSION 7) and the pages' data ------------------------------------------------


def test_positions_carry_the_hub_group_sector_and_names(desk, store_url):
    assert VALUATION_VERSION >= 7
    with store.connect(store_url) as con:
        rid = con.execute("SELECT max(run_id) FROM run").fetchone()[0]
        fact = con.execute("SELECT fact FROM run_position WHERE run_id = %s AND position_id = 'P4'", (rid,)).fetchone()[0]
        refs = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'issuers'", (rid,)).fetchone()[0]
    assert fact["extra"]["lat_name"] == "Lat " + RZD and fact["extra"]["issue_size"] == 15e6
    rzd = next(r for r in refs if r["issuer"] == 'ОАО "РЖД"')
    assert rzd["issuer_group"] == "РЖД" and rzd["sector"] == "Транспорт" and rzd["is_quasi"] and rzd["rating"] == "нет данных"
    d = desk.get("/api/v1/compat/desk").json()
    p4 = next(p for p in d["positions"] if p["id"] == "P4")
    assert p4["issuerGroup"] == "РЖД" and p4["sector"] == "Транспорт"


def test_concepts_data_market_universe_cbr_and_periods(desk):
    x = extras(desk)
    ofz = next(b for b in x["universeRub"] if b["id"] == OFZ)
    assert ofz["size"] == pytest.approx(1e12) and ofz["freq"] == "S" and ofz["cpn"] and ofz["type"] == "PD"
    rzd = next(b for b in x["universeRub"] if b["id"] == RZD)
    assert rzd["sector"] == "Транспорт" and rzd["group"] == "РЖД" and rzd["issuerName"] == "РЖД" and rzd["held"]
    usd = next(b for b in x["universeFx"] if b["id"] == USD_FACE)
    assert usd["ccy"] == "USD" and usd["outstanding"] > 0 and usd["coupons"] and usd["issuer"] == usd["issuerName"]
    assert x["staticDate"] == DATE and not any("снимок статики ещё не разобран" in n for n in x["notes"])
    assert x["cbr"]["next"]["date"] == "2026-10-23" and x["cbr"]["past"][0]["decisionText"] == "без изменений"
    ytd = x["pnlPeriods"]["ytd"]
    assert not ytd["complete"] and ytd["runs"] == 2 and ytd["from"] == PREV
    assert any("PnL с начала года — частичный" in n for n in x["notes"])


def test_market_calendar_endpoint_and_drill(desk):
    cal = desk.get("/api/v1/calendar/market").json()
    assert cal["staticDate"] == DATE and RZD in cal["held"] and cal["types"][0] == "PD"
    rows = [dict(zip(cal["fields"]["rub"], r)) for r in cal["rub"]]
    cpn = sum(r["amount"] for r in rows if r["kind"] == "cpn" and r["type"] == "PD")
    j = desk.get("/api/v1/calendar/market?market=rub&kind=cpn&type=PD").json()
    assert j["amount"] == pytest.approx(cpn) and all(r["kind"] == "cpn" and r["type"] == "PD" for r in j["rows"])
    red = desk.get("/api/v1/calendar/market?market=rub&kind=redemption").json()
    assert red["rows"] and {r["kind"] for r in red["rows"]} <= {"mat", "amort"}
    fx = desk.get("/api/v1/calendar/market?market=fx&ccy=USD").json()
    assert fx["rows"] and {r["ccy"] for r in fx["rows"]} == {"USD"} and any(r["held"] for r in fx["rows"])
    one = desk.get("/api/v1/calendar/market?market=rub&from=2026-10-16&to=2026-12-31&limit=2").json()
    assert len(one["rows"]) == 2 and one["count"] >= 2 and all("2026-10-16" <= r["date"] <= "2026-12-31" for r in one["rows"])
    assert desk.get("/api/v1/calendar/market?market=xx").status_code == 422


def test_summaries_written_at_publication_and_rebuilt_when_missing(desk, store_url):
    with store.connect(store_url) as con:
        ids = [r[0] for r in con.execute("SELECT run_id FROM run ORDER BY run_id")]
        assert [r[0] for r in con.execute("SELECT run_id FROM run_summary ORDER BY run_id")] == ids
        rows = con.execute("SELECT data FROM run_summary WHERE run_id = %s", (ids[-1],)).fetchone()[0]
    first = extras(desk)
    with store.connect(store_url) as con:
        con.execute("DELETE FROM run_summary")  # runs from before R23: no summary yet
    TestClient(create_app(store_url))  # a fresh process: no cached body
    again = extras(TestClient(create_app(store_url)))
    assert again["hist"] == first["hist"] and again["positions"] == first["positions"]
    with store.connect(store_url) as con:
        assert con.execute("SELECT count(*) FROM run_summary").fetchone()[0] == len(ids)  # written back by the API
    p1 = rows["pos"]["P1"]
    assert p1[0] == pytest.approx(10 * 1000) and p1[1] and p1[8] == {"RUB RUONIA": pytest.approx(p1[1])}
    assert summary.VERSION == 1


def test_bodies_cached_by_store_state_gzip_and_etag(desk, store_url, tmp_path, monkeypatch):
    calls = []
    real = concepts.extras
    monkeypatch.setattr(concepts, "extras", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    r1 = desk.get("/concepts-data.js", headers={"Accept-Encoding": "gzip"})
    r2 = desk.get("/concepts-data.js")
    assert len(calls) == 1 and r1.headers["etag"] == r2.headers["etag"] and r1.text == r2.text
    assert r1.headers.get("content-encoding") == "gzip" and "accept-encoding" in r1.headers["vary"].lower()
    assert desk.get("/concepts-data.js", headers={"If-None-Match": r1.headers["etag"]}).status_code == 304
    h1 = desk.get("/api/v1/history/positions").json()
    assert desk.get("/api/v1/history/positions").json() == h1
    cache, *_ = cache_11(tmp_path / "again")  # a new run: the bodies are built again
    book = tmp_path / "b.csv"
    book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},11\n", encoding="utf-8")
    assert cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=cache), DATE, store_url=store_url,
                       book=str(book), out=lambda s: None)
    r3 = desk.get("/concepts-data.js")
    assert len(calls) == 2 and r3.headers["etag"] != r1.headers["etag"]


def test_concepts_data_without_static_facts_says_so(store_url, tmp_path):
    cache, *_ = cache_11(tmp_path)
    book = tmp_path / "b.csv"
    book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},11\n", encoding="utf-8")
    assert cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=cache), DATE, store_url=store_url,
                       book=str(book), out=lambda s: None, read_model=False)
    c = TestClient(create_app(store_url))
    x = extras(c)
    assert any("снимок статики ещё не разобран" in n for n in x["notes"])
    assert any("заседаний ЦБ нет в данных" in n for n in x["notes"])
    assert c.get("/api/v1/calendar/market").json() == {"missing": True}
    assert c.get("/api/v1/calendar/market?market=rub").status_code == 404


def test_market_fixture_is_the_one_shifted():
    assert read_bundle(FIX / f"{MARKET}.json.gz").static_snapshot_id == STATIC


def test_serve_builds_the_newest_runs_bodies_ahead(desk, store_url, monkeypatch):
    from desk.api.app import warm_bodies

    app = create_app(store_url)
    assert warm_bodies(app) == ["desk", "extras", "history", "calendar"]
    assert warm_bodies(app) == []  # nothing changed
    calls = []
    monkeypatch.setattr(concepts, "extras", lambda *a, **kw: calls.append(1))
    assert TestClient(app).get("/concepts-data.js").status_code == 200 and not calls  # served from the cache


def test_desk_data_js_cached_but_not_stored_by_the_browser(desk, store_url, monkeypatch):
    from desk.api import compat

    calls = []
    real = compat.assemble
    monkeypatch.setattr(compat, "assemble", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    r1, r2 = desk.get("/desk-data.js"), desk.get("/desk-data.js", headers={"Accept-Encoding": "gzip"})
    assert len(calls) == 1 and r1.text == r2.text and r1.headers["cache-control"] == "no-store"
    with store.connect(store_url) as con:  # a scenario definition edited: built again
        con.execute("UPDATE scenario_def SET name = name || ' *' WHERE scenario_id = (SELECT min(scenario_id)"
                    " FROM scenario_def)")
    desk.get("/desk-data.js")
    assert len(calls) == 2
