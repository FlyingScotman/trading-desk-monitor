"""MOEX zero-coupon yield curve (KBD / G-curve) from published ZCYC parameters.

TEMPORARY: this belongs in core (PRODUCT_ROADMAP package 0.6, "KBD/ZCYC evaluator"). It lives
here so that slice S1 can run; it builds an ordinary core ``DiscountCurve`` and nothing else
depends on it. Move it to core and delete this module when core ships the evaluator.

MOEX methodology: the continuously compounded zero rate in basis points is

    G(t) = b1 + (b2 + b3) * (tau / t) * (1 - exp(-t / tau)) - b3 * exp(-t / tau)
           + sum_{i=1..9} g_i * exp(-(t - a_i)^2 / s_i^2)

with a_1 = 0, a_2 = 0.6, a_{i+1} = a_i + 0.6 * 1.6^(i-1), s_1 = 0.6, s_{i+1} = 1.6 * s_i, and
t in years. The published standard yields are effective annual: Y = exp(G / 10000) - 1.
The builder checks itself against them (``check_against_standard_yields``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from rub_fixed_income import CurveConvention, DayCount, DiscountCurve

SPEC_ID = "rub_ofz_kbd_v1"
MODEL_VERSION = "desk-kbd-nss-v1"

_K = 1.6
_A = [0.0, 0.6]
for _i in range(2, 9):
    _A.append(_A[-1] + 0.6 * _K ** (_i - 1))
_S = [0.6]
for _i in range(1, 9):
    _S.append(_S[-1] * _K)


@dataclass(frozen=True)
class KbdCurve:
    curve_date: date
    params: dict[str, float]

    def g_bp(self, t: float) -> float:
        """Continuously compounded zero rate in bp at ``t`` years."""
        p = self.params
        t = max(t, 1e-6)
        tau = p["T1"]
        e = math.exp(-t / tau)
        v = p["B1"] + (p["B2"] + p["B3"]) * (tau / t) * (1.0 - e) - p["B3"] * e
        for i in range(9):
            v += p[f"G{i + 1}"] * math.exp(-((t - _A[i]) ** 2) / _S[i] ** 2)
        return v

    def yield_pct(self, t: float) -> float:
        """Effective annual yield in percent (as MOEX publishes it)."""
        return (math.exp(self.g_bp(t) / 10_000.0) - 1.0) * 100.0

    def discount_curve(self, horizon_years: int = 40) -> DiscountCurve:
        """Core ``DiscountCurve`` with nodes every 7 days for 2 years, then monthly.

        Time is ACT/365F from the curve date, as in the KBD formula; core interpolates
        log-DF linearly between nodes, which reproduces G(t) to well below 0.01 bp.
        """
        nodes = []
        d = self.curve_date
        end = self.curve_date + timedelta(days=int(365.25 * horizon_years))
        while d < end:
            d = d + timedelta(days=7) if (d - self.curve_date).days < 730 else d + timedelta(days=30)
            t = (d - self.curve_date).days / 365.0
            nodes.append((d, math.exp(-self.g_bp(t) / 10_000.0 * t)))
        return DiscountCurve(self.curve_date, tuple(nodes), MODEL_VERSION,
                             convention=CurveConvention(time_day_count=DayCount.ACT_365_FIXED))


def from_zcyc(curve_date: date, params: dict[str, float]) -> KbdCurve:
    missing = {"B1", "B2", "B3", "T1", *(f"G{i}" for i in range(1, 10))} - set(params)
    if missing:
        raise ValueError(f"ZCYC parameters missing: {sorted(missing)}")
    return KbdCurve(curve_date, params)


def check_against_standard_yields(curve: KbdCurve, standard_yields: list[dict[str, str]]) -> float:
    """Largest absolute difference in bp between the curve and the published standard yields."""
    worst = 0.0
    for row in standard_yields:
        t = float(row["tenor_years"])
        worst = max(worst, abs(curve.yield_pct(t) - float(row["yield_pct"])) * 100.0)
    return worst
