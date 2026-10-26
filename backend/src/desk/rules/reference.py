"""The adapter's reference tables (private/1.2 ``reference()["funding_rules"]`` and ``["entities"]``, as
``inbox.validate.check_reference_tables`` normalises them or ``store.current_reference`` returns them) as the
funding engine's inputs: ``FundingRules``, the entity types, the per-entity write-off and the base of each book
type; ``policy_from_reference`` sets the last two on a ``FundingPolicy``, so the organisation's values come from
the adapter's settings and not from the engine's defaults.

The two models differ in a few places; the conversion is explicit about each:

- ``rate_kind`` ``contract`` (HTM: the purchase lot's own rate, ``HtmLot.contract_rate``) is a ``contract`` rule (so
  it holds its place in the order adapter -> settings -> default, #237); ``none`` (not funded) is not a rate: it is
  left out and only gives the book type's base.
- ``basis`` is per rule in the contract and per book type in the engine (``FundingPolicy.base_by_book_type``):
  ``bases_from_reference`` takes it from the rules naming a book type, and a book type whose rules disagree is
  a ``ValueError`` (the engine cannot fund one book type on two bases). A rule without a book type (an entity's
  cash rule) gives no base: its lines take their base from the entity's type.
- History: the rows of one ``rule_id`` with different ``valid_from`` are revisions; each ends where the next
  starts (``valid_to``), and a retired revision (``active`` false) ends the one before it and adds none.
- The engine's ``ExtraSpread`` (bond-type extra spread) has no contract table yet: always empty here.
- ``funding_policy`` (private/1.3): the organisation's answers to the open conventions that are not a rate
  (``apply_funding_policy``): the HTM base (OQ-02), the DEC01 timing choices, the REPO policy (DEC03), the write-off
  periodicity per book type and its date. A field the table does not give keeps the policy's value (the engine's
  provisional default, or unset); ``FundingSettings.source`` says which field came from where.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from functools import partial
from typing import Any

from .funding import (
    BaseDate,
    ClosedPosition,
    DayBasis,
    EntityType,
    FundingBase,
    FundingPolicy,
    FundingRule,
    FundingRules,
    FxDate,
    HtmBase,
    LineKind,
    NewPosition,
    OutOfCurrency,
    Periodicity,
    RepoRate,
    RuleTier,
    WriteOffDate,
)

_SCHEME_TYPES = {"by_book_type": EntityType.MAIN, "cash_balance": EntityType.OTHER}


def _date(v: Any) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _num(v: Any) -> float:
    return float(Decimal(str(v)))


def _active(row: Mapping[str, Any]) -> bool:
    return row.get("active", True) is not False


def _rule(row: Mapping[str, Any], valid_to: date | None, tier: int = RuleTier.ADAPTER) -> FundingRule | None:
    kind = row["rate_kind"]
    if kind not in ("index_spread", "fixed", "contract"):
        return None
    make = partial(FundingRule, ccy=row.get("ccy"), book_type=row.get("book_type"), entity=row.get("entity"),
                   book_id=row.get("book_id"), instrument_id=row.get("instrument_id"),
                   security_class=row.get("security_class"),
                   kind=LineKind(row["kind"]) if row.get("kind") else None,
                   valid_from=_date(row.get("valid_from")) or date.min, valid_to=valid_to,
                   day_basis=DayBasis(row["day_count"]) if row.get("day_count") else None, note=row["rule_id"],
                   tier=tier)
    if kind == "contract":
        return make(contract=True)
    if kind == "fixed":
        return make(fixed_rate=_num(row["fixed_rate_pct"]) / 100)
    return make(index=row["index_code"], spread_bp=_num(row.get("spread_bp") or 0))


def funding_rules_from_reference(rows: Iterable[Mapping[str, Any]],
                                 tier: int | Callable[[Mapping[str, Any]], int] = RuleTier.ADAPTER) -> FundingRules:
    """``reference()["funding_rules"]`` rows (every revision, or the ones in force on a day) as ``FundingRules``.
    ``tier`` (``RuleTier``, or one per row): the adapter's rules by default; the settings' rules of the same shape
    (``desk.funding_settings``) take theirs. A row may also name ``security_class`` and ``kind`` (settings only)."""
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
            rule = _rule(r, nxt, tier(r) if callable(tier) else tier)
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


def policy_from_reference(rules: Iterable[Mapping[str, Any]], entities: Iterable[Mapping[str, Any]] = (),
                          policy: FundingPolicy | None = None) -> FundingPolicy:
    """``policy`` (default ``FundingPolicy()``) with the base of each book type the active ``funding_rules`` name
    (over the engine's defaults) and the per-entity write-off of ``entities``. Everything else is unchanged."""
    pol = policy or FundingPolicy()
    _, sweeps = entities_from_reference(entities)
    return replace(pol, base_by_book_type={**pol.base_by_book_type, **bases_from_reference(rules)},
                   write_off=replace(pol.write_off, by_entity={**pol.write_off.by_entity, **sweeps}))


POLICY_FIELDS: dict[str, tuple[str, Any]] = {
    "htm_base": ("htm_base", HtmBase),
    "mv_base_date": ("mv_base_date", BaseDate),
    "cash_base_date": ("cash_base_date", BaseDate),
    "new_positions": ("new_positions", NewPosition),
    "closed_positions": ("closed_positions", ClosedPosition),
    "fx_date": ("fx_date", FxDate),
    "out_of_currency": ("out_of_currency", OutOfCurrency),
    "entity_cash_book_type": ("entity_cash_rate_book_type", str),
    "repo_rate": ("repo.rate", RepoRate),
    "repo_contract_from_term_days": ("repo.contract_rate_from_term_days", int),
    "repo_security_book_type": ("repo.security_rate_book_type", str),
    "write_off_date": ("write_off.date_rule", WriteOffDate),
}
"""The firm-wide ``funding_policy`` fields (a row without ``book_type``, private/1.3) -> (``FundingPolicy`` field path,
type). ``pnl_sweep`` is per book type: ``write_off.by_book_type[<book_type>]``."""


@dataclass(frozen=True)
class FundingSettings:
    """``apply_funding_policy``: the policy, where each field the table set came from, and the conflicts."""

    policy: FundingPolicy
    source: dict[str, str]
    """Per policy field path the table set: ``funding_policy:<policy_id>``."""
    conflicts: tuple[str, ...]
    """Settings given twice with different values: each keeps the policy's previous value."""


def _set(pol: FundingPolicy, path: str, value: Any) -> FundingPolicy:
    head, _, rest = path.partition(".")
    if not rest:
        return replace(pol, **{head: value})
    return replace(pol, **{head: replace(getattr(pol, head), **{rest: value})})


def apply_funding_policy(rows: Iterable[Mapping[str, Any]], policy: FundingPolicy | None = None) -> FundingSettings:
    """``policy`` (default ``FundingPolicy()``) with the settings of the active ``reference()["funding_policy"]``
    rows in force (``store.current_reference``): the firm-wide fields of ``POLICY_FIELDS`` and ``pnl_sweep`` per
    book type. A setting two rows give with different values is a conflict: it keeps the previous value and is
    listed, never decided by row order."""
    pol = policy or FundingPolicy()
    wanted: dict[str, dict[Any, list[str]]] = {}
    for r in rows:
        if not _active(r):
            continue
        if r.get("book_type") is not None:
            if r.get("pnl_sweep") is not None:
                wanted.setdefault(f"write_off.by_book_type[{r['book_type']}]", {}).setdefault(
                    r["pnl_sweep"], []).append(r["policy_id"])
            continue
        for f, (path, _) in POLICY_FIELDS.items():
            if r.get(f) is not None:
                wanted.setdefault(path, {}).setdefault(r[f], []).append(r["policy_id"])
    types = dict(POLICY_FIELDS.values())
    source: dict[str, str] = {}
    conflicts: list[str] = []
    sweeps = dict(pol.write_off.by_book_type)
    for path, values in sorted(wanted.items()):
        if len(values) > 1:
            conflicts.append(f"funding_policy: {path} given as "
                             + ", ".join(f"{v} ({', '.join(ids)})" for v, ids in values.items()))
            continue
        ((value, ids),) = values.items()
        source[path] = "funding_policy:" + ",".join(ids)
        if path.startswith("write_off.by_book_type["):
            sweeps[path[len("write_off.by_book_type["):-1]] = Periodicity(value)
        else:
            pol = _set(pol, path, types[path](value))
    if sweeps != pol.write_off.by_book_type:
        pol = replace(pol, write_off=replace(pol.write_off, by_book_type=sweeps))
    return FundingSettings(pol, source, tuple(conflicts))
