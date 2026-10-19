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
from desk.marketdata.bundle import SnapshotDir
from desk.scenario import jobs
from desk.scenario.definition import definition_hash, normalize
from desk.settings import REPO_ROOT

book = test_api.book  # the module fixture: every fixture bond, valued
PRESETS = ["par+100", "par-100", "keyhike", "bullsteep", "bearflat", "ofzwide", "rubdep", "credit", "sector",
           "stress22"]
KEYHIKE = {"curves": {"RUB OFZ": [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35]}, "ofzSpread": 10,
           "spreads": {"_all": 25}}


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
    assert kh["definition"] == normalize(KEYHIKE)
    assert next(s for s in sc if s["scenario_id"] == "ofzwide")["definition"] == {"ofzSpread": 40.0}


def test_risk_payload_and_compat_scenarios(store_url, book, client):
    rid = run(store_url, book)
    r = client.get(f"/api/v1/runs/{rid}/risk")
    assert r.status_code == 200
    p = r.json()
    ps = client.get(f"/api/v1/runs/{rid}/positions").json()["positions"]
    assert p["run_id"] == rid and len(p["buckets"]) == 11 and p["curves"] == ["RUB OFZ"]
    assert p["factors"][0] == "IR|RUB OFZ|1M" and "CS" in p["factors"]
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
    assert kh == {"id": "keyhike", "name": kh["name"], "kind": "preset", "description": None, **normalize(KEYHIKE)}
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
            "curves": {"RUB OFZ": KEYHIKE["curves"]["RUB OFZ"]}}
    r2 = client.post("/api/v1/scenario-jobs", json={"base_run_id": rid, "definition": same})
    assert r2.status_code == 200 and r2.json() == done
    # a failed job (the snapshots are missing) can be resubmitted: a new job
    par = {"curves": {"RUB OFZ": 1}}
    j1 = client.post("/api/v1/scenario-jobs", json={"base_run_id": rid, "definition": par}).json()
    with store.connect(store_url) as con:
        assert jobs.run_once(con, SnapshotDir(FIX / "nothing-here")) == j1["job_id"]
    failed = client.get(f"/api/v1/scenario-jobs/{j1['job_id']}").json()
    assert failed["status"] == "failed" and failed["error"] and failed["result"] is None
    r3 = client.post("/api/v1/scenario-jobs", json={"base_run_id": rid, "definition": par})
    assert r3.status_code == 202 and r3.json()["job_id"] != j1["job_id"]


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
    assert "not published" in assert_error(post({"curves": {"RUB KEY": 50}}), 422, "invalid_parameter")["message"]
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
    assert "scenario lab" in c.get("/app/").text and c.get("/desk-monitor.html").status_code == 200
    r = c.get("/app?run_id=5", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app/?run_id=5"
    committed = json.loads((REPO_ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8"))
    assert committed == create_app("postgresql://unused").openapi(), \
        "frontend/openapi.json is stale: desk openapi --out ../frontend/openapi.json"
