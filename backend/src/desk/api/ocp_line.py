"""One line of the published OCP for every route that carries it (TDM#567, D10, D24, D11).

The run's page data (``concepts_run.ocp_status``), the history by day (``ocp_history``) and the run's ``/ocp`` route
all serialize a ``published.by_book`` line with :func:`ocp_line`, so one line has the same fields wherever it is read;
the book type of a book without one is derived by :func:`book_type`, and a figure not on the current methodology is
named by :func:`method_note` (the pages show it as is, no rule of their own).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..rules import ocp as O

TRADING, DERIVS = "TRADING", "DERIVS"


def ocp_line(x: dict[str, Any]) -> dict[str, Any]:
    """A published line as the pages read it. ``book`` null: an entity-level report unit (``books`` the books it
    holds). Under ``report_base_plus_day`` (block v3, TDM#531) also the base's date, report set and kind
    (``baseKind``: ``without_derivatives`` is the current methodology, ``with_derivatives`` the report still with
    them, their PV not added, ``book_absent`` a book the report has no row of) and the terms (``parts``; they sum to
    ``amount``). Contributions stay in the run's block."""
    out = {"entity": x.get("entity"), "book": x.get("book_id"), "books": x.get("books") or [], "ccy": x.get("ccy"),
           "amount": x.get("amount"), "source": x.get("source"), "components": x.get("components") or [],
           "reported": x.get("reported"), "validDate": x.get("valid_date")}
    if x.get("parts") is not None:
        out.update(baseDate=x.get("base_date"), setId=x.get("set_id"), baseKind=x.get("base_kind"), parts=x["parts"],
                   baseOlder=bool(x.get("base_older")))
    return out


def book_type(recorded: Any, has_bonds: bool) -> str:
    """The type of a book: as recorded, else ``TRADING`` for a book with bonds in the run and ``DERIVS`` for one
    without (the run's own rule, now the history's too)."""
    return recorded if isinstance(recorded, str) and recorded else TRADING if has_bonds else DERIVS


def method_note(basis: str | None, on: date | str | None, lines: list[dict[str, Any]]) -> str | None:
    """Why a day's OCP is not on the current methodology (the report without the derivatives + their PV), else None.
    ``lines`` as :func:`ocp_line` gives them. No basis (no OCP at all): None, the day's state says why."""
    if not basis:
        return None
    if basis == O.PublishedBasis.REPORT_BASE_PLUS_DAY.value:
        old = sorted({x.get("book") or x.get("entity") or "—" for x in lines if x.get("baseKind") == O.BASE_WHOLE})
        if not old:
            return None
        return ("не по текущей методике: отчёт ещё с деривативами, их PV не добавлен (" + ", ".join(old[:5])
                + (f" и ещё {len(old) - 5}" if len(old) > 5 else "") + ")")
    if basis == O.PublishedBasis.REPORT_PLUS_OUTSIDE.value:
        since = O.BASIS_SINCE
        early = (str(on)[:10] < since.isoformat()) if on else False
        return ("другая методика: отчёт целиком (с деривативами) и слагаемые вне отчёта"
                + (f", день до {since.strftime('%d.%m.%Y')}" if early else ""))
    return "другая методика: ОВП из позиций, отчёта ОВП в силе нет"
