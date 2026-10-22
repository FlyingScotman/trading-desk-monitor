"""Adapter ``mdhub_v1``: Market Snapshot Contract v1 -> backend domain objects (ADR-003).

This is the only module that knows the hub's wire format. It converts decimal strings once,
keeps every hub fact it needs (coupon status, price basis, labels) and builds core
``BusinessCalendar`` objects from the snapshot's calendars. It never fills gaps: a missing
value stays ``None`` and the valuation layer decides what that means.

The hub facts live here as ``BondTerms`` / ``BondQuote``. ``core_instrument``,
``price_observation`` and ``MarketView.core_market`` map them to core's portfolio types
(``Instrument``, ``BondPriceObservation``, ``MarketSnapshot``) once the valuation layer has
decided that a bond is in scope and what its horizon is.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from rub_fixed_income import (
    AccrualBasis,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    CashflowKind,
    CouponPeriod,
    CurveEntry,
    FixedCouponLeg,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    Instrument,
    InvalidTerms,
    MarketSnapshot,
    OfferKind,
    PrincipalLeg,
    ProductType,
    RateAccrualCouponLeg,
    RateCouponPeriod,
    ZcycParameters,
)
from rub_fixed_income import Offer as CoreOffer
from rub_fixed_income import Redemption as CoreRedemption

from .bundle import Snapshot, SnapshotError


def _d(v: Any) -> Decimal | None:
    return None if v is None else Decimal(v)


def _f(v: Any) -> float | None:
    return None if v is None else float(Decimal(v))


def _date(v: Any) -> date | None:
    return None if v is None else date.fromisoformat(v)


@dataclass(frozen=True)
class Coupon:
    no: int
    start: date | None
    pay_date: date
    record_date: date | None
    rate_pct: Decimal | None
    amount_per_bond: Decimal | None
    status: str
    face: Decimal | None = None
    """Face outstanding during the period (an amortizer's coupon accrues on it)."""


@dataclass(frozen=True)
class Redemption:
    date: date
    kind: str
    value_per_bond: Decimal | None


@dataclass(frozen=True)
class Offer:
    date: date | None
    type: str
    status: str
    price_pct: Decimal | None
    window_end: date | None
    window_start: date | None = None
    """The holders' exercise window (MOEX ``offerdatestart`` / ``offerdateend``): core's ``open_put`` horizon skips a
    put whose window closed before the settlement date."""


@dataclass(frozen=True)
class BondTerms:
    secid: str
    isin: str | None
    name: str
    short_name: str | None
    bond_class: str
    status: str
    issuer_id: int | None
    currency: str | None
    face_unit: str | None
    face_value: Decimal | None
    coupon_type: str
    coupon_frequency: int | None
    maturity_date: date | None
    is_perpetual: bool
    is_structured: bool
    is_face_indexed: bool
    is_fx_face: bool
    is_amortizing: bool
    calendar: str
    primary_board: str | None
    settle_lag_days: int | None
    labels: tuple[str, ...]
    coupons: tuple[Coupon, ...]
    redemptions: tuple[Redemption, ...]
    offers: tuple[Offer, ...]
    floater_formula: dict[str, Any] | None
    face_date: date | None = None
    """The static snapshot's business date: ``face_value`` is the face outstanding then."""
    initial_face_value: Decimal | None = None
    """The face at issue (static ``initial_face_value``), before amortization and indexation."""
    lat_name: str | None = None
    """MOEX LATNAME (static 1.1, HS12)."""
    issue_size: Decimal | None = None
    """Number of bonds of the issue as MOEX lists it (``issue_size``; for OFZ the declared volume)."""


@dataclass(frozen=True)
class Issuer:
    issuer_id: int
    name: str
    short_name: str | None
    inn: str | None
    sector: str | None
    is_sovereign: bool
    group_id: str | None = None
    """Static 1.1 (HS4): the issuer's group, curated by the hub (``group_name`` its display name)."""
    group_name: str | None = None
    is_state_owned: bool | None = None
    country: str | None = None


@dataclass(frozen=True)
class BondQuote:
    secid: str
    board: str
    trade_date: date
    face_value: Decimal | None
    accrued_per_bond: Decimal | None
    prices_pct: dict[str, Decimal]
    """Every published price basis that is present: legal_close, close, waprice, market_price2, ..."""
    num_trades: int | None
    source_analytics: dict[str, Any]
    volume_bonds: Decimal | None = None
    value: Decimal | None = None
    """Turnover in ``currency_id``, the settlement currency (``SUR``: RUB)."""
    currency_id: str | None = None


OFZ_CLASSES = ("ofz_pd", "ofz_ad", "ofz_pk", "ofz_in")
KBD_CURVE_ID = "RUB_KBD"
KBD_SPEC = "rub_ofz_kbd_v1"
ZCYC_KEYS = ("B1", "B2", "B3", "T1", *(f"G{i}" for i in range(1, 10)))

PRICE_FIELDS = ("legal_close", "close", "waprice", "market_price2", "market_price3", "admitted_quote",
                "open", "high", "low")
INTRADAY_PRICE_FIELDS = (("close", "close_price_pct"), ("last", "last_pct"), ("waprice", "waprice_pct"),
                         ("market_price", "market_price_pct"), ("bid", "bid_pct"), ("offer", "offer_pct"))
"""An intraday cut's quote (``bond_quotes_intraday``: MOEX marketdata, the latest of the day per bond and board) as
price bases: the closing price once there is one, else the last trade (``PRICE_BASIS_ORDER``); its trade date is
the Moscow date of ``ts``. It has no MOEX accrued and no face: the valuation computes the accrued."""
MSK = timezone(timedelta(hours=3), "MSK")


CALENDAR_HORIZON = date(2079, 12, 31)
"""The hub's calendars end 2027-12-31; past that every calendar is extended provisionally to here: the
10Y curves need 2037, floaters observe their index to maturity (2062 the latest, bonds 2078)."""
RU_FIXED_HOLIDAYS = (*((1, d) for d in range(1, 9)), (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4))


def _easter(y: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = y % 19, y // 100, y % 100
    h = (19 * a + b - b // 4 - (b - (b + 8) // 25 + 1) // 3 + 15) % 30
    i, k = divmod(c, 4)
    wd = (32 + 2 * (b % 4) + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * wd) // 451
    month, day = divmod(h + wd - 7 * m + 114, 31)
    return date(y, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Monday 0) of the month; ``n`` = -1: the last one."""
    if n < 0:
        d = (date(year + month // 12, month % 12 + 1, 1)) - timedelta(days=1)
        return d - timedelta(days=(d.weekday() - weekday) % 7)
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def _observed(d: date) -> date:
    """A US federal holiday on a Saturday is observed on the Friday, on a Sunday on the Monday."""
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


RUSSIAN_CALENDARS = ("MOEX_", "RU_")
"""Calendar names (prefixes) extended by the fixed Russian public holidays (``RU_FIXED_HOLIDAYS``)."""


def _rule_holidays(name: str, year: int) -> set[date]:
    """Fixed-rule holidays of a calendar for a year past the hub's ``valid_to`` (provisional, ``core_calendar``).

    Russian calendars (``RUSSIAN_CALENDARS``): the fixed public holidays. TARGET: 1 Jan, Good Friday, Easter Monday,
    1 May, 25-26 Dec. US_FED: the eleven federal holidays (fixed dates observed off weekends, the n-th weekdays).
    CH_ZURICH: 1-2 Jan, Good Friday, Easter Monday, 1 May, Ascension, Whit Monday, 1 Aug, 25-26 Dec. CN_INTERBANK: 1
    Jan, 1 May and 1-3 Oct only (the lunar holidays and the make-up weekends are decreed yearly: unknown). Any other
    calendar: weekends only, never another country's holidays."""
    if name == "TARGET":
        e = _easter(year)
        return {date(year, 1, 1), e - timedelta(days=2), e + timedelta(days=1), date(year, 5, 1),
                date(year, 12, 25), date(year, 12, 26)}
    if name == "US_FED":
        fixed = {_observed(date(year, m, d)) for m, d in ((1, 1), (6, 19), (7, 4), (11, 11), (12, 25))}
        return fixed | {_nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3), _nth_weekday(year, 5, 0, -1),
                        _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 10, 0, 2), _nth_weekday(year, 11, 3, 4)}
    if name == "CH_ZURICH":
        e = _easter(year)
        return {date(year, 1, 1), date(year, 1, 2), e - timedelta(days=2), e + timedelta(days=1), date(year, 5, 1),
                e + timedelta(days=39), e + timedelta(days=50), date(year, 8, 1), date(year, 12, 25),
                date(year, 12, 26)}
    if name == "CN_INTERBANK":
        return {date(year, 1, 1), date(year, 5, 1), date(year, 10, 1), date(year, 10, 2), date(year, 10, 3)}
    if name.startswith(RUSSIAN_CALENDARS):
        return {date(year, m, d) for m, d in RU_FIXED_HOLIDAYS}
    return set()


def core_calendar(c: dict[str, Any]) -> tuple[BusinessCalendar, date | None]:
    """Core calendar of one hub calendar row and the date its days stop being official (``provisional_from``).

    Past the hub's ``valid_to`` the calendar is extended to ``CALENDAR_HORIZON`` by its own fixed rules
    (``_rule_holidays``: the fixed Russian public holidays for a Russian calendar, TARGET's, US_FED's, CH_ZURICH's,
    CN_INTERBANK's fixed days; weekends only for a calendar without rules, never Russian holidays). Holiday transfers
    to weekdays are decreed yearly and unknown, so they are not modelled. ``provisional_from`` is the hub's own or
    ``valid_to + 1``, whichever is earlier.
    """
    vf, vt = date.fromisoformat(c["valid_from"]), date.fromisoformat(c["valid_to"])
    holidays = {date.fromisoformat(x) for x in c["holidays"]}
    prov = _date(c.get("provisional_from"))
    end = max(vt, CALENDAR_HORIZON)
    if end > vt:
        holidays |= {d for y in range(vt.year, end.year + 1) for d in _rule_holidays(c["calendar"], y)
                     if d > vt and d.weekday() < 5}
        prov = min(prov or end, vt + timedelta(days=1))
    cal = BusinessCalendar(
        holidays=frozenset(holidays), working_days=frozenset(date.fromisoformat(x) for x in c["working_weekends"]),
        version=f"{c['calendar']}@{c['fingerprint'][:12]}" + (f"+rules{end.year}" if end > vt else ""),
        valid_from=vf, valid_to=end)
    return cal, prov


class StaticView:
    def __init__(self, snap: Snapshot) -> None:
        if snap.kind != "static":
            raise SnapshotError(f"{snap.id}: not a static snapshot")
        self.snapshot = snap
        d = snap.data
        self.issuers = {
            int(x["issuer_id"]): Issuer(int(x["issuer_id"]), x["name"], x.get("short_name"), x.get("inn"),
                                        x.get("sector"), bool(x.get("is_sovereign")), x.get("group_id"),
                                        x.get("group_name"), x.get("is_state_owned"), x.get("country"))
            for x in d["issuers"]
        }
        self.calendars = {c["calendar"]: c for c in d["calendars"]}
        coupons: dict[str, list[Coupon]] = defaultdict(list)
        for c in d["coupon_periods"]:
            coupons[c["secid"]].append(Coupon(
                int(c["coupon_no"]), _date(c.get("start_date")), date.fromisoformat(c["pay_date"]),
                _date(c.get("record_date")), _d(c.get("rate_pct")), _d(c.get("amount_per_bond")), c["status"],
                _d(c.get("face_value"))))
        reds: dict[str, list[Redemption]] = defaultdict(list)
        for r in d["redemptions"]:
            reds[r["secid"]].append(Redemption(date.fromisoformat(r["date"]), r["kind"], _d(r.get("value_per_bond"))))
        offers: dict[str, list[Offer]] = defaultdict(list)
        for o in d["offers"]:
            offers[o["secid"]].append(Offer(_date(o.get("offer_date")), o["type"], o["status"], _d(o.get("price_pct")),
                                         _date(o.get("window_end")), _date(o.get("window_start"))))
        formulas = {f["secid"]: f for f in d["floater_formulas"]}
        self.bonds: dict[str, BondTerms] = {}
        as_of = date.fromisoformat(snap.business_date)
        for i in d["instruments"]:
            sid = i["secid"]
            board = next((b for b in i.get("boards") or [] if b.get("is_primary")), None)
            self.bonds[sid] = BondTerms(
                secid=sid, isin=i.get("isin"), name=i["name"], short_name=i.get("short_name"),
                bond_class=i["bond_class"], status=i["status"], issuer_id=i.get("issuer_id"),
                currency=i.get("currency_id"), face_unit=i.get("face_unit"), face_value=_d(i.get("face_value")),
                coupon_type=i["coupon_type"], coupon_frequency=i.get("coupon_frequency"),
                maturity_date=_date(i.get("maturity_date")), is_perpetual=bool(i.get("is_perpetual")),
                is_structured=bool(i.get("is_structured")), is_face_indexed=bool(i.get("is_face_indexed")),
                is_fx_face=bool(i.get("is_fx_face")), is_amortizing=bool(i.get("is_amortizing")),
                calendar=i.get("calendar") or "MOEX_STOCK", primary_board=i.get("primary_board"),
                settle_lag_days=board.get("settle_lag_days") if board else None,
                labels=tuple(i.get("labels") or ()),
                coupons=tuple(sorted(coupons.get(sid, ()), key=lambda c: c.no)),
                redemptions=tuple(sorted(reds.get(sid, ()), key=lambda r: r.date)),
                offers=tuple(offers.get(sid, ())),
                floater_formula=formulas.get(sid), face_date=as_of,
                initial_face_value=_d(i.get("initial_face_value")),
                lat_name=i.get("lat_name"), issue_size=_d(i.get("issue_size")),
            )

    def business_calendar(self, name: str) -> tuple[BusinessCalendar, date, date]:
        """Core calendar (``core_calendar``) plus its validity window (outside it business days are unknown)."""
        cal, _ = core_calendar(self.calendars[name])
        return cal, cal.valid_from, cal.valid_to  # type: ignore[return-value]

    def provisional_from(self) -> dict[str, str | None]:
        return {n: (p := core_calendar(c)[1]) and p.isoformat() for n, c in self.calendars.items()}

    def issuer_of(self, terms: BondTerms) -> Issuer | None:
        return self.issuers.get(terms.issuer_id) if terms.issuer_id is not None else None


def zcyc_parameters(trade_date: str | date, params: dict[str, Any], where: str = "zcyc_params") -> ZcycParameters:
    """Core's KBD parameters of a hub ``zcyc_params`` row (``params``: its published decimal strings)."""
    missing = [k for k in ZCYC_KEYS if params.get(k) is None]
    if missing:
        raise SnapshotError(f"{where}: ZCYC parameters missing: {missing}")
    b1, b2, b3, t1, *g = (float(Decimal(params[k])) for k in ZCYC_KEYS)
    d = trade_date if isinstance(trade_date, date) else date.fromisoformat(trade_date)
    return ZcycParameters(d, b1, b2, b3, t1, tuple(g))  # type: ignore[arg-type]


class MarketView:
    def __init__(self, snap: Snapshot) -> None:
        if snap.kind != "market":
            raise SnapshotError(f"{snap.id}: not a market snapshot")
        self.snapshot = snap
        d = snap.data
        self.business_date = date.fromisoformat(snap.business_date)
        self.quotes: dict[tuple[str, str], BondQuote] = {}
        for q in d["bond_quotes"]:
            prices = {k: Decimal(q[f"{k}_pct"]) for k in PRICE_FIELDS if q.get(f"{k}_pct") is not None}
            self.quotes[(q["secid"], q["board"])] = BondQuote(
                q["secid"], q["board"], date.fromisoformat(q["trade_date"]), _d(q.get("face_value")),
                _d(q.get("accrued_per_bond")), prices, q.get("num_trades"), q.get("source_analytics") or {},
                _d(q.get("volume_bonds")), _d(q.get("value_ccy")), q.get("currency_id"))
        for q in d.get("bond_quotes_intraday") or ():  # an intraday cut (R11); EOD cuts leave it empty
            if (q["secid"], q["board"]) in self.quotes:
                continue
            prices = {k: Decimal(q[f]) for k, f in INTRADAY_PRICE_FIELDS if q.get(f) is not None}
            ts = datetime.fromisoformat(q["ts"])
            self.quotes[(q["secid"], q["board"])] = BondQuote(
                q["secid"], q["board"], ts.astimezone(MSK).date(), None, None, prices, q.get("num_trades"),
                q.get("source_analytics") or {}, _d(q.get("vol_today")), _d(q.get("val_today")), None)
        if len(d["zcyc_params"]) != 1:
            raise SnapshotError(f"{snap.id}: expected one zcyc_params row, got {len(d['zcyc_params'])}")
        self.zcyc = d["zcyc_params"][0]
        self.fixings = d["fixings"]
        self.fx_rates = d["fx_rates"]
        self.fx_swaps: list[dict[str, Any]] = d.get("fx_swaps") or []
        """Contract 1.4: MOEX CETS swaps (swap points as published and per one unit); empty before 1.4."""
        self.futures_prices: list[dict[str, Any]] = d.get("futures_prices") or []
        """Contract 1.4: FORTS settlement prices of the business date (FX, rate and index futures)."""
        self.curve_quote_sets = d["curve_quote_sets"]
        self.stale = {ds["name"]: ds["stale_reasons"] for ds in snap.doc["datasets"] if ds["stale"]}

    def quote(self, terms: BondTerms) -> BondQuote | None:
        if terms.primary_board and (terms.secid, terms.primary_board) in self.quotes:
            return self.quotes[(terms.secid, terms.primary_board)]
        alts = [q for (s, _), q in self.quotes.items() if s == terms.secid]
        return max(alts, key=lambda q: q.num_trades or 0) if alts else None

    def zcyc_parameters(self) -> ZcycParameters:
        """The KBD publication of the snapshot, dated its own trade date (core checks that it is the
        business date)."""
        return zcyc_parameters(self.zcyc["trade_date"], self.zcyc["params"], self.snapshot.id)

    def core_market(self, static: StaticView) -> MarketSnapshot:
        """Core market snapshot without quotes: the KBD under ``KBD_CURVE_ID`` and every calendar of
        the static snapshot. The valuation adds the quotes of the bonds it values."""
        return MarketSnapshot(
            self.snapshot.id, self.business_date, datetime.fromisoformat(self.snapshot.doc["known_at"]),
            curves=(CurveEntry(KBD_CURVE_ID, KBD_SPEC, self.zcyc_parameters()),),
            calendars=tuple(CalendarEntry(n, static.business_calendar(n)[0]) for n in static.calendars))

    def latest_fixing(self, index_code: str) -> tuple[date, float] | None:
        rows = [f for f in self.fixings if f["index_code"] == index_code and f["value"] is not None
                and f["fixing_date"] <= self.business_date.isoformat()]
        if not rows:
            return None
        r = max(rows, key=lambda f: f["fixing_date"])
        return date.fromisoformat(r["fixing_date"]), float(Decimal(r["value"]))

    def fx(self, ccy: str) -> dict[str, float | None]:
        """RUB per 1 unit: CBR official in force on the business date (``cbr``) and set on it for the next day
        (``cbr_next``); the MOEX indicative rates of the business date at 19:00 and 14:00 MSK (``moex_19``,
        ``moex_14``); the CETS TOM close (``cets_close``, else its weighted price) and weighted price (``moex_tom``)."""
        bd = self.business_date.isoformat()
        cbr = [r for r in self.fx_rates if r["kind"] == "cbr_official" and r["ccy"] == ccy and r["effective_date"] == bd]
        nxt = [r for r in self.fx_rates if r["kind"] == "cbr_official" and r["ccy"] == ccy and r["set_date"] == bd]
        tom = [r for r in self.fx_rates if r["kind"] == "moex_eod" and r["ccy"] == ccy
               and r.get("board") == "CETS" and (r.get("tenor") or "").upper() == "TOM" and r.get("trade_date") == bd
               and (r.get("waprice_rub_per_unit") or r.get("close_rub_per_unit"))]

        def indicative(kind: str) -> float | None:
            rows = [r for r in self.fx_rates if r["kind"] == kind and r["ccy"] == ccy and r.get("trade_date") == bd
                    and r.get("rate_rub_per_unit")]
            return _f(rows[0]["rate_rub_per_unit"]) if rows else None

        return {
            "cbr": _f(cbr[0]["rate_rub_per_unit"]) if cbr else None,
            "cbr_next": _f(nxt[0]["rate_rub_per_unit"]) if nxt else None,
            "moex_tom": _f(tom[0].get("waprice_rub_per_unit") or tom[0].get("close_rub_per_unit")) if tom else None,
            "moex_19": indicative("moex_indicative_19"),
            "moex_14": indicative("moex_indicative_14"),
            "cets_close": _f(tom[0].get("close_rub_per_unit") or tom[0].get("waprice_rub_per_unit")) if tom else None,
        }

    def fx_settlement(self, ccy: str) -> tuple[date | None, tuple[str, ...]]:
        """The TOM value date of ``ccy`` against RUB for trades of the business date (contract 1.4 ``settle_date`` of
        the CETS TOM row; None when the hub did not observe it, or before 1.4) and the hub calendars whose joint
        business days the pair settles on (``settlement_calendars``: MOEX_FX plus the currency's; ``("MOEX_FX",)``
        before 1.4)."""
        bd = self.business_date.isoformat()
        rows = [r for r in self.fx_rates if r["kind"] == "moex_eod" and r["ccy"] == ccy and r.get("trade_date") == bd
                and (r.get("tenor") or "").upper() == "TOM"]
        rows.sort(key=lambda r: (r.get("board") != "CETS", r.get("settle_date") is None))
        settle = next((date.fromisoformat(r["settle_date"]) for r in rows if r.get("settle_date")), None)
        cals = next((tuple(r["settlement_calendars"]) for r in rows if r.get("settlement_calendars")), ("MOEX_FX",))
        return settle, cals

    def fx_swap_quotes(self, ccy: str) -> list[dict[str, Any]]:
        """The business date's CETS swaps of ``ccy``/RUB with a price: per (near leg, far leg) the row with observed
        settlement dates first, then board CETS, then the most trades. ``points`` is the close per one unit
        (``close_rub_per_unit``; MOEX keeps a 0 close on a day with trades, then the weighted price)."""
        bd = self.business_date.isoformat()
        best: dict[tuple[str, str], tuple[tuple, dict[str, Any]]] = {}
        for r in self.fx_swaps:
            if r.get("base_ccy") != ccy or r.get("quote_ccy", "RUB") != "RUB" or r.get("trade_date") != bd:
                continue
            close, wap = _d(r.get("close_rub_per_unit")), _d(r.get("waprice_rub_per_unit"))
            pts = close if close else wap
            if pts is None or not r.get("near_leg") or not r.get("far_leg"):
                continue
            key = (r["near_leg"].upper(), r["far_leg"].upper())
            rank = (r.get("near_settle_date") is None, r.get("board") != "CETS", -(r.get("num_trades") or 0))
            if key not in best or rank < best[key][0]:
                best[key] = (rank, {**r, "points": float(pts)})
        return [v for _, v in sorted(best.values(), key=lambda x: (x[1]["near_leg"], x[1]["far_leg"]))]

    def fx_futures(self, ccy: str) -> list[dict[str, Any]]:
        """Dated FORTS FX futures on ``ccy``/RUB of the business date with a settlement price per one unit
        (``settle_rub_per_unit``) and a delivery date, by delivery date."""
        bd = self.business_date.isoformat()
        rows = [r for r in self.futures_prices if r.get("is_fx") and r.get("base_ccy") == ccy
                and (r.get("quote_ccy") or "RUB") == "RUB" and r.get("trade_date") == bd
                and r.get("settle_rub_per_unit") and r.get("last_del_date") and r.get("contract_type") != "perpetual"]
        return sorted(rows, key=lambda r: r["last_del_date"])

    def future(self, secid: str) -> dict[str, Any] | None:
        """The business date's FORTS row of a contract by its ticker (``SiZ6``), None when the snapshot has none."""
        bd = self.business_date.isoformat()
        return next((r for r in self.futures_prices if r.get("secid") == secid and r.get("trade_date") == bd), None)

    def cbr_fixings(self) -> list[dict[str, Any]]:
        """The CBR official rates of the snapshot as NDF fixings: (ccy, set date, RUB per one unit). A rate set on D is
        the CBR fixing of D (in force on the next calendar day)."""
        out = []
        for r in self.fx_rates:
            if r["kind"] != "cbr_official" or not r.get("set_date") or not r.get("rate_rub_per_unit"):
                continue
            d = date.fromisoformat(str(r["set_date"])[:10])
            if d <= self.business_date:
                out.append({"ccy": r["ccy"], "fixing_date": d, "rate": float(Decimal(r["rate_rub_per_unit"]))})
        return out


def price_observation(secid: str, basis: str, clean_pct: float, accrued: Decimal | None) -> BondPriceObservation:
    """The quote the backend selected, with the MOEX accrued as given (core does not recompute it; without
    one core computes a fixed bond's from its leg)."""
    return BondPriceObservation(secid, clean_pct, basis, None if accrued is None else float(accrued))


def _window(o: Offer, d: date) -> tuple[date | None, date | None]:
    """An offer's exercise window as core accepts it: an end after the offer date (bad data) is dropped, and so is a
    start after the end."""
    end = o.window_end if o.window_end is not None and o.window_end <= d else None
    start = o.window_start if o.window_start is not None and o.window_start <= (end or d) else None
    return start, end


def put_offers(terms: BondTerms, after: date, offer: tuple[date, Decimal, str]) -> tuple[CoreOffer, ...]:
    """The puts core chooses the horizon from (``ValuationPolicy(horizon="open_put")``, core 0.33): every scheduled put
    (or untyped offer) of ``terms`` in ``(after, offer date]`` with its exercise window, in date order, the chosen
    ``offer`` last. The earlier ones are there because their window has closed (rule F2, ``valuation.bonds.next_offer``),
    so core's ``open_put`` skips them by the same rule and lands on ``offer``: core, not the backend, applies the
    window. Confirmed, as the backend has always taken a ``scheduled`` offer (``open_put`` ignores it anyway)."""
    d, price, _ = offer
    cands = {o.date: o for o in terms.offers if o.status == "scheduled" and o.type in ("put", "unknown")
             and o.date is not None and after < o.date <= d}
    out = {k: CoreOffer(k, OfferKind.PUT, True, float(o.price_pct if o.price_pct is not None else 100), *_window(o, k))
           for k, o in cands.items() if k < d}
    chosen = cands.get(d)
    out[d] = CoreOffer(d, OfferKind.PUT, True, float(price), *(_window(chosen, d) if chosen else (None, None)))
    return tuple(out[k] for k in sorted(out))


def bond_currency(terms: BondTerms) -> str:
    """The currency core values a bond in: its face unit (a foreign-face bond: CNY, USD, EUR; its coupons and
    redemptions are amounts in it, MOEX's price % of it), else its currency, else RUB (``RUR``/``SUR`` are RUB)."""
    c = (terms.face_unit or terms.currency or "RUB").upper()
    return "RUB" if c in ("RUR", "SUR") else c


def core_instrument(terms: BondTerms, after: date, horizon: date, offer: tuple[date, Decimal, str] | None = None, *,
                    coupons: tuple[Coupon, ...] | None = None,
                    floater: tuple[FloatingCouponRule, str, str] | None = None,
                    redeem_at_horizon: bool = False) -> Instrument:
    """Core terms of a bond valued after ``after`` (the trade date).

    Only what the valuation needs, so that gaps outside it cannot reject the bond: the coupons
    paid in ``(after, horizon]`` (``coupons``, the valuation layer's schedule with any amounts it
    assumed, default the snapshot's; the valuation layer has checked them), the redemptions after
    ``after`` (their sum, the current face, is ``initial_face``), and the offer the backend chose
    as horizon (F2 open-window rule in ``valuation.bonds``): a put with the earlier puts whose window has closed, each
    with its window (``put_offers``), for core's ``open_put`` horizon to choose it (core 0.33). A call
    needs an ``ExerciseAssumption`` in the valuation policy; a perpetual has no redemption and is
    valued to its call that way. ``redeem_at_horizon`` redeems the face outstanding at ``horizon``
    instead, without offers: a call core cannot exercise (a callable floater).

    Coupon legs per core CONSUMERS.md: OFZ accrue pro rata on the amount (Minfin), the others on
    the rate (MOEX); the snapshot publishes no day count, so those are ACT/365. A period ends at
    the next one's start when its payment date was moved past it (a holiday). A floater
    (``floater`` = rule, fixing ID, projection curve ID) gets a ``FloatingCouponLeg``: coupons MOEX
    published (``known``/``current_published``, non-zero) are cash with their rate (``published_rate``), the others
    are projected; a corporate's accrues on the rate (``AccrualBasis.RATE_ACT_365``), an OFZ-PK's pro rata on the
    amount (core 0.33 ``Instrument.accrued``).

    ``settlement_lag_days`` is 0 (finding F9, core brief C2): the MOEX price and accrued are as of
    the trade date, so core must settle on it; at T+1 the Z-spread is off by 0.5-2.8 bp.
    """
    flows = [c for c in (terms.coupons if coupons is None else coupons) if after < c.pay_date <= horizon]
    ofz = terms.bond_class in OFZ_CLASSES
    periods: list[CouponPeriod | RateCouponPeriod | FloatingCouponPeriod] = []
    for i, c in enumerate(flows):
        start = c.start or (flows[i - 1].pay_date if i else None)
        nxt = flows[i + 1].start if i + 1 < len(flows) else None
        end = nxt if start is not None and nxt is not None and start < nxt < c.pay_date else c.pay_date
        if floater is not None and start is not None:
            known = c.status in ("known", "current_published") and bool(c.amount_per_bond)
            periods.append(FloatingCouponPeriod(
                start, end, c.pay_date, float(c.face or terms.face_value),  # type: ignore[arg-type]
                float(c.amount_per_bond) if known else None,  # type: ignore[arg-type]
                # the issuer's rate of a published coupon: a corporate accrues on it (core 0.33 accrued)
                float(c.rate_pct) / 100 if known and c.rate_pct else None))
            continue
        if start is None or c.amount_per_bond is None or (not ofz and c.rate_pct is None):
            raise InvalidTerms(f"coupon {c.no} on {c.pay_date}: start {c.start}, amount {c.amount_per_bond}, "
                               f"rate {c.rate_pct}")
        amount = float(c.amount_per_bond)
        periods.append(CouponPeriod(start, end, c.pay_date, amount) if ofz
                       else RateCouponPeriod(start, end, c.pay_date, amount, float(c.rate_pct) / 100))  # type: ignore[arg-type]
    leg: FixedCouponLeg | RateAccrualCouponLeg | FloatingCouponLeg | None
    if floater is not None and periods:  # OFZ-PK accrue pro rata on the amount (Minfin), corporates on the rate (MOEX)
        leg = FloatingCouponLeg(floater[0], tuple(periods), floater[1], floater[2],  # type: ignore[arg-type]
                                None if ofz else AccrualBasis.RATE_ACT_365)
    elif not periods:
        leg = None
    else:
        leg = (FixedCouponLeg(tuple(periods)) if ofz  # type: ignore[arg-type]
               else RateAccrualCouponLeg(tuple(periods), AccrualBasis.RATE_ACT_365))  # type: ignore[arg-type]
    reds = [(r.date, float(r.value_per_bond)) for r in terms.redemptions if r.date > after and r.value_per_bond]
    face = float(terms.face_value) if terms.is_perpetual else sum(a for _, a in reds)  # type: ignore[arg-type]
    if redeem_at_horizon:
        reds = [(d, a) for d, a in reds if d < horizon]
        reds.append((horizon, face - sum(a for _, a in reds)))
    principal = PrincipalLeg(tuple(
        CoreRedemption(d, a, CashflowKind.MATURITY if i == len(reds) - 1 else CashflowKind.AMORTIZATION)
        for i, (d, a) in enumerate(reds)))
    offers: tuple[CoreOffer, ...] = ()
    if offer is not None and not redeem_at_horizon and offer[2] == "call":
        offers = (CoreOffer(offer[0], OfferKind.CALL, True, float(offer[1])),)
    elif offer is not None and not redeem_at_horizon:
        offers = put_offers(terms, after, offer)
    return Instrument(
        terms.secid, ProductType.FLOATING_BOND if isinstance(leg, FloatingCouponLeg) else ProductType.FIXED_BOND,
        bond_currency(terms), face, leg, principal,
        terms.calendar, offers=offers, settlement_lag_days=0, isin=terms.isin,
        issuer_id=None if terms.issuer_id is None else str(terms.issuer_id))
