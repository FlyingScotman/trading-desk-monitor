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


class HeldFx:
    """``FxProvider`` of a forward period: every day after ``on`` at the rate in force on ``on`` (the forward carry
    holds FX; a table's ``max_age_days`` would otherwise make the rates of later days none)."""

    def __init__(self, table: FxProvider, on: date) -> None:
        self.table, self.on = table, on

    def rate(self, ccy: str, on: date) -> float | None:
        return self.table.rate(ccy, min(on, self.on))


class HeldRates:
    """``RateIndexProvider`` of a forward period, ``flat``: every index on every day after ``on`` at its value in force
    on ``on`` (a later row of the table, such as a key rate decided and not yet effective, is not used)."""

    def __init__(self, table: RateIndexProvider, on: date) -> None:
        self.table, self.on = table, on

    def rate(self, index: str, on: date) -> float | None:
        return self.table.rate(index, min(on, self.on))


KEY_RATE = "KEY_RATE"


def follows_key_rate(index: str) -> bool:
    """A RUB money-market index that moves with the key rate (``KeyRatePath``): RUONIA and term RUONIA, the RUB RUSFAR
    family (not ``RUSFARC*``, the CNY one), the CBR standing facilities. Any other index (SOFR, a CNY rate) is held."""
    return (index.startswith(("RUONIA", "TERM_RUONIA", "CBR_"))
            or (index.startswith("RUSFAR") and not index.startswith("RUSFARC")))


class KeyRatePath(HeldRates):
    """``RateIndexProvider`` of a forward period, ``steps``: the key rate in force on ``on`` moved by a step (decimal,
    0.0025 = 25 bp) from each effective date of ``steps`` on, never below 0 (as the Scenario & PnL Lab's path), and every
    index that ``follows_key_rate`` at its value on ``on`` plus the key rate's move since (today's spread to the key rate
    held: an assumption). Other indices are held. ``path``: (effective date, key rate) after each step."""

    def __init__(self, table: RateIndexProvider, on: date, steps: Iterable[tuple[date, float]]) -> None:
        super().__init__(table, on)
        self.ks0 = table.rate(KEY_RATE, on)
        self.path: list[tuple[date, float]] = []
        cur = self.ks0
        for eff, step in sorted(steps):
            if cur is not None and eff > on:
                cur = max(0.0, cur + step)
                self.path.append((eff, cur))
        self._days = [d for d, _ in self.path]

    def key_rate(self, on: date) -> float | None:
        i = bisect_right(self._days, on)
        return self.ks0 if not i else self.path[i - 1][1]

    def rate(self, index: str, on: date) -> float | None:
        base = super().rate(index, on)
        if base is None or on <= self.on or not (index == KEY_RATE or follows_key_rate(index)):
            return base
        ks = self.key_rate(on)
        return None if ks is None or self.ks0 is None else base + (ks - self.ks0)
