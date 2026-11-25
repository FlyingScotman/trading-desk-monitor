"""Recalculate flagged market quotes from the newest classified static. No snapshot files at read time."""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from psycopg.rows import dict_row
from rub_fixed_income import (
    BondCashflow,
    BondMetricsInput,
    BusinessCalendar,
    CoreRejection,
    Instrument,
    RateAccrualCouponLeg,
    ZcycParameters,
    accrued_interest,
    bond_metrics_batch,
    clean_price_pct_to_dirty_rub,
    g_spread_bp,
    kbd_zero_rate_bp,
    solve_z_spread,
)

from ..valuation.bonds import (
    BROKEN_REDEMPTION_LABELS,
    COUPON_DAY_COUNTS,
    MANUAL_OVERRIDE_LABEL,
    SPREAD_CONVENTION,
    Rejected,
    coupon_day_count,
    face_on,
    fixed_schedule,
    settlement_date,
)
from .adapter import BondTerms, StaticView, bond_currency, ccy_code, core_calendar, core_instrument
from .bundle import Snapshot

ANALYTICS_COLUMNS = ("yield_pct", "duration_days", "gspread_bp", "zspread_bp", "yield_date")
KNOWN_COUPONS = ("fixed", "variable_known", "zero")
REFUSALS = {
    "calendar_unknown": "нет календаря для расчётов Т+1",
    "coupon_not_known": "график купонов не подтверждён",
    "no_face_value": "нет остаточного номинала",
    "face_mismatch": "номинал в котировке не сходится с графиком",
    "amortization_transition": ("день перехода амортизации: номинал котировки отсутствует или не совпадает "
                                "с номиналом на дату расчётов Т+1"),
    "redemption_incomplete": "график погашений не сходится с номиналом",
    "redemption_amount_missing": "график погашений неполный",
    "matured": "после расчётов Т+1 нет выплат",
}


def moex_bond_analytics_unreliable(labels: Collection[str] = (), reasons: Collection[str] = (),
                                  quote_flag: dict[str, Any] | None = None) -> bool:
    """The market's yield, horizon, duration and spreads depend on the same redemption schedule."""
    if reasons or any(label in labels for label in BROKEN_REDEMPTION_LABELS):
        return True
    fields = ("effective_yield_pct", "yield_close_pct", "yield_date", "duration_days", "gspread_bp", "zspread_bp")
    return bool(quote_flag) and any(f in quote_flag["fields"] for f in fields)


@dataclass(frozen=True)
class StaticTerms:
    bond: BondTerms
    calendar: BusinessCalendar | None
    calendar_to: date | None

    @property
    def manual(self) -> bool:
        return MANUAL_OVERRIDE_LABEL in self.bond.labels


def _face_anchor(bond: BondTerms, as_of: date) -> BondTerms:
    # face_on interprets the anchor as before that date's payments. An exact post-payment balance
    # on the cut date is also possible; placing its anchor at the next morning preserves both dates.
    if any(r.date == as_of for r in bond.redemptions):
        future = [r.value_per_bond for r in bond.redemptions if r.date > as_of]
        if future and all(v is not None for v in future) and sum(v for v in future if v is not None) == bond.face_value:
            return replace(bond, face_date=as_of + timedelta(days=1))
    return bond


def _terms(con, secids: list[str]) -> dict[str, StaticTerms]:
    sql = """
        SELECT a.secid, a.snapshot_id, a.business_date,
               b.data->'market_valuation_terms' AS terms, s.data->'market_valuation_calendars' AS calendars
          FROM md_bond_analytics_static a JOIN md_static_bond b USING (secid, snapshot_id)
          JOIN md_static_summary s USING (snapshot_id)
         WHERE a.secid = ANY(%s)
    """
    result = {}
    calendars: dict[tuple[str, str], tuple[BusinessCalendar, date]] = {}
    with con.cursor(row_factory=dict_row) as cur:
        for r in cur.execute(sql, (secids,)):
            if r["terms"] is None:
                continue
            data = {**r["terms"], "issuers": [], "calendars": r["calendars"] or []}
            sid = r["snapshot_id"]
            try:
                view = StaticView(Snapshot(sid, "static", "", r["business_date"].isoformat(), None, {"data": data}))
                bond = _face_anchor(view.bonds[r["secid"]], r["business_date"])
                key = (sid, bond.calendar)
                if key not in calendars and (cal := view.calendars.get(bond.calendar)) is not None:
                    calendars[key] = core_calendar(cal)[0], date.fromisoformat(cal["valid_to"])
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue  # the quote gets an explicit missing-terms reason
            result[bond.secid] = StaticTerms(bond, *calendars.get(key, (None, None)))
    return result


def _accrued(inst: Instrument, terms: BondTerms, on: date, *, contractual: bool) -> float:
    """Unrounded accrual. Core's standard corporate accrual rounds to cents, too coarse for small face values."""
    leg = inst.coupon_leg
    if not isinstance(leg, RateAccrualCouponLeg):
        return accrued_interest(inst.cashflows(), on, quantum=None)
    for p in leg.periods:
        if p.accrual_start <= on < p.accrual_end:
            if contractual:
                dc = coupon_day_count(terms)
                if dc is None:
                    raise Rejected("day_count_missing", "конвенция НКД в валюте номинала не известна")
                days, year = COUPON_DAY_COUNTS[dc](p.accrual_start, on)
            else:
                days, year = leg.accrual_basis.days(p.accrual_start, on)
            return float(face_on(terms, p.accrual_start)) * p.rate * days / year
    if not leg.periods or on >= leg.periods[-1].accrual_end:
        return 0.0
    raise Rejected("coupon_not_known")


def _input(q: dict[str, Any], static: StaticTerms | None) -> tuple[BondMetricsInput, str, date, str]:
    if q["clean_pct"] is None:
        raise Rejected("no_price", "нет чистой цены")
    if static is None:
        raise Rejected("no_terms", "нет условий выпуска в статике")
    terms, cal = static.bond, static.calendar
    if terms.coupon_type not in KNOWN_COUPONS:
        raise Rejected("coupon_type", "флоатер или неизвестные купоны")
    day = q["business_date"]
    if cal is None or cal.valid_from is None or static.calendar_to is None or day < cal.valid_from:
        raise Rejected("calendar_unknown")
    settle = settlement_date(day, 1, cal, cal.valid_from, static.calendar_to)
    if static.manual:
        # The source can round a coupon to zero while publishing its confirmed positive rate.
        # fixed_schedule derives a missing amount from the rate and the corrected principal schedule.
        terms = replace(terms, coupons=tuple(
            replace(c, amount_per_bond=None)
            if c.status in ("known", "current_published") and c.amount_per_bond == 0 and c.rate_pct and c.rate_pct > 0
            else c for c in terms.coupons))
    sch = fixed_schedule(terms, day, offer_on=settle)
    if sch.horizon_kind == "perpetual" or sch.horizon_date <= settle:
        raise Rejected("horizon", "после расчётов Т+1 нет конечного горизонта")
    if ((terms.coupon_type != "zero" and not sch.coupons)
            or any(c.status not in ("known", "current_published") for c in sch.coupons)):
        raise Rejected("coupon_not_known")
    settle_face = float(face_on(terms, settle))
    amortizes = not math.isclose(settle_face, sch.current_face, rel_tol=1e-8, abs_tol=1e-9)
    quote_face = q["face_value"]
    if quote_face is None:
        if amortizes:
            raise Rejected("amortization_transition")
    elif not math.isclose(quote_face, settle_face, rel_tol=1e-8, abs_tol=1e-9):
        raise Rejected("amortization_transition" if amortizes else "face_mismatch")
    inst = core_instrument(terms, day, sch.horizon_date, sch.offer, coupons=sch.coupons)
    if not math.isclose(inst.initial_face, sch.current_face, rel_tol=1e-8, abs_tol=1e-9):
        raise Rejected("redemption_incomplete")
    horizon, offer = inst.horizon("open_put", settle)
    if horizon != sch.horizon_date:
        raise Rejected("offer", "условия исполнения оферты не подтверждены")
    if sch.offer and not any(o.date == horizon and o.price_pct is not None for o in terms.offers):
        raise Rejected("offer", "цена оферты не известна")
    if not q["currency_id"] or not terms.face_unit:
        raise Rejected("no_currency", "не известна валюта НКД или номинала")
    currency = bond_currency(terms)
    quote_ccy = ccy_code(q["currency_id"])
    crosses_coupon = any(day < c.pay_date <= settle for c in sch.coupons)
    accrual_note = ""
    if crosses_coupon:
        accrued = inst.accrued(settle)
        accrual_note = " НКД пересчитан ядром на дату расчётов после выплаты купона."
    elif quote_ccy != currency:
        # ACCINT is in the settlement currency. Compute contractual accrual in face currency.
        accrued = _accrued(inst, terms, settle, contractual=True)
        accrual_note = " НКД рассчитан в валюте номинала по графику."
    else:
        if q["accrued_per_bond"] is None:
            raise Rejected("no_accrued", "нет НКД на дату сделки")
        accrued = q["accrued_per_bond"] + _accrued(inst, terms, settle, contractual=False) - _accrued(
            inst, terms, day, contractual=False)
    dirty = clean_price_pct_to_dirty_rub(q["clean_pct"], settle_face, accrued)
    cash = inst.cashflows(after=settle, horizon=horizon, offer=offer)
    item = BondMetricsInput(settle, dirty, tuple(BondCashflow(f.payment_date, f.amount) for f in cash))
    return item, currency, horizon, accrual_note


@dataclass(frozen=True)
class _KbdCurve:
    """Use the core KBD at requested dates. kbd_discount_curve builds and interpolates a full grid per quote date."""
    params: ZcycParameters
    factors: dict[date, float] = field(default_factory=dict)

    @property
    def curve_date(self) -> date:
        return self.params.trade_date

    def discount_factor(self, target: date) -> float:
        if target not in self.factors:
            t = (target - self.curve_date).days / 365
            self.factors[target] = math.exp(-kbd_zero_rate_bp(self.params, t) * t / 10_000) if t else 1.0
        return self.factors[target]


def recalculate(con, rows: Iterable[dict[str, Any]]) -> None:
    """Replace flagged rows in place. One yield/duration batch across all dates and one KBD lookup for RUB rows."""
    flagged = [q for q in rows if q["effective_yield_unreliable"]]
    if not flagged:
        return
    terms = _terms(con, sorted({q["secid"] for q in flagged}))
    ready = []
    for q in flagged:
        q.update(dict.fromkeys(ANALYTICS_COLUMNS))
        static = terms.get(q["secid"])
        q["yield_manual_override"] = bool(static and static.manual)
        try:
            item, currency, horizon, accrual_note = _input(q, static)
        except Rejected as e:
            reason = REFUSALS.get(e.reason, e.detail or "условия выпуска не поддерживаются")
            q["yield_note"] = "Аналитика MOEX недостоверна. " + reason[0].upper() + reason[1:] + "."
        except (CoreRejection, ValueError, ArithmeticError):
            q["yield_note"] = "Аналитика MOEX недостоверна. Ядро отклонило цену, НКД или график выплат."
        else:
            ready.append((q, item, currency, horizon, accrual_note))
    if not ready:
        return
    from .history import kbd

    rub_dates = sorted({q["business_date"] for q, _, c, _, _ in ready if c == "RUB"})
    with con.cursor(row_factory=dict_row) as cur:
        curves = {
            r["business_date"]: kbd(r["trade_date"], r["params"])
            for r in cur.execute("SELECT business_date, trade_date, params FROM md_kbd_day"
                                 " WHERE business_date = ANY(%s)", (rub_dates,))
        } if rub_dates else {}
    try:
        metrics = bond_metrics_batch([item for _, item, _, _, _ in ready])
    except (CoreRejection, ValueError, ArithmeticError):
        for q, *_ in ready:
            q["yield_note"] = "Аналитика MOEX недостоверна. Ядро отклонило расчёт доходности."
        return
    for (q, item, currency, horizon, accrual_note), m in zip(ready, metrics, strict=True):
        if m.effective_yield is None or m.macaulay_years is None:
            q["yield_note"] = "Аналитика MOEX недостоверна. Ядро не нашло доходность для этой цены и потоков."
            continue
        note = "Расчёт ядра по цене MOEX и графику выплат. Аналитика MOEX недостоверна."
        if q["yield_manual_override"]:
            note += " Применена ручная правка графика погашений."
        q.update(yield_pct=m.effective_yield * 100, duration_days=m.macaulay_years * 365, yield_date=horizon,
                 yield_source="core", yield_note=note + accrual_note)
        if currency != "RUB" or (params := curves.get(q["business_date"])) is None:
            continue
        if params.trade_date != q["business_date"]:
            continue
        try:
            q["gspread_bp"] = g_spread_bp(params, m.effective_yield, m.macaulay_years)
            q["zspread_bp"] = 10_000 * solve_z_spread(
                ((f.payment_date, f.amount) for f in item.cashflows), item.dirty_price, _KbdCurve(params),
                settlement_date=item.settlement_date, convention=SPREAD_CONVENTION)
        except (CoreRejection, ValueError, ArithmeticError):
            q["yield_note"] += " Расчёт спредов к КБД неполный."
