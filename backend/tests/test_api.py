"""Slice S5 desk API v1 and the window.DESK compatibility adapter, on runs of the golden fixtures written
into the desk store (store_rows.insert_run). The previous date is the synthetic one of test_slice_s2."""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from store_rows import insert_run
from tdm.assemble import assemble, resolve_run
from tdm.db import connect
from test_slice_s2 import prev_outcome, shifted_prev

from desk.api import create_app
from desk.marketdata.adapter import MarketView
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare, value
from desk.publish import tdm_run
from desk.store import MIGRATIONS

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
STATIC = Path(__file__).resolve().parents[2] / "prototypes"
DATE = "2026-10-15"
QTY = 1000.0
# Keys of window.DESK.source the parity test leaves out; everything else must be equal.
EXCLUDED = {"runId": "the two stores number their runs independently",
            "publishedAt": "publication time of each store's run",
            **dict.fromkeys(("cutType", "cutTs", "slice", "prevCutType", "pnlBasis"),
                            "the run's market slice (R11): the desk store's only")}
R10_RUN = {"pnlAvailable", "pnlReason", "pnlReasonText", "pnlCoverage", "riskNotes"}  # riskNotes: R14
"""window.DESK keys compat adds (R10: a missing PnL is said, not shown as 0); tdm's assembler has none."""
R10_PNL = {"available", "reason", "reasonText", "reasonDetail", "method", "methodText", "flag", "reported"}
"""Keys of a position's ``pnl`` compat adds."""


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


def test_compat_parity_with_tdm_assemble(store_url, book, client, tmp_path):
    """The same outcomes through tdm_run.publish into a tdm store and through insert_run into the desk
    store, on the synthetic previous date of S2 and on the fixture date: tdm.assemble.assemble on the first
    equals the compat adapter on the second, history included."""
    ctx, positions, view = book
    early = copy.copy(ctx.market)
    early.business_date = view.business_date
    early = dataclasses.replace(ctx, market=early)  # the same outcomes, one business date earlier
    db = tmp_path / "tdm.sqlite"
    assert tdm_run.publish(str(db), early, [p[:3] for p in positions], prev=None)[1] == "complete"
    insert_run(store_url, early, positions, None)
    tdm_id, status, _ = tdm_run.publish(str(db), ctx, [p[:3] for p in positions], prev=view)
    assert status == "complete"
    with psycopg.connect(store_url) as pg, connect(db) as con:  # the presets, as the tdm store would hold them
        con.executemany("INSERT INTO scenario_def (scenario_id, name, ord, definition_json, active, updated_at,"
                        " kind, description) VALUES (?, ?, ?, ?, ?, '-', ?, ?)",
                        [(i, n, o, json.dumps(d, ensure_ascii=False), int(a), k, desc) for i, n, o, d, a, k, desc in
                         pg.execute("SELECT scenario_id, name, ord, definition, active, kind, description"
                                    " FROM scenario_def")])
    con = connect(db, readonly=True)
    want = json.loads(json.dumps(assemble(con, resolve_run(con))))
    con.close()
    rid = run(store_url, book, cut_type="eod_preliminary")
    run(store_url, book, run_type="TEST")
    js = client.get("/desk-data.js").text
    assert js.startswith("window.DESK = ") and js.endswith(";\n")
    got = json.loads(js[len("window.DESK = "):-2])
    assert got == client.get(f"/api/v1/compat/desk?as_of={DATE}").json() == \
        client.get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert got["source"]["runId"] == rid and want["source"]["runId"] == tdm_id
    for key in EXCLUDED:
        got["source"].pop(key), want["source"].pop(key, None)
    assert set(got) - R10_RUN == set(want)
    assert got["pnlAvailable"] and got["pnlReason"] is None and got["pnlCoverage"]["withPnl"] > 0
    for k in want:
        if k != "positions":
            assert got[k] == want[k], k
    nv = [p for p in got["positions"] if p.get("notValued")]  # compat lists the unvalued position, tdm does not
    assert len(nv) == 1 and nv[0]["mvRub"] is None and nv[0]["extra"]["quality"] == "not_valued"
    assert nv[0]["extra"]["reason"] and not nv[0]["risk"] and nv[0]["dv01"] == 0 and got["positions"][-1] is nv[0]
    assert nv[0]["pnl"]["available"] is False and nv[0]["pnl"]["reason"] == "not_valued"
    assert nv[0]["pnl"]["explained"] is None and nv[0]["pnl"]["carry"] is None
    got["positions"] = got["positions"][:-1]
    assert [p["id"] for p in got["positions"]] == [p["id"] for p in want["positions"]]
    for a, b in zip(got["positions"], want["positions"], strict=True):  # tdm: a missing PnL as 0, compat: null
        if a["pnl"]["available"]:
            assert a["pnl"]["reported"] == 0.0 and a["pnl"]["method"]
        else:
            assert a["pnl"]["reasonText"] and all(a["pnl"][k] is None for k in b["pnl"])
            assert not any(b["pnl"][k] for k in b["pnl"])
            a["pnl"] = {**b["pnl"]}
        for k in R10_PNL & set(a["pnl"]):
            a["pnl"].pop(k)
    for a, b in zip(got["positions"], want["positions"], strict=True):
        if b.get("couponType") == "fix" and a.get("couponType") not in ("fix", "float"):
            a = {**a, "couponType": "fix"}  # tdm knows fix/float only; the desk store keeps the hub's type (D5)
        assert a == b,(a["id"], {f: (a.get(f), b.get(f)) for f in set(a) | set(b) if a.get(f) != b.get(f)})
    w = want["positions"]  # the comparison covered PnL, fallback rungs, gamma, bucket risk, reference data
    assert len(w) == len(positions) - 1  # the unvalued position is in neither
    assert any(p["pnl"]["explained"] for p in w) and any(not p["pnl"]["explained"] for p in w)
    assert {p["extra"]["quality"] for p in w} >= {"exact", "assumed", "market_value"}
    assert any(p["gamma"] for p in w) and any(p["risk"] for p in w)
    h = want["history"]
    assert [x["date"] for x in h] == [view.business_date.isoformat(), DATE] and h[0]["pnlOfficial"]
    assert h[1]["pnlCum"] == h[0]["pnlOfficial"] + h[1]["pnlOfficial"] and h[0]["dv01"] and h[0]["cs01"]
    assert "keyhike" in [x["id"] for x in want["SCENARIOS"]]
    assert want["CURVES"] and want["ISSUERS"] and len(want["FX"]) > 1 and want["INDICES"] and want["BOOKS"]
