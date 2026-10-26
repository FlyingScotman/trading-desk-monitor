"""Slice S5 desk API v1 and the window.DESK compatibility adapter, on runs of the golden fixtures written
into the desk store (store_rows.insert_run). The previous date is the synthetic one of test_slice_s2."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from store_rows import insert_run
from test_slice_s2 import prev_outcome, shifted_prev

from desk.api import create_app
from desk.api.compat import run_slice
from desk.marketdata.adapter import MarketView
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare, value
from desk.store import MIGRATIONS

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
STATIC = Path(__file__).resolve().parents[2] / "prototypes"
DATE = "2026-10-15"
QTY = 1000.0


@pytest.fixture(scope="module")
def book():
    """(ctx, positions, prev view): every fixture bond (exact, assumed, market_value rungs) plus one bond
    unknown to the static snapshot; exact bonds get a flash PnL, the rest no_pnl (not priced the day
    before)."""
    ctx = prepare(SnapshotDir(FIX), DATE)
    secids = sorted(ctx.static.bonds) + ["NOSUCH"]
    cur = value(ctx, [(s, QTY) for s in secids], second_order=True)
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1))
    prevs = [prev_outcome(prev, o) if o.valuation and o.valuation.quality == "exact"
             else Outcome(o.secid, o.terms, None, reason="no_quote") for o in cur]
    attach_pnl(prev, prevs, ctx, cur)
    view = MarketView(ctx.market.snapshot)
    view.business_date = prev.kbd.trade_date
    positions = [(f"P{i:02d}", "OFZ" if o.secid.startswith("SU") else "CORP", o, QTY)
                 for i, o in enumerate(cur)]
    return ctx, positions, view


@pytest.fixture
def client(store_url) -> TestClient:
    return TestClient(create_app(store_url))


def run(store_url, book, **kw) -> int:
    ctx, positions, view = book
    return insert_run(store_url, ctx, positions, view, **kw)


def assert_error(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"code", "message", "request_id", "details"} and body["code"] == code
    assert body["request_id"] == r.headers["X-Request-ID"]
    return body


def test_runs_list_filters_and_pagination(store_url, book, client):
    ids = [run(store_url, book, cut_type="eod_preliminary"), run(store_url, book),
           run(store_url, book, run_type="TEST")]
    r = client.get("/api/v1/runs").json()
    assert [x["run_id"] for x in r["runs"]] == ids[::-1] and r["next_cursor"] is None
    x = r["runs"][-1]
    assert x["as_of"] == x["business_date"] == DATE and x["published_at"] == x["completed_at"]
    assert x["status"] == "complete" and x["coverage"]["_total"]["exact"] >= 3 and "_total" in x["totals"]
    assert [x["run_id"] for x in client.get("/api/v1/runs?run_type=TEST").json()["runs"]] == [ids[2]]
    assert client.get("/api/v1/runs?business_date=2026-10-14").json()["runs"] == []
    assert len(client.get("/api/v1/runs?status=complete").json()["runs"]) == 3
    p1 = client.get("/api/v1/runs?limit=2").json()
    assert [x["run_id"] for x in p1["runs"]] == [ids[2], ids[1]] and p1["next_cursor"] == ids[1]
    p2 = client.get(f"/api/v1/runs?limit=2&cursor={p1['next_cursor']}").json()
    assert [x["run_id"] for x in p2["runs"]] == [ids[0]] and p2["next_cursor"] is None
    assert_error(client.get("/api/v1/runs?run_type=NIGHTLY"), 422, "invalid_parameter")
    assert_error(client.get("/api/v1/runs?limit=501"), 422, "invalid_parameter")


def test_default_run_rule(store_url, book, client):
    assert_error(client.get("/api/v1/runs/default"), 404, "not_found")
    pre = run(store_url, book, cut_type="eod_preliminary")
    assert client.get("/api/v1/runs/default").json()["run_id"] == pre
    test = run(store_url, book, run_type="TEST")  # newer, but TEST is never the default
    assert client.get(f"/api/v1/runs/default?business_date={DATE}").json()["run_id"] == pre
    final = run(store_url, book, cut_type="eod_final")
    run(store_url, book, cut_type="eod_preliminary")  # a later preliminary run does not displace it
    assert client.get("/api/v1/runs/default").json()["run_id"] == final
    flags = {x["run_id"]: x["is_default"] for x in client.get("/api/v1/runs").json()["runs"]}
    assert [k for k, v in flags.items() if v] == [final] and not flags[test] and not flags[pre]
    assert client.get(f"/api/v1/runs/{final}").json()["supersedes_run_id"] == pre  # latest EOD of the date
    assert_error(client.get("/api/v1/runs/default?business_date=2026-10-14"), 404, "not_found")


def test_run_detail_lineage_and_log(store_url, book, client):
    rid = run(store_url, book)
    d = client.get(f"/api/v1/runs/{rid}").json()
    ctx = book[0]
    assert d["lineage"]["market_snapshot_id"] == d["market_snapshot_id"] == ctx.market.snapshot.id
    lin = d["lineage"]
    assert lin["cut_type"] == d["cut_type"] == "eod_final" and len(lin["position_set_ids"]) == 2
    assert lin["prev_market_snapshot_id"] and lin["private_inputs_hash"] and d["error"] is None
    assert d["log"][0]["message"].startswith("lineage ") and any("NOSUCH" in x["message"] for x in d["log"])
    r = client.get("/api/v1/runs/999999", headers={"X-Request-ID": "abc-123"})
    assert assert_error(r, 404, "not_found")["request_id"] == "abc-123"
    assert_error(client.get("/api/v1/runs/nope"), 422, "invalid_parameter")


def test_run_market_date_for_the_header(store_url, book, client):
    """#22: the run records its market snapshot's date (lineage ``market_business_date``) and window.DESK's
    ``source.marketDate`` says it, so the header shows both dates when the market is older than the positions."""
    rid = run(store_url, book)
    ctx = book[0]
    assert client.get(f"/api/v1/runs/{rid}").json()["lineage"]["market_business_date"] == \
        ctx.market.snapshot.business_date == DATE
    assert client.get(f"/api/v1/compat/desk?run_id={rid}").json()["source"]["marketDate"] == DATE
    base = {"business_date": date(2026, 10, 23), "cut_type": "eod_final", "cut_ts": None, "run_type": "EOD"}
    older = run_slice({**base, "lineage": {"market_business_date": "2026-10-22"}})  # the morning run on D-1 (#21)
    assert older["marketDate"] == "2026-10-22" and older["slice"] == "23.10 · закрытие"
    assert run_slice({**base, "lineage": {}})["marketDate"] is None  # a run made before #22
    morning = run_slice({**base, "cut_type": "prev_close", "run_type": "INTRADAY", "lineage": {
        "market_business_date": "2026-10-22", "market_close": {"date": "2026-10-22", "cut_type": "eod_preliminary"}}})
    assert (morning["cutType"], morning["slice"], morning["marketDate"], morning["marketCutType"]) == (
        "prev_close", "23.10 · закрытие предв. 22.10", "2026-10-22", "eod_preliminary")  # the morning run (#21)
    assert older["marketCutType"] is None


def test_positions_keep_nulls(store_url, book, client):
    rid = run(store_url, book)
    ps = client.get(f"/api/v1/runs/{rid}/positions").json()["positions"]
    assert len(ps) == len(book[1])
    gone = next(p for p in ps if p["secid"] == "NOSUCH")
    assert gone["quality"] is None and gone["reason"] == "not_in_static_snapshot" and gone["quantity"] == QTY
    assert all(gone[k] is None for k in ("pv_rub", "dv01_rub", "cs01_rub", "fact", "pnl", "no_pnl"))
    valued = [p for p in ps if p["quality"]]
    assert all(p["pv_rub"] is not None and p["fact"]["book_id"] == p["book_id"] for p in valued)
    assert any(p["pnl"] is None and (p["no_pnl"] or {}).get("reason") == "not_priced_prev" for p in valued)
    ofz = client.get(f"/api/v1/runs/{rid}/positions?book=OFZ").json()["positions"]
    assert ofz and all(p["book_id"] == "OFZ" for p in ofz)
    ex = client.get(f"/api/v1/runs/{rid}/positions?quality=exact&book=CORP").json()["positions"]
    assert ex and all(p["quality"] == "exact" and p["book_id"] == "CORP" for p in ex)


def test_pnl_totals_are_the_sums(store_url, book, client):
    rid = run(store_url, book)
    r = client.get(f"/api/v1/runs/{rid}/pnl").json()
    ps, tot = r["positions"], r["totals"]
    with_pnl = [p for p in ps if p["components"]]
    assert with_pnl and all(p["no_pnl"] for p in ps if not p["components"])
    assert all(p["extra"]["method"] and p["total"] == pytest.approx(p["extra"]["total"]) for p in with_pnl)
    for key in ("OFZ", "CORP", "_total"):
        mine = [p for p in ps if key in ("_total", p["book_id"])]
        assert tot[key]["n_positions"] == len(mine)
        assert tot[key]["n_pnl"] == sum(bool(p["components"]) for p in mine)
        for k in ("carry", "rolldown", "rates", "spread", "other", "total"):
            assert tot[key][k] == pytest.approx(sum(p["total"] if k == "total" else p["components"][k]
                                                    for p in mine if p["components"]))
    assert tot["_total"]["total"] == pytest.approx(tot["OFZ"]["total"] + tot["CORP"]["total"])


def test_pnl_is_null_without_pnl(store_url, book, client):
    ctx, positions, _ = book
    bare = [(pid, bk, dataclasses.replace(o, pnl=None), q) for pid, bk, o, q in positions]  # as --no-pnl
    rid = insert_run(store_url, ctx, bare, None)
    tot = client.get(f"/api/v1/runs/{rid}/pnl").json()["totals"]
    assert set(tot) == {"OFZ", "CORP", "_total"}
    assert all(t["n_pnl"] == 0 and t["total"] is None and t["carry"] is None for t in tot.values())
    assert client.get(f"/api/v1/runs/{rid}").json()["totals"]["_total"]["pnl"]["total"] is None
    rows = client.get(f"/api/v1/runs/{rid}/aggregates?dims=book").json()["rows"]
    assert len(rows) == 2 and all(r["pnl"]["total"] is None and r["pv_rub"] for r in rows)


def test_aggregates(store_url, book, client):
    rid = run(store_url, book)
    ps = client.get(f"/api/v1/runs/{rid}/positions").json()["positions"]
    rows = client.get(f"/api/v1/runs/{rid}/aggregates?dims=book,quality").json()["rows"]
    assert sum(r["n_positions"] for r in rows) == len(ps)
    for r in rows:
        mine = [p for p in ps if p["book_id"] == r["key"]["book"] and p["quality"] == r["key"]["quality"]]
        assert r["n_positions"] == len(mine) and r["n_pnl"] == sum(bool(p["pnl"]) for p in mine)
        pv = [p["pv_rub"] for p in mine if p["pv_rub"] is not None]
        assert r["pv_rub"] == (pytest.approx(sum(pv)) if pv else None)
        carry = [p["pnl"]["carry"] for p in mine if p["pnl"]]
        assert r["pnl"]["carry"] == (pytest.approx(sum(carry)) if carry else None)
    unvalued = next(r for r in rows if r["key"]["quality"] is None)
    assert unvalued["pv_rub"] is None and unvalued["n_valued"] == 0  # null, not 0
    by_book = client.get(f"/api/v1/runs/{rid}/aggregates?dims=book").json()["rows"]
    assert {r["key"]["book"] for r in by_book} == {"OFZ", "CORP"}
    assert client.get(f"/api/v1/runs/{rid}/aggregates?dims=issuer,ccy,segment,asset_class").status_code == 200
    r = client.get(f"/api/v1/runs/{rid}/aggregates?dims=book,curve")
    body = assert_error(r, 422, "invalid_parameter")
    assert "book" in body["details"]["allowed"]


def test_errors_etag_and_health(store_url, book, client):
    assert_error(client.get("/api/v1/nothing-here"), 404, "not_found")
    assert_error(client.post("/api/v1/runs"), 405, "invalid_parameter")
    assert client.get("/health/live").json() == {"status": "ok"}
    h = client.get("/health/ready").json()
    assert h["schema_version"] == max(f.stem for f in MIGRATIONS.glob("*.sql")) and h["latest_run"] is None
    rid = run(store_url, book)
    h = client.get("/health/ready").json()["latest_run"]
    assert h["run_id"] == rid and h["business_date"] == DATE and h["age_seconds"] >= 0
    tags = {}
    for path in ("", "/positions", "/pnl", "/aggregates?dims=book"):
        tag = tags[path] = client.get(f"/api/v1/runs/{rid}{path}").headers["ETag"]
        r2 = client.get(f"/api/v1/runs/{rid}{path}", headers={"If-None-Match": tag})
        assert r2.status_code == 304 and r2.headers["ETag"] == tag and not r2.content
    assert client.get(f"/api/v1/runs/{rid}/positions?book=OFZ").headers["ETag"] != tags["/positions"]
    down = TestClient(create_app("postgresql://desk@127.0.0.1:1/none"))
    assert_error(down.get("/health/ready"), 503, "store_unavailable")
    spec = client.get("/openapi.json").json()
    assert {"RunHeader", "RunList", "Position", "ErrorOut"} <= set(spec["components"]["schemas"])
    assert "compat" in spec["components"]["schemas"]["RunHeader"]["properties"]["as_of"]["description"]


def test_desk_data_js_without_runs_gives_the_empty_state_not_the_demo(client):
    r = client.get("/desk-data.js")
    assert r.status_code == 200 and "window.DESK_MISSING" in r.text and "window.DESK =" not in r.text
    assert 'window.DESK_EMPTY = {"reason": "no_runs", "message": "нет опубликованных прогонов"};' in r.text
    r = client.get("/desk-data.js?run_id=5")
    assert "не найден" in r.text and '"reason": "not_found"' in r.text
    assert "window.DESK" not in client.get("/desk-data.js?demo=1").text  # the demo only when asked for
    fallback = (STATIC / "desk-fallback.js").read_text(encoding="utf-8")
    assert "window.DESK_EMPTY" in fallback and "desk-empty.js" in fallback  # the loader honours it
    empty = (STATIC / "desk-empty.js").read_text(encoding="utf-8")
    assert "Прогонов ещё нет" in empty and "settings.html" in empty and "api/v1/autopilot/status" in empty
    assert client.get("/desk-empty.js").status_code == 200
    assert_error(client.get("/api/v1/compat/desk"), 404, "not_found")
    assert client.get("/desk-monitor.html").status_code == 200
