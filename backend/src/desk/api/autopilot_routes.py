"""Autopilot routes (I4): the status of the ``desk serve`` autopilot thread and a request to run a cycle now.

The thread is ``request.app.state.autopilot`` (``serve`` sets it); without one the status says ``enabled: false``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from ..autopilot import (
    INTRADAY_FROM,
    POLL_LAG_S,
    POLL_SLOT_S,
    PRELIM_FROM,
    WARN_AFTER,
    idle_status,
    latest_run,
)
from .auth import trader_guard

router = APIRouter(tags=["autopilot"])


class ProblemDate(BaseModel):
    """A hub date whose sync failed and that waits for a retry (#146)."""

    date: str
    error: str
    attempts: int
    next_attempt: str | None


class AutopilotStep(BaseModel):
    name: str
    status: Literal["ok", "skipped", "error"]
    detail: str
    reason: str | None = None  # the intraday / morning step: why there is (no) run of today,
    # ``autopilot.INTRADAY_REASONS`` / ``autopilot.MORNING_REASONS``
    failed_dates: list[str] | None = None  # the sync step: hub dates that failed alone this cycle (#20)
    problem_dates: list[ProblemDate] | None = None  # the sync step: failed dates waiting for a retry (#146)
    at: str


class AutopilotRun(BaseModel):
    """The newest run shown (R11: today's intraday run when there is one) and its market slice."""

    run_id: str
    as_of: str
    created_at: str | None
    run_type: str | None = None
    cut_type: str | None = None
    cut_ts: str | None = None
    label: str | None = None
    pnl_basis: str | None = None
    market_close: dict[str, str] | None = None  # the morning run (cut_type prev_close, #21): {date, cut_type} of its close


class AutopilotProgress(BaseModel):
    step: Literal["sync", "scan", "eod", "intraday", "morning", "history", "backfill"]
    done: int
    total: int | None
    detail: str


class AutopilotSchedule(BaseModel):
    """When the intraday step asks the hub (MSK): the header's «следующее обновление» and its warning (#22)."""

    intraday_from: str  # the first intraday cut of a business day
    prelim_from: str    # the eod_preliminary cut
    poll_slot_s: int    # the hub is asked at most once per slot
    poll_lag_s: int     # slots start this long after the slot times (:05, :15, ...)
    warn_after: str     # no intraday run of today by then: the header warns, with the intraday step's reason


# warn_after (#22): the first cut is 10:00, its run is expected by then; the status message says so too (#156)
SCHEDULE = {"intraday_from": f"{INTRADAY_FROM:%H:%M}", "prelim_from": f"{PRELIM_FROM:%H:%M}",
            "poll_slot_s": POLL_SLOT_S, "poll_lag_s": POLL_LAG_S, "warn_after": f"{WARN_AFTER:%H:%M}"}


class AutopilotStatus(BaseModel):
    enabled: bool
    state: Literal["idle", "running", "error"]
    last_cycle_at: str | None
    last_ok_at: str | None
    message: str
    steps: list[AutopilotStep]
    latest_run: AutopilotRun | None
    progress: AutopilotProgress | None = None  # while a cycle runs: its step, done of total, detail
    now: str | None = None  # the server's clock: the header computes against it, not the browser's (#22)
    schedule: AutopilotSchedule | None = None


class Queued(BaseModel):
    queued: bool


@router.get("/api/v1/autopilot/status", response_model=AutopilotStatus)
def autopilot_status(request: Request):
    ap = getattr(request.app.state, "autopilot", None)
    st = ap.status() if ap is not None else idle_status()
    try:  # the store's answer, also when the thread is off or has not finished a cycle
        with psycopg.connect(request.app.state.store_url, autocommit=True, connect_timeout=5) as con:
            st["latest_run"] = latest_run(con)
    except psycopg.Error:
        pass
    return {**st, "now": datetime.now(UTC).isoformat(), "schedule": SCHEDULE}


@router.post("/api/v1/autopilot/run", response_model=Queued, status_code=202,
             dependencies=[Depends(trader_guard)])  # a trader or admin when sign-in is on
def autopilot_run(request: Request):
    ap = getattr(request.app.state, "autopilot", None)
    if ap is None or not ap.running:
        raise HTTPException(409, "autopilot is not running (desk serve --no-autopilot)")
    ap.trigger(manual=True)  # the owner's «Обновить»: the backfill's stall does not wait (#447)
    return {"queued": True}
