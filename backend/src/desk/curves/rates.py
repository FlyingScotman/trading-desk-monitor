"""RUB rates inputs through core: the RUONIA OIS curve, the key-rate projection curve and fixings.

Slice S4 (ADR-004). Each builder returns the core snapshot entry, the built curve and a lineage
dict for the run record and the curves panel.

**Quote-set choice** (``select_quote_set``): among the sets of the dataset dated the business date,
take one the hub does not flag stale, then one with bid and ask, then the latest fetched. Today that
is ``ratescalcs`` (bid/ask/mid, morning mark) when present and ``otc_monitor`` (mid only, flagged
"off-hours mark") otherwise; the stale reasons travel in the lineage.

**Calendars.** MOEX quotes OIS RUONIA on the joint SPFI, MOSCOW and RUONIA calendar. The snapshot
has no SPFI or RUONIA calendar, so SPFI is taken as ``MOEX_FORTS`` (the exchange's derivatives
calendar), MOSCOW as ``RU_PRODUCTION`` and RUONIA as ``ruonia_calendar``. Key-rate swaps use SPFI and
MOSCOW. The calendars are bounded; core raises ``CalendarRangeError`` outside.

**Calendar range.** The hub's calendars end on 2027-12-31; ``adapter.core_calendar`` extends them to
2037-12-31 by fixed rules (``lineage["calendar"]["provisional_from"]`` says where the official days stop),
so every tenor to 10Y is scheduled. A quote whose dates still fall outside is dropped with its reason in
``lineage["tenors_dropped"]``. The curve extrapolates flat-forward past its last node (``lineage["last_node"]``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from rub_fixed_income import (
    BusinessCalendar,
    CalendarRangeError,
    CurveBootstrapRequest,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FixingEntry,
    FixingHistory,
    KeyRateCurveBuildRequest,
    KeyRateSwapQuote,
    OisQuote,
    bootstrap_discount_curve,
    build_key_rate_forward_curve,
    forward_curve,
    joint_calendar,
    key_rate_projection_curve,
    ois_curve_convention,
    ois_quote_periods,
    rebase_key_rate_curve,
    resolve_tenor,
)

from desk.marketdata import adapter_rates as ar
from desk.marketdata.bundle import Snapshot

RUONIA_OIS_SPEC = "rub_ruonia_ois_moex_v2"
KEY_RATE_PROJECTION_SPEC = "rub_key_rate_projection_v1"
RUONIA_OIS_CURVE_ID, KEY_RATE_CURVE_ID = "RUB_RUONIA_OIS", "RUB_KEY_RATE_PROJ"
SELECTION_RULE = "business-date sets; not stale > bid_ask_mid > latest fetched_at"
RUONIA_MAX_UNPUBLISHED_DAYS = 1
"""RUONIA of day D is published on D+1, so one trailing business day may be unpublished."""


class RatesInputError(ValueError):
    pass


class QuoteSetMissing(RatesInputError):
    """The market snapshot has no quote set of the dataset dated its business date: a coverage gap of the hub
    (``curve_quote_sets`` coverage ``missing``), not a build error."""

    def __init__(self, message: str, dataset: str) -> None:
        super().__init__(message)
        self.dataset = dataset


@dataclass(frozen=True)
class RatesCurve:
    entry: CurveEntry
    curve: DiscountCurve
    lineage: dict[str, Any]


def ruonia_calendar(static: Snapshot, since: date) -> tuple[BusinessCalendar, date | None]:
    """RUONIA publication calendar: ``RU_PRODUCTION`` plus, from ``since`` on, the last business day of
    each year as a holiday (CBR does not compute RUONIA that day). Before ``since`` the hub's
    ``RU_PRODUCTION`` already follows the observed RUONIA days (2024-12-28, 2025-12-30 are holidays there)."""
    base, prov = ar.calendar(static, "RU_PRODUCTION")
    year_ends = set()
    for year in range(since.year, base.valid_to.year + 1):
        if base.covers(d := date(year, 12, 31)):
            while not base.is_business_day(d):
                d -= timedelta(days=1)
            if d >= since:
                year_ends.add(d)
    return BusinessCalendar(base.holidays | year_ends, base.working_days - year_ends,
                            f"RUONIA_PUB({base.version}, year-end from {since})", base.valid_from, base.valid_to), prov


def _joint(parts: list[tuple[BusinessCalendar, date | None]]) -> tuple[BusinessCalendar, dict[str, Any]]:
    cal = joint_calendar([c for c, _ in parts], version="+".join(c.version for c, _ in parts))
    provisional = min((p for _, p in parts if p is not None), default=None)
    return cal, {"version": cal.version, "valid_from": cal.valid_from.isoformat(), "valid_to": cal.valid_to.isoformat(),
                 "provisional_from": provisional and provisional.isoformat()}


def ois_calendar(static: Snapshot, business_date: date) -> tuple[BusinessCalendar, dict[str, Any]]:
    """Joint SPFI (MOEX_FORTS) + MOSCOW (RU_PRODUCTION) + RUONIA publication calendar."""
    return _joint([ar.calendar(static, "MOEX_FORTS"), ar.calendar(static, "RU_PRODUCTION"),
                   ruonia_calendar(static, business_date)])


def krs_calendar(static: Snapshot) -> tuple[BusinessCalendar, dict[str, Any]]:
    """Joint SPFI (MOEX_FORTS) + MOSCOW (RU_PRODUCTION): MOEX IRS KEYRATE."""
    return _joint([ar.calendar(static, "MOEX_FORTS"), ar.calendar(static, "RU_PRODUCTION")])


def select_quote_set(market: Snapshot, dataset: str) -> tuple[ar.QuoteSet, dict[str, Any]]:
    bd = date.fromisoformat(market.business_date)
    sets = ar.quote_sets(market, dataset)
    dated = [s for s in sets if s.snapshot_date == bd]
    if not dated:
        raise QuoteSetMissing(f"{market.business_date}: no {dataset} quote set dated the business date "
                              f"(present: {[(s.source, s.snapshot_date.isoformat()) for s in sets]})", dataset)
    best = min(dated, key=lambda s: (bool(s.stale_reasons), s.quote_basis != "bid_ask_mid", -s.fetched_at.timestamp()))
    return best, {
        "dataset": dataset, "quote_set_id": best.quote_set_id, "source": best.source, "quote_basis": best.quote_basis,
        "fetched_at": best.fetched_at.isoformat(), "stale_reasons": list(best.stale_reasons),
        "selection_rule": SELECTION_RULE,
        "alternatives": [{"quote_set_id": s.quote_set_id, "source": s.source} for s in dated if s is not best]}


def ruonia_ois(market: Snapshot, static: Snapshot, curve_id: str = RUONIA_OIS_CURVE_ID) -> RatesCurve:
    bd = date.fromisoformat(market.business_date)
    qs, lineage = select_quote_set(market, "ruonia_ois")
    cal, cal_info = ois_calendar(static, bd)
    convention = ois_curve_convention(RUONIA_OIS_SPEC, cal)
    quotes, dropped = [], {}
    for q in qs.quotes:
        if q.mid is None:
            dropped[q.tenor] = "no mid"
            continue
        try:
            quote = OisQuote(q.tenor, resolve_tenor(bd, q.tenor, cal).end_date, q.mid)
            ois_quote_periods(bd, quote, convention)  # the payment dates must lie in the calendar too
        except CalendarRangeError as e:
            dropped[q.tenor] = f"calendar range: {e}"
            continue
        quotes.append(quote)
    if not quotes:
        raise RatesInputError(f"{market.business_date}: no usable RUONIA OIS quote in set {qs.quote_set_id}: {dropped}")
    request = CurveBootstrapRequest(bd, tuple(quotes), convention)
    curve = bootstrap_discount_curve(request)
    lineage.update(spec_id=RUONIA_OIS_SPEC, curve_id=curve_id, curve_date=bd.isoformat(), calendar=cal_info,
                   tenors_used=[q.tenor for q in quotes], tenors_dropped=dropped,
                   last_node=curve.nodes[-1][0].isoformat(),
                   max_repricing_error_bp=curve.max_repricing_error_bp(quotes))
    return RatesCurve(CurveEntry(curve_id, RUONIA_OIS_SPEC, request), curve, lineage)


def key_rate_in_force(market: Snapshot) -> tuple[float, dict[str, Any]]:
    """The key rate in force on the business date: the latest KEY_RATE row on or before it, which the
    latest decided CBR meeting effective by then must confirm."""
    bd = date.fromisoformat(market.business_date)
    rows = [f for f in ar.fixings(market, "KEY_RATE") if f.fixing_date <= bd]
    if not rows:
        raise RatesInputError(f"{market.business_date}: no KEY_RATE fixing on or before the business date")
    decided = [m for m in ar.meetings(market) if m.effective_date <= bd and not m.is_estimated and m.rate is not None]
    if decided and decided[-1].rate != rows[-1].rate:
        raise RatesInputError(f"{market.business_date}: KEY_RATE {rows[-1].rate} on {rows[-1].fixing_date} differs from "
                              f"the CBR decision of {decided[-1].meeting_date} ({decided[-1].rate})")
    return rows[-1].rate, {"key_rate_fixing_date": rows[-1].fixing_date.isoformat(),
                           "decision_meeting": decided[-1].meeting_date.isoformat() if decided else None}


def key_rate_projection(market: Snapshot, static: Snapshot, discount: DiscountCurve,
                        curve_id: str = KEY_RATE_CURVE_ID, *, rate_day_count: DayCount = DayCount.ACT_365_FIXED,
                        observation_days: Literal["business", "calendar"] = "calendar") -> RatesCurve:
    """Key-rate projection from MOEX KEYRATE swaps, discounted on ``discount`` (the RUONIA OIS curve).

    The defaults match the key-rate corporates (calendar-day observation, ACT/365F); a floater family
    observed otherwise needs its own curve.
    """
    bd = date.fromisoformat(market.business_date)
    qs, lineage = select_quote_set(market, "key_rate_swap")
    cal, cal_info = krs_calendar(static)
    current, current_info = key_rate_in_force(market)
    tom = cal.add_business_days(bd, 1)
    nxt = next((m for m in ar.meetings(market) if m.effective_date > bd), None)
    if nxt is None:
        raise RatesInputError(f"{market.business_date}: no CBR meeting effective after the business date")
    if nxt.effective_date <= tom:
        raise RatesInputError(f"{market.business_date}: a key-rate decision effective on {nxt.effective_date} (TOM) "
                              "cannot be expressed by core rebase_key_rate_curve")
    quotes, dropped = [], {}
    for q in qs.quotes:
        if q.mid is None:
            dropped[q.tenor] = "no mid"
            continue
        try:
            resolve_tenor(bd, q.tenor, cal)
        except CalendarRangeError as e:
            dropped[q.tenor] = f"calendar range: {e}"
            continue
        # A mid-only set has no sides: bid = ask = mid (the bid/ask scenarios then equal the mid).
        quotes.append(KeyRateSwapQuote(q.tenor, q.bid if q.bid is not None else q.mid, q.mid,
                                       q.ask if q.ask is not None else q.mid))
    if not quotes:
        raise RatesInputError(f"{market.business_date}: no usable key-rate swap quote in set {qs.quote_set_id}: {dropped}")
    built = build_key_rate_forward_curve(KeyRateCurveBuildRequest(
        bd, current, nxt.effective_date, tuple(quotes), forward_curve(discount, tom), cal))
    fwd = rebase_key_rate_curve(built.mid.curve, bd, rate_in_force=current)
    curve = key_rate_projection_curve(fwd, cal, rate_day_count=rate_day_count, observation_days=observation_days)
    lineage.update(
        spec_id=KEY_RATE_PROJECTION_SPEC, curve_id=curve_id, curve_date=bd.isoformat(), calendar=cal_info,
        current_rate=current, **current_info, next_policy_effective_date=nxt.effective_date.isoformat(),
        next_policy_estimated=nxt.is_estimated, tenors_used=[q.tenor for q in quotes], tenors_dropped=dropped,
        discount_curve_date=tom.isoformat(), last_node=curve.nodes[-1][0].isoformat(),
        rate_day_count=rate_day_count.name, observation_days=observation_days,
        max_repricing_error_bp=built.mid.max_repricing_error_bp, usable=built.usable,
        segments=[{"start": s.start_date.isoformat(), "end": s.end_date.isoformat(), "rate": s.rate,
                   "tenor": s.pillar_tenor} for s in fwd.segments])
    return RatesCurve(CurveEntry(curve_id, KEY_RATE_PROJECTION_SPEC, curve), curve, lineage)


def fixing_entries(market: Snapshot, static: Snapshot,
                   known_at: datetime | None = None) -> tuple[tuple[FixingEntry, ...], dict[str, Any]]:
    """RUONIA and KEY_RATE histories as known at ``known_at`` (default: the snapshot's ``known_at``).

    Kept: rows dated before the business date (core values from it on), published by ``known_at``:
    ``published_at <= known_at``, or, with only a ``published_on`` date, ``published_on`` before the
    ``known_at`` date (the time that day is unknown, so the same day counts as not yet published).
    RUONIA rows before its calendar's ``valid_from`` are dropped (core cannot check them).
    """
    bd = date.fromisoformat(market.business_date)
    cut = known_at or ar.known_at(market)

    def known(f: ar.Fixing) -> bool:
        if f.published_at is not None:
            return f.published_at <= cut
        return f.published_on is None or f.published_on < cut.date()

    entries, lineage = [], {"known_at": cut.isoformat()}
    for code, index, (cal, _) in (("RUONIA", "ruonia", ruonia_calendar(static, bd)),
                                  ("KEY_RATE", "key_rate", ar.calendar(static, "RU_PRODUCTION"))):
        rows = [f for f in ar.fixings(market, code) if f.fixing_date < bd]
        unknown = [f.fixing_date.isoformat() for f in rows if not known(f)]
        rows = [f for f in rows if known(f)]
        early = [f for f in rows if index == "ruonia" and f.fixing_date < cal.valid_from]
        rows = rows[len(early):]
        history = FixingHistory(index, bd, tuple((f.fixing_date, f.rate) for f in rows), cal, market.id,
                                RUONIA_MAX_UNPUBLISHED_DAYS if index == "ruonia" else 0, cut.isoformat())
        entries.append(FixingEntry(code, history))
        info = {"rows": len(rows), "first": rows[0].fixing_date.isoformat() if rows else None,
                "latest": rows[-1].fixing_date.isoformat() if rows else None, "unpublished_at_cut": unknown,
                "dropped_before_calendar": len(early), "calendar": cal.version}
        if index == "ruonia" and rows:
            have = {f.fixing_date for f in rows}
            days = (rows[0].fixing_date + timedelta(days=i) for i in range((rows[-1].fixing_date - rows[0].fixing_date).days))
            info["missing_business_days"] = [d.isoformat() for d in days if cal.is_business_day(d) and d not in have]
            info["unpublished_business_days"] = [d.isoformat() for d in history.unpublished_business_days]
        lineage[code] = info
    return tuple(entries), lineage


# ---- what a run lacks (R14, R15): codes for the run log, the pages and the diagnostics

RATES_DATASETS = ("ruonia_ois", "key_rate_swap")
INPUT_CODE = {"missing": "curve_missing", "error": "curve_error", "no_discount": "curve_unbuilt"}
INPUT_TEXT = {
    "curve_missing:ruonia_ois": "нет кривой RUONIA в срезе хаба за {d} (нет котировок RUONIA OIS) — облигации "
                                "дисконтированы по КБД, риск отнесён на ОФЗ (RUB OFZ)",
    "curve_error:ruonia_ois": "кривая RUONIA за {d} не построена из котировок RUONIA OIS — облигации дисконтированы "
                              "по КБД, риск отнесён на ОФЗ (RUB OFZ)",
    "curve_missing:key_rate_swap": "нет кривой ключевой ставки в срезе хаба за {d} (нет котировок IRS KEYRATE) — "
                                   "флоатеры на КС оценены запасной ступенью, риск КС по дюрации",
    "curve_error:key_rate_swap": "кривая ключевой ставки за {d} не построена из котировок IRS KEYRATE — флоатеры на "
                                 "КС оценены запасной ступенью, риск КС по дюрации",
    "curve_unbuilt:key_rate_swap": "кривая ключевой ставки за {d} не строится без кривой RUONIA — флоатеры на КС "
                                   "оценены запасной ступенью, риск КС по дюрации",
    "fixings_error": "фиксинги RUONIA и КС за {d} не прочитаны — флоатеры оценены запасной ступенью",
}
"""Russian text of each code, ``{d}``: the business date (dd.mm)."""
INPUT_CURVE = {"ruonia_ois": "RUB RUONIA", "key_rate_swap": "RUB KEY"}
"""The published curve (``valuation.bonds.RISK_CURVES``) a code's dataset gives."""


def quote_set_coverage(market: Snapshot) -> dict[str, dict[str, list[str]]]:
    """The hub's coverage of the RUONIA OIS and key-rate swap series in ``market``: per dataset the sources present and
    the expected ones missing (``curve_quote_sets`` coverage: ``source/dataset``)."""
    ds = next((d for d in market.doc.get("datasets") or () if d.get("name") == "curve_quote_sets"), None)
    cov = (ds or {}).get("coverage") or {}
    out = {}
    for name in RATES_DATASETS:
        out[name] = {k: sorted(s.split("/")[0] for s in cov.get(k) or () if s.endswith("/" + name))
                     for k in ("present", "missing")}
    return out


def input_codes(status: dict[str, str]) -> list[str]:
    """``curve_missing:ruonia_ois``, ``curve_error:key_rate_swap``, ``curve_unbuilt:key_rate_swap``, ``fixings_error``
    for the inputs of ``orchestrator.with_rates`` that are not ``ok``."""
    out = [f"{INPUT_CODE[status[n]]}:{n}" for n in RATES_DATASETS if status.get(n, "ok") != "ok"]
    return out + (["fixings_error"] if status.get("fixings", "ok") != "ok" else [])


def _inferred_codes(lineage: dict[str, Any]) -> list[str]:
    """The codes of a run made before ``with_rates`` recorded them, from its error messages."""
    err, out = lineage.get("curve_error") or "", []
    if "ruonia_ois" not in lineage and err:
        out += ["curve_missing:ruonia_ois" if "no ruonia_ois quote set" in err else "curve_error:ruonia_ois",
                "curve_unbuilt:key_rate_swap"]
    elif "key_rate" not in lineage and err:
        out.append("curve_missing:key_rate_swap" if "no key_rate_swap quote set" in err else "curve_error:key_rate_swap")
    return out + (["fixings_error"] if lineage.get("fixings_error") else [])


def input_notes(lineage: dict[str, Any] | None, business_date: date | str) -> list[dict[str, Any]]:
    """What the run lacks, one ``{code, curve, text, detail}`` per code of its rates lineage (``with_rates``; inferred
    from the error messages for an older run): ``curve`` is the published curve that is missing (None: fixings),
    ``text`` the Russian line the run log and the risk pages show, ``detail`` the error (no position data)."""
    if not lineage:
        return []
    d = date.fromisoformat(business_date) if isinstance(business_date, str) else business_date
    codes = lineage.get("codes")
    if codes is None:
        codes = _inferred_codes(lineage)
    out = []
    for code in codes:
        name = code.partition(":")[2]
        out.append({"code": code, "curve": INPUT_CURVE.get(name),
                    "text": INPUT_TEXT.get(code, code).format(d=f"{d:%d.%m}"), "detail": _detail(code, lineage)})
    return out


def _detail(code: str, lineage: dict[str, Any]) -> str | None:
    if code == "fixings_error":
        return lineage.get("fixings_error")
    if code.startswith("curve_unbuilt"):
        return None
    if code.endswith(":key_rate_swap"):
        return lineage.get("key_rate_error") or lineage.get("curve_error")
    return lineage.get("curve_error")
