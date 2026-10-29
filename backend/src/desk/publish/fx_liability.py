"""The currency OCP liability of the run's foreign-currency bonds and the bonds' FX delta net of it (OQ-04, OQ-17,
TDM#54, TDM#169, TDM#266).

**Rule** (owner's decisions: OQ-04 of 23.10.2026, a foreign-currency bond is funded in its own currency; DEC01, the
funding base is the day's clean MV; OQ-17 of 26.10.2026, the organisation's rule, kept in the private adapter;
``docs/program/DECISIONS.md``):

- the funding *base* (the body the rate accrues on, interest paid in RUB) follows the day's MV (DEC01): that is the
  funding engine's (``funding_run``), untouched here;
- the currency *OCP liability* does **not** follow the MV. The bond's price move, its accrued growth and its coupons
  are FX PnL: they create the bond book's OCP, and the daily OCP transfer to the derivatives book (``ocp_to``, DEC06)
  moves it out, so at the end of the day the bond book keeps a residual only;
- the data has no purchase and no transfer history per position, so with a daily transfer the liability is the bond's
  **dirty MV of the previous business day** in its currency (the previous default run's row; at today's quantity when
  the quantity changed), as if yesterday's transfer had been exact. **Предварительно:** the residual of past days does
  not accumulate;
- the position's FX delta (OCP) = its dirty MV of today − the liability + the coupons received on the day, in the
  currency: the FX PnL since the last transfer. The bond book's OCP is then Σ of these − the day's transfer
  (``rules.ocp``: the transfer deals are their own component, counted once);
- without the previous business day's row (a position new on D, no run of D-1): the purchase cost (the report's
  ``purchase_price_pct`` x face + ``purchase_accrued_ccy``) when the position was bought after D-1 (on D when there
  is no D-1), else today's dirty MV (OCP 0); the reason is in the position's ``reference_text`` and the block's notes, never silent.

**Coupons received on D** (in the currency, signed as the position): the flash PnL's received flows (``received_rub``
at its D rate) when the bond has one, else the bond's schedule (``amount_per_bond`` x quantity, paid in (D-1, D]); a
coupon of the day without an amount is said and left out.

**Without a liability** the bond keeps its dirty MV as FX delta and says why (``state``): ``not_configured`` (no
funding rule), ``problem`` (an unknown entity, a book type without a base, no FX rate for an out-of-currency bond),
``none`` (a book not funded on its MV, an entity funded on its whole cash balance, a bond funded in RUB).

**FX revaluation of the liability** (``fx_funding`` of the flash PnL, TDM#54): the liability held over the day, the
previous business day's dirty MV, x the rate move, at the rates of the position's ``fx_gross`` (the market rates:
``FlashPnl.fx_rates``, else the markets'), and x the CBR move in ``fx_funding_cbr``. ``fx_gross + fx_funding`` is then
the FX of the bond net of its funding: the revaluation of yesterday's OCP, which the transfer moved out (≈ 0); the
day's FX PnL itself stays in the OCP. A liability not of D-1 (purchase cost, today's MV) is not held over the day:
``fx_funding`` 0 and the reason.

**Output** (``RunFxLiability``): per position ``fact.extra.fx_liability`` and the row's ``fx_delta`` net of the
liability; the block stored inside the run's funding block (``run_reference`` kind ``funding``, key ``fx_liability``);
the PnL's ``fx_funding`` / ``fx_funding_cbr``; log lines. The OCP of the run (``ocp_run``) then takes the bonds net and
leaves its ``funding`` component off (``OcpPolicy.bond_funding_liability``): the liability is subtracted once.

The run never fails on it: an error is status ``failed``, the bonds keep their dirty MV, the log says why.
"""

from __future__ import annotations

import dataclasses
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg

from .. import funding_settings, store
from ..marketdata.adapter import MarketView
from ..rules import funding as F
from ..valuation.fallback import market_fx
from . import funding_run

KEY = "fx_liability"
"""The key of the block inside the run's funding block."""
COMPUTED, NOT_CONFIGURED, INVALID, FAILED = "computed", "not_configured", "invalid_config", "failed"
NONE, PROBLEM = "none", "problem"
STATUS_TEXT = {
    COMPUTED: "валютный ОВП-пассив облигаций посчитан, FX-дельта облигаций — за его вычетом",
    NOT_CONFIGURED: "валютный пассив не посчитан: фондирование не настроено (нет правил ни от адаптера, ни в настройках, "
                    "ставка по умолчанию выключена); FX-дельта облигаций — грязная MV",
    INVALID: "валютный пассив не посчитан: правила фондирования противоречат друг другу; FX-дельта облигаций — "
             "грязная MV",
    FAILED: "валютный пассив не посчитан: ошибка расчёта; FX-дельта облигаций — грязная MV",
}
RULE = ("ОВП облигации = грязная MV в валюте − валютный пассив + купоны дня: валютный PnL с прошлого переноса ОВП; "
        "пассив = грязная MV на последний перенос (прошлый рабочий день), за MV дня не следует (OQ-04, OQ-17). "
        "База фондирования и проценты (в рублях) — от чистой MV дня (DEC01), ОВП не меняют")
PROVISIONAL = [("residual прошлых дней не накапливается: пассив — грязная MV прошлого рабочего дня, как будто вчерашний "
                "перенос ОВП был точным (истории переносов и покупок по позициям в данных нет)")]
"""What the rule takes until the data has more (shown in the block and the log as «предварительно»)."""
PREV, PURCHASE, CURRENT = "prev_dirty_mv", "purchase_cost", "current_dirty_mv"
REFERENCE_TEXT = {
    PREV: "грязная MV на {d0} (последний перенос ОВП)",
    PURCHASE: "стоимость покупки {pdate} (чистая цена × номинал + НКД при покупке): позиции на {d0} не было",
    CURRENT: "грязная MV дня, ОВП 0: {why}",
}
LOG_ITEMS = 50


def _day(x: Any) -> date | None:
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10]) if x else None
    except ValueError:
        return None


def _qty(row: Mapping[str, Any]) -> float | None:
    return funding_run._f((row.get("extra") or {}).get("quantity_bonds"))


def purchase_cost(row: Mapping[str, Any], x: Mapping[str, Any]) -> float | None:
    """The purchase cost of a bond position in its currency, signed as the position: face x ``purchase_price_pct`` /
    100 + ``purchase_accrued_ccy`` (the report's, private/1.3); None without the price or the face. ``x``: the report
    row's fields (``PositionInputs.extra``); the face is the report's ``face_outstanding_ccy`` / ``face_amount_ccy``,
    else the row's notional."""
    price = funding_run._f(x.get("purchase_price_pct"))
    face = next((f for k in ("face_outstanding_ccy", "face_amount_ccy") if (f := funding_run._f(x.get(k))) is not None),
                funding_run._f(row.get("notional")))
    if price is None or not face:
        return None
    mv = funding_run._f(row.get("mv")) or face
    sign = -1.0 if mv < 0 else 1.0
    return sign * (abs(face) * price / 100 + abs(funding_run._f(x.get("purchase_accrued_ccy")) or 0.0))


def reference(row: Mapping[str, Any], prev_row: Mapping[str, Any] | None, d0: date | None, has_prev_run: bool,
              x: Mapping[str, Any], on: date) -> tuple[float, str, str]:
    """(the bond's dirty MV at the last OCP transfer in its currency, signed; its kind ``PREV`` / ``PURCHASE`` /
    ``CURRENT``; the text) of a bond row of ``on`` (module docstring). ``prev_row``: the same position's row of the
    previous default run (of ``d0``); ``x``: the report row's fields."""
    dirty = float(row["mv"])
    if prev_row is not None and (prev_row.get("ccy") or "") == (row.get("ccy") or "") \
            and (mv0 := funding_run._f(prev_row.get("mv"))) is not None:
        text = REFERENCE_TEXT[PREV].format(d0=d0)
        q0, q1 = _qty(prev_row), _qty(row)
        if q0 and q1 is not None and abs(q1 - q0) > 1e-9:
            mv0 *= q1 / q0
            text += f"; количество {q0:g} → {q1:g}: пассив — по грязной цене {d0} на текущее количество (предварительно)"
        return mv0, PREV, text
    if prev_row is not None:
        why = f"на {d0} позиция в другой валюте или без MV"
    elif d0 is None:
        why = "нет предыдущего рабочего дня"
    elif not has_prev_run:
        why = f"нет прогона за {d0}"
    else:
        why = f"позиции на {d0} не было"
    pdate = _day(x.get("purchase_date"))
    bought_today = pdate is not None and pdate <= on and (pdate > d0 if d0 is not None else pdate == on)
    if prev_row is None and bought_today and (cost := purchase_cost(row, x)) is not None:
        return cost, PURCHASE, REFERENCE_TEXT[PURCHASE].format(pdate=pdate.isoformat(), d0=d0 or "прошлый день")
    return dirty, CURRENT, REFERENCE_TEXT[CURRENT].format(why=why)


def coupons_received(row: Mapping[str, Any], terms: Any, outcome: Any, d0: date | None, on: date
                     ) -> tuple[float, str]:
    """(the coupons the bond received on the day in its currency, signed as the position; a note when one is left
    out): the flash PnL's received flows, else the schedule's ``amount_per_bond`` paid in (``d0``, ``on``] x the
    quantity."""
    fp = getattr(outcome, "pnl", None)
    rates = getattr(fp, "fx_rates", None)
    if fp is not None and rates and getattr(fp, "ccy", None) == row.get("ccy") and rates[1]:
        return float(fp.received_rub) / rates[1], ""
    if terms is None or d0 is None:
        return 0.0, ""
    paid = [c for c in getattr(terms, "coupons", ()) if d0 < c.pay_date <= on]
    if not paid:
        return 0.0, ""
    q = _qty(row)
    if q is None:
        return 0.0, ("купон " + ", ".join(c.pay_date.isoformat() for c in paid)
                     + ": количество бумаг неизвестно, в ОВП не учтён")
    missing = [c.pay_date.isoformat() for c in paid if getattr(c, "amount_per_bond", None) is None]
    amount = q * sum(float(c.amount_per_bond) for c in paid if getattr(c, "amount_per_bond", None) is not None)
    return amount, ("купон " + ", ".join(missing) + " без суммы: в ОВП не учтён") if missing else ""


@dataclass
class _Policy:
    policy: F.FundingPolicy | None
    entities: dict[str, F.EntityType] | None
    status: str
    text: str


def resolve_policy(con: psycopg.Connection, on: date, known_at: Any,
                   fs: funding_settings.FundingSettings | None = None) -> _Policy:
    """The funding policy of the run's funding on ``on`` (``funding_run.run_policy``: the adapter's and the settings'
    rules, bases and conventions, #237), or why there is none."""
    rp = funding_run.run_policy(con, on, known_at, fs if fs is not None else funding_settings.load())
    text = STATUS_TEXT[rp.status] + (f" ({rp.text})" if rp.status == INVALID else "")
    return _Policy(rp.policy, rp.entities, rp.status, text)


def liabilities(rows: list[dict[str, Any]], books: Mapping[str, Mapping[str, Any]], extra: Mapping[str, Mapping],
                on: date, pol: F.FundingPolicy, entities: Mapping[str, F.EntityType] | None,
                fx: F.FxProvider | None = None, *, previous: Mapping[str, Mapping[str, Any]] | None = None,
                d0: date | None = None, coupons: Mapping[str, float] | None = None
                ) -> dict[str, dict[str, Any]]:
    """position_id -> ``extra.fx_liability`` of every foreign-currency bond row (``state`` and, when computed, the
    liability ``amount_ccy`` (negative for a long), its ``reference`` (``PREV`` / ``PURCHASE`` / ``CURRENT``),
    ``reference_mv_ccy``, ``reference_date``, ``reference_text``, today's ``dirty_mv_ccy``, the day's
    ``coupons_ccy``, the FX delta before (``fx_delta_gross``) and after (``fx_delta_net``)). ``previous``: position_id
    -> its row of the previous default run (of ``d0``), None without that run; ``coupons``: position_id -> the coupons
    received on the day in the currency."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("instr_type") != "BOND" or (r.get("ccy") or pol.reporting_ccy) == pol.reporting_ccy:
            continue
        pid = r["position_id"]
        p, _ = funding_run._position(r, dict(books))
        if p is None:
            out[pid] = {"state": PROBLEM, "reason": "нет MV"}
            continue
        base = pol.base_by_book_type.get(p.book_type)
        ref, kind, text = reference(r, (previous or {}).get(pid), d0, previous is not None, extra.get(pid) or {}, on)
        at = dataclasses.replace(p, mv_clean=ref, accrued=0.0)  # the position as of its reference: dirty MV
        liab, probs = F.funding_liabilities([at], policy=pol, entities=entities, fx=fx, on=on)
        if probs:
            out[pid] = {"state": PROBLEM, "reason": "; ".join(x.reason for x in probs)}
            continue
        if not liab:
            etype = (entities or {}).get(p.entity, F.EntityType.MAIN) if entities else F.EntityType.MAIN
            out[pid] = {"state": NONE, "reason": "юрлицо фондируется по всему остатку ДС" if etype is F.EntityType.OTHER
                        else f"тип книги {p.book_type or '—'}: база {base.value if base else '—'}, не по MV"}
            continue
        x = liab[0]
        if x.ccy == pol.reporting_ccy:
            out[pid] = {"state": NONE, "reason": "фондируется в рублях (out_of_currency = settlement_ccy): FX открыт"}
            continue
        gross = (r.get("fx_delta") or {}).get(x.ccy, 0.0)
        cpn = (coupons or {}).get(pid, 0.0) if kind == PREV else 0.0
        out[pid] = {
            "state": COMPUTED, "ccy": x.ccy, "amount_ccy": x.amount, "reference": kind, "reference_mv_ccy": ref,
            "reference_date": (d0.isoformat() if d0 else None) if kind == PREV else None, "reference_text": text,
            "clean_mv_ccy": p.mv_clean, "accrued_ccy": p.accrued, "dirty_mv_ccy": p.mv_dirty, "coupons_ccy": cpn,
            "base": base.value if base else None, "fx_delta_gross": gross, "fx_delta_net": gross + x.amount + cpn,
        }
    return out


def fx_funding(amount_ccy: float, rates: tuple[float, float] | None, cbr: tuple[float | None, float | None]
               ) -> tuple[float | None, float | None]:
    """(``fx_funding``, ``fx_funding_cbr``): the liability held over the day x the market and the CBR rate move; None
    without a rate."""
    mkt = amount_ccy * (rates[1] - rates[0]) if rates else None
    c = amount_ccy * (cbr[1] - cbr[0]) if cbr[0] and cbr[1] else None
    return mkt, c


@dataclass
class RunFxLiability:
    status: str
    block: dict[str, Any]
    per_position: dict[str, dict[str, Any]] = field(default_factory=dict)
    pnl: dict[str, tuple[float, float | None]] = field(default_factory=dict)
    """position_id -> (fx_funding, fx_funding_cbr) of its flash PnL."""
    log: list[tuple[str, str]] = field(default_factory=list)

    def lineage(self) -> dict[str, Any]:
        n = sum(1 for x in self.per_position.values() if x.get("state") == COMPUTED)
        return {"status": self.status, "positions": n, "provisional": self.block.get("provisional") or []}

    def apply(self, rows: list[dict[str, Any]], reference: dict[str, Any], log: list[tuple[str, str]],
              outcomes: Mapping[str, Any] | None = None) -> None:
        """Into the run's content: ``extra.fx_liability`` of each foreign bond, its ``fx_delta`` net of the liability,
        the PnL's ``fx_funding`` (the row's ``pnl`` and the outcome's ``pnl``), the block into the funding block, the
        log lines before the run log's last line."""
        for r in rows:
            pid = r["position_id"]
            if (x := self.per_position.get(pid)) is None:
                continue
            r.setdefault("extra", {})[KEY] = x
            if x.get("state") == COMPUTED:
                r["fx_delta"] = {**(r.get("fx_delta") or {}), x["ccy"]: x["fx_delta_net"]}
            if pid in self.pnl and r.get("pnl") is not None:
                mkt, cbr = self.pnl[pid]
                r["pnl"]["fx_funding"] = mkt
                if cbr is not None:
                    r["pnl"]["fx_funding_cbr"] = cbr
                o = (outcomes or {}).get(pid)
                if o is not None and o.pnl is not None and hasattr(o.pnl, "fx_funding"):
                    o.pnl = dataclasses.replace(o.pnl, fx_funding=mkt, fx_funding_cbr=cbr)
        reference.setdefault("funding", {})[KEY] = self.block
        at = len(log) - 1 if log else 0
        log[at:at] = self.log


def _empty(status: str, text: str, **kw: Any) -> dict[str, Any]:
    return {"status": status, "text": text, "rule": RULE, "provisional": PROVISIONAL, "by_ccy": {},
            "positions": {}, "notes": [], **kw}


def compute(con: psycopg.Connection, on: date, prev: MarketView | None, market: MarketView,
            rows: list[dict[str, Any]], run_books: list[dict[str, Any]], inputs: store.PositionInputs,
            terms: Mapping[str, Any] | None = None, outcomes: Mapping[str, Any] | None = None) -> RunFxLiability:
    """The liabilities of the run of ``on`` (module docstring). ``rows``: ``RunContent.rows`` (after funding);
    ``terms``: position_id -> ``BondTerms``; ``outcomes``: position_id -> ``Outcome`` (its flash PnL: FX rates and
    the flows received). In a savepoint (TDM#215), as the funding and the OCP: an SQL error fails only the block."""
    try:
        with funding_run.savepoint(con):
            return _compute(con, on, prev, market, rows, run_books, inputs, terms or {}, outcomes or {})
    except Exception as e:  # noqa: BLE001 - never fails the run; the bonds keep their dirty MV
        text = f"{STATUS_TEXT[FAILED]}: {type(e).__name__}: {e}"
        return RunFxLiability(FAILED, _empty(FAILED, text), log=[("WARN", text[:2000])])


def _books(con: psycopg.Connection, on: date, known_at: Any, run_books: list[dict[str, Any]]) -> dict[str, dict]:
    books = {b["book_id"]: b for b in store.current_books(con, on, known_at)}
    books.update({b["book_id"]: b for b in run_books})
    return books


def _compute(con: psycopg.Connection, on: date, prev: MarketView | None, market: MarketView,
             rows: list[dict[str, Any]], run_books: list[dict[str, Any]], inputs: store.PositionInputs,
             terms: Mapping[str, Any], outcomes: Mapping[str, Any]) -> RunFxLiability:
    foreign = [r for r in rows if r.get("instr_type") == "BOND"
               and (r.get("ccy") or funding_run.REPORTING_CCY) != funding_run.REPORTING_CCY]
    rp = resolve_policy(con, on, inputs.known_at)
    if rp.policy is None:
        per = {r["position_id"]: {"state": rp.status, "reason": rp.text} for r in foreign}
        # the funding's own log line already says it is not configured (or invalid): no second line
        return RunFxLiability(rp.status, _empty(rp.status, rp.text, positions=per), per)
    pol, books = rp.policy, _books(con, on, inputs.known_at, run_books)
    d0 = prev.business_date if prev is not None else None
    previous: dict[str, dict[str, Any]] | None = None
    if d0 is not None and foreign:
        rid, facts, _ = funding_run._previous_rows(con, d0)
        previous = None if rid is None else {f["position_id"]: f for f in facts}
    notes: list[str] = []
    warn: set[str] = set()
    coupons = {}
    for r in foreign:
        pid = r["position_id"]
        coupons[pid], why = coupons_received(r, terms.get(pid), outcomes.get(pid), d0, on)
        if why:
            notes.append(m := f"{pid}: {why}")
            warn.add(m)
    per = liabilities(foreign, books, inputs.extra, on, pol, rp.entities, funding_run.fx_table([market]),
                      previous=previous, d0=d0, coupons=coupons)
    by_ccy: dict[str, float] = defaultdict(float)
    for pid, x in sorted(per.items()):
        if x["state"] == COMPUTED:
            by_ccy[x["ccy"]] += x["amount_ccy"]
            if x["reference"] != PREV or "количество" in x["reference_text"]:
                notes.append(f"{pid}: пассив — {x['reference_text']}")
        else:
            notes.append(m := f"{pid}: валютного пассива нет ({x['reason']}): FX-дельта — грязная MV")
            warn.add(m)
    pnl, pnl_notes = _fx_funding(on, prev, market, rows, outcomes, per)
    notes += pnl_notes
    for pid, x in per.items():
        if pid in pnl:
            x["fx_funding"], x["fx_funding_cbr"] = pnl[pid]
    block = funding_run._jsonable(_empty(COMPUTED, STATUS_TEXT[COMPUTED], by_ccy=dict(by_ccy), positions=per,
                                         notes=notes))
    n = sum(1 for x in per.values() if x["state"] == COMPUTED)
    log = [("INFO", f"валютный ОВП-пассив: {n} из {len(per)} валютных облигаций, "
                    + (", ".join(f"{c} {a:,.0f}" for c, a in sorted(by_ccy.items())) or "нет")
                    + "; FX-дельта облигаций — валютный PnL с прошлого переноса ОВП; предварительно: "
                    + "; ".join(PROVISIONAL))]
    log += [("WARN" if m in warn else "INFO", f"валютный пассив: {m}") for m in notes[:LOG_ITEMS]]
    if len(notes) > LOG_ITEMS:
        log.append(("INFO", f"валютный пассив: ещё {len(notes) - LOG_ITEMS} заметок (в блоке funding прогона)"))
    return RunFxLiability(COMPUTED, block, funding_run._jsonable(per), pnl, log)


def _fx_funding(on: date, prev: MarketView | None, market: MarketView, rows: list[dict[str, Any]],
                outcomes: Mapping[str, Any], per: Mapping[str, dict[str, Any]]
                ) -> tuple[dict[str, tuple[float, float | None]], list[str]]:
    """position_id -> (fx_funding, fx_funding_cbr) of the foreign bonds with a flash PnL, and the notes: the liability
    held over the day (of the previous business day, ``PREV``) x the rate move; any other liability was not held over
    the day: 0."""
    with_pnl = [r["position_id"] for r in rows if r.get("pnl") and r["position_id"] in per]
    if not with_pnl or prev is None:
        return {}, []
    d0 = prev.business_date
    out, notes = {}, []
    for pid in with_pnl:
        b = per[pid]
        if b.get("state") != COMPUTED or b.get("reference") != PREV:
            out[pid] = (0.0, None)
            if b.get("state") == COMPUTED:
                notes.append(f"{pid}: пассива на {d0} нет ({b['reference_text']}): fx_funding — 0")
            continue
        ccy, amount = b["ccy"], b["amount_ccy"]
        fp = getattr((outcomes.get(pid) or object()), "pnl", None)
        rates = tuple(fp.fx_rates) if fp is not None and getattr(fp, "fx_rates", None) else None
        if rates is None:
            r0, r1 = market_fx(prev.fx(ccy)), market_fx(market.fx(ccy))
            rates = (r0[0], r1[0]) if r0 and r1 else None
        mkt, cbr = fx_funding(amount, rates, (prev.fx(ccy)["cbr"], market.fx(ccy)["cbr"]))
        if mkt is None:
            notes.append(f"{pid}: нет курса {ccy} на {d0} или {on}: fx_funding — 0")
        out[pid] = (0.0 if mkt is None or not math.isfinite(mkt) else mkt, cbr)
    return out, notes
