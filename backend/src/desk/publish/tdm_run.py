"""The content of an EOD run (``run_content``): the position rows, the reference and the run log that the desk
store publishes (``desk_run.publish``). The module name is historical: it once also wrote the run into the legacy
``tdm`` SQLite store through its writer SDK; that path is gone (#294).
Every valued position is written, also those on a fallback rung (``valuation.fallback``): its
``extra`` carries ``quality`` and ``assumptions``, and the run log counts the qualities and lists
each non-exact position with its reasons as a WARN line (the prototype has no quality column).
A position with no data at all is not written with zeros: it gets a WARN line with its reason.
Rows of the private contract v1 (S7a, ``valuation.positions``) carry the report's row as it came in
``extra.reported`` (reconciliation figures, ``src``, ``x``) next to ours; a row valued without core (an FX row,
an unmapped bond, an OTC or other row at the report's value) is written by ``direct_row`` with its currency, the
report's instrument name and the report's risk when it gave one. Books: the store's metadata in force
(``PositionInputs.books``: name, desk, entity, book_type); the demo ``BOOKS`` only for a book without it.
The same holds for the flash PnL of slice S2: a priced position without PnL gets no ``pnl`` dict
and a WARN line with the reason; a simple PnL (all in ``other``) is counted in its own WARN line. A row valued without
core gets the PnL of ``pnl.direct`` (by the rate move, or by the reports: an INFO line per method). A run without a
previous market says so in a WARN line: no position has a PnL then, and none is shown as 0 (R10).

Risk by curve (R8): a row's ``risk`` and ``gamma`` are per published curve (``BondValuation.risk``, the Hessian
factors ``IR|<curve>|<bucket>``): ``RUB RUONIA`` (the discount curve of every RUB bond, a RUONIA floater's
projection), ``RUB KEY`` (a key-rate floater's projection), ``RUB OFZ`` (the KBD: only on a date without RUONIA,
``valuation.bonds.discount_curve``), core's foreign curves (``curves.fx``: ``CNY XCCY`` a CNY bond's, ``USD UST`` /
``EUR AAA`` the par-yield proxies with ``proxy_curves_for_risk``, ``<CCY> FX`` an FX derivative's foreign leg), a
currency's curve (``USD``: a foreign bond's duration proxy on the fallback ladder, a report's DV01). ``curves_reference``
publishes the KBD, the RUONIA OIS and key-rate curves and the foreign curves the run built (zero rates and the day's
move; a foreign curve with its own ``ccy``) and a curve without points for every other curve of the risk; ``dv01``
stays the sum over the curves. An FX derivative valued by core from its terms (``valuation.derivatives``) is a row
``instr_type`` FX with its ``fx_delta`` per currency (core's spot equivalent), its Hessian over the rate and FX factors
and ``extra`` legs, ``ocp_amount``, ``basis01``, ``fixings``, ``core``; ``fx_log`` says what of the FX book was valued how.
``spread_curve`` is the curve ``spread_bp`` is to (the discount curve).

Display fields (R9 static audit, D1-D8): a bond is named by MOEX's full ``name`` (the short name in
``extra.short_name``, the SECID in ``instrument_id``); its coupon is ``bonds.coupon_view`` for every rung (a floater:
the published rate or amount, else ``index + margin`` at the latest fixing, ``extra.coupon_basis`` ``projected``)
with ``index_name``/``margin``; ``coupon_type`` keeps the hub's zero / indexed_face / variable_known / unknown;
``coupon_freq`` comes from the schedule when MOEX gives none. The issuer shows ``issuer_display`` of its name, sector
and rating ``NO_DATA`` when the hub has none. An unmapped bond shows the report's name marked ``FROM_REPORT``, its
ISIN, and the report's issuer (``x_issuer``).

Ratings and reference facts (R25): the issuer's short name, group, sector and rating come from the newest static of
the cache (``EodContext.reference_facts``); the issuer's rating is the desk's view (``marketdata.ratings``), a bond's
own (issue, else issuer level) is in ``extra.rating`` (grade, agency, the rating as written, date, outlook, basis,
bucket, every agency's rating).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from rub_fixed_income import (
    RISK_BUCKETS,
    RISK_PILLARS,
    CoreRejection,
    CurveEntry,
    DiscountCurve,
    kbd_effective_yield,
)

from ..curves import fx as fxcurves
from ..curves import proxy, rates
from ..marketdata import ratings
from ..marketdata.adapter import MarketView, bond_currency
from ..marketdata.bundle import SnapshotError
from ..marketdata.static_facts import SECTOR_LABELS
from ..orchestrator import EodContext, Outcome, market_rates
from ..pnl.direct import DirectPnl
from ..pnl.flash import METHOD, STORE_ONLY_KEYS, FlashPnl
from ..pnl.reasons import (
    CORE_FX_METHOD,
    FX_METHOD,
    NO_PNL_TEXT,
    OTHER_REASON_TEXT,
    REPORTED_METHOD,
    RUN_NO_PREV_MARKET,
)
from ..settings import REPO_ROOT
from ..store import PositionInputs
from ..valuation.bonds import (
    OFZ_CURVE,
    OFZ_KBD_CLASSES,
    RISK_CURVES,
    BondValuation,
    coupon_view,
    implied_frequency,
    is_kbd_floater,
)
from ..valuation.fallback import METHOD as SIMPLE_METHOD
from ..valuation.fallback import QUALITIES, SimplePnl, fx_rate, market_fx
from ..valuation.positions import normalised
from .summary import MINFIN_CLASSES, MINFIN_INN, NOT_MINFIN_CLASSES, risk_problem

ASSET_CLASS = {"ofz_pd": "ОФЗ-ПД", "ofz_ad": "ОФЗ-АД", "ofz_pk": "ОФЗ-ПК", "ofz_in": "ОФЗ-ИН",
               "subfederal": "Субфедеральные", "municipal": "Муниципальные", "corporate": "Корпоративные"}
SEGMENT = {"ofz_pd": "Гос", "ofz_ad": "Гос", "ofz_pk": "Гос", "ofz_in": "Гос",
           "subfederal": "Суб", "municipal": "Суб"}
"""Segment of a bond (default ``Корп``). The Desk Monitor builds its segment chips from the data, so a segment
outside its known list (``Суб``) gets a chip of its own and is never filtered out silently."""
DIRECT_KIND = {"fx": ("FX", "Валюта", "FX"), "otc": ("OTC", "OTC-деривативы", "Деривативы"),
               "other": ("OTHER", "Прочее", "Прочее"), "bond": ("BOND", "Не сопоставлено", "Корп")}
"""asset_class of a direct row -> (instr_type, asset class, segment); an unknown one is treated as ``other``.
``OTC`` and ``OTHER`` are desk-store types (the legacy tdm store had no such instrument type)."""
BOOKS = {
    "OFZ": {"book_id": "OFZ", "name": "ОФЗ (демо)", "desk": "Rates", "trader": None, "book_type": "TRADING",
            "entity": "Демо", "ocp_to": None},
    "CORP": {"book_id": "CORP", "name": "Корпоративные (демо)", "desk": "Credit", "trader": None,
             "book_type": "TRADING", "entity": "Демо", "ocp_to": None},
    "MIXED": {"book_id": "MIXED", "name": "Смешанная (тест)", "desk": "Rates", "trader": None,
              "book_type": "TRADING", "entity": "Демо", "ocp_to": None},
}
DEFAULT_BOOK = {"desk": "—", "trader": None, "book_type": None, "entity": "—", "ocp_to": None}
"""A book with no store metadata and outside the demo ``BOOKS``: named by its id."""
NO_DATA = "нет данных"
"""An issuer's sector and rating the hub does not publish (D3: until HS3/HS5), said instead of left blank."""
UNMAPPED_ISSUER = {"issuer": "Не сопоставлено", "name": None, "issuer_group": "Не сопоставлено", "sector": NO_DATA,
                   "rating": NO_DATA, "is_gov": False, "is_quasi": False}
"""The issuer of the unmapped bonds (an instrument the static snapshot does not know) whose row names none."""
FROM_REPORT = " (из отчёта)"
"""Marks a name that is the bank report's, not the static snapshot's (an unmapped bond, D6)."""
FX_CCYS = ("USD", "EUR", "CNY")
INDICES = (("KEY_RATE", "КС"), ("RUONIA", "RUONIA"), ("SOFR", "SOFR"), ("ESTR", "€STR"), ("CPI_RU_YOY", "CPI г/г"))
"""Fixings of the market snapshot the run publishes as its indices (SOFR, €STR and the CBR's CPI y/y: contract 1.3,
R25; absent from an older snapshot, then not published)."""
COUPON_TYPES = {"fixed": "fix", "floating": "float"}
"""Hub coupon type -> the position's ``coupon_type``. The others (``zero``, ``indexed_face``, ``variable_known``,
``unknown``) are kept as the hub names them (D5)."""

_LEGAL_FORMS = re.compile(
    r"(?:публичное|непубличное|открытое|закрытое)?\s*акционерное\s+общество|общество\s+с\s+ограниченной\s+"
    r"ответственностью|государственная\s+корпорация(?:\s+развития)?|государственная\s+компания", re.IGNORECASE)
_SHORT_FORMS = re.compile(r"^(?:ПАО|ОАО|ЗАО|НАО|АО|ООО|ГК)\s+|\(\s*(?:ПАО|ОАО|ЗАО|НАО|АО|ООО)?\s*\)")
_ABBREVIATED = ((re.compile(r"специализированное\s+финансовое\s+общество", re.IGNORECASE), "СФО"),
                (re.compile(r"ипотечный\s+агент", re.IGNORECASE), "ИА"))
_QUOTES = re.compile("[\"«»“”„']")
_CAPS_WORD = re.compile(r"[А-ЯЁA-Z]{4,}")
_VOWELS = set("АЕЁИОУЫЭЮЯAEIOUY")


def issuer_display(name: str) -> str:
    """A readable issuer name from MOEX's legal one until the hub sends short names (D2, HS1): the legal form
    dropped (``публичное акционерное общество``, ``ПАО``, ``(публичное акционерное общество)``), quotes dropped, an
    all-capitals name in normal case (a word of 4+ letters with a vowel; ``ГТЛК``, ``РЖД`` stay), ``СФО`` and ``ИА``
    abbreviated. ``открытое акционерное общество "Российские железные дороги"`` -> ``Российские железные дороги``,
    ``АКЦИОНЕРНОЕ ОБЩЕСТВО "АЛЬФА-БАНК"`` -> ``Альфа-Банк``."""
    s = _LEGAL_FORMS.sub(" ", name)
    for rx, short in _ABBREVIATED:
        s = rx.sub(short, s)
    s = _QUOTES.sub("", s)
    s = " ".join(s.split())
    s = _SHORT_FORMS.sub("", s).strip(" ,.-")
    s = " ".join(s.split())
    if not s:
        return name
    if not any(ch.islower() for ch in s):
        s = _CAPS_WORD.sub(lambda m: m[0][0] + m[0][1:].lower() if _VOWELS & set(m[0]) else m[0], s)
    return s[0].upper() + s[1:]


def _issuer_key(ctx: EodContext, o: Outcome) -> tuple[str, dict[str, Any]]:
    """The issuer reference of a bond: its key (the issuer's short name, else its legal name) and the record, from the
    newest static of the cache (``ctx.reference_facts``, R25; the run's own static for an issuer it does not know).
    The display name is ``issuer_display``; the group is the hub's group (``group_name``, static 1.1, HS4), else the
    issuer itself; the sector the hub's (HS3, or by the bond class: ``reference.CLASS_SECTOR``) in Russian
    (``static_facts.SECTOR_LABELS``), ``NO_DATA`` when there is none; ``is_quasi`` the hub's ``is_state_owned``; the
    rating the desk's view of the issuer (``ratings.RatingBook.issuer``: the grade, its agency and basis in
    ``rating_info``), ``б/р`` when the static has ratings but none of this issuer, ``NO_DATA`` when it has none."""
    assert o.terms is not None
    ref = ctx.reference_facts
    iss = ref.issuer(o.terms.issuer_id) or ctx.static.issuer_of(o.terms)
    # the Minfin (R39, ``summary.is_minfin``): by the issuer -- the hub's sovereign flag or the Minfin's INN -- or the
    # classes the hub derives from that INN (OFZ, the Minfin's FX bonds); a foreign sovereign is not it
    cls = o.terms.bond_class
    is_gov = cls not in NOT_MINFIN_CLASSES and (cls.startswith("ofz") or cls in MINFIN_CLASSES or bool(
        iss and (iss.is_sovereign or iss.inn == MINFIN_INN)))
    rt = ref.ratings.issuer(o.terms.issuer_id)
    # the sovereign is not rated on the agencies' national scales: no rating rather than «б/р»
    label = None if is_gov and rt is None and ref.ratings.has_data else ref.ratings.label(rt)
    rating = {"rating": label, **({"rating_info": ratings.compact(rt)} if rt else {})}
    if iss is None:
        key = f"issuer:{o.terms.issuer_id or o.secid}"
        return key, {"issuer": key, "name": None, "issuer_group": key, "sector": NO_DATA, **rating,
                     "is_gov": is_gov, "is_quasi": False}
    key = iss.short_name or iss.name
    name = issuer_display(key)
    sector = SECTOR_LABELS.get(iss.sector, iss.sector) if iss.sector else NO_DATA
    src = ref.sector_source.get(iss.issuer_id)
    return key, {"issuer": key, "name": name, "issuer_group": iss.group_name or name, "sector": sector, **rating,
                 **({"sector_source": src} if src and iss.sector else {}),
                 "is_gov": is_gov, "is_quasi": bool(iss.is_state_owned)}


def _report_issuer(x: dict[str, Any]) -> dict[str, Any]:
    """The issuer of an unmapped bond: the report's ``x_issuer`` when it names one (D6), else ``UNMAPPED_ISSUER``."""
    name = ((x.get("x") or {}).get("x_issuer") or "").strip() if isinstance(x.get("x"), dict) else ""
    if not name:
        return UNMAPPED_ISSUER
    shown = issuer_display(name)  # the same display as a mapped issuer's: one line per issuer in the Desk Monitor
    return {"issuer": f"report:{name}", "name": shown, "issuer_group": shown, "sector": NO_DATA, "rating": NO_DATA,
            "is_gov": False, "is_quasi": False}


def curve_points(ctx: EodContext, prev: MarketView | None) -> list[dict[str, Any]]:
    prev_kbd = prev.zcyc_parameters() if prev else None
    pts = []
    for b, t in zip(RISK_BUCKETS, RISK_PILLARS):
        r = kbd_effective_yield(ctx.kbd, t) * 100
        move = (r - kbd_effective_yield(prev_kbd, t) * 100) * 100 if prev_kbd else 0.0
        pts.append({"bucket": b, "rate": round(r, 4), "move_bp": round(move, 2)})
    return pts


RATES_CURVES = {rates.RUONIA_OIS_CURVE_ID: "RUONIA OIS (MOEX)",
                rates.KEY_RATE_CURVE_ID: "Ключевая ставка (IRS KEYRATE)"}
"""The rates curves of ``orchestrator.with_rates`` the run publishes (under ``RISK_CURVES``), with their names."""
CCY_CURVE_NAME = "{ccy}: кривой нет, риск по дюрации"
"""A currency's curve (``bonds.ccy_curve``): no points, only the risk booked on it."""


def _zero_points(curve: DiscountCurve, prev: DiscountCurve | None) -> list[dict[str, Any]]:
    """Zero rates (annual compounding, %) of a built curve at the risk pillars, and their move since ``prev``
    at the same tenor, bp."""
    def zero(c: DiscountCurve, t: float) -> float:
        return c.zero_rate(c.curve_date + timedelta(days=max(1, round(t * 365.25)))) * 100

    pts = []
    for b, t in zip(RISK_BUCKETS, RISK_PILLARS):
        r = zero(curve, t)
        move = (r - zero(prev, t)) * 100 if prev else 0.0
        pts.append({"bucket": b, "rate": round(r, 4), "move_bp": round(move, 2)})
    return pts


def _prev_rates(ctx: EodContext, prev: MarketView) -> dict[str, DiscountCurve]:
    """The previous date's RUONIA OIS and key-rate curves, built as ``with_rates`` builds them (on this run's static,
    for the calendars): only for the day's move, so one that cannot be built is left out (move 0)."""
    out: dict[str, DiscountCurve] = {}
    try:
        ois = rates.ruonia_ois(prev.snapshot, ctx.static.snapshot)
        out[ois.entry.curve_id] = ois.curve
        kr = rates.key_rate_projection(prev.snapshot, ctx.static.snapshot, ois.curve)
        out[kr.entry.curve_id] = kr.curve
    except (rates.RatesInputError, SnapshotError, CoreRejection, ValueError):
        pass
    return out


def _prev_fx(ctx: EodContext, prev: MarketView, ois: DiscountCurve | None) -> dict[str, DiscountCurve]:
    """The previous date's foreign curves (``curves.fx.with_fx`` on its market, this run's static and the previous
    RUONIA OIS curve), only for the day's move: one that cannot be built is left out (move 0)."""
    if ois is None:
        return {}
    try:
        base = prev.core_market(ctx.static)
        core = replace(base, curves=(*base.curves, CurveEntry(rates.RUONIA_OIS_CURVE_ID, rates.RUONIA_OIS_SPEC, ois)))
        _, fxm = fxcurves.with_fx(prev, ctx.static, core, market_rates(prev), ctx.fx_options)
    except (rates.RatesInputError, SnapshotError, CoreRejection, ValueError, KeyError):
        return {}
    return {cid: c.curve for cid, c in fxm.curves.items()}


def curves_reference(ctx: EodContext, prev: MarketView | None, rows: list[dict[str, Any]],
                     fx: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The run's curves: the KBD (``RUB OFZ``), the RUONIA OIS and key-rate curves built for the run, and a curve
    without points for any other curve a row's risk is on (a currency's, ``bonds.ccy_curve``), so that every
    curve of the risk is in the reference (the Desk Monitor shows only those, tdm requires them). ``fx``: the run's
    rates (``_fx_rows``); a currency without one gives its curve in RUB (tdm needs a rate per currency), as
    ``direct_row`` shows such a row in RUB."""
    out = [{"curve_id": OFZ_CURVE, "name": "ОФЗ (КБД MOEX)", "ccy": "RUB", "kind": "bond",
            "points": curve_points(ctx, prev)}]
    built = {c.curve_id: c.curve for c in ctx.core_market.curves
             if c.curve_id in RATES_CURVES and isinstance(c.curve, DiscountCurve)}
    old = _prev_rates(ctx, prev) if prev and built else {}
    for cid, name in RATES_CURVES.items():
        if cid in built:
            out.append({"curve_id": RISK_CURVES[cid], "name": name, "ccy": "RUB", "kind": "swap",
                        "points": _zero_points(built[cid], old.get(cid))})
    risk_curves = dict.fromkeys(c for r in rows for c in r.get("risk") or {})
    foreign = [c for cid, c in ctx.fx.curves.items() if cid in risk_curves or c.ccy in fxcurves.MAIN_CCYS]
    if foreign:  # core's foreign curves of the run (curves.fx): zero rates, the move on the previous date's
        old_fx = _prev_fx(ctx, prev, old.get(rates.RUONIA_OIS_CURVE_ID)) if prev else {}
        for c in foreign:
            out.append({"curve_id": c.curve_id, "name": c.name, "ccy": c.ccy,
                        "kind": "bond" if c.kind == "proxy" else "swap",
                        "points": _zero_points(c.curve, old_fx.get(c.curve_id))})
    have = {c["curve_id"] for c in out}
    for c in risk_curves:
        if c not in have:  # a currency's curve; a RUB curve core did not build keeps its risk, without points
            ccy = c if c in {r["ccy"] for r in fx} else "RUB"
            pts = _proxy_points(ctx, prev, c) if ctx.proxy_curves else None
            if pts is not None:  # the setting proxy_curves_for_risk (default off): UST par / ECB AAA, not OIS
                out.append({"curve_id": c, "name": PROXY_CURVE_NAME.format(ccy=c, what=pts[0]), "ccy": ccy,
                            "kind": "bond", "points": pts[1]})
                continue
            out.append({"curve_id": c, "name": CCY_CURVE_NAME.format(ccy=c), "ccy": ccy, "kind": "bond",
                        "points": []})
    return out


PROXY_CURVE_NAME = "{ccy}: {what}, риск по дюрации"
"""A currency's curve given by the hub's government par-yield proxy (``curves.proxy``; setting
``proxy_curves_for_risk``)."""


def _proxy_points(ctx: EodContext, prev: MarketView | None, ccy: str) -> tuple[str, list[dict[str, Any]]] | None:
    """(label, points) of a currency's curve from the market's proxy quote set at the risk pillars, the move to the
    previous market's same proxy; None when the market has none."""
    now = proxy.points(ctx.market.curve_quote_sets, ccy)
    if now is None:
        return None
    old = proxy.points(prev.curve_quote_sets, ccy) if prev else None
    old = old if old and old[0] == now[0] else None
    pts = []
    for b, t in zip(RISK_BUCKETS, RISK_PILLARS):
        r = proxy.at(now[1], t)
        o = proxy.at(old[1], t) if old else None
        pts.append({"bucket": b, "rate": round(r, 4), "move_bp": round((r - o) * 100, 2) if o is not None else 0.0})
    return proxy.label(now[0]), pts


def pnl_extra(pnl: Any) -> dict[str, Any]:
    """The fact's copy of a PnL's ``extra``: without the flash breakdowns of TDM#406 and the reason of the unexplained
    (TDM#442; ``pnl.flash.STORE_ONLY_KEYS``), which the store keeps in ``run_position.pnl`` only."""
    return {k: v for k, v in pnl.extra().items() if k not in STORE_ONLY_KEYS}


def risk_by_curve(v: BondValuation) -> dict[str, dict[str, float]]:
    """``v.risk()`` without the curves where every bucket is 0."""
    return {c: bs for c, bs in v.risk().items() if any(bs.values())}


def row_currencies(rows: list[dict[str, Any]]) -> list[str]:
    """The currencies of the rows: their own, their FX deltas' and their risk curves named by a currency (``USD``,
    ``CHF``: ``bonds.ccy_curve``), in order of appearance."""
    out: dict[str, None] = {}
    for r in rows:
        for c in [r.get("ccy"), *(r.get("fx_delta") or {}), *(r.get("risk") or {})]:
            if isinstance(c, str) and len(c) == 3 and c.isalpha() and c.isupper():
                out[c] = None
    return list(out)


def _fx_rows(ctx: EodContext, prev: MarketView | None, also: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The run's rates (``mkt_fx``, DATA_CONTRACT §0.2): ``rate`` the market rate the flash PnL is in
    (``fallback.FX_MARKET``: MOEX 19:00, else 14:00, else CETS close, else CBR) with its ``rate_source``, and its move
    on the previous date's by the same chain; ``rate_cbr`` the CBR rate in force, its move, and ``rate_cbr_next``.
    ``FX_CCYS`` and ``also``: every other currency of the run's rows (a CHF bond, R27), so that its amounts convert
    and its curve is its own, not RUB's."""
    rows = []
    for ccy in dict.fromkeys([*FX_CCYS, *(c for c in also if c != "RUB")]):
        now = ctx.market.fx(ccy)
        mkt = market_fx(now)
        if mkt is None:
            continue
        old = prev.fx(ccy) if prev else {}
        was = market_fx(old) if old else None
        rows.append({
            "ccy": ccy, "rate": mkt[0], "rate_source": mkt[1],
            "move_pct": (mkt[0] / was[0] - 1) * 100 if was else 0.0,
            **({"prev_rate": was[0], "prev_rate_source": was[1]} if was else {}),
            "rate_cbr": now["cbr"],
            "move_pct_cbr": (now["cbr"] / old["cbr"] - 1) * 100 if now["cbr"] and old.get("cbr") else None,
            "rate_cbr_next": now["cbr_next"],
        })
    return rows


def _why(o: Outcome) -> str:
    """The reason code and detail of an outcome without PnL, for the run log."""
    e = o.no_pnl
    return f"{e.reason}{': ' + e.detail if e.detail else ''}" if e else "флэш-PnL не считался"


def _num(x: float | None) -> float | None:
    return None if x is None or math.isnan(x) else x


def reported(x: dict[str, Any] | None) -> dict[str, Any]:
    """The report's row (``PositionInputs.extra``) as JSON: ``{"reported": {...}}``, or nothing for a legacy row."""
    if not x:
        return {}
    return {"reported": {k: v.isoformat() if isinstance(v, date) else v for k, v in x.items() if v is not None}}


def book_rows(used: list[str], meta: dict[str, dict]) -> list[dict[str, Any]]:
    """Reference rows of the books used: the store's metadata (``current_books``) where a book has it, else the
    demo ``BOOKS`` (the synthetic demo and test books), else ``DEFAULT_BOOK``. Demo books first, as before."""
    def row(b: str) -> dict[str, Any]:
        if (m := meta.get(b)) is not None:
            return {"book_id": b, "name": m.get("name") or b, "desk": m.get("desk") or "—", "trader": m.get("trader"),
                    "book_type": m.get("book_type"), "entity": m.get("entity") or "—", "ocp_to": m.get("ocp_to")}
        return BOOKS.get(b, {"book_id": b, "name": b, **DEFAULT_BOOK})
    return [row(b) for b in BOOKS if b in used] + [row(b) for b in used if b not in BOOKS]


def rates_side(extra: dict[str, Any]) -> str:
    """The side of a row of swaps or REPOs valued by core: ``PAY`` / ``REC`` (the fixed leg, as tdm's IRS), ``REPO`` /
    ``REVERSE_REPO``; ``MIXED`` when its trades differ."""
    sides = {"PAY" if s["side"] == "payer" else "REC" for s in extra.get("swaps") or ()}
    sides |= {str(r["side"]).upper() for r in extra.get("repos") or ()}
    return sides.pop() if len(sides) == 1 else "MIXED"


def direct_row(ctx: EodContext, book: str, pid: str, o: Outcome, x: dict[str, Any], qty: float | None
               ) -> dict[str, Any]:
    """An FX row, an unmapped bond or an OTC/other row (``valuation.positions``): PV, FX delta in currency (an FX
    row's is its amount, also for a forward whose PV is only its mv_rub or 0), and
    the report's own risk (``reported_risk``) if any, else none. Fields the tdm schema requires and the report does
    not give: the notional of a bond without a face is its MV, an amount without a rate of the day is in RUB
    (both said in the assumptions)."""
    v = o.valuation
    assert v is not None
    x = normalised(x)
    kind = (x.get("asset_class") or "bond").lower()
    fx = kind == "fx"
    itype, aclass, segment = DIRECT_KIND.get(kind, DIRECT_KIND["other"])
    why = list(v.assumptions)
    ccy = x.get("ccy") or "RUB"
    rate = v.face_rub if fx else (r[0] if (r := fx_rate(ctx, ccy)) else None)
    amount = v.quantity if fx else next((x[f] for f in ("face_outstanding_ccy", "face_amount_ccy")
                                         if x.get(f) is not None), None)
    if rate is None:
        why.append(f"no {ccy} rate on {ctx.business_date}: shown in RUB")
        ccy, rate, amount = "RUB", 1.0, None  # the face is in the foreign currency: the notional is the MV
    core = bool(v.extra.get("core"))  # a derivative valued by core from its terms (valuation.derivatives)
    # a swap or a REPO valued by core: its notional (a REPO's signed cash) and its maturity are the terms'
    rates_core = core and bool(v.extra.get("swaps") or v.extra.get("repos"))
    if rates_core:
        ccy, rate, amount = "RUB", 1.0, v.quantity
    if amount is None:
        amount = v.pv_rub / rate
        why.append("notional unknown: the market value is shown")
    vd = x.get("value_date")
    if core and v.horizon_kind == "value_date":
        vd = v.horizon_date
    mat = v.horizon_date if rates_core else x.get("maturity_date") or vd
    name = x.get("source_instrument_name") or (f"{ccy} {vd or 'spot'}" if fx else x.get("source_instrument_id"))
    if kind == "bond":  # unmapped: the bank's name (D6), else its ISIN
        name = (name or x.get("isin") or pid) + FROM_REPORT
    if core:  # core's spot equivalent per currency, not the notional (the OCP amounts are in extra.ocp_amount)
        delta = {c: d for c, d in (v.fx_delta or {}).items() if c != "RUB"}
    else:
        delta = {ccy: amount if fx else v.pv_rub / rate} if ccy != "RUB" else {}
    return {
        "position_id": pid, "book_id": book, "instr_type": "FX" if core and not rates_core else itype,
        "asset_class": aclass,
        "segment": segment, "name": name or pid, "isin": x.get("isin"), "issuer": None, "ccy": ccy,
        "maturity": mat.isoformat() if mat else None,
        "tenor_y": (mat - ctx.business_date).days / 365.25 if mat and (core or not fx) else None,
        "side": rates_side(v.extra) if rates_core else ("BUY" if amount >= 0 else "SELL") if fx or core
        else "long" if amount >= 0 else "short",
        "notional": amount, "notional_rub": amount * rate, "price": _num(v.clean_pct), "mv": v.pv_rub / rate,
        "mv_rub": v.pv_rub, "dv01": v.dv01_rub, "cs01": v.spread01_rub, "ofz_spr01": 0.0,
        "risk": risk_by_curve(v), "gamma": [[a, b, g] for a, b, g in v.hessian] if core else [],
        "instrument_id": x.get("moex_secid") or x.get("source_instrument_id"), "new_today": False,
        **({"fx_delta": delta} if delta else {}),
        **({"pnl": o.pnl.as_tdm()} if o.pnl else {}),
        "extra": {
            "valuation": ("computed" if core else "reported" if v.quality == "reported_value" else "approximated"),
            "quality": v.quality,
            "assumptions": why, "quantity_bonds": qty, **({"not_exact_reason": o.reason} if o.reason else {}),
            **({"pnl": pnl_extra(o.pnl)} if o.pnl else {}),
            **({"fx_rate": rate} if ccy != "RUB" else {}), **v.extra, **reported(x),
            **({"fx_delta_basis": "notional (no trade terms: undiscounted)"} if fx and not core and delta else {}),
        },
    }


CASHFLOW_DAYS = 548
"""How far ahead the payment calendars of the concept pages read a position's ``extra.cashflows`` (18 months), up to
the offer the bond is priced to (``concepts.calendar_flows``); the flows themselves go to maturity (TDM#166)."""


def _per_bond(c: Any, face: float, freq: int | None, pct: float | None) -> tuple[float | None, bool]:
    """A coupon's amount per bond in the face currency and whether it is a forecast: the published amount, else the
    published rate on the face, else ``pct`` (the current or projected coupon) on the face (a forecast)."""
    if c.amount_per_bond is not None:
        return float(c.amount_per_bond), False
    f = freq or 2
    if c.rate_pct is not None:
        return float(c.rate_pct) / 100 * face / f, False
    return (pct / 100 * face / f, True) if pct is not None else (None, True)


def cashflows(t: Any, v: BondValuation, as_of: date, face_ccy: float, freq: int | None, pct: float | None
              ) -> list[list[Any]]:
    """The position's flows after ``as_of`` to maturity (TDM#166: the position card's schedule when the hub's static is
    not at hand; the calendars cut them, ``CASHFLOW_DAYS``): ``[date, kind, amount, forecast]`` with kind ``cpn`` /
    ``amort`` / ``put`` / ``call`` / ``mat``, the amount in the face currency for the whole position (signed by the
    side). The offer or call the bond is priced to is the face outstanding on its date (in the flows, not paid: the
    bond is redeemed only if put); a never-called perpetual redeems nothing. A coupon without a published amount or
    rate is the current coupon on the face, a forecast; so is every floater's coupon after the next one (its rate is
    fixed later, whatever the schedule repeats)."""
    q, out, floating, nth = v.quantity, [], t.coupon_type == "floating", 0
    mat = t.maturity_date or (v.horizon_date if v.horizon_kind == "maturity" else None)
    for c in sorted(t.coupons, key=lambda c: c.pay_date):
        if as_of < c.pay_date and (mat is None or c.pay_date <= mat):
            face = float(c.face) if c.face is not None else face_ccy
            amt, est = _per_bond(c, face, freq, pct)
            nth += 1
            if amt is not None:
                out.append([c.pay_date.isoformat(), "cpn", amt * q, est or (floating and nth > 1)])
    amort = [(r.date, float(r.value_per_bond)) for r in t.redemptions
             if as_of < r.date and (mat is None or r.date < mat) and r.value_per_bond is not None]
    out += [[d.isoformat(), "amort", a * q, False] for d, a in amort]

    def outstanding(d: date) -> float:  # the face per bond left on ``d``: today's less what is redeemed before
        return face_ccy - sum(a for x, a in amort if x < d)

    if v.horizon_kind in ("offer", "call") and as_of < v.horizon_date:
        out.append([v.horizon_date.isoformat(), "call" if v.horizon_kind == "call" else "put",
                     outstanding(v.horizon_date) * q, False])
    if mat is not None and as_of < mat and v.horizon_kind != "perpetual" and not t.is_perpetual:
        out.append([mat.isoformat(), "mat", outstanding(mat) * q, False])
    return sorted(out, key=lambda x: x[0])


def _quote_extra(q: Any, sa: dict[str, Any]) -> dict[str, Any]:
    """The day's quote beyond the price the valuation uses: MOEX G-spread, bid/offer when the cut has them (intraday),
    volume, turnover and trades. Null fields are left out."""
    if q is None:
        return {}
    px = q.prices_pct or {}
    out = {"gspread_bp": _f(sa.get("gspread_bp")), "bid": _f(px.get("bid")), "ask": _f(px.get("offer")),
           "volume_bonds": _f(q.volume_bonds), "value": _f(q.value), "value_ccy": q.currency_id,
           "num_trades": q.num_trades}
    return {"quote": {k: x for k, x in out.items() if x is not None}}


def core_analytics(v: BondValuation) -> dict[str, Any]:
    """Core 0.33's analytics of a valuation for ``extra`` (null fields left out): ``g_spread_bp`` (core's G-spread to
    the KBD, MOEX's definition, beside MOEX's own in ``quote.gspread_bp``), ``dm_bp`` (a floater's discount margin over
    its projection curve) and ``spread_buckets`` (``cs01`` or ``ofz_spr01`` by ``RISK_BUCKETS``: CS01 by tenor, an OFZ's
    spread to its discount curve by tenor: the KBD's since 0.2.6, RUONIA's before, R18), which sum to the row's
    ``cs01`` / ``ofz_spr01``."""
    out: dict[str, Any] = {}
    if v.g_spread_bp is not None and math.isfinite(v.g_spread_bp):
        out["g_spread_bp"] = v.g_spread_bp
    if v.discount_margin_bp is not None and math.isfinite(v.discount_margin_bp):
        out["dm_bp"] = v.discount_margin_bp
    if v.spread_buckets:
        out["spread_buckets"] = {b: x for b, x in v.spread_buckets.items() if x}
    return out


def ofz_basis(t: Any, v: BondValuation) -> dict[str, Any]:
    """``ofz_basis01`` of an OFZ on the KBD (``valuation.bonds.OFZ_KBD_CLASSES``, a RUB face): its DV01 on ``RUB OFZ``,
    the risk to the KBD's par yields moving against RUONIA OIS's (the par-par basis); its buckets are the row's
    ``risk["RUB OFZ"]``. Its presence marks a row of the KBD scheme (``bonds.OFZ_SCHEME``): until 0.2.5 an OFZ was on
    RUONIA and its basis was ``ofz_spr01``. ``ofz_ruonia01`` (TDM#213, core 0.39): its RUONIA risk, computed apart from
    the basis (to RUONIA's OIS quotes, the basis held); the row's risk is in the scenarios' coordinates
    (``bonds.ofz_levels``), so ``risk["RUB RUONIA"]`` is the RUONIA risk minus the basis and its sum over the curves is
    ``ofz_ruonia01``. Without it (0.2.6, an OFZ on a fallback rung) the pages' RUONIA is the basis again (variant P).
    A KBD floater (``bonds.is_kbd_floater``, TDM#33) projects its coupons on the KBD and is discounted on RUONIA OIS:
    core (0.40, core#90) values it in RUONIA + basis too: its basis is the projection's KBD par risk with
    RUONIA held (``RUB OFZ``, to the bit the plain two-curve risk), its ``ofz_ruonia01`` RUONIA's quotes moving the
    discount and the projection's KBD with the basis held, the row in the scenarios' coordinates as an OFZ's. Empty
    for every other row."""
    if (t.bond_class not in OFZ_KBD_CLASSES and not is_kbd_floater(t)) or bond_currency(t) != "RUB":
        return {}
    out = {"ofz_basis01": math.fsum((v.risk().get(OFZ_CURVE) or {}).values()) + 0.0}
    if v.ofz_ruonia:
        out["ofz_ruonia01"] = math.fsum(v.ofz_ruonia.values()) + 0.0
    return out


def _f(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def position_row(ctx: EodContext, book: str, pid: str, o: Outcome, issuer: str,
                 x: dict[str, Any] | None = None) -> dict[str, Any]:
    t, q, v = o.terms, o.quote, o.valuation
    assert t is not None and v is not None
    as_of = ctx.business_date
    spread_dur = -v.spread01_rub / v.pv_rub * 1e4 if v.pv_rub and v.spread01_rub else None
    sa = q.source_analytics if q else {}
    ccy = bond_currency(t)  # the face unit, else the currency (RUR, SUR: RUB), TDM#169
    fx = fx_rate(ctx, ccy) if ccy != "RUB" else None  # foreign face: amounts in RUB, notional and FX delta in ccy
    cpn = coupon_view(t, as_of, lambda code: (f[1] if (f := ctx.market.latest_fixing(code)) else None))
    if cpn.pct is None and v.current_coupon_pct is not None:
        cpn = replace(cpn, pct=v.current_coupon_pct, basis="rate")
    freq = t.coupon_frequency or implied_frequency(t)
    face_ccy = v.face_rub / (fx[0] if fx else 1.0)
    rating = ratings.compact(ctx.reference_facts.ratings.bond(t.secid, t.issuer_id))
    offer = ({"offer_date": v.horizon_date.isoformat(), "offer_kind": "call" if v.horizon_kind == "call" else "put"}
             if v.horizon_kind in ("offer", "call") else {})
    return {
        "position_id": pid, "book_id": book, "instr_type": "BOND",
        "asset_class": ASSET_CLASS.get(t.bond_class, t.bond_class), "segment": SEGMENT.get(t.bond_class, "Корп"),
        "name": t.name or t.short_name, "isin": t.isin, "issuer": issuer, "ccy": ccy if fx else "RUB",
        "maturity": t.maturity_date.isoformat() if t.maturity_date else None,
        "tenor_y": (t.maturity_date - as_of).days / 365.25 if t.maturity_date else None,
        "coupon_type": COUPON_TYPES.get(t.coupon_type, t.coupon_type), "coupon": cpn.pct,
        "index_name": cpn.index_name, "margin": cpn.margin_pct,
        "side": "long" if v.quantity >= 0 else "short",
        "notional": v.quantity * v.face_rub / (fx[0] if fx else 1.0), "notional_rub": v.quantity * v.face_rub,
        "price": v.clean_pct, "mv": v.pv_rub / (fx[0] if fx else 1.0), "mv_rub": v.pv_rub,
        "ytm": _num(v.ytm_pct), "spread_bp": _num(v.z_spread_bp), "mdur": _num(v.modified_duration), "spr_dur": spread_dur,
        "convexity": v.curve_convexity, "dv01": v.dv01_rub,
        "cs01": v.spread01_rub if v.spread_kind == "credit" else 0.0,
        "ofz_spr01": v.spread01_rub if v.spread_kind == "ofz" else 0.0,
        "instrument_id": t.secid, "accrued": v.accrued_rub, "coupon_freq": freq,
        "yield_date": v.horizon_date.isoformat() if v.horizon_kind in ("offer", "call") else None,
        "spread_type": "Z", "spread_curve": v.spread_curve, "new_today": False,
        "risk": risk_by_curve(v),
        **({"fx_delta": {ccy: v.pv_rub / fx[0]}} if fx else {}),
        **({"pnl": o.pnl.as_tdm()} if o.pnl else {}),
        "gamma": [[a, b, g] for a, b, g in v.hessian],
        "extra": {
            "price_basis": v.price_basis, "horizon_kind": v.horizon_kind, "valuation_date": v.valuation_date.isoformat(),
            "settlement": v.settlement.isoformat(),
            "quantity_bonds": v.quantity, "calculation_id": v.calculation_id, "core_version": v.core_version,
            "moex_yield_close_pct": sa.get("yield_close_pct"), "moex_duration_days": sa.get("duration_days"),
            "valuation": "computed" if v.quality == "exact" else "approximated",
            "quality": v.quality, "assumptions": list(v.assumptions),
            "short_name": t.short_name, "coupon_basis": cpn.basis, **offer,
            **({"lat_name": t.lat_name} if t.lat_name else {}),
            **({"issue_size": float(t.issue_size)} if t.issue_size is not None else {}),
            **({"rating": rating} if rating else {}),
            "cashflows": cashflows(t, v, as_of, face_ccy, freq, cpn.pct), "cashflows_to": "maturity",
            **_quote_extra(q, sa), **core_analytics(v),
            **ofz_basis(t, v),
            **({"coupon_freq_source": "schedule"} if freq and not t.coupon_frequency else {}),
            **({"not_exact_reason": o.reason} if o.reason else {}),
            **({"fx_rate": fx[0], "fx_source": fx[1]} if fx else {}),
            **({"pnl": pnl_extra(o.pnl)} if o.pnl else {}),
            **reported(x),
        },
    }


def git_sha() -> str | None:
    """Backend commit of a source checkout (None for an installed wheel)."""
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"],
                             check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


REFERENCE_KINDS = ("books", "issuers", "curves", "fx", "indices")


@dataclass
class RunContent:
    """What a run publishes into the desk store."""

    rows: list[dict[str, Any]]
    """``position_row`` of each valued position, in book order."""
    reference: dict[str, list[dict[str, Any]]]
    """By ``REFERENCE_KINDS``."""
    log: list[tuple[str, str]]
    """(level, message) of the run log, in order."""


def run_content(ctx: EodContext, positions: list[tuple[str, str, Outcome]], prev: MarketView | None,
                lineage: dict[str, Any], inputs: PositionInputs | None = None) -> RunContent:
    """positions: (position_id, book_id, outcome) of ``inputs`` (needed for the v1 rows of S7a). The log counts
    the qualities and says, per position, why it is not exact, not valued, or without PnL."""
    extra = inputs.extra if inputs else {}
    quantities = {pid: q for pid, _, _, q in inputs.rows} if inputs else {}
    issuers: dict[str, dict[str, Any]] = {}
    rows, skipped, no_pnl, approx = [], [], [], []
    quality: Counter[str] = Counter()
    for pid, book, o in positions:
        x = extra.get(pid)
        label = o.secid or (x and (x.get("source_instrument_id") or x.get("ccy"))) or "—"
        if o.valuation is None:
            skipped.append(f"{pid} {label}: не посчитано, нет данных "
                           f"({o.reason}{': ' + o.detail if o.detail else ''})")
            continue
        quality[o.valuation.quality] += 1
        if o.valuation.quality != "exact":
            approx.append(f"{pid} {label}: оценка {o.valuation.quality}: " + "; ".join(o.valuation.assumptions))
        if o.terms is None:  # valued without core (valuation.positions): PnL by the rate move or the reports
            if prev is not None and o.pnl is None:
                no_pnl.append(f"{pid} {label}: нет PnL ({_why(o)})")
            rows.append(row := direct_row(ctx, book, pid, o, x or {}, quantities.get(pid)))
            if row["instr_type"] == "BOND":  # tdm needs an issuer for a bond: the report's (D6), else none known
                iss = _report_issuer(x or {})
                row["issuer"] = iss["issuer"]
                issuers[row["issuer"]] = iss
            continue
        if o.pnl is None and prev is not None:
            no_pnl.append(f"{pid} {o.secid}: нет PnL ({_why(o)})")
        if isinstance(o.pnl, FlashPnl) and o.pnl.flag:
            no_pnl.append(f"{pid} {o.secid}: PnL {o.pnl.total:,.0f} ₽ с модельной ценой ({o.pnl.flag})")
        key, rec = _issuer_key(ctx, o)
        issuers[key] = rec
        rows.append(position_row(ctx, book, pid, o, key, x))
    # every book of the run, also one whose positions are all unvalued (an OTC-only book): the Desk Monitor shows
    # only the positions of the reference books, so a book missing here would hide its rows from coverage
    used = list(dict.fromkeys([r["book_id"] for r in rows] + [book for _, book, _ in positions]))
    indices = []
    for code, name in INDICES:
        fx = ctx.market.latest_fixing(code)
        if fx:
            indices.append({"index_name": name, "value": fx[1]})
    # and the currencies of the rows not valued (R45: an OMR row without MV left the page without an OMR rate, so it
    # could not say what the row is in roubles)
    unvalued = [x for pid, _, o in positions if o.valuation is None and (x := extra.get(pid))]
    rates_fx = _fx_rows(ctx, prev, [*row_currencies(rows), *row_currencies(unvalued)])
    reference = {
        "books": book_rows(used, inputs.books if inputs else {}),
        "issuers": list(issuers.values()),
        "curves": curves_reference(ctx, prev, rows, rates_fx),
        "fx": rates_fx,
        "indices": indices,
    }
    log = [("INFO", "lineage " + json.dumps(lineage, ensure_ascii=False, default=str))]
    if ctx.point_in_time_warning:
        log.append(("WARN", "статика не на дату рынка: " + ctx.point_in_time_warning))
    log.append(("INFO", "качество оценки: " + ", ".join(f"{k} {quality[k]}" for k in QUALITIES if quality[k])
                + (f", не посчитано {len(skipped)}" if skipped else "")))
    log += rates_log(ctx, positions, rows)
    log.append(("INFO", reference_log(ctx, rows, reference["issuers"])))
    if prev is None:
        log.append(("WARN", (f"PnL не посчитан: {NO_PNL_TEXT[RUN_NO_PREV_MARKET]} (нет среза eod_final раньше "
                             f"{ctx.business_date}); позиции без PnL, а не с нулём")))
    log += [("WARN", s) for s in skipped + approx + no_pnl]
    for r in rows:  # TDM#170: an impossible risk is the run's problem, said here and kept out of every sum (summary)
        if (why := risk_problem(r.get("risk"), r.get("dv01"), r.get("mv_rub"), r.get("notional_rub"),
                                 r.get("instr_type") == "BOND")) is not None:
            r["extra"]["risk_problem"] = why
            log.append(("WARN", (f"{r['position_id']} {r.get('instrument_id') or r.get('name') or '—'}: {why}; "
                                 "риск позиции не в суммах DV01 (сводка прогона, таблица риска)")))
    direct = [o.pnl for _, _, o in positions if isinstance(o.pnl, DirectPnl)]
    for method, flag in ((FX_METHOD, "по курсу"), (REPORTED_METHOD, "по отчёту"), (CORE_FX_METHOD, "ядром")):
        if xs := [x for x in direct if x.method == method]:
            log.append(("INFO", f"PnL {flag} ({method}) у {len(xs)} поз.: {sum(x.total for x in xs):,.0f} ₽"))
    with_pnl = [o.pnl for _, _, o in positions if o.pnl and not isinstance(o.pnl, DirectPnl)]
    simple = [x for x in with_pnl if isinstance(x, SimplePnl)]
    if simple:
        log.append(("WARN", (f"флэш-PnL без разложения ({SIMPLE_METHOD}: q x Δdirty + выплаты, всё в other) "
                             f"у {len(simple)} поз.: {sum(x.total for x in simple):,.0f} ₽")))
    if with_pnl:
        tot = {k: sum(getattr(x, k) for x in with_pnl) for k in ("carry", "rolldown", "rates", "spread", "other", "fx_gross")}
        worst = max(abs(x.identity_error) for x in with_pnl)
        log.append(("INFO", f"флэш-PnL {with_pnl[0].prev_date} -> {with_pnl[0].as_of} ({METHOD}), "
                    f"позиций {len(with_pnl)} (без разложения {len(simple)}): "
                    + ", ".join(f"{k} {v:,.0f}" for k, v in tot.items())
                    + f"; итог {sum(x.total for x in with_pnl):,.0f} ₽, макс. ошибка тождества {worst:.2e} ₽"))
    reasons: dict[str, list[float]] = {}  # the unexplained by its reason (TDM#442): other, and reported's other
    for x in [o.pnl for _, _, o in positions if o.pnl]:
        if isinstance(r := x.other_reason, dict) and r.get("code") in OTHER_REASON_TEXT:
            reasons.setdefault(r["code"], []).append(x.other)
    if reasons:
        log.append(("INFO", "необъяснённое по причинам: " + "; ".join(
            f"{OTHER_REASON_TEXT[c]} — {len(reasons[c])} поз., {sum(reasons[c]):,.0f} ₽"
            for c in OTHER_REASON_TEXT if c in reasons)))
    log.append(("WARN", "JTD не считается в слайсе S1 (нет решения по recovery): поле jtd = 0"))
    log.append(("INFO", f"опубликовано позиций: {len(rows)} (точно {quality['exact']}), не посчитано: {len(skipped)}"))
    return RunContent(rows, reference, log)


def reference_log(ctx: EodContext, rows: list[dict[str, Any]], issuers: list[dict[str, Any]]) -> str:
    """The run log's line on the reference facts (R25): which static they come from and how many bonds have a rating
    (by basis) and how many issuers a sector."""
    ref = ctx.reference_facts
    bonds = [r for r in rows if r["instr_type"] == "BOND" and r.get("coupon_type") is not None]
    by = Counter(((r.get("extra") or {}).get("rating") or {}).get("basis", "нет") for r in bonds)
    mapped = [i for i in issuers if not str(i["issuer"]).startswith(("report:", "issuer:"))]
    own = "" if ref.snapshot_id == ctx.static.snapshot.id else f" (новее статики рынка {ctx.static.snapshot.id[:12]})"
    return (f"справочник эмитентов и рейтинги: статика {ref.snapshot_id[:12]} от {ref.business_date}"
            f" ({ref.schema_version}){own}; рейтинг у {len(bonds) - by.get('нет', 0)} из {len(bonds)} облигаций ("
            + ", ".join(f"{ratings.BASIS_TEXT.get(k, k)} {n}" for k, n in sorted(by.items())) + ")"
            + ("; строк рейтингов в статике нет" if not ref.ratings.has_data else "")
            + f"; сектор у {sum(i['sector'] != NO_DATA for i in mapped)} из {len(mapped)} эмитентов")


def rates_log(ctx: EodContext, positions: list[tuple[str, str, Outcome]], rows: list[dict[str, Any]]
              ) -> list[tuple[str, str]]:
    """The run log's lines on the rates inputs and the risk by curve (R14, R15): a WARN line per missing input
    (``rates.input_notes``: ``<code>: <text>``), the number of positions with risk on each curve, and the floaters by
    benchmark: how many core valued (exact/assumed) and why the others are on a fallback rung (reason codes)."""
    out = [("WARN", f"{n['code']}: {n['text']}" + (f" ({n['detail']})" if n["detail"] else ""))
           for n in rates.input_notes(ctx.rates, ctx.business_date)]
    on = Counter(c for r in rows for c in r.get("risk") or {})
    out.append(("INFO", "риск по кривым, позиций: " + (", ".join(f"{c} {n}" for c, n in sorted(on.items())) or "нет")))
    floaters: dict[str, Counter[str]] = {}
    for _, _, o in positions:
        t = o.terms
        if t is None or t.coupon_type != "floating":
            continue
        bench = (t.floater_formula or {}).get("benchmark") or "без формулы"
        v = o.valuation
        key = (v.quality if v and v.quality in ("exact", "assumed") else
               f"{v.quality if v else 'не посчитано'} ({o.reason or 'нет причины'})")
        floaters.setdefault(bench, Counter())[key] += 1
    if floaters:
        out.append(("INFO", "флоатеры по индексам: " + "; ".join(
            f"{b}: " + ", ".join(f"{k} {n}" for k, n in sorted(c.items())) for b, c in sorted(floaters.items()))))
    out += fx_log(ctx, positions)
    return out


def fx_summary(ctx: EodContext, positions: list[tuple[str, str, Outcome]]) -> dict[str, Any]:
    """What was valued how in the FX and derivative book (M6): rows by core (per product), by the report or the
    notional (per reason), foreign bonds by curve, and the FX inputs that are not built (``curves.fx`` codes)."""
    core: Counter[str] = Counter()
    other: Counter[str] = Counter()
    bonds: Counter[str] = Counter()
    for _, _, o in positions:
        v = o.valuation
        if v is not None and v.extra.get("core"):
            core[v.extra["core"]["product"]] += 1
        elif o.terms is None and v is not None and v.price_basis == "fx" and v.horizon_kind == "value_date" \
                and v.horizon_date > ctx.business_date:
            other[o.reason or "no_reason"] += 1
        elif o.terms is not None and v is not None and v.foreign:
            bonds[f"{v.spread_curve} {v.quality}"] += 1
    return {"core": dict(core), "not_core": dict(other), "foreign_bonds": dict(bonds),
            "inputs_missing": list(ctx.fx.lineage.get("codes") or [])}


def fx_log(ctx: EodContext, positions: list[tuple[str, str, Outcome]]) -> list[tuple[str, str]]:
    """The run log's line on the FX book (``fx_summary``), when there is any."""
    s = fx_summary(ctx, positions)
    if not (s["core"] or s["not_core"] or s["foreign_bonds"]):
        return []
    parts = []
    if s["core"]:
        parts.append("ядром: " + ", ".join(f"{k} {n}" for k, n in sorted(s["core"].items())))
    if s["not_core"]:
        parts.append("форварды не ядром: " + ", ".join(f"{k} {n}" for k, n in sorted(s["not_core"].items())))
    if s["foreign_bonds"]:
        parts.append("валютные облигации: " + ", ".join(f"{k} {n}" for k, n in sorted(s["foreign_bonds"].items())))
    if s["inputs_missing"]:
        parts.append("нет входов: " + ", ".join(s["inputs_missing"]))
    return [("INFO", "FX-книга: " + "; ".join(parts))]

