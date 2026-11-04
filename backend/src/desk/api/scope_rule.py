"""One rule for a position's portfolio under the header filter «legal entity / desk / books / portfolios»
(TDM#566, #496).

The effective portfolio of a position:

1. its book's type is ``HTM`` (the books reference of the run, ``book_type``): ``HTM``, whatever the report says;
2. else the report's Portfolio column of the position (``position_input.x->>'x_portfolio'``);
3. else the column the run kept on the row itself (``fact.extra.x.x_portfolio`` or ``fact.extra.reported.x.x_portfolio``):
   rows the day's trades built (a closed lot ``<id>|closed``, a bought-and-sold row, a position the trades opened)
   have no input row;
4. else none (the pages call it «без портфеля»).

A book without a reference row or without a type goes to 2, no error. When the sets of a run hold several rows of the
same position in the same book, the LAST row (the greatest ``set_id``) stands for the position whole (currency, name
and the rest); only the Portfolio column itself has a second step: if the last row has none, the column of the latest
row that has it. The choice never depends on the order the database returns the rows in.
A book of type HTM is also marked for the pages (``BOOKS[book].portfolio = "HTM"``, ``book_portfolio``): a row without
a position (a cash balance, a report row) takes its book's portfolio from it.

The rule is written once in two forms of the same text: ``effective_portfolio`` / ``portfolio_map`` for the assemblers
of the run (``concepts_run``, ``compat``) and ``EFFECTIVE_PORTFOLIO_SQL`` / ``INPUT_ROW_ORDER_SQL`` for the rows of the PnL
report (``pnl_history.DIMENSIONS``). ``tests/test_scope_566.py`` keeps them equal. The pages take the field as it
comes (``p.portfolio``), they never compute it (``prototypes/concepts/lib/kit.js``, ``K.scope``)."""
from __future__ import annotations

from typing import Any

HTM = "HTM"

EFFECTIVE_PORTFOLIO_SQL = (
    "CASE WHEN upper(btrim(b.data->>'book_type', E' \\t\\r\\n\\f\\013')) = 'HTM' THEN 'HTM' ELSE COALESCE(ip.portfolio,"
    " p.fact->'extra'->'x'->>'x_portfolio', p.fact->'extra'->'reported'->'x'->>'x_portfolio') END")
"""SQL of the rule over the aliases ``b`` (the run's book reference row, may be absent), ``ip`` (the Portfolio column
of the position: ``PORTFOLIO_ROW_SQL``, may be absent) and ``p`` (the run's position), as ``pnl_history.DIMENSIONS``
has them."""

INPUT_ROW_ORDER_SQL = "set_id DESC"
"""Which input row stands for a position (the module's docstring: the last one); a lateral subquery over
``position_input`` orders by it and takes one."""

PORTFOLIO_ROW_SQL = ("SELECT x->>'x_portfolio' AS portfolio FROM position_input i2 JOIN position_set s2 USING (set_id)"
                     " WHERE set_id = ANY(r.position_set_ids) AND i2.position_id = p.position_id"
                     " AND s2.book_id = p.book_id AND x ? 'x_portfolio' ORDER BY set_id DESC LIMIT 1")
"""The second step for the Portfolio column: the latest input row of the position that has it."""


def is_htm(book_type: Any) -> bool:
    return isinstance(book_type, str) and book_type.strip(" \t\r\n\f\v").upper() == HTM


def book_portfolio(book_type: Any) -> str | None:
    """A book's own portfolio for the pages: ``HTM`` for a book of that type, else none."""
    return HTM if is_htm(book_type) else None


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


def fact_portfolio(fact: dict[str, Any] | None) -> str | None:
    """The Portfolio column kept on the row itself (rule 3 of the module's docstring)."""
    extra = (fact or {}).get("extra")
    if not isinstance(extra, dict):
        return None
    for path in (("x",), ("reported", "x")):
        node: Any = extra
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get("x_portfolio") is not None:
            return node["x_portfolio"]
    return None


def portfolio_of(reported: dict[tuple[str, str], str | None], book_types: dict[str, Any], book: str,
                 position_id: str, fact: dict[str, Any] | None = None) -> str | None:
    """A position's effective portfolio from ``portfolio_map``, the books' raw types ``{book_id: book_type}`` and the
    row's own ``fact`` (the fallback of rules 2 and 3)."""
    rep = reported.get((book, position_id))
    return effective_portfolio(book_types.get(book), rep if rep is not None else fact_portfolio(fact))
