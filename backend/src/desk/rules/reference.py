"""The adapter's reference tables (private/1.2 ``reference()["funding_rules"]`` and ``["entities"]``, as
``inbox.validate.check_reference_tables`` normalises them or ``store.current_reference`` returns them) as the
funding engine's inputs: ``FundingRules``, the entity types, the per-entity write-off and the base of each book
type.

The two models differ in a few places; the conversion is explicit about each:

- ``rate_kind`` ``contract`` (HTM: the purchase lot's own rate, ``HtmLot.contract_rate``) and ``none`` (not
  funded) are not rates in ``FundingRules``: they are left out of it and only give the book type's base.
- ``basis`` is per rule in the contract and per book type in the engine (``FundingPolicy.base_by_book_type``):
  ``bases_from_reference`` takes it from the rules naming a book type, and a book type whose rules disagree is
  a ``ValueError`` (the engine cannot fund one book type on two bases). A rule without a book type (an entity's
  cash rule) gives no base: its lines take their base from the entity's type.
- History: the rows of one ``rule_id`` with different ``valid_from`` are revisions; each ends where the next
  starts (``valid_to``), and a retired revision (``active`` false) ends the one before it and adds none.
- The engine's ``ExtraSpread`` (bond-type extra spread, Q-F2) has no contract table yet: always empty here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from functools import partial
from typing import Any

from .funding import DayBasis, EntityType, FundingBase, FundingRule, FundingRules, Periodicity

_SCHEME_TYPES = {"by_book_type": EntityType.MAIN, "cash_balance": EntityType.OTHER}


def _date(v: Any) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _num(v: Any) -> float:
    return float(Decimal(str(v)))


def _active(row: Mapping[str, Any]) -> bool:
    return row.get("active", True) is not False


def _rule(row: Mapping[str, Any], valid_to: date | None) -> FundingRule | None:
    kind = row["rate_kind"]
    if kind not in ("index_spread", "fixed"):
        return None
    make = partial(FundingRule, ccy=row.get("ccy"), book_type=row.get("book_type"), entity=row.get("entity"),
                   book_id=row.get("book_id"), instrument_id=row.get("instrument_id"),
                   valid_from=_date(row.get("valid_from")) or date.min, valid_to=valid_to,
                   day_basis=DayBasis(row["day_count"]) if row.get("day_count") else None, note=row["rule_id"])
    if kind == "fixed":
        return make(fixed_rate=_num(row["fixed_rate_pct"]) / 100)
    return make(index=row["index_code"], spread_bp=_num(row.get("spread_bp") or 0))


def funding_rules_from_reference(rows: Iterable[Mapping[str, Any]]) -> FundingRules:
    """``reference()["funding_rules"]`` rows (every revision, or the ones in force on a day) as ``FundingRules``."""
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        by_id.setdefault(r["rule_id"], []).append(r)
    rates: list[FundingRule] = []
    for revs in by_id.values():
        revs.sort(key=lambda r: _date(r.get("valid_from")) or date.min)
        for i, r in enumerate(revs):
            if not _active(r):
                continue
            nxt = _date(revs[i + 1].get("valid_from")) if i + 1 < len(revs) else None
            rule = _rule(r, nxt)
            if rule is not None:
                rates.append(rule)
    return FundingRules(rates=tuple(rates))


def bases_from_reference(rows: Iterable[Mapping[str, Any]]) -> dict[str, FundingBase]:
    """The base of each book type its active rules name (``FundingPolicy.base_by_book_type``)."""
    out: dict[str, FundingBase] = {}
    for r in rows:
        if not _active(r) or r.get("book_type") is None or r.get("basis") is None:
            continue
        b, bt = FundingBase(r["basis"]), r["book_type"]
        if out.setdefault(bt, b) is not b:
            raise ValueError(f"funding_rules: book type {bt} has rules on two bases ({out[bt]}, {b})")
    return out


def entities_from_reference(rows: Iterable[Mapping[str, Any]]
                            ) -> tuple[dict[str, EntityType], dict[str, Periodicity]]:
    """``reference()["entities"]`` rows as the engine's entity types (``by_book_type``: main, ``cash_balance``:
    other, funded on its whole cash) and the per-entity PnL write-off (``WriteOffPolicy.by_entity``, from
    ``pnl_sweep``). Of several revisions the latest ``valid_from`` wins; a retired entity is left out."""
    types: dict[str, EntityType] = {}
    sweeps: dict[str, Periodicity] = {}
    for e in sorted(rows, key=lambda e: _date(e.get("valid_from")) or date.min):  # the latest revision wins
        types.pop(e["entity"], None)
        sweeps.pop(e["entity"], None)
        if not _active(e):
            continue
        types[e["entity"]] = _SCHEME_TYPES[e["funding_scheme"]]
        if e.get("pnl_sweep"):
            sweeps[e["entity"]] = Periodicity(e["pnl_sweep"])
    return types, sweeps
