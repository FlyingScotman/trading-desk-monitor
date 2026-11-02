"""The day's trades from the private adapter (S13a, TDM#525; ``desk.trades``): the monitors' screen «Сделки дня».

* ``GET /api/v1/trades?date=YYYY-MM-DD&book_id=B&days=N``: the current version of every trade of the date (08 §10
  ``trades``, a flow: never carried forward), of one book when ``book_id`` is given; cancelled trades with their
  status, late bookings marked; the counts per book, the files read, and the last ``N`` dates that have trades.

A read route like the cash and the day status: every signed-in user, the ``viewer`` role included; with sign-in off,
this computer, as every other page. No valuation and no new-trade PnL (the rest of S13).
"""

from __future__ import annotations

from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import store, trades

public = APIRouter()


@public.get("/api/v1/trades")
def get_trades(request: Request, date: str | None = None, book_id: str | None = None,
               days: Annotated[int, Query(ge=1, le=120)] = 20) -> JSONResponse:
    """The trades of ``date`` (default: the latest date with trades, else today, Moscow)."""
    try:
        day = Date.fromisoformat(date) if date else None
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD") from None
    with store.connect(request.app.state.store_url) as con:
        return JSONResponse(trades.view(con, day, book_id or None, days), headers={"Cache-Control": "no-store"})
