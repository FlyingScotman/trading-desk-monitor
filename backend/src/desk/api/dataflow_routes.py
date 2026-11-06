"""The data flow of a date (TDM#586; ``desk.dataflow``): the monitors' screen C21 «Схема данных».

* ``GET /api/v1/dataflow?on=YYYY-MM-DD``: the blocks (sources, processing, results) with their status, what each got
  and when, what it left out and why, what it waits for, the progress of the background work, the links; the edges,
  red where incomplete data went on; the queue of background work ahead.

A read route like the day status: every signed-in user, the ``viewer`` role included (codes, counts, book ids, file
names and sources, #25); the links to the settings only for a user who may open them. It never asks the hub or the
adapter's services: it reads the store, the cache, the autopilot's and the services' state.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import dataflow, store
from ..settings import SettingsError, load
from . import auth
from .app import ApiError

public = APIRouter()


def _admin(request: Request) -> bool:
    try:  # the settings page answers this user (an admin, or this computer with sign-in off): the page may link it
        auth.require_admin(request, ())
        return True
    except ApiError as e:  # 401/403: a viewer, another computer
        if e.status not in (401, 403):
            raise
        return False


@public.get("/api/v1/dataflow")
def get_dataflow(request: Request, on: str | None = None) -> JSONResponse:
    """The data flow of ``on`` (default: the date of the latest default run, else today, Moscow)."""
    try:
        day = date.fromisoformat(on) if on else None
    except ValueError:
        raise HTTPException(400, "on must be YYYY-MM-DD") from None
    try:
        settings = load()
    except SettingsError as e:
        raise HTTPException(500, f"settings: {e}") from None
    ap = getattr(request.app.state, "autopilot", None)
    services = getattr(request.app.state, "adapter_services", None)
    with store.connect(request.app.state.store_url or store.resolve_url(settings)) as con:
        doc = dataflow.build(con, settings, day, autopilot=ap.status() if ap is not None else None,
                             services=services, admin=_admin(request))
    return JSONResponse(doc, headers={"Cache-Control": "no-store"})
