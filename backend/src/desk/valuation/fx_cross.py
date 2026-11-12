"""The pair of an FX row of the positions report that no trade's terms value (TDM#672): which currencies its amount is
in and what its FX delta is.

A row without terms is valued by ``valuation.positions._fx`` as one amount in the row's currency; for a forward on a
**cross** (EUR/USD, no rouble in the pair) that is not enough: the report's ``x_prim_cur`` / ``x_sec_cur`` name the pair,
and the amount may be the nominal of the first currency while the row's currency is the second. ``cross_row`` reads
the pair and gives the row's FX delta by the cases below; ``publish.tdm_run.direct_row`` (the FX delta, the OCP) and
``pnl.direct`` (the PnL by the rate move) take it from here, so that the two always agree.

- a pair with the rouble on either side, or no pair: ``None``, the row is as before (its amount in its currency);
- a cross and both amounts of the far leg in the row (``x_fx_far_prim_amount``, ``x_fx_far_sec_amount``, signed):
  ``{prim: far_prim, sec: far_sec}``;
- a cross, the row's currency the first of the pair: the nominal in it and the second leg at the cross of the run's
  rates (``quote_leg``, the quote leg of an FX future, DEC18), «без условий сделки: номинал, не PV»; no rate of the
  second currency: no second leg, said;
- a cross, the row's currency not the first of the pair: the unit of the nominal is not confirmed (the report's
  Quantity may be in either currency), so **no FX delta** is made up: ``unconfirmed``, said in the row's assumptions, its
  card and the OCP (``rules.ocp.unconfirmed_gap``) until the trade's terms are loaded.

The second leg is always at the cross of the run's rates, never at ``x_trade_price``: the price only checks it
(``PRICE_TOLERANCE``) and a difference is said, the legs unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..inbox.schema import norm_ccy
from .fallback import fx_rate

if TYPE_CHECKING:
    from ..orchestrator import EodContext

PRICE_TOLERANCE = 0.2
"""The most the report's trade price may differ from the cross of the run's rates (either way of the quote) before the
row says so."""
NO_TERMS_TEXT = "без условий сделки: номинал, не PV"
"""The row's mark (and the OCP's, ``rules.ocp.classify``) of an amount that is a nominal, not a value."""
NO_TERMS_ROW = "no_trade_terms"
"""``valuation.positions.NO_TERMS``, the ``core_not_valued`` reason of a row no ``trade_terms`` row names (not imported:
positions imports the orchestrator, which imports ``pnl.direct``, which imports this module; a test keeps them equal)."""
FAR_LEGS = "far_legs"
NOMINAL = "nominal"
UNCONFIRMED = "unit_unconfirmed"
"""``CrossRow.kind``: the legs of the row, the nominal and the second leg at the cross, no delta."""


def unconfirmed_text(ccy: str, base: str, quote: str) -> str:
    """What the owner reads on a row whose currency is not the first of its cross (the card, the run's notes, the
    OCP's gap)."""
    return (f"валюта строки отчёта ({ccy}) не совпадает с первой валютой пары {base}/{quote}, единица номинала "
            f"не подтверждена: в ОВП не включено до загрузки условий сделки")


def report_field(x: dict[str, Any], key: str) -> Any:
    """A column of the positions report of the row: under ``x`` (the adapter's extras), else flat."""
    rep = x.get("x") if isinstance(x.get("x"), dict) else {}
    v = rep.get(key)
    return v if v is not None else x.get(key)


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = float(v) if isinstance(v, int | float | Decimal | str) else None
    except ValueError:
        return None
    return n if n is not None and math.isfinite(n) else None


def pair_of(x: dict[str, Any]) -> tuple[str, str] | None:
    """(first, second) currency of the row's pair, upper case; None without both or when they are the same."""
    prim, sec = report_field(x, "x_prim_cur"), report_field(x, "x_sec_cur")
    if not prim or not sec:
        return None
    a, b = norm_ccy(str(prim)).upper(), norm_ccy(str(sec)).upper()
    return (a, b) if a and b and a != b else None


def is_cross(pair: tuple[str, str] | None) -> bool:
    """A pair with no rouble on either side."""
    return pair is not None and "RUB" not in pair


def is_future(x: dict[str, Any]) -> bool:
    """A future of any kind (the report's product group has ``future``): DEC18 has its own rule for the FX future."""
    return "future" in str(report_field(x, "x_product_group") or "").lower()


def cross_price(ctx: EodContext, base: str, quote: str) -> float | None:
    """Units of ``quote`` per unit of ``base`` at the run's rates (each in RUB); None when either has no rate."""
    rb, rq = fx_rate(ctx, base), fx_rate(ctx, quote)
    return None if rb is None or rq is None else rb[0] / rq[0]


def missing_rate(ctx: EodContext, base: str, quote: str) -> str:
    """The currency of the pair that has no rate in the run (the quote's when both lack one)."""
    return quote if fx_rate(ctx, quote) is None else base


def quote_leg(ctx: EodContext, base: str, quote: str, nominal: float) -> tuple[float, float] | None:
    """(price, the quote leg) of a nominal in ``base`` on the cross: the price the cross of the run's rates, the leg
    ``-nominal x price`` in ``quote``; None without a rate of either. The one rule of an FX future's quote leg (DEC18)
    and of a forward without terms (TDM#672)."""
    price = cross_price(ctx, base, quote)
    return None if price is None else (price, -nominal * price)


@dataclass(frozen=True)
class CrossRow:
    kind: str
    pair: tuple[str, str]
    delta: dict[str, float]
    """The FX delta per currency; empty for ``UNCONFIRMED``."""
    notes: list[str] = field(default_factory=list)
    """For the row's assumptions."""
    unconfirmed: dict[str, str] | None = None
    """``extra.ocp_unconfirmed``: ``{reason, pair, ccy, text}`` for ``UNCONFIRMED``."""


def without_terms(o: Any) -> bool:
    """The row has no trade terms: core was not asked (``core_not_valued`` None) or no ``trade_terms`` row names it. A
    row with terms that core could not value (no curve or spot of a currency, an NDF it does not value, terms it could
    not read, a trade settled) is not this module's: its terms exist, the unit of its amount is not in question."""
    nv = getattr(o, "core_not_valued", None)
    return nv is None or nv[0] == NO_TERMS_ROW


def cross_row(ctx: EodContext, o: Any, x: dict[str, Any], ccy: str, amount: float) -> CrossRow | None:
    """The row's cross by the module's cases, or None where the row stays as it was (it has trade terms, a future, a
    pair with the rouble, no pair). ``o``: the row's outcome; ``ccy`` and ``amount``: the report row's own currency
    (not the currency the row may be shown in) and signed amount."""
    pair = pair_of(x)
    if not is_cross(pair) or is_future(x) or not without_terms(o):
        return None
    assert pair is not None
    base, quote = pair
    far_p, far_s = _number(report_field(x, "x_fx_far_prim_amount")), _number(report_field(x, "x_fx_far_sec_amount"))
    if not far_p or not far_s:  # a zero amount is no amount: the legs are not taken from the row
        far_p = far_s = None
    price = cross_price(ctx, base, quote)
    notes = _price_check(report_field(x, "x_trade_price"), price, base, quote) if (
        far_p is not None and far_s is not None) or ccy == base else []
    if far_p is not None and far_s is not None:
        # the sign of the second amount is not confirmed by the data: the legs of a forward are of opposite signs
        second = -math.copysign(abs(far_s), far_p)
        far_note = f"{base}/{quote} {NO_TERMS_TEXT} — ноги из строки отчёта: {far_p:g} {base}, {second:g} {quote}"
        if far_p * far_s > 0:
            far_note += (f"; знаки сумм в строке совпадают ({far_p:g} и {far_s:g}): вторая нога взята противоположной "
                         "по знаку, проверьте строку")
        return CrossRow(FAR_LEGS, pair, {base: far_p, quote: second}, [far_note, *notes])
    if ccy == base:
        if price is None:
            no_rate = (f"{base}/{quote} {NO_TERMS_TEXT} — нет курса {missing_rate(ctx, base, quote)} на "
                       f"{ctx.business_date}, второй ноги в ОВП нет")
            return CrossRow(NOMINAL, pair, {base: amount}, [no_rate])
        leg = -amount * price
        leg_note = (f"{base}/{quote} {NO_TERMS_TEXT} — номинал {amount:g} {base}, вторая нога {leg:g} {quote} по "
                    f"кросс-курсу {price:.6g} курсов прогона")
        return CrossRow(NOMINAL, pair, {base: amount, quote: leg}, [leg_note, *notes])
    text = unconfirmed_text(ccy, base, quote)
    return CrossRow(UNCONFIRMED, pair, {}, [text], {"reason": UNCONFIRMED, "pair": f"{base}/{quote}", "ccy": ccy,
                                                    "text": text})


def _price_check(trade_price: Any, cross: float | None, base: str, quote: str) -> list[str]:
    """A note when the report's trade price is further than ``PRICE_TOLERANCE`` from the cross of the run's rates, as
    quoted either way (units of ``quote`` per ``base`` or the reverse); nothing without a price or a cross."""
    p = _number(trade_price)
    if p is None or p <= 0 or cross is None or cross <= 0:
        return []
    if min(abs(p / cross - 1), abs(p * cross - 1)) <= PRICE_TOLERANCE:
        return []
    note = (f"цена сделки в отчёте {p:g} расходится с кросс-курсом прогона {cross:.6g} ({base}/{quote}) больше чем "
            f"на {PRICE_TOLERANCE:.0%}: ноги не менялись, проверьте пару и сумму строки")
    return [note]
