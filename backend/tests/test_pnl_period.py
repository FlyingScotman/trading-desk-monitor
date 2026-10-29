"""PnL for a period on synthetic history only: day, bucket and position sums, null not zero, curves, one position."""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from desk import store
from desk.api import create_app
from desk.api.pnl_history import GROSS_KEYS
from desk.api.pnl_period import buckets, position_days
from desk.api.runs import PNL_KEYS

DAYS = ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
        "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-15", "2026-10-16"]
"""Run dates; 2026-10-14 is a market day without a run."""
MARKET = [d for d in (date(2026, 9, 30) + timedelta(days=i) for i in range(17)) if d.weekday() < 5]
CURVE = "RUB RUONIA"
GOV = "Synthetic gov issuer"
FX_USD = {"2026-10-07": 84.0}
"""USD at 80 but on 2026-10-07: +5 % and back on the next run."""
STALE = {"2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"}


def prev_market(day: str) -> date:
    d = date.fromisoformat(day)
    return max(m for m in MARKET + [date(2026, 9, 29)] if m < d)


def holdings(day: str) -> list[str]:
    out = ["A1", "B1", "U1", "F1", "S1"]
    if day <= "2026-10-08":
        out.append("C1")
    if day >= "2026-10-12":
        out.append("NEW1")
    if day == "2026-10-13":
        out.append("N1")
    return out


BOOK = {"A1": "DEMO-A", "B1": "DEMO-B", "U1": "DEMO-B", "F1": "DEMO-A", "S1": "DEMO-A", "C1": "DEMO-A",
        "NEW1": "DEMO-A", "N1": "DEMO-B"}
INSTRUMENT = {"A1": "SYN-1", "B1": "SYN-1", "U1": "SYN-USD", "F1": "SYN-GOV", "S1": "SYN-STALE", "C1": "SYN-C",
              "NEW1": "SYN-NEW", "N1": "SYN-N"}


def price(pid: str, k: int, day: str) -> float:
    return 99.5 if pid == "S1" and day in STALE else 100 + 0.1 * k + 0.01 * len(pid)


def quantity(pid: str, day: str) -> float:
    return 15.0 if pid == "A1" and day >= "2026-10-12" else 10.0


def flash(pid: str, k: int, day: str) -> dict | None:
    """Components of a row; the planted cases by position and day."""
    if pid == "N1":
        return None
    q = dict.fromkeys(PNL_KEYS, 0.0)
    q.update(carry=1000.125 + 10 * k + len(pid), rolldown=50.5, spread=10.25, funding=-200.375,
             rates=100.0 * k + 1, other=25.0 * (k % 3) + 0.5)
    lin = q["rates"]
    if pid == "U1":
        q["fx_gross"] = 4000.0 if day == "2026-10-07" else -3800.0 if day == "2026-10-08" else 1.5
    if pid == "F1" and day == "2026-10-09":
        q["funding"] = 800.0  # funding sign error: long position, positive funding
    if pid == "B1" and day == "2026-10-12":
        q["other"] = 5e6  # large unexplained, an outlier of the position
    if pid == "B1" and day == "2026-10-15":
        q["rates"], lin = 1e6, 2e5  # exact rates far from linear
        del q["funding"]  # absent component: null, not 0
    total = math.fsum(q[k] for k in GROSS_KEYS if k in q)
    if pid == "A1" and day == "2026-10-13":
        total += 5000.0  # saved total off its components
    prev = prev_market(day)
    extra = {"method": "desk-flash-v1", "total": total, "prev_date": str(prev), "z0_bp": 150.0 + k,
             "z1_bp": 151.0 + k, "rates_linear": lin, "received_rub": 0}
    if pid == "A1":  # the TDM#406 keys
        extra.update(carry_coupon=q["carry"] - 300.0, carry_pull=300.0,
                     rates_linear_curves={CURVE: {"1Y": lin * 0.25, "5Y": lin * 0.75}},
                     prev={"clean_pct": price(pid, k - 1, day), "accrued_per_bond_rub": 1.0, "ytm_pct": 12.0 - 0.01 * (k - 1),
                           "dv01_rub": -4000.0, "cs01_rub": -100.0, "quantity": 10.0, "risk": {}})
    q["extra"] = extra
    return q


def fact(pid: str, k: int, day: str) -> dict:
    ccy = "USD" if pid == "U1" else "RUB"
    out = {"book_id": BOOK[pid], "instrument_id": INSTRUMENT[pid], "name": "Synthetic " + pid, "ccy": ccy,
           "asset_class": "corporate", "instr_type": "BOND", "segment": "Корп", "coupon_type": "fixed",
           "issuer": GOV if pid == "F1" else "Synthetic issuer", "price": price(pid, k, day),
           "ytm": 12.0 - 0.01 * k, "spread_curve": "USD" if pid == "U1" else None if pid == "C1" else CURVE,
           "risk": {"USD" if pid == "U1" else CURVE: {"1Y": -1000.0, "5Y": -3000.0}},
           "cashflows": [{"synthetic": True}], "gamma": [[0, 0, 1.0]],
           "extra": {"rating": {"grade": "AA"} if pid != "S1" else {}}}
    if pid == "A1":
        out["extra"]["funding"] = {"lines": [{"rate_pct": 16.0, "body_ccy": 1e7, "days": 1},
                                             {"rate_pct": 18.0, "body_ccy": -1e7, "days": 3}]}
        out["extra"]["offer_date"] = "2026-10-11"
    return out


def seed_period(url, *, intraday_anchor_prev: str | None = None, more=None) -> list[int]:
    """Two books, one instrument in both, a closed and a new position, a missing run date, a stale price, a
    funding sign error, an FX jump that reverts, a non-zero gross check, a row without PnL, the TDM#406 keys on
    one position only. ``more(con, run_id, day)``: extra rows of a run, before it completes (rows then frozen)."""
    ids = []
    with store.connect(url) as con:
        for k, day in enumerate(DAYS):
            rid = con.execute(
                "INSERT INTO run(run_type,business_date,prev_date,cut_type,market_snapshot_id,static_snapshot_id,"
                "private_inputs_hash,position_set_ids,positions_known_at,core_version,backend_version,lineage)"
                " VALUES ('EOD',%s,%s,'eod_final','synthetic-market','synthetic-static','synthetic','{}',now(),"
                " 'test','test','{}') RETURNING run_id", (day, prev_market(day))).fetchone()[0]
            ids.append(rid)
            for i, pid in enumerate(holdings(day)):
                q, f = flash(pid, k, day), fact(pid, k, day)
                stale = pid == "S1" and day in ("2026-10-07", "2026-10-08")
                con.execute(
                    "INSERT INTO run_position(run_id,position_id,ord,book_id,secid,quantity,quality,pv_rub,dv01_rub,"
                    "cs01_rub,fact,pnl,no_pnl) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (rid, pid, i, BOOK[pid], INSTRUMENT[pid], quantity(pid, day),
                     "stale_price" if stale else "exact", 1e7, -4000.0, -100.0, Jsonb(f), Jsonb(q) if q else None,
                     Jsonb({"reason": "no_prev_report", "detail": "synthetic"}) if q is None else None))
            con.execute("INSERT INTO run_reference VALUES (%s,'books',%s)", (rid, Jsonb([
                {"book_id": b, "name": b, "entity": "Synthetic entity", "desk": "Example"}
                for b in ["DEMO-A", "DEMO-B"]])))
            con.execute("INSERT INTO run_reference VALUES (%s,'fx',%s)", (rid, Jsonb([
                {"ccy": "USD", "rate": FX_USD.get(day, 80.0), "rate_source": "synthetic"}])))
            con.execute("INSERT INTO run_reference VALUES (%s,'issuers',%s)", (rid, Jsonb([
                {"issuer": GOV, "is_gov": True}, {"issuer": "Synthetic issuer", "is_gov": False}])))
            con.execute("INSERT INTO run_reference VALUES (%s,'curves',%s)", (rid, Jsonb([
                {"curve_id": CURVE, "name": "Synthetic OIS", "ccy": "RUB", "kind": "swap",
                 "points": [{"bucket": "1Y", "rate": 15.0 + 0.01 * k, "move_bp": 1.0},
                            {"bucket": "5Y", "rate": 14.0, "move_bp": 0.0}]},
                {"curve_id": "USD", "name": "USD: synthetic", "ccy": "USD", "kind": "bond", "points": []}])))
            sets = []
            for book in ["DEMO-A", "DEMO-B"]:
                sid = con.execute("INSERT INTO position_set(book_id,valid_date,revision,source_name,"
                                  "source_file_sha256) VALUES (%s,%s,1,'synthetic','synthetic') RETURNING set_id",
                                  (book, day)).fetchone()[0]
                sets.append(sid)
                for pid in holdings(day):
                    if BOOK[pid] == book:
                        con.execute("INSERT INTO position_input(set_id,position_id,secid,quantity,ccy,"
                                    "source_instrument_name,x) VALUES (%s,%s,%s,1,%s,%s,%s)",
                                    (sid, pid, INSTRUMENT[pid], "USD" if pid == "U1" else "RUB", "Synthetic " + pid,
                                     Jsonb({"x_portfolio": "Active"})))
            if more is not None:
                more(con, rid, day)
            if intraday_anchor_prev and day == DAYS[-1]:
                con.execute("UPDATE run SET run_type='INTRADAY',cut_type='intraday',prev_date=%s WHERE run_id=%s",
                            (intraday_anchor_prev, rid))
            con.execute("UPDATE run SET position_set_ids=%s,status='complete',completed_at=now() WHERE run_id=%s",
                        (sets, rid))
        for d in MARKET:
            con.execute("INSERT INTO md_kbd (business_date,snapshot_id,known_at) VALUES (%s,'synthetic',now())", (d,))
        con.execute("INSERT INTO md_cbr_meeting VALUES ('2026-10-10','2026-10-13',false,false,'hold',16.0,16.0,"
                    "'synthetic','2026-10-10')")
    return ids


@pytest.fixture
def period_client(store_url):
    return TestClient(create_app(store_url)), seed_period(store_url)


def get(client, path="period", **kw):
    params = {"from": DAYS[0], "to": DAYS[-1], **({"gran": "week"} if path == "period" else {}), **kw}
    response = client.get(f"/api/v1/history/pnl/{path}", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def fsum(values):
    vals = [v for v in values if v is not None]
    return math.fsum(vals) if vals else None


def test_buckets_cover_the_period():
    weeks = buckets(date(2026, 9, 30), date(2026, 10, 16), "week")
    assert [(b["from"], b["to"]) for b in weeks] == [
        (date(2026, 9, 30), date(2026, 10, 4)), (date(2026, 10, 5), date(2026, 10, 11)),
        (date(2026, 10, 12), date(2026, 10, 16))]
    assert all(b["from"].weekday() == 0 for b in weeks[1:])
    months = buckets(date(2026, 9, 30), date(2026, 10, 16), "month")
    assert [b["key"] for b in months] == ["2026-09", "2026-10"] and months[0]["to"] == date(2026, 9, 30)
    days = buckets(date(2024, 2, 28), date(2024, 3, 1), "day")
    assert [b["key"] for b in days] == ["2024-02-28", "2024-02-29", "2024-03-01"]
    dec = buckets(date(2026, 12, 15), date(2027, 1, 2), "month")
    assert [(b["from"], b["to"]) for b in dec] == [(date(2026, 12, 15), date(2026, 12, 31)),
                                                    (date(2027, 1, 1), date(2027, 1, 2))]


@pytest.mark.parametrize("gran", ["day", "week", "month"])
def test_period_sums(period_client, gran):
    c, _ids = period_client
    p = get(c, gran=gran)
    assert [r["date"] for r in p["runs"]] == DAYS and [d["date"] for d in p["days"]] == DAYS
    assert p["keys"][:3] == ["carry", "carry_coupon", "carry_pull"] and p["keys"][-1] == "reported"
    assert len(p["buckets"]) == {"day": 17, "week": 3, "month": 2}[gran]
    s = p["summary"]
    assert s["rows"] == sum(d["rows"] for d in p["days"]) == sum(x["rows"] for x in p["positions"])
    for k in p["keys"]:
        assert s["c"][k] == pytest.approx(fsum(d["c"][k] for d in p["days"])), k
        assert s["c"][k] == pytest.approx(fsum(x["c"][k] for x in p["positions"])), k
        assert s["m"][k] == sum(d["m"][k] for d in p["days"]), k
    assert s["total"] == pytest.approx(fsum(d["total"] for d in p["days"]))
    assert s["total"] == pytest.approx(fsum(fsum(d["c"][k] for k in PNL_KEYS) for d in p["days"]))
    for x in p["positions"]:
        assert len(x["buckets"]) == len(p["buckets"])
        assert x["total"] == pytest.approx(fsum(x["buckets"])), x["ref"]
    for d in p["days"]:
        assert fsum(d["ratesByCurve"].values()) == pytest.approx(d["c"]["rates"])
    # carry split only where saved: one position, its rows; others count as missing
    assert s["c"]["carry_coupon"] == pytest.approx(math.fsum(x["c"]["carry_coupon"] or 0 for x in p["positions"]))
    a1 = next(x for x in p["positions"] if x["ref"] == "DEMO-A|A1")
    assert a1["c"]["carry_coupon"] + a1["c"]["carry_pull"] == pytest.approx(a1["c"]["carry"])
    assert s["m"]["carry_coupon"] == s["rows"] - a1["rows"]
    assert s["noTotal"] == 1 and s["grossCount"] == s["rows"] - 1
    assert s["grossCheck"] == pytest.approx(5000.0)
    assert p["curves"] == [{"id": CURVE, "name": "Synthetic OIS"}, {"id": "USD", "name": "USD: synthetic"}]
    assert p["cbrMeetings"][0]["date"] == "2026-10-10"
    assert p["marketDates"] == [str(d) for d in MARKET]


def test_period_null_not_zero(period_client):
    c, _ids = period_client
    p = get(c, books="DEMO-B", gran="day")
    b1 = next(x for x in p["positions"] if x["ref"] == "DEMO-B|B1")
    assert b1["m"]["funding"] == 1 and b1["m"]["carry_coupon"] == b1["rows"]
    assert b1["c"]["carry_coupon"] is None and b1["c"]["carry_pull"] is None
    assert p["keys"] == [k for k in p["keys"] if k not in ("carry_coupon", "carry_pull")]  # no split in the slice
    assert "2026-10-14" not in {d["date"] for d in p["days"]}  # a market day without a run: no row, not zeros
    i14 = [b["key"] for b in p["buckets"]].index("2026-10-14")
    assert all(x["buckets"][i14] is None for x in p["positions"])
    n1 = next(x for x in p["positions"] if x["ref"] == "DEMO-B|N1")
    assert n1["total"] is None and n1["c"]["carry"] is None and n1["noTotal"] == 1 and n1["m"]["carry"] == 1
    assert n1["pvAvg"] == 1e7 and n1["calDays"] is None
    assert get(c, ccy="JPY")["summary"]["total"] is None and get(c, ccy="JPY")["positions"] == []


def test_period_positions(period_client):
    c, _ids = period_client
    p = get(c)
    by = {x["ref"]: x for x in p["positions"]}
    assert {"DEMO-A|A1", "DEMO-B|B1"} <= set(by) and by["DEMO-A|A1"]["instrument"] == by["DEMO-B|B1"]["instrument"]
    assert by["DEMO-A|C1"]["closed"] and by["DEMO-A|C1"]["lastDate"] == "2026-10-08"
    assert by["DEMO-A|C1"]["curve"] is None
    assert by["DEMO-B|N1"]["closed"] and not by["DEMO-A|A1"]["closed"]
    assert by["DEMO-A|NEW1"]["firstDate"] == "2026-10-12"
    assert (by["DEMO-A|F1"]["group"], by["DEMO-A|F1"]["kind"]) == ("GOV", "PD")
    assert (by["DEMO-A|A1"]["group"], by["DEMO-A|A1"]["kind"]) == ("CORP", "FIX")
    assert by["DEMO-A|A1"]["rating"] == "AA" and by["DEMO-A|S1"]["rating"] is None
    assert by["DEMO-A|A1"]["fundingRatePct"] == pytest.approx(17.5)  # weighted by |body| x days: (16 + 3 x 18) / 4
    assert by["DEMO-B|B1"]["fundingRatePct"] is None
    assert by["DEMO-A|A1"]["calDays"] == sum((date.fromisoformat(d) - prev_market(d)).days for d in DAYS)
    assert by["DEMO-A|A1"]["pvAvg"] == 1e7
    assert by["DEMO-A|A1"]["book"] == "DEMO-A" and by["DEMO-A|A1"]["positionId"] == "A1"
    a = get(c, books="DEMO-A")["summary"]["total"]
    b = get(c, books="DEMO-B")["summary"]["total"]
    assert p["summary"]["total"] == pytest.approx(a + b)
    assert {r["book"] for r in get(c, books="DEMO-B")["catalogue"]} == {"DEMO-A", "DEMO-B"}
    assert "catalogue" not in get(c, catalogue="false")


def test_period_validation(period_client):
    c, ids = period_client
    base = {"from": DAYS[0], "to": DAYS[-1], "gran": "week"}
    for path in ["period", "curves", "position", "checks"]:
        for params in [{"from": "2026-10-17"}, {"to": "2026-10-17"}, {"from": "2023-01-01"}, {"from": "invalid"}]:
            extra = {"position_book": "DEMO-A", "position_id": "A1"} if path == "position" else {}
            r = c.get(f"/api/v1/history/pnl/{path}", params={**base, **extra, **params})
            assert r.status_code == 422, (path, params)
        assert c.get(f"/api/v1/history/pnl/{path}", params={**base, "run_id": 9999, "position_book": "DEMO-A",
                                                          "position_id": "A1"}).status_code == 404
    assert c.get("/api/v1/history/pnl/period", params={**base, "gran": "year"}).status_code == 422
    assert c.get("/api/v1/history/pnl/period", params={"from": DAYS[0], "to": DAYS[-1]}).status_code == 422
    assert c.get("/api/v1/history/pnl/position", params=base).status_code == 422
    assert c.get("/api/v1/history/pnl/checks", params={**base, "thr_abs": -1}).status_code == 422
    sub = get(c, run_id=ids[3], to=DAYS[3])
    assert sub["anchorRunId"] == ids[3] and [r["date"] for r in sub["runs"]] == DAYS[:4]


def test_old_endpoint_unchanged(period_client):
    c, _ids = period_client
    old = c.get("/api/v1/history/pnl", params={"from": DAYS[0], "to": DAYS[-1]}).json()
    new = get(c)
    assert old["summary"]["total"] == pytest.approx(new["summary"]["total"])
    assert old["summary"]["rowCount"] == new["summary"]["rows"]
    assert [r["runId"] for r in old["runs"]] == [r["runId"] for r in new["runs"]]
    assert {"summary", "days", "positions", "books", "observations"} <= set(old)


def test_separate_intraday_anchor(store_url):
    ids = seed_period(store_url, intraday_anchor_prev="2026-10-13")
    c = TestClient(create_app(store_url))
    p = get(c)
    sep = p["separateAnchor"]
    assert sep["run"]["runId"] == ids[-1] and sep["overlappingDates"] == ["2026-10-15"]
    assert [d["date"] for d in p["days"]] == DAYS[:-1] and sep["summary"]["rows"] == len(holdings(DAYS[-1]))
    assert p["summary"]["total"] == pytest.approx(fsum(d["total"] for d in p["days"]))
    assert get(c, "curves")["lastRun"]["date"] == DAYS[-2]


def test_curves(period_client):
    c, _ids = period_client
    k = get(c, "curves")
    assert k["firstRun"]["date"] == DAYS[0] and k["lastRun"]["date"] == DAYS[-1]
    assert k["buckets"] == ["1Y", "5Y"] and [x["id"] for x in k["curves"]] == [CURVE, "USD"]
    assert k["move"][CURVE]["1Y"] == pytest.approx(12.0)  # 15.11 - (15.00 - 1 bp)
    assert k["move"][CURVE]["5Y"] == pytest.approx(0.0) and k["move"]["USD"]["1Y"] is None
    # mean over runs of the slice's risk: positions on the curve per run x their bucket DV01
    rows_on = sum(sum(1 for pid in holdings(d) if pid != "U1") for d in DAYS)
    assert k["dv01"][CURVE]["5Y"] == pytest.approx(-3000.0 * rows_on / len(DAYS))
    assert k["dv01"]["USD"]["1Y"] == pytest.approx(-1000.0)
    lin_a1 = math.fsum(100.0 * i + 1 for i in range(len(DAYS)))
    assert k["linear"][CURVE]["1Y"] == pytest.approx(lin_a1 * .25) and k["linear"]["USD"]["1Y"] is None
    assert k["coverage"]["bucketRows"] == len(DAYS)
    assert k["coverage"]["rows"] == sum(len(holdings(d)) for d in DAYS)
    assert k["coverage"]["linearRows"] == k["coverage"]["rows"] - 1
    p = get(c)
    assert k["exact"][CURVE] == pytest.approx(fsum(d["ratesByCurve"].get(CURVE) for d in p["days"]))
    assert set(k["exact"]) == {CURVE, "USD", ""} and k["linearTotal"][""] is not None
    none = get(c, "curves", ccy="JPY")
    assert none["coverage"] == {"rows": 0, "linearRows": 0, "bucketRows": 0} and none["curves"] == []


def test_position(period_client):
    c, _ids = period_client
    e = get(c, "position", position_book="DEMO-A", position_id="A1")
    assert e["position"]["ref"] == "DEMO-A|A1" and e["position"]["curve"] == CURVE
    days = {d["date"]: d for d in e["days"]}
    assert list(days) == DAYS
    d = days["2026-10-13"]
    assert d["inputs"]["source"] == "prev" and d["inputs"]["cleanPrev"] == pytest.approx(price("A1", 8, DAYS[9]))
    assert d["inputs"]["z"] == 160.0 and d["inputs"]["zPrev"] == 159.0 and d["grossCheck"] == pytest.approx(5000)
    assert d["c"]["carry_coupon"] is not None and d["ratesLinear"] == pytest.approx(901.0)
    assert d["total"] == pytest.approx(fsum(d["c"][k] for k in PNL_KEYS))
    assert "количество изменилось" in days["2026-10-12"]["events"]
    assert any(x.startswith("оферта") for x in days["2026-10-12"]["events"])
    b = {d["date"]: d for d in get(c, "position", position_book="DEMO-B", position_id="B1")["days"]}
    assert b["2026-10-01"]["inputs"]["source"] == "previous_row"
    assert b["2026-10-01"]["inputs"]["cleanPrev"] == pytest.approx(price("B1", 0, DAYS[0]))
    assert b["2026-09-30"]["inputs"]["source"] is None and b["2026-09-30"]["inputs"]["cleanPrev"] is None
    assert b["2026-10-15"]["c"]["funding"] is None and b["2026-10-15"]["c"]["carry_coupon"] is None
    assert b["2026-10-15"]["inputs"]["source"] == "previous_row"  # the previous run is 2026-10-13: 2026-10-14 has none
    closed = get(c, "position", position_book="DEMO-A", position_id="C1")["days"]
    assert closed[-1]["events"] == ["позиции нет в прогоне 09.10.2026"]
    n1 = get(c, "position", position_book="DEMO-B", position_id="N1")["days"]
    assert n1[0]["noPnl"]["reason"] == "no_prev_report" and n1[0]["total"] is None
    assert n1[0]["events"][0] == "позиция появилась"
    missing = get(c, "position", position_book="DEMO-B", position_id="A1")
    assert missing == {"position": None, "days": []}


def test_position_quantity_change_hides_previous_row():
    rows = [{"date": date(2026, 10, d), "runId": d, "book": "B", "positionId": "P", "name": "P", "instrument": "I",
             "ccy": "RUB", "quantity": q, "clean": 100.0 + d, "ytm": 10.0, "dv01": -1.0, "cs01": 0.0, "pv": 1.0,
             "pnl": {"carry": 1.0}, "runPrevDate": date(2026, 10, d - 1)} for d, q in [(2, 1.0), (3, 2.0), (4, 2.0)]]
    out = position_days(rows, [date(2026, 10, d) for d in (2, 3, 4)])
    i = [d["inputs"] for d in out["days"]]
    assert i[1]["source"] == "previous_row" and i[1]["cleanPrev"] is None and i[1]["quantityPrev"] == 1.0
    assert i[2]["cleanPrev"] == 103.0 and out["days"][1]["events"] == ["количество изменилось"]


def test_period_per_row_splits(period_client):
    """TDM#432: the carry of rows without ``carry_coupon`` summed apart (never a missing component); TDM#435: linear
    rates of RUB bonds and the exact rates of the same rows; a position's methods and reasons without PnL."""
    c, _ids = period_client
    p = get(c)
    s, by = p["summary"], {x["ref"]: x for x in p["positions"]}
    assert {"carry_nosplit", "rates_linear", "rates_exact_lin"} <= set(p["keys"])
    a1, b1, u1, n1 = by["DEMO-A|A1"], by["DEMO-B|B1"], by["DEMO-B|U1"], by["DEMO-B|N1"]
    assert a1["c"]["carry_nosplit"] is None and s["m"]["carry_nosplit"] == 0
    for ref in ("DEMO-B|B1", "DEMO-A|C1", "DEMO-B|U1"):
        assert by[ref]["c"]["carry_nosplit"] == pytest.approx(by[ref]["c"]["carry"]), ref
    assert s["c"]["carry_coupon"] + s["c"]["carry_pull"] + s["c"]["carry_nosplit"] == pytest.approx(s["c"]["carry"])
    assert n1["c"]["carry_nosplit"] is None
    # a USD bond keeps its rates whole; the exact - linear of the others is the rates convexity
    assert u1["c"]["rates_linear"] is None and u1["m"]["rates_linear"] == u1["rows"]
    assert b1["c"]["rates_exact_lin"] == pytest.approx(b1["c"]["rates"])
    assert b1["c"]["rates_exact_lin"] - b1["c"]["rates_linear"] == pytest.approx(1e6 - 2e5)
    assert s["c"]["rates"] - s["c"]["rates_exact_lin"] == pytest.approx(u1["c"]["rates"])
    assert s["m"]["rates_linear"] == u1["rows"] + 1  # and the row without PnL
    for d in p["days"]:
        assert d["c"]["rates_exact_lin"] == pytest.approx(fsum(x for k, x in d["ratesByCurve"].items() if k != "USD"))
    assert a1["methods"] == ["desk-flash-v1"] and n1["methods"] == []
    assert n1["noPnlReasons"] == {"no_prev_report": 1} and a1["noPnlReasons"] == {}
    usd = get(c, ccy="USD")
    assert "rates_linear" not in usd["keys"] and usd["summary"]["c"]["rates_linear"] is None
    b = {d["date"]: d for d in get(c, "position", position_book="DEMO-B", position_id="B1")["days"]}
    assert b["2026-10-01"]["c"]["carry_nosplit"] == pytest.approx(b["2026-10-01"]["c"]["carry"])
