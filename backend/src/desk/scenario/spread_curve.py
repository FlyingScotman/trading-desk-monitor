"""The spread roll of the forward carry (TDM#66, the owner's answer of 26.10, ``spread=curve``): as a bond shortens to
the horizon its spread rolls along the spread curve of its peers, the run date's median MOEX G-spread of RUB fixed-coupon
corporates of its rating grade over tenor (what C09 «G-спред по рейтингу и сроку» shows, ``c09-market.html``
``medOf``).

**The curves** (``build_curves``): the peers are the bonds of the run's own market snapshot quoted on its date with a
MOEX G-spread (``gspread_bp``; C09 reads the same MOEX field from ``md_bond_day``, the snapshot is the run's point in
time): RUB face, not OFZ, not a floater, a tenor after the date (to the offer MOEX prices to, else maturity, as
``api.concepts.universe``), a desk rating (``EodContext.reference_facts.ratings``, the grade C09 shows). Per grade and
C09's tenor bucket (``TENOR_BUCKETS``: up to 1, 2, 3, 5 years and beyond) a bucket of at least ``MIN_BUCKET`` peers is a
point (its median tenor, its median spread); a grade with at least two points is a curve, linear between them and flat
beyond.

**A bond's roll** (``roll``): Δ = curve(tenor at the horizon) − curve(tenor now), bp, on its grade's curve, else a
neighbouring grade's (one notch worse, then one better: ``spreadCurve`` says which), else none with a reason
(``no_rating``, ``no_peer_curve``). The carry adds ``spreadRoll = CS01 x Δ`` (the run's ``cs01``, RUB per bp; first
order). OFZ keep their spread to the KBD (no peer curve: ``ofz_held``), floaters their discount margin
(``floater_held``), a never-called perpetual its spread (``perpetual_held``: no tenor to roll down, TDM#35), a bond
redeemed by the horizon has nothing left to roll (``redeemed``).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Any

from ..marketdata.adapter import OFZ_CLASSES, BondQuote, BondTerms, bond_currency
from ..marketdata.ratings import GRADES

TENOR_BUCKETS = (1.0, 2.0, 3.0, 5.0, math.inf)
"""C09's tenor buckets, years (``c09-market.html`` ``TBK``): a bucket holds the tenors up to its bound."""
MIN_BUCKET = 3
"""Peers a bucket needs to be a point of its grade's curve (a median of one or two issues is an issue's spread)."""
YEAR_DAYS = 365.25
"""Tenor in years as C09 and ``api.concepts.universe`` count it."""
HELD_REASONS = ("no_rating", "no_peer_curve", "ofz_held", "floater_held", "perpetual_held", "not_rub", "redeemed",
                "no_cs01", "no_quote", "not_covered")


@dataclass(frozen=True)
class PeerCurve:
    grade: str
    points: tuple[tuple[float, float], ...]
    """(median tenor, median G-spread bp) per bucket, by tenor."""
    issues: int

    def at(self, t: float) -> float:
        """The curve's spread at tenor ``t``: linear between the points, flat beyond them."""
        ts = [p[0] for p in self.points]
        if t <= ts[0]:
            return self.points[0][1]
        if t >= ts[-1]:
            return self.points[-1][1]
        i = bisect_left(ts, t)
        (t0, s0), (t1, s1) = self.points[i - 1], self.points[i]
        return s0 + (s1 - s0) * (t - t0) / (t1 - t0)

    def json(self) -> dict[str, Any]:
        return {"grade": self.grade, "issues": self.issues, "points": [[round(t, 4), s] for t, s in self.points]}


def tenor(terms: BondTerms, quote: BondQuote, on: date) -> float | None:
    """Years from ``on`` to the date MOEX prices the bond to (its offer when before maturity), else maturity."""
    yd = quote.source_analytics.get("yield_date")
    yd = date.fromisoformat(yd) if isinstance(yd, str) else yd
    mat = terms.maturity_date
    end = yd if yd and (mat is None or yd < mat) else mat
    return (end - on).days / YEAR_DAYS if end and end > on else None


def _bucket(t: float) -> int:
    return next(i for i, b in enumerate(TENOR_BUCKETS) if t <= b)


def build_curves(peers: Iterable[tuple[str, float, float]], min_bucket: int = MIN_BUCKET) -> dict[str, PeerCurve]:
    """Grade -> its curve from ``peers`` (grade, tenor, G-spread bp); a grade with fewer than two buckets of
    ``min_bucket`` peers has none."""
    grid: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)
    for g, t, s in peers:
        grid[g, _bucket(t)].append((t, s))
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    n: dict[str, int] = defaultdict(int)
    for (g, _), xs in sorted(grid.items()):
        if len(xs) >= min_bucket:
            out[g].append((median(t for t, _ in xs), median(s for _, s in xs)))
            n[g] += len(xs)
    return {g: PeerCurve(g, tuple(sorted(ps)), n[g]) for g, ps in out.items() if len(ps) >= 2}


def peers(bonds: Mapping[str, BondTerms], quote_of: Any, grade_of: Any, on: date) -> list[tuple[str, float, float]]:
    """The run's peers (module docstring): (grade, tenor, G-spread) of every RUB fixed-coupon non-OFZ bond quoted on
    ``on`` with a G-spread and a rating. ``quote_of(terms)``: its quote (``MarketView.quote``); ``grade_of(terms)``: its
    grade or None."""
    out = []
    for t in bonds.values():
        if t.bond_class in OFZ_CLASSES or t.coupon_type == "floating" or bond_currency(t) != "RUB":
            continue
        q = quote_of(t)
        if q is None or q.trade_date != on or q.source_analytics.get("gspread_bp") in (None, ""):
            continue
        g, ten = grade_of(t), tenor(t, q, on)
        if g in GRADES and ten is not None:
            out.append((g, ten, float(q.source_analytics["gspread_bp"])))
    return out


def curve_for(curves: Mapping[str, PeerCurve], grade: str | None) -> tuple[PeerCurve | None, str]:
    """The curve a bond of ``grade`` rolls on and what ``spreadCurve`` says: its grade, a neighbour's («A (для A+)»),
    else None with the reason."""
    if grade not in GRADES:
        return None, "no_rating"
    if grade in curves:
        return curves[grade], grade
    i = GRADES.index(grade)
    for j in (i + 1, i - 1):  # one notch worse, then one better
        if 0 <= j < len(GRADES) and GRADES[j] in curves:
            return curves[GRADES[j]], f"{GRADES[j]} (для {grade})"
    return None, "no_peer_curve"


def roll(curve: PeerCurve, tenor_now: float, years: float) -> float:
    """Δ bp of a bond ``tenor_now`` years long carried ``years`` along ``curve`` (its tenor at the horizon floored at
    0)."""
    return curve.at(max(tenor_now - years, 0.0)) - curve.at(tenor_now)
