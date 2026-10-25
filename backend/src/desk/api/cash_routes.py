"""Cash balances and the breaks between bookkeeping systems (B3, issue #61; ``desk.cash``): the monitors' C15 screen.

* ``GET /api/v1/cash?on=YYYY-MM-DD&days=N``: the balances in force on ``on`` per (entity, book, currency) by ledger,
  their status (``match``, ``break``, ``one_ledger``, ``single``), accounts, a history over the last ``N`` dates that
  have cash, and per date the counts for the screen's strip.

A read route like the runs and the day status: every signed-in user, the ``viewer`` role included; with sign-in off,
this computer, as every other page.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import cash, store

public = APIRouter()


@public.get("/api/v1/cash")
def get_cash(request: Request, on: str | None = None,
             days: Annotated[int, Query(ge=1, le=120)] = 20) -> JSONResponse:
    """The cash screen of ``on`` (default: the latest date with cash, else today, Moscow)."""
    try:
        day = date.fromisoformat(on) if on else None
    except ValueError:
        raise HTTPException(400, "on must be YYYY-MM-DD") from None
    with store.connect(request.app.state.store_url) as con:
        return JSONResponse(cash.view(con, day, days), headers={"Cache-Control": "no-store"})
