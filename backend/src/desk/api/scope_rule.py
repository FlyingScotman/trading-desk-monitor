"""One rule for a position's portfolio under the header filter «legal entity / desk / books / portfolios»
(TDM#566, #496).

The effective portfolio of a position:

1. its book's type is ``HTM`` (the books reference of the run, ``book_type``): ``HTM``, whatever the report says;
2. else the report's Portfolio column of the position (``position_input.x->>'x_portfolio'``);
3. else none (the pages call it «без портфеля»).

A book without a reference row or without a type goes to 2, no error. When the sets of a run hold several rows of the
same position in the same book, the row with the Portfolio column wins, then the one of the latest set: the choice
never depends on the order the database returns the rows in.

The rule is written once in two forms of the same text: ``effective_portfolio`` / ``portfolio_map`` for the assemblers
of the run (``concepts_run``, ``compat``) and ``EFFECTIVE_PORTFOLIO_SQL`` / ``INPUT_ROW_SQL`` for the rows of the PnL
report (``pnl_history.DIMENSIONS``). ``tests/test_scope_rule.py`` keeps them equal. The pages take the field as it
comes (``p.portfolio``), they never compute it (``prototypes/concepts/lib/kit.js``, ``K.scope``)."""
from __future__ import annotations

from typing import Any

HTM = "HTM"

EFFECTIVE_PORTFOLIO_SQL = "CASE WHEN upper(btrim(b.data->>'book_type')) = 'HTM' THEN 'HTM' ELSE i.x->>'x_portfolio' END"
"""SQL of the rule over the alias ``b`` (the run's book reference row, may be absent) and ``i`` (the chosen input row,
may be absent), as ``pnl_history.DIMENSIONS`` has them."""

INPUT_ROW_ORDER_SQL = "(x ? 'x_portfolio') DESC, set_id DESC"
"""Which input row of a position stands for it (the module's docstring); a lateral subquery over ``position_input``
orders by it and takes one."""


def is_htm(book_type: Any) -> bool:
    return isinstance(book_type, str) and book_type.strip().upper() == HTM


def effective_portfolio(book_type: Any, reported: str | None) -> str | None:
    """The rule: the HTM type of the book first, else the report's Portfolio column, else None."""
    return HTM if is_htm(book_type) else reported


def portfolio_map(con, set_ids: list[int] | None) -> dict[tuple[str, str], str | None]:
    """The report's Portfolio column per (book, position id) over the sets of a run: of several rows of a position the
    one with the column, then of the latest set (``INPUT_ROW_ORDER_SQL``)."""
    rows = con.execute(
        "SELECT s.book_id, i.position_id, i.x->>'x_portfolio' AS portfolio FROM position_input i"
        " JOIN position_set s USING (set_id) WHERE i.set_id = ANY(%s) AND i.x ? 'x_portfolio'"
        " ORDER BY i.set_id", (set_ids or [],))
    return {(r["book_id"], r["position_id"]): r["portfolio"] for r in rows}


def portfolio_of(reported: dict[tuple[str, str], str | None], book_types: dict[str, Any], book: str,
                 position_id: str) -> str | None:
    """A position's effective portfolio from ``portfolio_map`` and the books' raw types ``{book_id: book_type}``."""
    return effective_portfolio(book_types.get(book), reported.get((book, position_id)))
