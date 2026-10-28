"""Checks of a period's saved PnL on synthetic history: each finds its planted case and says no_data without inputs."""
from __future__ import annotations

import tracemalloc
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_pnl_period import DAYS, MARKET, seed_period

from desk.api import create_app
from desk.api.pnl_checks import (
    HEAT_REFS,
    Scan,
    check_fsign,
    check_fxrev,
    check_life,
    check_lin,
    check_norun,
    check_outlier,
    check_recon,
    check_stale,
    check_unexpl,
    evaluate,
    explained,
    observe,
    series,
)


@pytest.fixture
def checks_client(store_url):
    return TestClient(create_app(store_url)), seed_period(store_url)


def get(client, **kw):
    response = client.get("/api/v1/history/pnl/checks", params={"from": DAYS[0], "to": DAYS[-1], **kw})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def found(body, check):
    return {(f["ref"] or f["name"], f["date"], f["dateTo"]) for f in body["findings"] if f["check"] == check}


def test_planted_cases(checks_client):
    c, _ids = checks_client
    body = get(c)
    status = {x["key"]: x for x in body["checks"]}
    assert list(status) == ["recon", "unexpl", "stale", "fsign", "lin", "fxrev", "outlier", "norun", "life"]
    assert all(x["status"] == "findings" and x["count"] and x["label"] and x["note"] for x in status.values())
    assert found(body, "recon") == {("DEMO-A|A1", "2026-10-13", None)}
    assert found(body, "unexpl") == {("DEMO-B|B1", "2026-10-12", None)}
    assert found(body, "stale") == {("DEMO-A|S1", "2026-10-05", "2026-10-08"), ("DEMO-A|S1", "2026-10-07", "2026-10-08")}
    assert found(body, "fsign") == {("DEMO-A|F1", "2026-10-09", None)}
    assert found(body, "lin") == {("DEMO-B|B1", "2026-10-15", None)}
    assert found(body, "fxrev") == {("USD", "2026-10-07", "2026-10-08")}
    assert ("DEMO-B|B1", "2026-10-12", None) in found(body, "outlier")
    assert found(body, "norun") == {(None, "2026-10-14", None)}
    life = {(f["ref"], f["date"], f["text"]) for f in body["findings"] if f["check"] == "life"}
    assert life == {("DEMO-A|C1", "2026-10-08", "позиция закрыта: нет с 09.10.2026"),
                    ("DEMO-A|NEW1", "2026-10-12", "позиция появилась"),
                    ("DEMO-A|A1", "2026-10-12", "количество изменилось"),
                    ("DEMO-B|N1", "2026-10-13", "позиция появилась"),
                    ("DEMO-B|N1", "2026-10-13", "нет PnL: no_prev_report"),
                    ("DEMO-B|N1", "2026-10-13", "позиция закрыта: нет с 15.10.2026")}
    assert [f["sev"] for f in body["findings"]] == sorted((f["sev"] for f in body["findings"]),
                                                          key=["crit", "warn", "info"].index)
    assert all(isinstance(f["text"], str) and f["text"] for f in body["findings"])
    assert body["coverage"]["missingRuns"] == ["2026-10-14"] and body["coverage"]["runDates"] == DAYS
    assert body["coverage"]["marketDates"] == [str(d) for d in MARKET]
    u = body["unexplained"]
    assert u["dates"] == DAYS and "DEMO-B|N1" not in u["refs"]
    big = [cell for cell in u["cells"] if cell[2] == 5e6]
    assert big == [[DAYS.index("2026-10-12"), u["refs"].index("DEMO-B|B1"), 5e6]]
    assert body["thrAbs"] == 3e6 and body["thrPct"] == 0.25


def test_thresholds_and_slices(checks_client):
    c, _ids = checks_client
    assert {x["key"]: x["status"] for x in get(c, thr_abs=1e7)["checks"]}["unexpl"] == "ok"
    assert {x["key"]: x["status"] for x in get(c, thr_pct=1e9)["checks"]}["unexpl"] == "ok"
    a = get(c, books="DEMO-A")
    assert {x["key"]: x["status"] for x in a["checks"]}["fxrev"] == "no_data"  # no foreign-currency position
    none = {x["key"]: x for x in get(c, ccy="JPY")["checks"]}
    for k in ["recon", "unexpl", "stale", "fsign", "lin", "fxrev", "outlier", "life"]:
        assert none[k]["status"] == "no_data" and none[k]["count"] == 0 and none[k]["note"], k
    assert none["norun"]["status"] == "findings"  # the calendar does not depend on the slice


def o(day, ref="B|P", **kw):
    pnl = kw.pop("pnl", {"carry": 1.0, "other": 0.0, "extra": {"method": "desk-flash-v1"}})
    return observe({"date": day, "runId": kw.pop("run", day.day), "book": ref.split("|")[0],
                    "positionId": ref.split("|")[1], "name": "P", "ccy": kw.pop("ccy", "RUB"), "pnl": pnl, **kw})


def test_no_data_without_inputs():
    d = date(2026, 10, 1)
    bare = [o(d, pnl=None, noPnl=None)]
    assert check_recon(bare)["status"] == "no_data"
    assert check_unexpl(bare)["status"] == "no_data"
    assert check_unexpl([o(d, pnl={"other": 9e9, "extra": {"method": "desk-reported-v1"}})])["status"] == "no_data"
    assert check_stale(series(bare), [1])["status"] == "no_data"
    assert check_fsign([o(d, pnl={"funding": -1.0})])["status"] == "no_data"  # no PV
    assert check_lin([o(d, pnl={"rates": 5.0})])["status"] == "no_data"
    assert check_fxrev([(d, {"USD": 80.0})], ["USD"])["status"] == "no_data"
    assert check_fxrev([], ["RUB"])["status"] == "no_data"
    assert check_outlier(series([o(d)] * 4))["status"] == "no_data"
    assert check_norun([], [d])["status"] == "no_data"
    assert check_life({}, [], [])["status"] == "no_data"
    ok = evaluate([o(d, pv=1.0, clean=100.0, quality="exact", dv01=-1.0, quantity=1.0)], [1], [d], [d],
                  [(d, {})])
    assert {x["key"]: x["status"] for x in ok["checks"]} == {
        "recon": "no_data", "unexpl": "ok", "stale": "ok", "fsign": "no_data", "lin": "no_data", "fxrev": "no_data",
        "outlier": "no_data", "norun": "ok", "life": "ok"}


def test_pure_checks_edges():
    days = [date(2026, 10, i) for i in range(1, 8)]
    flat = [o(x, run=i, clean=100.0, dv01=-1.0, quality="exact", quantity=1.0) for i, x in enumerate(days)]
    assert check_stale(series(flat), list(range(7)))["findings"][0]["dateTo"] == days[-1]
    no_risk = [o(x, run=i, clean=100.0, dv01=0.0, quality="exact") for i, x in enumerate(days)]
    assert check_stale(series(no_risk), list(range(7)))["status"] == "ok"  # no DV01: an unchanged price is fine
    gap = [r for r in flat if r["runId"] != 2]  # the position misses a run: no streak across it
    assert len(check_stale(series(gap), list(range(7)))["findings"]) == 1
    up = [(x, {"USD": 80.0 * (1.03 if i == 3 else 1.0)}) for i, x in enumerate(days)]
    assert len(check_fxrev(up, ["USD"])["findings"]) == 1
    trend = [(x, {"USD": 80.0 * 1.03 ** i}) for i, x in enumerate(days)]
    assert check_fxrev(trend, ["USD"])["status"] == "ok"  # moves without a reversal
    vals = [o(x, run=i, pnl={"carry": 100.0 + i}) for i, x in enumerate(days)]
    vals.append(o(date(2026, 10, 8), run=7, pnl={"carry": 1e6}))
    hits = check_outlier(series(vals))["findings"]
    assert [h["date"] for h in hits] == [date(2026, 10, 8)]
    assert check_fsign([o(days[0], pnl={"funding": 0.0}, pv=1.0)])["status"] == "no_data"
    assert check_lin([o(days[0], pnl={"rates": 1e6, "extra": {"rates_linear": 9.8e5}})])["status"] == "ok"
    assert check_norun([days[0], days[1]], [days[0]], [days[1]])["findings"][0]["text"].startswith("только")


def test_unexplained_against_net_fx():
    """FX is explained once, net (TDM#414): a USD bond's asset FX and its funding's FX offsetting each other do not
    hide a large ``other`` (with the moduli apart, 80e6 of FX would)."""
    d = date(2026, 10, 1)
    pnl = {"carry": 1e6, "rates": 1e6, "spread": 1e6, "funding": -1e6, "fx_gross": 40e6, "fx_funding": -40e6,
           "other": 5e6, "extra": {"method": "desk-flash-v1"}}
    hit = check_unexpl([o(d, ccy="USD", pnl=pnl)])
    assert hit["status"] == "findings" and hit["findings"][0]["value"] == 5e6
    assert check_unexpl([o(d, ccy="USD", pnl={**pnl, "fx_funding": 40e6})])["status"] == "ok"  # 80e6 net FX
    assert explained({"carry": 1.0, "fx_gross": None, "fx_funding": None, "other": 9.0}) == 1.0


MANY = 70
NO_PNL_DAYS = DAYS[2:8]
"""A position without PnL on six consecutive runs."""


@pytest.fixture
def many_client(store_url):
    """The planted history plus MANY positions with a saved ``other`` (M00 the smallest) and one without PnL."""

    def more(con, rid, day):
        rows = [(f"M{j:02d}", {"other": 1000.0 * (j + 1), "extra": {"method": "desk-flash-v1"}}, None)
                for j in range(MANY)]
        if day in NO_PNL_DAYS:
            rows.append(("NP1", None, {"reason": "no_prev_report", "detail": "synthetic"}))
        for j, (pid, pnl, no_pnl) in enumerate(rows):
            con.execute(
                "INSERT INTO run_position(run_id,position_id,ord,book_id,secid,quantity,quality,pv_rub,dv01_rub,"
                "cs01_rub,fact,pnl,no_pnl) VALUES (%s,%s,%s,'DEMO-B','SYN-M',10,'exact',1e6,-10,0,%s,%s,%s)",
                (rid, pid, 100 + j, Jsonb({"name": "Synthetic " + pid, "ccy": "RUB"}),
                 Jsonb(pnl) if pnl else None, Jsonb(no_pnl) if no_pnl else None))

    seed_period(store_url, more=more)
    return TestClient(create_app(store_url))


def test_heat_map_cap_and_folded_no_pnl(many_client):
    body = get(many_client)
    u = body["unexplained"]
    assert HEAT_REFS == 60 and len(u["refs"]) == HEAT_REFS
    assert u["refsTotal"] == MANY + 7  # A1 B1 U1 F1 S1 C1 NEW1 save ``other``; N1 and NP1 have no PnL
    # the largest by max |other|: B1 (5e6) and M11..M69; the smaller M00..M10 and the planted rest left out
    assert set(u["refs"]) == {"DEMO-B|B1", *(f"DEMO-B|M{j:02d}" for j in range(11, MANY))}
    assert u["refs"] == sorted(u["refs"]) and u["dates"] == DAYS
    assert all(0 <= ri < HEAT_REFS for _di, ri, _v in u["cells"])
    assert len(u["cells"]) == len(DAYS) * HEAT_REFS  # cells of the kept positions only, every day
    nopnl = [f for f in body["findings"] if f["check"] == "life" and f["ref"] == "DEMO-B|NP1"]
    assert sorted((f["date"], f["dateTo"] or "", f["text"]) for f in nopnl) == [
        (NO_PNL_DAYS[0], "", "позиция появилась"),
        (NO_PNL_DAYS[0], NO_PNL_DAYS[-1], f"нет PnL: no_prev_report; дней подряд: {len(NO_PNL_DAYS)}"),
        (NO_PNL_DAYS[-1], DAYS[8], "позиция закрыта: нет с 12.10.2026")]


def test_life_folds_quantity_changes():
    days = [date(2026, 10, i) for i in range(1, 7)]
    rows = [o(x, run=i, quantity=[1.0, 2.0, 3.0, 4.0, 4.0, 5.0][i]) for i, x in enumerate(days)]
    life = check_life(series(rows), list(range(6)), days)["findings"]
    assert [(f["date"], f["dateTo"], f["value"], f["text"]) for f in life] == [
        (days[1], days[3], 4.0, "количество изменилось; дней подряд: 3"),
        (days[5], None, 5.0, "количество изменилось")]


def test_scan_memory_does_not_grow_with_positions():
    """``Scan`` keeps one position's rows at a time: its peak barely moves from 200 to 2000 clean positions."""

    def peak(positions: int) -> int:
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
        scan = Scan(list(range(60)), days)
        tracemalloc.start()
        for p in range(positions):
            scan.add([o(d, ref=f"B|P{p}", run=i, quantity=1.0) for i, d in enumerate(days)])
        size = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert scan.refs_total == positions and len(scan.unexplained()["refs"]) == HEAT_REFS
        return size

    small, large = peak(200), peak(2000)
    assert large < 1.5 * small, (small, large)
