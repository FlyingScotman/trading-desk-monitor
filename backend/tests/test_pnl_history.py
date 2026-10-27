"""Saved history on synthetic positions only; precision, gaps, historical identity and filter parity."""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from desk import store
from desk.api import create_app
from desk.api.pnl_history import GROSS_KEYS, Sum, amounts, summarize
from desk.api.runs import PNL_KEYS


def components(carry=1000.125, other=200.255, funding=-20.375, **extra):
    q = dict.fromkeys(PNL_KEYS, 0.0)
    q.update(carry=carry, other=other, funding=funding, fx_funding=-3.125)
    q.update(extra)
    q["extra"] = {"method": "desk-flash-v1", "total": math.fsum(q[k] for k in GROSS_KEYS),
                  "received_rub": 100000, "prev_date": "2026-10-09"}
    return q


def seed_history(url, *, intraday_day=None, anchor_prev=None):
    """A missing close date, a closed holding, two books sharing an instrument, a reported PV difference."""
    ids = []
    with store.connect(url) as con:
        for day, holdings in [("2026-10-12", ["A", "B", "CLOSED"]),
                              ("2026-10-13", ["A", "B", "CLOSED", "NO_PNL"]),
                              ("2026-10-15", ["A", "B"]), ("2026-10-16", ["A", "B"] )]:
            rid = con.execute(
                "INSERT INTO run(run_type,business_date,prev_date,cut_type,market_snapshot_id,static_snapshot_id,"
                "private_inputs_hash,position_set_ids,positions_known_at,core_version,backend_version,lineage)"
                " VALUES ('EOD',%s,%s,'eod_final','synthetic-market','synthetic-static','synthetic', '{}', now(),"
                " 'test','test','{}') RETURNING run_id", (day, date.fromisoformat(day) - timedelta(days=1))) \
                .fetchone()[0]
            ids.append(rid)
            for i, pid in enumerate(holdings):
                book = "DEMO-B" if pid == "B" else "DEMO-A"
                fact = {"book_id": book, "instrument_id": "SYNTHETIC-1", "name": "Synthetic " + pid,
                        "ccy": "USD" if pid == "B" else "RUB", "asset_class": "other", "instr_type": "OTHER",
                        "issuer": "Synthetic issuer", "quantity": 1, "notional": 1, "notional_rub": 1,
                        "mv": 1, "mv_rub": 1, "dv01": 0, "cs01": 0, "quality": "reported_value"}
                q = components(carry=-500.555 if pid == "B" else 1000.125,
                               other=-350.225 if pid == "B" else 200.255)
                if pid == "CLOSED":
                    q = components(carry=0.0, other=300.135)
                    q["extra"]["method"] = "desk-reported-v1"
                    q["extra"]["note"] = "Synthetic reported PV difference; trades may be inside."
                if day == "2026-10-15" and pid == "B":
                    del q["funding"]
                if pid == "NO_PNL":
                    q = None
                if q:
                    q["extra"]["prev_date"] = (anchor_prev if day == "2026-10-16" and anchor_prev else
                                              str(date.fromisoformat(day) - timedelta(days=1)))
                con.execute("INSERT INTO run_position(run_id,position_id,ord,book_id,secid,quantity,quality,fact,pnl,"
                            "no_pnl) VALUES (%s,%s,%s,%s,'SYNTHETIC-1',1,'reported_value',%s,%s,%s)",
                            (rid, pid, i, book, Jsonb(fact), Jsonb(q) if q else None,
                             Jsonb({"reason": "no_prev_report"}) if q is None else None))
            con.execute("INSERT INTO run_reference VALUES (%s,'books',%s)", (rid, Jsonb([
                {"book_id": b, "name": b, "entity": "Synthetic entity", "desk": "Example", "book_type": "TRADING"}
                for b in ["DEMO-A", "DEMO-B"]])))
            con.execute("INSERT INTO run_reference VALUES (%s,'fx',%s)", (rid, Jsonb([
                {"ccy": "USD", "rate": 80.0, "rate_source": "synthetic", "rate_cbr": 80.0}])))
            sets = []
            for book in ["DEMO-A", "DEMO-B"]:
                sid = con.execute("INSERT INTO position_set(book_id,valid_date,revision,source_name,"
                                  "source_file_sha256) VALUES (%s,%s,1,'synthetic','synthetic') RETURNING set_id",
                                  (book, day)).fetchone()[0]
                sets.append(sid)
                for pid in holdings:
                    if ("DEMO-B" if pid == "B" else "DEMO-A") == book:
                        con.execute("INSERT INTO position_input(set_id,position_id,secid,quantity,ccy,"
                                    "source_instrument_name,x) VALUES (%s,%s,'SYNTHETIC-1',1,%s,%s,%s)",
                                    (sid, pid, "USD" if pid == "B" else "RUB", "Synthetic " + pid,
                                     Jsonb({"x_portfolio": "Historical" if pid == "CLOSED" else "Active"})))
            if intraday_day == day:
                con.execute("UPDATE run SET run_type='INTRADAY',cut_type='intraday' WHERE run_id=%s", (rid,))
            if day == "2026-10-16" and anchor_prev:
                con.execute("UPDATE run SET prev_date=%s WHERE run_id=%s", (anchor_prev, rid))
            con.execute("UPDATE run SET position_set_ids=%s,status='complete',completed_at=now() WHERE run_id=%s",
                        (sets, rid))
        con.execute("INSERT INTO md_kbd (business_date,snapshot_id,known_at) VALUES"
                    " ('2026-10-14','synthetic-missing-day',now())")
    return ids


@pytest.fixture
def history_client(store_url):
    return TestClient(create_app(store_url)), seed_history(store_url)


def test_original_amounts_and_missing():
    q = components()
    q.update(fx_gross_cbr=999999, fx_funding_cbr=888888)
    a = amounts(q)
    assert a["total"] == pytest.approx(1176.88)
    assert a["gross"] == pytest.approx(1200.38)
    assert a["grossCheck"] == pytest.approx(0)
    assert a["components"]["funding"] == -20.375
    del q["funding"]
    a = amounts(q)
    assert a["total"] == pytest.approx(1197.255)
    assert a["components"]["funding"] is None and a["missing"] == ["funding"]
    assert amounts(None)["total"] is None
    assert amounts({"extra": {"total": 77}})["total"] is None
    q["extra"]["method"] = "desk-reported-v1"
    a = amounts(q)
    assert a["components"]["other"] is None and a["components"]["reported"] == 200.255
    assert amounts({"carry": 0})["total"] == 0
    assert amounts({"carry": 1, "extra": {"total": 3}})["grossCheck"] is None


def test_compensated_sum_and_position_identity():
    s = Sum()
    for x in [1e16, 1.0, -1e16, None]:
        s.add(x)
    assert s.value() == 1
    rows = [{"date": "2026-10-12", "book": b, "positionId": "same", "pnl": components(carry=v)}
            for b, v in [("A", 10.125), ("B", -9.255)]]
    a = summarize(iter(rows), date(2026, 10, 12), date(2026, 10, 13), ["carry"])
    assert len(a["positions"]) == 2
    assert a["summary"]["selected"] == pytest.approx(.87)
    assert a["days"][1]["total"] is None and a["days"][1]["cumulative"] is None
    assert a["observations"] == []


def get(client, **kw):
    response = client.get('/api/v1/history/pnl', params={"from": "2026-10-12", "to": "2026-10-16", **kw})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_report_history_and_filters(history_client):
    c, ids = history_client
    all_ = get(c)
    assert len(all_["runs"]) == 4 and len(all_["positions"]) == 4
    assert all_["marketDates"] == ["2026-10-14"]
    assert all_["days"][2]["total"] is None
    assert "CLOSED" in {p["row"]["positionId"] for p in all_["positions"]}
    assert all_["observations"] == []
    assert all_["summary"]["noTotal"] == 1 and all_["summary"]["partialRows"] == 2
    for key in ["total", "selected"]:
        assert all_["summary"][key] == pytest.approx(math.fsum(p[key] for p in all_["positions"] if p[key] is not None))
        assert all_["summary"][key] == pytest.approx(math.fsum(d[key] for d in all_["days"] if d[key] is not None))
    a, b = get(c, books="DEMO-A"), get(c, books="DEMO-B")
    assert a["summary"]["total"] > 0 and b["summary"]["total"] < 0
    assert all_["summary"]["total"] == pytest.approx(a["summary"]["total"] + b["summary"]["total"])
    assert {p["book"] for p in b["catalogue"]} == {"DEMO-A", "DEMO-B"}  # options independent of slice
    for opts in [{"ccy": "USD"}, {"books": "DEMO-B", "entity": "Synthetic entity", "desk": "Example"},
                 {"q": "synthetic b"}]:
        x = get(c, **opts)
        assert x["summary"] == b["summary"]
    assert get(c, ccy="JPY")["summary"]["total"] is None
    assert get(c, asset_class="missing")["positions"] == []
    assert get(c, issuer="missing")["summary"]["total"] is None
    assert get(c, ports_off=["Active", "Historical"])["positions"] == []
    historical = get(c, ports_off="Active")
    assert len(historical["positions"]) == 1 and historical["positions"][0]["row"]["positionId"] == "CLOSED"
    assert {r["portfolio"] for r in historical["catalogue"]} == {"Active", "Historical"}
    chosen = get(c, components="other")
    assert chosen["summary"]["total"] == all_["summary"]["total"]
    assert chosen["summary"]["selected"] != all_["summary"]["total"]
    assert get(c, components="")["summary"]["selected"] is None
    assert get(c, catalogue="false").get("catalogue") is None
    sub = get(c, **{"from": "2026-10-15"})
    assert len(sub["positions"]) == 2 and len(sub["runs"]) == 2
    assert get(c, run_id=ids[1], to="2026-10-13")["anchorRunId"] == ids[1]


def test_detail_same_slice_and_validation(history_client):
    c, _ids = history_client
    a = get(c, books="DEMO-A", components="carry,other")
    for p in a["positions"]:
        d = get(c, books="DEMO-A", components="carry,other", position_book=p["row"]["book"],
                position_id=p["row"]["positionId"], catalogue="false")
        assert d["summary"]["total"] == p["total"] and d["summary"]["selected"] == p["selected"]
        assert len(d["observations"]) == p["rowCount"]
    assert get(c, books="DEMO-B", position_book="DEMO-A", position_id="CLOSED")["observations"] == []
    for params in [{"from": "2026-10-17"}, {"to": "2026-10-17"}, {"components": "fx_gross_cbr"},
                   {"position_id": "A"}, {"from": "invalid"}]:
        assert c.get('/api/v1/history/pnl', params={"from": "2026-10-12", "to": "2026-10-16", **params}).status_code == 422
    assert c.get('/api/v1/history/pnl', params={"from": "2026-10-12", "to": "2026-10-16", "run_id": 9999}).status_code == 404


def test_intraday_only_historical_day_is_not_a_daily_close(store_url):
    seed_history(store_url, intraday_day="2026-10-13")
    a = get(TestClient(create_app(store_url)))
    assert a["intradayOnlyDates"] == ["2026-10-13"]
    assert [r["date"] for r in a["runs"]] == ["2026-10-12", "2026-10-15", "2026-10-16"]
    assert a["days"][1]["total"] is None


@pytest.mark.parametrize("prev,overlap", [("2026-10-12", True), ("2026-10-15", False)])
def test_selected_intraday_anchor_overlap(store_url, prev, overlap):
    seed_history(store_url, intraday_day="2026-10-16", anchor_prev=prev)
    c = TestClient(create_app(store_url))
    a = get(c)
    if overlap:
        anchor = a["separateAnchor"]
        assert len(a["runs"]) == 3 and a["days"][-1]["total"] is None
        assert anchor["summary"]["rowCount"] == 2 and anchor["overlappingDates"]
        assert a["summary"]["total"] == pytest.approx(math.fsum(d["total"] for d in a["days"] if d["total"] is not None))
        d = get(c, position_book="DEMO-B", position_id="B", catalogue="false")
        assert len(d["observations"]) == 3 and len(d["separateAnchor"]["observations"]) == 1
    else:
        assert "separateAnchor" not in a and len(a["runs"]) == 4
        assert a["days"][-1]["total"] is not None
