"""Exact (T2) scenario jobs in the desk store (S6 §4): submit, claim and run, read. All store SQL of the
scenario tables (``migrations/0002_scenarios.sql``) lives here. A job is keyed by (run, definition hash,
``METHOD``, core version): a resubmission of an equivalent definition by the same code gets the live or done
job, a failed one can be resubmitted. Bump ``exact.METHOD`` when the result changes for the same inputs.
Every function runs its own transactions: pass a connection from ``store.connect`` (autocommit)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from rub_fixed_income import __version__ as CORE_VERSION

from .. import __version__ as BACKEND_VERSION
from ..marketdata.bundle import SnapshotDir
from .definition import definition_hash, normalize
from .exact import METHOD, reprice_run

log = logging.getLogger("desk.scenario")
JOB_SQL = "SELECT j.*, r.result FROM scenario_job j LEFT JOIN scenario_result r USING (job_id)"
WORKER_LOCK = 0x6465736B5343454E  # pg advisory lock key ("deskSCEN"): the one worker of a store holds it


def _cur(con: psycopg.Connection) -> psycopg.Cursor:
    return con.cursor(row_factory=dict_row)


def submit(con: psycopg.Connection, base_run_id: int, definition: Mapping[str, Any]) -> tuple[dict, bool]:
    """(job row with "result", created). Raises LookupError when the run is missing, ValueError when it is
    not complete or the definition is invalid for it (DefinitionError)."""
    cur = _cur(con)
    run = cur.execute("SELECT status FROM run WHERE run_id = %s", (base_run_id,)).fetchone()
    if run is None:
        raise LookupError(f"run {base_run_id} not found")
    if run["status"] != "complete":
        raise ValueError(f"run {base_run_id} is {run['status']}, not complete")
    ref = cur.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'curves'",
                      (base_run_id,)).fetchone()
    norm = normalize(definition, [c["curve_id"] for c in ref["data"]] if ref else [])
    h = definition_hash(norm)
    for _ in range(2):  # the live job can fail between the conflict and the read: then insert again
        job = cur.execute(
            "INSERT INTO scenario_job (base_run_id, definition_hash, definition, method, core_version)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (base_run_id, definition_hash, method, core_version)"
            " WHERE status <> 'failed' DO NOTHING RETURNING *",
            (base_run_id, h, Jsonb(norm), METHOD, CORE_VERSION)).fetchone()
        if job:
            return {**job, "result": None}, True
        job = cur.execute(JOB_SQL + " WHERE j.base_run_id = %s AND j.definition_hash = %s AND j.method = %s"
                          " AND j.core_version = %s AND j.status <> 'failed'",
                          (base_run_id, h, METHOD, CORE_VERSION)).fetchone()
        if job:
            return job, False
    raise RuntimeError(f"scenario job of run {base_run_id}, {h[:12]} keeps failing")


def get(con: psycopg.Connection, job_id: int) -> dict | None:
    return _cur(con).execute(JOB_SQL + " WHERE j.job_id = %s", (job_id,)).fetchone()


def run_once(con: psycopg.Connection, snapshots: SnapshotDir, ctx_cache: dict | None = None) -> int | None:
    """Claim the oldest queued job of this code's method and core and run it: the result and status done in
    one transaction, any error gives failed with the reason. A job no longer running when it finishes (failed
    as orphaned meanwhile) keeps its status and gets no result. Returns the job id, None when none is queued."""
    cur = _cur(con)
    with con.transaction():
        job = cur.execute(
            "UPDATE scenario_job SET status = 'running', started_at = now(), backend_version = %s"
            " WHERE job_id = (SELECT job_id FROM scenario_job WHERE status = 'queued' AND method = %s"
            " AND core_version = %s ORDER BY job_id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
            (BACKEND_VERSION, METHOD, CORE_VERSION)).fetchone()
    if job is None:
        return None
    jid, rid = job["job_id"], job["base_run_id"]
    try:
        run = cur.execute("SELECT run_id, business_date, cut_type, market_snapshot_id,"
                          " (lineage->>'quote_curve_risk')::boolean AS quote_curve_risk,"
                          " lineage->>'ofz_scheme' AS ofz_scheme FROM run WHERE run_id = %s", (rid,)).fetchone()
        rows = cur.execute("SELECT position_id, book_id, secid, quantity, quality, pv_rub, fact"
                           " FROM run_position WHERE run_id = %s AND fact IS NOT NULL ORDER BY ord",
                           (rid,)).fetchall()
        ref = {r["kind"]: r["data"] for r in cur.execute(
            "SELECT kind, data FROM run_reference WHERE run_id = %s", (rid,))}
        result = reprice_run(snapshots, run, rows, ref, job["definition"], ctx_cache)
        with con.transaction():
            cur.execute("INSERT INTO scenario_result (job_id, base_run_id, definition_hash, result)"
                        " VALUES (%s, %s, %s, %s)", (jid, rid, job["definition_hash"], Jsonb(result)))
            if cur.execute("UPDATE scenario_job SET status = 'done', finished_at = now()"
                           " WHERE job_id = %s AND status = 'running'", (jid,)).rowcount != 1:
                raise RuntimeError(f"scenario job {jid} is no longer running")  # rolls the result back
    except Exception as e:
        log.exception("scenario job %s failed", jid)
        cur.execute("UPDATE scenario_job SET status = 'failed', finished_at = now(), error = %s"
                    " WHERE job_id = %s AND status = 'running'", (f"{type(e).__name__}: {e}", jid))
    return jid


def fail_orphans(con: psycopg.Connection) -> list[int]:
    """Jobs left running by a worker that died: failed, so they can be resubmitted. Only with ``WORKER_LOCK``."""
    return [r[0] for r in con.execute(
        "UPDATE scenario_job SET status = 'failed', finished_at = now(), error = 'orphaned'"
        " WHERE status = 'running' RETURNING job_id")]


class Worker:
    """One per store: it works only while it holds ``WORKER_LOCK`` on its connection (a second ``desk serve``
    stands by and takes over when the first dies), and fails the running jobs when it gets the lock.
    shortcut: a daemon thread inside ``desk serve``; core work holds the GIL and slows the API during a job:
    a ``desk worker`` process (03 §8)."""

    def __init__(self, store_url: str, snapshots: SnapshotDir, poll: float = 0.5) -> None:
        self.store_url, self.snapshots, self.poll = store_url, snapshots, poll
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="desk-scenario-worker", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        ctx_cache: dict = {}
        while not self._stop.is_set():
            try:
                with psycopg.connect(self.store_url, autocommit=True, connect_timeout=5) as con:
                    # session level: released when the connection or the process ends
                    while not con.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,)).fetchone()[0]:
                        if self._stop.wait(self.poll):
                            return
                    # also after a lost connection: a job it was running stays running otherwise
                    if failed := fail_orphans(con):
                        log.warning("scenario jobs orphaned: %s", failed)
                    while not self._stop.is_set():
                        if run_once(con, self.snapshots, ctx_cache) is None:
                            self._stop.wait(self.poll)
            except psycopg.Error as e:
                log.error("scenario worker: store unavailable, retrying in 5 s: %s", e)
                self._stop.wait(5)
