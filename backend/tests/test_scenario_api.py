"""Slice S6 scenario store, jobs and API: presets, the T1 risk payload, exact (T2) jobs end to end on a run
of the golden fixtures (POST, jobs.run_once on the fixture snapshots, GET), the worker thread, errors,
append-only results and the committed OpenAPI document."""

from __future__ import annotations

import json
import time

import psycopg
import pytest
import test_api
from fastapi.testclient import TestClient
from test_api import FIX, assert_error, run

from desk import store
from desk.api import create_app
from desk.autopilot import Autopilot
from desk.marketdata.bundle import SnapshotDir
from desk.scenario import jobs
from desk.scenario.definition import RUB_CURVES, definition_hash, normalize
from desk.settings import REPO_ROOT

book = test_api.book  # the module fixture: every fixture bond, valued
PRESETS = ["par+100", "par-100", "keyhike", "bullsteep", "bearflat", "basis", "ofzwide", "rubdep", "credit",
           "sector", "stress22"]
KEYHIKE = {"curves": {"RUB RUONIA": [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35]}, "ofzSpread": 10,
           "spreads": {"_all": 25}}
KEYHIKE_PRESET = {**KEYHIKE, "curves": dict.fromkeys(RUB_CURVES, KEYHIKE["curves"]["RUB RUONIA"])}
"""R8: a move of all rates shifts the three RUB curves alike."""


@pytest.fixture
def client(store_url) -> TestClient:
    return TestClient(create_app(store_url))


def test_presets_listed_normalized(client):
    r = client.get("/api/v1/scenario-defs")
    assert r.status_code == 200
    sc = r.json()["scenarios"]
    assert [s["scenario_id"] for s in sc] == PRESETS
    for s in sc:  # seeded normalized: the store's definition is what normalize gives, and hashes the same
        assert s["kind"] == "preset" and s["description"] is None
        assert normalize(s["definition"]) == s["definition"]
        assert s["definition_hash"] == definition_hash(normalize(s["definition"]))
    kh = next(s for s in sc if s["scenario_id"] == "keyhike")
    assert kh["definition"] == normalize(KEYHIKE_PRESET)
    assert next(s for s in sc if s["scenario_id"] == "ofzwide")["definition"] == {"ofzSpread": 40.0}
    for s in sc:  # every curve preset moves the three RUB curves, except the basis (the key rate only)
        assert set(s["definition"].get("curves", RUB_CURVES)) == ({"RUB KEY"} if s["scenario_id"] == "basis"
                                                                  else set(RUB_CURVES))


def test_risk_payload_and_compat_scenarios(store_url, book, client):
    rid = run(store_url, book)
    r = client.get(f"/api/v1/runs/{rid}/risk")
    assert r.status_code == 200
    p = r.json()
    ps = client.get(f"/api/v1/runs/{rid}/positions").json()["positions"]
    assert p["run_id"] == rid and len(p["buckets"]) == 11
    assert p["curves"] == ["RUB KEY", "RUB OFZ", "RUB RUONIA", "USD"]  # the floaters' and the USD bond's too
    # every curve carries risk: the fixed OFZ on the KBD (RUB OFZ, #32), the rest on RUONIA, KEY and USD
    assert {f.split("|")[1] for f in p["factors"] if f.startswith("IR|")} == set(p["curves"])
    assert "CS" in p["factors"]
    assert [x["position_id"] for x in p["positions"]] == [x["position_id"] for x in ps if x["fact"]]
    n = len(p["factors"])
    assert all(0 <= i < n for x in p["positions"] for i, _ in x["delta"])
    assert all(0 <= i <= j < n for x in p["positions"] for i, j, _ in x["gamma"])
    assert any(x["gamma"] for x in p["positions"]) and {x["spread_kind"] for x in p["positions"]} >= {"ofz", "credit"}
    tag = r.headers["ETag"]
    assert client.get(f"/api/v1/runs/{rid}/risk", headers={"If-None-Match": tag}).status_code == 304
    assert_error(client.get("/api/v1/runs/999999/risk"), 404, "not_found")
    sc = client.get(f"/api/v1/compat/desk?run_id={rid}").json()["SCENARIOS"]
    kh = next(s for s in sc if s["id"] == "keyhike")
    assert kh == {"id": "keyhike", "name": kh["name"], "kind": "preset", "description": None,
                  **normalize(KEYHIKE_PRESET)}
    assert [s["id"] for s in sc] == PRESETS


def test_job_end_to_end(store_url, book, client):
    rid = run(store_url, book)
    r = client.post("/api/v1/scenario-jobs", json={"base_run_id": rid, "definition": KEYHIKE})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["result"] is None and job["base_run_id"] == rid
    assert job["definition"] == normalize(KEYHIKE) and job["definition_hash"] == definition_hash(normalize(KEYHIKE))
    with store.connect(store_url) as con:
        assert jobs.run_once(con, SnapshotDir(FIX)) == job["job_id"]
        assert jobs.run_once(con, SnapshotDir(FIX)) is None
    done = client.get(f"/api/v1/scenario-jobs/{job['job_id']}").json()
    assert done["status"] == "done" and done["error"] is None and done["finished_at"] >= done["started_at"]
    res = done["result"]
    assert res["definition_hash"] == job["definition_hash"] and res["coverage"]["status"] == "partial"
    assert 0 < res["coverage"]["n_exact"] < res["coverage"]["n_positions"]
    assert not any(m["reason"] == "base_mismatch" for m in res["coverage"]["missing"])
    t = res["totals"]["_total"]
    assert t["exact"] is not None and t["residual"] == pytest.approx(t["exact"] - t["taylor_covered"])
    assert set(res["totals"]) == {"_total", "OFZ", "CORP"}
    # an equivalent definition (scalar-free, reordered, zeros) is the same job, now with its result
    same = {"fx": {"USD": 0}, "spreads": {"_all": 25, "_mult": 1, "sector": {}}, "ofzSpread": 10,
            "curves": {"RUB RUONIA": KEYHIKE["curves"]["RUB RUONIA"]}}
    r2 = client.post("/api/v1/scenario-jobs", json={"base_run_id": rid, "definition": same})
    assert r2.status_code == 200 and r2.json() == done
    # a failed job (the snapshots are missing) can be resubmitted: a new job
    par = {"curves": {"RUB RUONIA": 1}}
    j1 = client.post("/api/v1/scenario-jobs", json={"base_run_id": rid, "definition": par}).json()
    with store.connect(store_url) as con:
        assert jobs.run_once(con, SnapshotDir(FIX / "nothing-here")) == j1["job_id"]
    failed = client.get(f"/api/v1/scenario-jobs/{j1['job_id']}").json()
    assert failed["status"] == "failed" and failed["error"] and failed["result"] is None
    r3 = client.post("/api/v1/scenario-jobs", json={"base_run_id": rid, "definition": par})
    assert r3.status_code == 202 and r3.json()["job_id"] != j1["job_id"]


def test_every_active_scenario_queued_once_per_run_and_its_results(store_url, book, client):
    """TDM#71: the autopilot's scenarios step queues every active scenario on the newest default run once; the run's
    scenario results list each with its latest job, the totals without the positions."""
    old = run(store_url, book)
    rid = run(store_url, book)
    with store.connect(store_url) as con:
        assert jobs.latest_default_run(con)[0] == rid
        con.execute("INSERT INTO scenario_def (scenario_id, name, kind, ord, definition) VALUES"
                    " ('cnyonly', 'CNY curve', 'custom', 10000, %s)", (json.dumps({"curves": {"CNY XCCY": 50}}),))
        first = jobs.submit_all(con, rid)
        assert first["queued"] == PRESETS and first["reused"] == 0
        assert list(first["invalid"]) == ["cnyonly"] and "not published by the run" in first["invalid"]["cnyonly"]
        again = jobs.submit_all(con, rid)
        assert again == {"queued": [], "reused": len(PRESETS), "invalid": first["invalid"]}
        status, detail = Autopilot(settings_loader=lambda: None)._scenarios(con)  # the cycle's step: nothing new
        assert status == "ok" and detail.startswith(f"run {rid} (") and f"0 queued, {len(PRESETS)} already" in detail
        assert "not repriced: cnyonly: " in detail
        assert jobs.run_once(con, SnapshotDir(FIX)) is not None  # the first preset in order: par+100
    body = client.get(f"/api/v1/runs/{rid}/scenario-results").json()
    assert body["run_id"] == rid and body["method"] == jobs.METHOD
    sc = {s["scenario_id"]: s for s in body["scenarios"]}
    assert list(sc) == [*PRESETS, "cnyonly"] and sc["cnyonly"]["job"] is None
    done = sc["par+100"]["job"]
    assert done["status"] == "done" and set(done["totals"]) == {"_total", "OFZ", "CORP"}
    assert set(done["coverage"]) == {"status", "n_positions", "n_exact"}  # no missing list
    assert done["totals"]["_total"]["n_positions"] == done["coverage"]["n_positions"]
    assert all(s["job"]["status"] == "queued" and s["job"]["totals"] is None for k, s in sc.items()
               if k not in ("par+100", "cnyonly"))
    defs = {d["scenario_id"]: d for d in client.get("/api/v1/scenario-defs").json()["scenarios"]}
    assert all(s["definition_hash"] == defs[k]["definition_hash"] for k, s in sc.items() if k in defs)
    # another run has none of them; a missing run is 404
    assert all(s["job"] is None for s in client.get(f"/api/v1/runs/{old}/scenario-results").json()["scenarios"])
    assert_error(client.get("/api/v1/runs/999999/scenario-results"), 404, "not_found")


def test_scenario_results_prefer_a_live_job_to_a_failed_one(store_url, book, client):
    rid = run(store_url, book)
    par = client.get("/api/v1/scenario-defs").json()["scenarios"][0]
    with store.connect(store_url) as con:
        job, _ = jobs.submit(con, rid, par["definition"])
        assert jobs.run_once(con, SnapshotDir(FIX / "nothing-here")) == job["job_id"]  # failed: no snapshots
        assert jobs.submit_all(con, rid)["queued"] == PRESETS  # the failed one queued again
    got = next(s for s in client.get(f"/api/v1/runs/{rid}/scenario-results").json()["scenarios"]
               if s["scenario_id"] == par["scenario_id"])["job"]
    assert got["status"] == "queued" and got["job_id"] > job["job_id"]


def test_worker_runs_jobs_and_fails_orphans(store_url, book, client):
    rid = run(store_url, book)
    with store.connect(store_url) as con:
        orphan, _ = jobs.submit(con, rid, {"ofzSpread": 5})
        con.execute("UPDATE scenario_job SET status = 'running' WHERE job_id = %s", (orphan["job_id"],))
    job = client.post("/api/v1/scenario-jobs", json={"base_run_id": rid, "definition": {"ofzSpread": 7}}).json()
    w = jobs.Worker(store_url, SnapshotDir(FIX), poll=0.05)
    other = store.connect(store_url)  # another live worker: this one stands by, its running job is not orphaned
    assert other.execute("SELECT pg_try_advisory_lock(%s)", (jobs.WORKER_LOCK,)).fetchone()[0]
    w.start()
    try:
        time.sleep(0.5)
        assert [jobs.get(other, j["job_id"])["status"] for j in (orphan, job)] == ["running", "queued"]
        other.close()  # the other worker dies: this one takes over
        deadline = time.monotonic() + 120
        while (got := client.get(f"/api/v1/scenario-jobs/{job['job_id']}").json())["status"] in ("queued", "running"):
            assert time.monotonic() < deadline, got
            time.sleep(0.1)
    finally:
        w.stop()
        other.close()
    assert got["status"] == "done" and got["result"]["coverage"]["n_exact"] > 0
    o = client.get(f"/api/v1/scenario-jobs/{orphan['job_id']}").json()
    assert o["status"] == "failed" and o["error"] == "orphaned"


def test_job_failed_while_running_keeps_its_status(store_url, book, monkeypatch):
    rid = run(store_url, book)

    def orphaned_meanwhile(*a, **k):  # a second worker fails it as orphaned while it runs
        with store.connect(store_url) as c:
            assert jobs.fail_orphans(c) == [job["job_id"]]
        return {}

    monkeypatch.setattr(jobs, "reprice_run", orphaned_meanwhile)
    with store.connect(store_url) as con:
        job, _ = jobs.submit(con, rid, {"ofzSpread": 3})
        assert jobs.run_once(con, SnapshotDir(FIX)) == job["job_id"]
        got = jobs.get(con, job["job_id"])
    assert got["status"] == "failed" and got["error"] == "orphaned" and got["result"] is None


def test_job_cache_keyed_by_method_and_core(store_url, book):
    rid = run(store_url, book)
    with store.connect(store_url) as con:
        old, _ = jobs.submit(con, rid, {"ofzSpread": 4})
        con.execute("UPDATE scenario_job SET status = 'done', core_version = '0.0.1' WHERE job_id = %s",
                    (old["job_id"],))  # done by an older core
        new, created = jobs.submit(con, rid, {"ofzSpread": 4})
        assert created and new["job_id"] != old["job_id"] and new["method"] == jobs.METHOD
        con.execute("UPDATE scenario_job SET method = 'desk-scenario-exact-v0' WHERE job_id = %s", (new["job_id"],))
        assert jobs.run_once(con, SnapshotDir(FIX)) is None  # not this code's method: left to its own worker


def test_job_errors(store_url, book, client):
    rid = run(store_url, book)
    def post(d, run_id=rid):
        return client.post("/api/v1/scenario-jobs", json={"base_run_id": run_id, "definition": d})

    assert_error(post({"ofzSpread": 1}, 999999), 404, "not_found")
    assert_error(client.get("/api/v1/scenario-jobs/999999"), 404, "not_found")
    assert_error(post({"basis": 1}), 422, "invalid_parameter")  # unknown key
    assert_error(post({"spreads": {"_avg": 1}}), 422, "invalid_parameter")
    assert_error(post({"spreads": {"all": 1, "_all": 2}}), 422, "invalid_parameter")  # the alias only
    assert_error(post({"curves": {"RUB OFZ": [1, 2, 3]}}), 422, "invalid_parameter")
    assert "not published" in assert_error(post({"curves": {"EUR": 50}}), 422, "invalid_parameter")["message"]
    assert_error(post({"ofzSpread": 6000}), 422, "invalid_parameter")
    assert_error(post({"fx": {"USD": -100}}), 422, "invalid_parameter")
    assert_error(post({"spreads": {"_mult": 0}}), 422, "invalid_parameter")
    r = client.post("/api/v1/scenario-jobs", content=f'{{"base_run_id": {rid}, "definition": {{"ofzSpread": NaN}}}}',
                    headers={"Content-Type": "application/json"})
    assert_error(r, 422, "invalid_parameter")
    with store.connect(store_url) as con:
        loading = con.execute(
            "INSERT INTO run (run_type, business_date, cut_type, market_snapshot_id, static_snapshot_id,"
            " private_inputs_hash, position_set_ids, positions_known_at, core_version, backend_version, lineage)"
            " VALUES ('EOD', '2026-10-15', 'eod_final', 'm', 's', 'h', '{}', now(), 'c', 'b', '{}')"
            " RETURNING run_id").fetchone()[0]
    assert "not complete" in assert_error(post({"ofzSpread": 1}, loading), 422, "invalid_parameter")["message"]


def test_results_append_only_jobs_kept(store_url, book):
    rid = run(store_url, book)
    with store.connect(store_url) as con:
        job, created = jobs.submit(con, rid, {"ofzSpread": 1})
        assert created and jobs.submit(con, rid, {"ofzSpread": 1.0}) == (job, False)
        con.execute("INSERT INTO scenario_result VALUES (%s, %s, %s, '{}')", (job["job_id"], rid, job["definition_hash"]))
        for sql in ("UPDATE scenario_result SET result = '[]'", "DELETE FROM scenario_result",
                    "TRUNCATE scenario_result", "DELETE FROM scenario_job", "TRUNCATE scenario_job CASCADE"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                con.execute(sql)
        con.execute("UPDATE scenario_job SET status = 'done'")  # status changes are allowed


def test_app_mount_and_openapi_is_current(store_url, tmp_path):
    (tmp_path / "index.html").write_text("<p>scenario lab</p>", encoding="utf-8")
    c = TestClient(create_app(store_url, app_dir=tmp_path))
    assert "scenario lab" in c.get("/app/").text and c.get("/scenario-lab.html").status_code == 200
    r = c.get("/app?run_id=5", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app/?run_id=5"
    # the removed early screens (#292): their addresses lead to the monitors for one release, run selection kept
    for old, new in (("desk-monitor", "c01-cockpit"), ("widget-board", "c06-launchpad"), ("pivot-studio", "c04-pivot")):
        r = c.get(f"/{old}.html?run_id=5", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"/concepts/{new}.html?run_id=5", old
        assert c.get(r.headers["location"]).status_code == 200, new
        assert c.get(f"/{old}.html", follow_redirects=False).headers["location"] == f"/concepts/{new}.html"
        r = c.head(f"/{old}.html", follow_redirects=False)  # HEAD too, not the static files' 404
        assert r.status_code == 302 and r.headers["location"] == f"/concepts/{new}.html", old
    committed = json.loads((REPO_ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8"))
    assert committed == create_app("postgresql://unused").openapi(), \
        "frontend/openapi.json is stale: desk openapi --out ../frontend/openapi.json"
