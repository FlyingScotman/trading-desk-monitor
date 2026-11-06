"""The day's trades from the private adapter (S13a, TDM#525; ``desk.trades``): the monitors' screen «Сделки дня».

* ``GET /api/v1/trades?date=YYYY-MM-DD&book_id=B&days=N&run_id=R``: the current version of every trade of the date (08 §10
  ``trades``, a flow: never carried forward), of one book when ``book_id`` is given; cancelled trades with their
  status, late bookings marked; the counts per book, the files read, and the last ``N`` dates that have trades. Per
  trade where it came from and when it was received, the book's legal entity, desk and portfolio (TDM#585).
* ``GET /api/v1/trades/summary?date=&run_id=&books=B1&books=B2``: the same trades counted for the general screens
  (C01, C03, C05, C14; TDM#585) under the books of the header's filter, without the rows (``trades.brief``).

Read routes like the cash and the day status: every signed-in user, the ``viewer`` role included; with sign-in off,
this computer, as every other page.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import store, trades
from ..settings import SettingsError, load

public = APIRouter()


@public.get("/api/v1/trades")
def get_trades(request: Request, date: str | None = None, book_id: str | None = None,
               days: Annotated[int, Query(ge=1, le=120)] = 20, run_id: int | None = None) -> JSONResponse:
    """The trades of ``date`` (default: the latest date with trades, else today, Moscow). ``run_id``: the run of the
    pages' navigation, whose PnL of the trades is shown (TDM#568); without a date, its date; a run of another date than
    ``date`` is ignored: the date's default run."""
    try:
        day = Date.fromisoformat(date) if date else None
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD") from None
    try:  # the one setting of the screen, read as the day status's routes read the settings
        intraday = load().intraday_trades
    except SettingsError:
        intraday = True
    with store.connect(request.app.state.store_url) as con:
        if run_id is not None and day is None:
            r = con.execute("SELECT business_date FROM run WHERE run_id = %s AND status = 'complete'",
                            (run_id,)).fetchone()
            day = r[0] if r else None
        return JSONResponse(trades.view(con, day, book_id or None, days, intraday, run_id),
                            headers={"Cache-Control": "no-store"})


@public.get("/api/v1/trades/summary")
def get_trades_summary(request: Request, date: str | None = None, run_id: int | None = None,
                       books: Annotated[list[str] | None, Query()] = None) -> JSONResponse:
    """The day's trades counted for the general screens (TDM#585, ``trades.brief``): of the books of the header's
    filter (``books``, repeated; none: every book), without the rows. ``date``: default the date of ``run_id``, else
    the latest date with trades."""
    try:
        day = Date.fromisoformat(date) if date else None
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD") from None
    if books is not None and (len(books) > 2000 or any(len(b) > 200 for b in books)):
        raise HTTPException(400, "books: at most 2000 book ids of at most 200 characters")
    try:
        intraday = load().intraday_trades
    except SettingsError:
        intraday = True
    with store.connect(request.app.state.store_url) as con:
        return JSONResponse(trades.brief(con, day, books or None, intraday, run_id),
                            headers={"Cache-Control": "no-store"})
