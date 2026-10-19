"""Test helper: write one run into the desk store from golden-fixture outcomes through the store's own
path (``store.load_positions`` + ``positions_for`` + ``desk_run.publish``), as ``desk eod`` does."""

from __future__ import annotations

import copy
import dataclasses

from desk import store
from desk.marketdata.adapter import MarketView
from desk.orchestrator import EodContext
from desk.publish import desk_run


def insert_run(store_url: str, ctx: EodContext, positions: list[tuple], prev: MarketView | None, *,
               run_type: str = "EOD", cut_type: str | None = None) -> int:
    """positions: (position_id, book_id, outcome[, quantity]); the quantity is needed only for a position
    without a valuation. ``cut_type`` relabels the market snapshot's cut. Returns the run_id."""
    if cut_type:
        market = copy.copy(ctx.market)
        market.snapshot = dataclasses.replace(ctx.market.snapshot, cut_type=cut_type)
        ctx = dataclasses.replace(ctx, market=market)
    rows = [(pid, book, o.secid, qty[0] if qty else o.valuation.quantity) for pid, book, o, *qty in positions]
    with store.connect(store_url) as con:
        store.load_positions(con, rows, valid_date=ctx.business_date, source_name="tests", source_sha256="-")
        inputs = store.positions_for(con, ctx.business_date)
        run_id, status, _ = desk_run.publish(con, ctx, [p[:3] for p in positions], prev=prev, run_type=run_type,
                                             inputs=inputs)
    assert status == "complete"
    return run_id
