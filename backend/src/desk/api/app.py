"""Desk API v1 (05 §2, the S5 subset) over the desk store, read-only but for scenario job submission (S6),
plus the window.DESK compatibility adapter (05 §4), the S6 app at /app/ and the prototypes as static files.

shortcut: no authentication in S5; the reverse proxy authenticates (05 §6), bind 127.0.0.1 until then.
shortcut: one psycopg connection per request, no pool; fine for a few users, pool it if latency shows."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from starlette.exceptions import HTTPException

from .. import __version__
from ..marketdata import history
from ..scenario import jobs
from ..scenario.definition import definition_hash, normalize
from ..scenario.taylor import risk_payload
from ..settings import REPO_ROOT
from . import compat, concepts
from .autopilot_routes import router as autopilot_router
from .models import (
    COMPAT,
    AggregateRow,
    Aggregates,
    BondHistory,
    ErrorOut,
    JobIn,
    PnlSums,
    Position,
    PositionList,
    PositionPnl,
    Ready,
    RiskPayload,
    RunDetail,
    RunHeader,
    RunList,
    RunPnl,
    RunStatus,
    RunType,
    ScenarioDefList,
    ScenarioJob,
)

log = logging.getLogger("desk.api")


def static_root() -> Path:
    """Where ``prototypes/`` and ``frontend/dist/`` are: ``DESK_STATIC_ROOT`` (the Windows bundle's ``app\\``, set
    by start.bat) or the repository root (a checkout; an installed wheel without it serves no pages)."""
    return Path(os.environ.get("DESK_STATIC_ROOT") or REPO_ROOT)


REQUEST_ID = re.compile(r"[\w.:-]{1,128}")
HEADER_SQL = (f"SELECT r.*, r.run_id IN ({compat.DEFAULT_RUNS}) AS is_default,"
              f" r.run_id IN ({compat.CURRENT_RUNS}) AS is_current FROM run r")
DIMS = {"book": "book_id", "quality": "quality", "asset_class": "fact->>'asset_class'",
        "segment": "fact->>'segment'", "issuer": "fact->>'issuer'", "ccy": "fact->>'ccy'"}
PNL_KEYS = compat.PNL_KEYS
JS = "text/javascript; charset=utf-8"
NO_STORE = {"Cache-Control": "no-store"}


class Revalidated(StaticFiles):
    """The pages and scripts with ``Cache-Control: no-cache``: the browser asks each time (a 304 when unchanged), so
    an update takes effect on the next load. Without it the browser may keep a page it guesses fresh from its age and
    run the pre-update code on the fresh data (``desk-data.js`` is no-store): R16, the coupon «null»."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _error(request: Request, status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    rid = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    body = ErrorOut(code=code, message=message, request_id=rid,  # type: ignore[arg-type]
                    details=jsonable_encoder(details))
    return JSONResponse(body.model_dump(), status_code=status, headers={"X-Request-ID": rid})


def _header(row: dict[str, Any]) -> dict[str, Any]:
    basis = (row.get("lineage") or {}).get("pnl_basis") or {}
    return {**row, "as_of": row["business_date"], "published_at": row["completed_at"],
            "label": compat.slice_label(row["business_date"], row["cut_type"], row.get("cut_ts"), row["run_type"]),
            "pnl_basis": basis.get("label")}


def _run_or_404(con, run_id: int) -> dict[str, Any]:
    row = con.execute(HEADER_SQL + " WHERE r.run_id = %s", (run_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"run {run_id} not found")
    return row


def _cached(request: Request, run: dict[str, Any], model) -> Response:
    """A complete run never changes, but its header's is_default can, so the ETag is the body's hash."""
    body = model.model_dump_json().encode()
    if run["status"] != "complete":
        return Response(body, media_type="application/json")
    tag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"ETag": tag, "Cache-Control": "no-cache"}
    if tag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


def _total(components: dict[str, Any] | None) -> float | None:
    return sum(components.get(k) or 0.0 for k in PNL_KEYS) if components else None


def db(request: Request) -> Iterator[psycopg.Connection]:
    with psycopg.connect(request.app.state.store_url, row_factory=dict_row, autocommit=True,
                         connect_timeout=5, options="-c default_transaction_read_only=on") as con:
        yield con


def db_rw(request: Request) -> Iterator[psycopg.Connection]:
    """Read-write: only for submitting scenario jobs."""
    with psycopg.connect(request.app.state.store_url, row_factory=dict_row, autocommit=True,
                         connect_timeout=5) as con:
        yield con


Con = Annotated[psycopg.Connection, Depends(db)]
ConRW = Annotated[psycopg.Connection, Depends(db_rw)]


def create_app(store_url: str, static_dir: Path | None = None, app_dir: Path | None = None,
               adapter_status: Callable[[], str] | None = None) -> FastAPI:
    """No scenario worker here (``serve`` starts one): jobs submitted to this app stay queued.
    ``adapter_status``: the private adapter's ``ok <name> <version> <fp>`` or ``error <reason>`` for
    ``/health/ready`` (``desk.inbox.scan.adapter_line``, loaded once per process); None: not reported."""
    app = FastAPI(title="Desk API", version=__version__, description="Desk API v1 (docs/architecture/05 §2)",
                  responses={404: {"model": ErrorOut}, 422: {"model": ErrorOut}, 503: {"model": ErrorOut}})

    app.state.store_url = store_url
    app.state.started_at = datetime.now(UTC)  # the diagnostics' uptime (R13)
    # window.DESK and the concept pages' series of a large book are megabytes of JSON: compressed on the wire
    app.add_middleware(GZipMiddleware, minimum_size=4096)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        h = request.headers.get("x-request-id")
        rid = h if h and REQUEST_ID.fullmatch(h) else uuid.uuid4().hex
        request.state.request_id = rid
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        log.info("%s %s %s %s %.0fms", rid, request.method, request.url.path, response.status_code,
                 (time.perf_counter() - t0) * 1e3)
        return response

    @app.exception_handler(ApiError)
    def api_error(request: Request, exc: ApiError):
        return _error(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        code = ("not_found" if exc.status_code == 404 else "internal" if exc.status_code >= 500
                else "invalid_parameter")
        return _error(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    def validation_error(request: Request, exc: RequestValidationError):
        # without the input: the client sent it, and a NaN in it would break the JSON of the error
        errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
        return _error(request, 422, "invalid_parameter", "invalid request parameters", errors)

    @app.exception_handler(psycopg.OperationalError)
    def store_error(request: Request, exc: psycopg.OperationalError):
        log.error("store unavailable: %s", exc)
        return _error(request, 503, "store_unavailable", "desk store unavailable")

    @app.exception_handler(Exception)
    def internal_error(request: Request, exc: Exception):
        log.exception("internal error", exc_info=exc)
        return _error(request, 500, "internal", "internal error")

    # ---- health
    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", response_model=Ready)
    def ready(con: Con):
        ver = con.execute("SELECT max(version) AS v FROM schema_migration").fetchone()["v"]
        last = con.execute("SELECT run_id, business_date, completed_at,"
                           " extract(epoch FROM now() - completed_at)::float8 AS age_seconds FROM run"
                           " WHERE status = 'complete' ORDER BY completed_at DESC LIMIT 1").fetchone()
        return {"status": "ok", "schema_version": ver, "latest_run": last,
                "adapter": adapter_status() if adapter_status else None}

    # ---- runs
    @app.get("/api/v1/runs", response_model=RunList)
    def runs(con: Con, business_date: date | None = None, run_type: RunType | None = None,
             status: RunStatus | None = None, limit: int = Query(50, ge=1, le=500),
             cursor: int | None = Query(None, description="next_cursor of the previous page")):
        where, args = [], []
        for sql, v in (("r.business_date = %s", business_date), ("r.run_type = %s", run_type),
                       ("r.status = %s", status), ("r.run_id < %s", cursor)):
            if v is not None:
                where.append(sql)
                args.append(v)
        rows = con.execute(HEADER_SQL + (" WHERE " + " AND ".join(where) if where else "")
                           + " ORDER BY r.run_id DESC LIMIT %s", (*args, limit + 1)).fetchall()
        return {"runs": [_header(r) for r in rows[:limit]],
                "next_cursor": rows[limit - 1]["run_id"] if len(rows) > limit else None}

    @app.get("/api/v1/runs/default", response_model=RunHeader,
             description="The run shown (R11): of business_date, its default run, otherwise its latest intraday "
                         "run; without business_date, the newest such run (today's intraday run when there is one)")
    def default_run(con: Con, business_date: date | None = None):
        sql = HEADER_SQL + f" WHERE r.run_id IN ({compat.CURRENT_RUNS})"
        if business_date:
            row = con.execute(sql + " AND r.business_date = %s", (business_date,)).fetchone()
        else:
            row = con.execute(sql + " ORDER BY r.business_date DESC LIMIT 1").fetchone()
        if row is None:
            raise ApiError(404, "not_found", f"no default run on {business_date}" if business_date
                           else "no default run")
        return _header(row)

    @app.get("/api/v1/runs/{run_id}", response_model=RunDetail)
    def run_detail(con: Con, run_id: int, request: Request):
        run = _run_or_404(con, run_id)
        lineage = {**run["lineage"], **{k: run[k] for k in (
            "prev_market_snapshot_id", "private_inputs_hash", "position_set_ids", "positions_known_at",
            "core_version", "backend_version", "backend_git_sha")}}
        lines = con.execute("SELECT seq, level, message FROM run_log WHERE run_id = %s ORDER BY seq",
                            (run_id,)).fetchall()
        detail = RunDetail(**{**_header(run), "lineage": jsonable_encoder(lineage)}, log=lines)
        return _cached(request, run, detail)

    @app.get("/api/v1/runs/{run_id}/positions", response_model=PositionList)
    def positions(con: Con, run_id: int, request: Request, book: str | None = None,
                  quality: str | None = Query(None, description="exact, assumed, market_value, ...")):
        run = _run_or_404(con, run_id)
        sql, args = "SELECT * FROM run_position WHERE run_id = %s", [run_id]
        for col, v in (("book_id", book), ("quality", quality)):
            if v is not None:
                sql += f" AND {col} = %s"
                args.append(v)
        rows = con.execute(sql + " ORDER BY ord", args).fetchall()  # whole: a book is <= 2k positions
        return _cached(request, run, PositionList(run_id=run_id, positions=[Position(**r) for r in rows]))

    @app.get("/api/v1/runs/{run_id}/pnl", response_model=RunPnl)
    def pnl(con: Con, run_id: int, request: Request):
        run = _run_or_404(con, run_id)
        rows = con.execute("SELECT position_id, book_id, pnl, no_pnl FROM run_position WHERE run_id = %s"
                           " AND quality IS NOT NULL ORDER BY ord", (run_id,)).fetchall()
        out, totals = [], {}
        for r in rows:
            comp = {k: v for k, v in r["pnl"].items() if k != "extra"} if r["pnl"] else None
            out.append(PositionPnl(position_id=r["position_id"], book_id=r["book_id"], components=comp,
                                   total=_total(comp), extra=(r["pnl"] or {}).get("extra"),
                                   no_pnl=r["no_pnl"]))
            for key in (r["book_id"], "_total"):
                t = totals.setdefault(key, {**dict.fromkeys((*PNL_KEYS, "total")), "n_positions": 0, "n_pnl": 0})
                t["n_positions"] += 1
                if comp:  # sums stay null, not 0, while no position of the key has a PnL
                    t["n_pnl"] += 1
                    for k, x in {**{k: comp.get(k) or 0.0 for k in PNL_KEYS}, "total": _total(comp)}.items():
                        t[k] = (t[k] or 0.0) + x
        return _cached(request, run, RunPnl(run_id=run_id, positions=out,
                                            totals={k: PnlSums(**v) for k, v in totals.items()}))

    @app.get("/api/v1/runs/{run_id}/aggregates", response_model=Aggregates)
    def aggregates(con: Con, run_id: int, request: Request,
                   dims: str = Query("book", description="Comma-separated: " + ", ".join(DIMS))):
        names = [d.strip() for d in dims.split(",") if d.strip()]
        bad = [d for d in names if d not in DIMS]
        if bad or not names:
            raise ApiError(422, "invalid_parameter", f"unknown dims: {', '.join(bad) or '(none)'}",
                           {"allowed": list(DIMS)})
        names = list(dict.fromkeys(names))
        run = _run_or_404(con, run_id)
        cols = [DIMS[d] for d in names]  # whitelisted: safe to format into the SQL
        sums = ", ".join(f"sum((pnl->>'{k}')::float8) AS {k}" for k in PNL_KEYS)
        rows = con.execute(
            f"SELECT {', '.join(f'{c} AS k{i}' for i, c in enumerate(cols))}, count(*) AS n_positions,"
            f" count(fact) AS n_valued, count(pnl) AS n_pnl, sum(pv_rub) AS pv_rub,"
            f" sum(dv01_rub) AS dv01_rub, sum(cs01_rub) AS cs01_rub, {sums}"
            f" FROM run_position WHERE run_id = %s"
            f" GROUP BY {', '.join(cols)} ORDER BY {', '.join(cols)}", (run_id,)).fetchall()
        out = []
        for r in rows:
            p = {k: r[k] for k in PNL_KEYS}
            have = [v for v in p.values() if v is not None]
            out.append(AggregateRow(key={d: r[f"k{i}"] for i, d in enumerate(names)},
                                    **{k: r[k] for k in ("n_positions", "n_valued", "n_pnl", "pv_rub",
                                                         "dv01_rub", "cs01_rub")},
                                    pnl=p | {"total": sum(have) if have else None}))
        return _cached(request, run, Aggregates(run_id=run_id, dims=names, rows=out))

    @app.get("/api/v1/runs/{run_id}/risk", response_model=RiskPayload)
    def risk(con: Con, run_id: int, request: Request):
        run = _run_or_404(con, run_id)
        rows = con.execute("SELECT * FROM run_position WHERE run_id = %s AND fact IS NOT NULL ORDER BY ord",
                           (run_id,)).fetchall()
        ref = {r["kind"]: r["data"] for r in con.execute("SELECT kind, data FROM run_reference WHERE run_id = %s",
                                                         (run_id,))}
        return _cached(request, run, RiskPayload(**risk_payload(run, rows, ref)))

    # ---- bonds
    @app.get("/api/v1/bonds/history", response_model=BondHistory)
    def bond_history(con: Con, instrument_id: str | None = None, isin: str | None = None,
                     curve: str | None = None, wal: float | None = None,
                     from_: Annotated[date | None, Query(alias="from")] = None, to: date | None = None):
        """The issue's quoted days from the market history (the hub's history API, else the cached hub snapshots) merged
        with the default runs that hold it (Z-spread, the desk's face), and the KBD rate at its term with the spread
        to it (``desk.marketdata.history.bond_history``). ``instrument_id`` is the MOEX secid (or the report's id),
        ``isin`` finds the bond through the static snapshots; ``curve``: the KBD (RUB OFZ; own, base) has rates,
        another curve gives none; ``wal`` is accepted for the contract and not used yet."""
        if not (instrument_id or isin):
            raise ApiError(422, "invalid_parameter", "instrument_id or isin is required")
        return BondHistory(**history.bond_history(con, instrument_id=instrument_id, isin=isin, curve=curve,
                                                  from_=from_, to=to))

    # ---- scenarios (S6)
    @app.get("/api/v1/scenario-defs", response_model=ScenarioDefList, response_model_exclude_unset=True)
    def scenario_defs(con: Con):
        rows = con.execute("SELECT scenario_id, name, kind, description, ord, definition FROM scenario_def"
                           " WHERE active ORDER BY ord, scenario_id").fetchall()
        return {"scenarios": [{**r, "definition_hash": definition_hash(normalize(r["definition"]))}
                              for r in rows]}

    @app.post("/api/v1/scenario-jobs", response_model=ScenarioJob, response_model_exclude_unset=True,
              status_code=status.HTTP_202_ACCEPTED,
              description="202: a new job, queued; 200: the live or done job of an equivalent definition")
    def submit_job(con: ConRW, body: JobIn, response: Response):
        try:
            job, created = jobs.submit(con, body.base_run_id,
                                       body.definition.model_dump(by_alias=True, exclude_none=True))
        except LookupError as e:
            raise ApiError(404, "not_found", str(e)) from None
        except ValueError as e:
            raise ApiError(422, "invalid_parameter", str(e)) from None
        if not created:
            response.status_code = status.HTTP_200_OK
        return job

    @app.get("/api/v1/scenario-jobs/{job_id}", response_model=ScenarioJob, response_model_exclude_unset=True)
    def get_job(con: Con, job_id: int):
        job = jobs.get(con, job_id)
        if job is None:
            raise ApiError(404, "not_found", f"scenario job {job_id} not found")
        return job

    # ---- compatibility adapter (05 §4)
    def _compat(con, as_of: date | None, run_id: int | None) -> tuple[dict | None, str]:
        run = compat.resolve_run(con, as_of, run_id)
        if run is None:
            return None, (f"прогон {run_id} не найден" if run_id else
                          "нет опубликованных прогонов" + (f" на {as_of}" if as_of else ""))
        return compat.assemble(con, run), ""

    @app.get("/desk-data.js", include_in_schema=False)
    def desk_js(con: Con, as_of: date | None = None, run_id: int | None = None, demo: str | None = None):
        # no run: DESK_MISSING and DESK_EMPTY, no window.DESK; desk-fallback.js then shows the empty state
        # ("no runs yet", the autopilot's progress), never the demo data set. Only ?demo=1 asks for data.js.
        if demo:
            return Response("/* demo requested: data.js */\n", media_type=JS, headers=NO_STORE)
        desk, missing = _compat(con, as_of, run_id)
        if desk is None:
            empty = {"reason": "not_found" if as_of or run_id else "no_runs", "message": missing}
            body = (f"/* {missing}: empty state */\nwindow.DESK_MISSING = {json.dumps(missing, ensure_ascii=False)};\n"
                    f"window.DESK_EMPTY = {json.dumps(empty, ensure_ascii=False)};\n")
        else:
            body = "window.DESK = " + json.dumps(desk, ensure_ascii=False, separators=(",", ":")) + ";\n"
        return Response(body, media_type=JS, headers=NO_STORE)

    @app.get("/api/v1/compat/desk", description=f"window.DESK as JSON; {COMPAT}")
    def compat_desk(con: Con, as_of: date | None = None, run_id: int | None = None) -> dict[str, Any]:
        desk, missing = _compat(con, as_of, run_id)
        if desk is None:
            raise ApiError(404, "not_found", missing)
        return desk

    # ---- concept pages (prototypes/concepts/, lib/real.js): their data beyond window.DESK
    @app.get("/concepts-data.js", include_in_schema=False)
    def concepts_js(con: Con, request: Request, as_of: date | None = None, run_id: int | None = None,
                    demo: str | None = None):
        """``window.DESK_EXTRAS`` of the run ``/desk-data.js`` shows for the same parameters (``concepts.extras``);
        nothing without a run. The ETag is the body's hash: a new default run changes the series, a page reload then
        gets them, an unchanged run a 304."""
        run = None if demo else compat.resolve_run(con, as_of, run_id)
        if run is None:
            return Response("/* no run: window.DESK_EXTRAS not set */\n", media_type=JS, headers=NO_STORE)
        body = concepts.script(concepts.extras(con, run)).encode()
        tag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        headers = {"ETag": tag, "Cache-Control": "no-cache"}
        if tag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type=JS, headers=headers)

    @app.get("/api/v1/history/positions", include_in_schema=False)
    def positions_history(con: Con, run_id: int | None = None, as_of: date | None = None,
                          ids: str | None = Query(None, description="comma-separated position ids; all when unset"),
                          from_: Annotated[date | None, Query(alias="from")] = None) -> dict[str, Any]:
        """The run's positions' series on one date axis (``concepts.positions_history``): MOEX price, yield and
        G-spread, the KBD at the bond's term, face and DV01 by the default runs. For the concept pages' charts."""
        run = compat.resolve_run(con, as_of, run_id)
        if run is None:
            raise ApiError(404, "not_found", f"run {run_id} not found" if run_id else "no runs")
        wanted = [i for i in (ids or "").split(",") if i] or None
        return concepts.positions_history(con, run, wanted, from_)

    from .diagnostics_routes import router as diagnostics_router
    from .market_history import router as market_history_router  # /api/v1/history/market, /status (R24)
    from .settings_routes import router as settings_router  # here: it imports ApiError from this module
    from .update_routes import router as update_router

    app.include_router(settings_router, include_in_schema=False)  # local admin routes (/settings.html)
    app.include_router(update_router, include_in_schema=False)
    app.include_router(diagnostics_router, include_in_schema=False)
    app.include_router(market_history_router, include_in_schema=False)
    app.include_router(autopilot_router)

    built = Path(app_dir) if app_dir else static_root() / "frontend" / "dist"
    if built.is_dir():  # before "/": the frontend build, absent until npm run build
        @app.get("/app", include_in_schema=False)
        def app_slash(request: Request):  # the "/" mount would 404 it
            return RedirectResponse("/app/" + (f"?{request.url.query}" if request.url.query else ""))

        app.mount("/app", Revalidated(directory=built, html=True), name="app")
    static = Path(static_dir) if static_dir else static_root() / "prototypes"
    if static.is_dir():  # an installed wheel has no prototypes/
        app.mount("/", Revalidated(directory=static, html=True), name="static")
    return app


def serve(store_url: str, host: str = "127.0.0.1", port: int = 8100, static_dir: Path | None = None,
          snapshots: Path | None = None, adapter_status: Callable[[], str] | None = None,
          autopilot: Any = None) -> int:
    """``snapshots``: the snapshot cache the scenario worker reprices from; None runs no worker.
    ``autopilot``: a ``desk.autopilot.Autopilot``, started here and shown by ``/api/v1/autopilot/*``.
    Returns the exit code for ``desk serve``: 0, or the launcher's restart code after ``app.state.request_restart``
    (an update staged by ``/api/v1/update/install``, desk.update)."""
    import threading

    import uvicorn

    from ..marketdata.bundle import SnapshotDir
    from ..update import restart_code

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if snapshots is not None:
        jobs.Worker(store_url, SnapshotDir(snapshots)).start()
    app = create_app(store_url, static_dir, adapter_status=adapter_status)
    if autopilot is not None:
        app.state.autopilot = autopilot
        autopilot.start()
    # the desk.api logger logs every request with its request id, so no uvicorn access log
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, access_log=False))
    restart = restart_code()
    asked: list[bool] = []

    def exit_for_restart() -> None:
        time.sleep(0.5)  # the install response reaches the page first
        if autopilot is not None:
            log.info("restart for an update: waiting for the autopilot's cycle to end")
            autopilot.stop(timeout=300)
        server.should_exit = True

    def request_restart() -> None:
        if not asked:
            asked.append(True)
            threading.Thread(target=exit_for_restart, name="desk-restart", daemon=True).start()

    if restart is not None:  # only under the launcher (start.bat): something must start the server again
        app.state.request_restart = request_restart
    try:
        server.run()
    except KeyboardInterrupt:  # uvicorn raises the captured Ctrl+C again after its shutdown
        pass
    if not server.started:
        sys.exit(3)  # as uvicorn.run: the port was taken or the app failed to start
    return restart if asked and restart is not None else 0
