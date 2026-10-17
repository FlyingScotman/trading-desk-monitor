"""Adapter ``mdhub_v1``: Market Snapshot Contract v1 -> backend domain objects (ADR-003).

This is the only module that knows the hub's wire format. It converts decimal strings once,
keeps every hub fact it needs (coupon status, price basis, labels) and builds core
``BusinessCalendar`` objects from the snapshot's calendars. It never fills gaps: a missing
value stays ``None`` and the valuation layer decides what that means.

Core has no ``Instrument`` type yet (core package 0.1), so the terms live here as
``BondTerms`` until core provides its own.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from rub_fixed_income import BusinessCalendar

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


@dataclass(frozen=True)
class Issuer:
    issuer_id: int
    name: str
    short_name: str | None
    inn: str | None
    sector: str | None
    is_sovereign: bool


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


PRICE_FIELDS = ("legal_close", "close", "waprice", "market_price2", "market_price3", "admitted_quote",
                "open", "high", "low")


class StaticView:
    def __init__(self, snap: Snapshot) -> None:
        if snap.kind != "static":
            raise SnapshotError(f"{snap.id}: not a static snapshot")
        self.snapshot = snap
        d = snap.data
        self.issuers = {
            int(x["issuer_id"]): Issuer(int(x["issuer_id"]), x["name"], x.get("short_name"), x.get("inn"),
                                        x.get("sector"), bool(x.get("is_sovereign")))
            for x in d["issuers"]
        }
        self.calendars = {c["calendar"]: c for c in d["calendars"]}
        coupons: dict[str, list[Coupon]] = defaultdict(list)
        for c in d["coupon_periods"]:
            coupons[c["secid"]].append(Coupon(
                int(c["coupon_no"]), _date(c.get("start_date")), date.fromisoformat(c["pay_date"]),
                _date(c.get("record_date")), _d(c.get("rate_pct")), _d(c.get("amount_per_bond")), c["status"]))
        reds: dict[str, list[Redemption]] = defaultdict(list)
        for r in d["redemptions"]:
            reds[r["secid"]].append(Redemption(date.fromisoformat(r["date"]), r["kind"], _d(r.get("value_per_bond"))))
        offers: dict[str, list[Offer]] = defaultdict(list)
        for o in d["offers"]:
            offers[o["secid"]].append(Offer(_date(o.get("offer_date")), o["type"], o["status"], _d(o.get("price_pct")),
                                         _date(o.get("window_end"))))
        formulas = {f["secid"]: f for f in d["floater_formulas"]}
        self.bonds: dict[str, BondTerms] = {}
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
                floater_formula=formulas.get(sid),
            )

    def business_calendar(self, name: str) -> tuple[BusinessCalendar, date, date]:
        """Core calendar plus its validity window (outside it business days are unknown)."""
        c = self.calendars[name]
        cal = BusinessCalendar(
            holidays=frozenset(date.fromisoformat(x) for x in c["holidays"]),
            working_days=frozenset(date.fromisoformat(x) for x in c["working_weekends"]),
            version=f"{name}@{c['fingerprint'][:12]}",
        )
        return cal, date.fromisoformat(c["valid_from"]), date.fromisoformat(c["valid_to"])

    def issuer_of(self, terms: BondTerms) -> Issuer | None:
        return self.issuers.get(terms.issuer_id) if terms.issuer_id is not None else None


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
                _d(q.get("accrued_per_bond")), prices, q.get("num_trades"), q.get("source_analytics") or {})
        if len(d["zcyc_params"]) != 1:
            raise SnapshotError(f"{snap.id}: expected one zcyc_params row, got {len(d['zcyc_params'])}")
        self.zcyc = d["zcyc_params"][0]
        self.fixings = d["fixings"]
        self.fx_rates = d["fx_rates"]
        self.curve_quote_sets = d["curve_quote_sets"]
        self.stale = {ds["name"]: ds["stale_reasons"] for ds in snap.doc["datasets"] if ds["stale"]}

    def quote(self, terms: BondTerms) -> BondQuote | None:
        if terms.primary_board and (terms.secid, terms.primary_board) in self.quotes:
            return self.quotes[(terms.secid, terms.primary_board)]
        alts = [q for (s, _), q in self.quotes.items() if s == terms.secid]
        return max(alts, key=lambda q: q.num_trades or 0) if alts else None

    def zcyc_params(self) -> dict[str, float]:
        return {k: float(Decimal(v)) for k, v in self.zcyc["params"].items()}

    def latest_fixing(self, index_code: str) -> tuple[date, float] | None:
        rows = [f for f in self.fixings if f["index_code"] == index_code and f["value"] is not None
                and f["fixing_date"] <= self.business_date.isoformat()]
        if not rows:
            return None
        r = max(rows, key=lambda f: f["fixing_date"])
        return date.fromisoformat(r["fixing_date"]), float(Decimal(r["value"]))

    def fx(self, ccy: str) -> dict[str, float | None]:
        """RUB per 1 unit: CBR official in force on the business date and the MOEX TOM weighted price."""
        bd = self.business_date.isoformat()
        cbr = [r for r in self.fx_rates if r["kind"] == "cbr_official" and r["ccy"] == ccy and r["effective_date"] == bd]
        nxt = [r for r in self.fx_rates if r["kind"] == "cbr_official" and r["ccy"] == ccy and r["set_date"] == bd]
        tom = [r for r in self.fx_rates if r["kind"] == "moex_eod" and r["ccy"] == ccy
               and r.get("board") == "CETS" and (r.get("tenor") or "").upper() == "TOM" and r.get("trade_date") == bd
               and (r.get("waprice_rub_per_unit") or r.get("close_rub_per_unit"))]
        return {
            "cbr": _f(cbr[0]["rate_rub_per_unit"]) if cbr else None,
            "cbr_next": _f(nxt[0]["rate_rub_per_unit"]) if nxt else None,
            "moex_tom": _f(tom[0].get("waprice_rub_per_unit") or tom[0].get("close_rub_per_unit")) if tom else None,
        }
