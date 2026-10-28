"""Checks of a period's saved PnL on synthetic history: each finds its planted case and says no_data without inputs."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_pnl_period import DAYS, MARKET, seed_period

from desk.api import create_app
from desk.api.pnl_checks import (
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
