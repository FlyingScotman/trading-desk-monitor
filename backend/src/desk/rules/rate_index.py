"""Index fixings and FX rates for the funding engine, behind two small protocols, so the run can feed them
from the hub's market snapshot (``marketdata.adapter_rates.fixings``: KEY_RATE, RUONIA, SOFR, ...) and the
tests from tables.

**Value in force on a day:** the latest row dated on or before it. A key-rate decision counts from its
effective date, so a change effective on Monday leaves Saturday and Sunday at the old rate; over a weekend
or a holiday without a fixing the last fixing holds. ``max_age_days`` bounds that carry-forward: a row
older than that is no value (``None``), so a series the hub stopped delivering is not silently extended.

**Units.** Index rates are decimals (0.16 = 16 %), as ``adapter_rates`` gives them. FX rates are units of
the reporting currency per unit of the currency; the reporting currency itself is 1.

**Alignment** of a fixing to an accrual day (an overnight fixing of D is the rate of the night D -> D+1 and
is published on D+1) is the provider's business: a provider with a lag wraps another one. The engine asks
for "the rate of accrual day d" only.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any, Protocol


class RateIndexProvider(Protocol):
    def rate(self, index: str, on: date) -> float | None:
        """The index rate (decimal) for accrual day ``on``, or None when there is none."""
        ...


class FxProvider(Protocol):
    def rate(self, ccy: str, on: date) -> float | None:
        """Units of the reporting currency per unit of ``ccy`` in force on ``on``, or None."""
        ...


class _AsOf:
    """One dated series; ``at(d)`` is the latest value dated on or before ``d``."""

    def __init__(self, name: str, rows: Mapping[date, float] | Iterable[tuple[date, float]]) -> None:
        pairs = sorted(rows.items() if isinstance(rows, Mapping) else rows)
        days = [d for d, _ in pairs]
        if len(set(days)) != len(days):
            raise ValueError(f"{name}: duplicate dates")
        self.days, self.values = days, [float(v) for _, v in pairs]

    def at(self, on: date, max_age_days: int | None) -> float | None:
        i = bisect_right(self.days, on)
        if not i:
            return None
        if max_age_days is not None and (on - self.days[i - 1]).days > max_age_days:
            return None
        return self.values[i - 1]


class FixingTable:
    """``RateIndexProvider`` over in-memory series: ``{index: {date: rate}}`` (or ``[(date, rate)]``)."""

    def __init__(self, series: Mapping[str, Mapping[date, float] | Iterable[tuple[date, float]]], *,
                 max_age_days: int | None = None) -> None:
        self._series = {k: _AsOf(k, v) for k, v in series.items()}
        self.max_age_days = max_age_days

    @classmethod
    def from_fixings(cls, fixings: Iterable[Any], *, max_age_days: int | None = None) -> FixingTable:
        """From rows with ``index_code``, ``fixing_date`` and ``rate`` (``adapter_rates.Fixing``)."""
        series: dict[str, list[tuple[date, float]]] = {}
        for f in fixings:
            if f.rate is not None:
                series.setdefault(f.index_code, []).append((f.fixing_date, f.rate))
        return cls(series, max_age_days=max_age_days)

    @property
    def indices(self) -> tuple[str, ...]:
        return tuple(sorted(self._series))

    def rate(self, index: str, on: date) -> float | None:
        s = self._series.get(index)
        return None if s is None else s.at(on, self.max_age_days)


class FxTable:
    """``FxProvider`` over in-memory series ``{ccy: {date: rate}}``; ``reporting_ccy`` is always 1."""

    def __init__(self, reporting_ccy: str, series: Mapping[str, Mapping[date, float] | Iterable[tuple[date, float]]],
                 *, max_age_days: int | None = None) -> None:
        self.reporting_ccy = reporting_ccy
        self._series = {k: _AsOf(k, v) for k, v in series.items()}
        self.max_age_days = max_age_days

    def rate(self, ccy: str, on: date) -> float | None:
        if ccy == self.reporting_ccy:
            return 1.0
        s = self._series.get(ccy)
        return None if s is None else s.at(on, self.max_age_days)
