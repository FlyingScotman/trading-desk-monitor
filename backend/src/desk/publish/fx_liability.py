"""The currency funding liability of the run's foreign-currency bonds and the bonds' FX delta net of it (OQ-04,
TDM#54, TDM#169).

**Rule** (owner's decisions: OQ-04 of 23.10.2026, a foreign-currency bond is funded in its own currency; DEC01 of
23.10.2026, the funding base is the clean price; the rule of 26.10.2026, ``docs/program/DECISIONS.md``):

- the bond's currency liability = its clean MV in the currency + the accrued paid at the purchase (funded at a zero
  rate: the liability covers it, no interest accrues on it). ``rules.funding.funding_liabilities`` on the funding policy
  the run's funding applies (the adapter's ``funding_rules``, ``entities``, ``funding_policy`` in force on ``D``);
- the bond's FX delta (OCP) = its dirty MV in the currency − the liability: the accrued earned since the purchase (and
  the clean price's move, see below). Coupons received are cash of the book, outside the position: PnL, they stay in
  the OCP. The funding interest is paid in RUB: it does not change the FX delta;
- **предварительно** (open question to the owner, TDM#169): the clean part of the liability is the *current* clean MV
  (as the funding base of DEC01), not the purchase price. With the purchase price a move of the clean price would stay
  in the OCP; with the current MV it does not.

**The accrued paid at the purchase** (``purchase_accrued``): the report's ``purchase_accrued_ccy`` (private/1.3, the
position's, signed as the position), zero once a coupon was paid after the ``purchase_date`` (the coupon returned it,
as an HTM lot's ``accrued_until``). Without it, from what the data has: with a ``purchase_date`` in the running coupon
period, the current accrued x (purchase date − period start) / (as-of − period start) (accrual is linear in a period;
an estimate for a position bought in several lots); without a purchase date, 0: the whole current accrued stays in the
OCP. Each such case is said in the position's ``basis`` and the block's notes, never silently.

**Without a liability** the bond keeps its dirty MV as FX delta and says why (``state``): ``not_configured`` (no
funding rule), ``problem`` (an unknown entity, a book type without a base, no FX rate for an out-of-currency bond),
``none`` (a book not funded on its MV, an entity funded on its whole cash balance, a bond funded in RUB). A bond whose
accrued is unknown on a clean base gets the liability on its dirty MV (FX delta 0), said.

**FX revaluation of the liability** (``fx_funding`` of the flash PnL, TDM#54): the liability of D-1 (the previous
default run's rows, on the same rule) x the rate move, at the rates of the position's ``fx_gross`` (the market rates:
``FlashPnl.fx_rates``, else the markets'), and x the CBR move in ``fx_funding_cbr``. ``fx_gross + fx_funding`` is then the
FX of the bond net of its funding. A position new on D has no PnL, a bond without a D-1 liability gets 0 and the reason.

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

from .. import store
from ..marketdata.adapter import MarketView
from ..rules import funding as F
from ..rules.reference import apply_funding_policy, entities_from_reference, policy_from_reference
from ..valuation.bonds import current_period
from ..valuation.fallback import market_fx
from . import funding_run

KEY = "fx_liability"
"""The key of the block inside the run's funding block."""
COMPUTED, NOT_CONFIGURED, INVALID, FAILED = "computed", "not_configured", "invalid_config", "failed"
NONE, PROBLEM = "none", "problem"
STATUS_TEXT = {
    COMPUTED: "валютный пассив фондирования облигаций посчитан, FX-дельта облигаций — за его вычетом",
    NOT_CONFIGURED: "валютный пассив не посчитан: фондирование не настроено (нет funding_rules); FX-дельта облигаций — "
                    "грязная MV",
    INVALID: "валютный пассив не посчитан: правила фондирования противоречат друг другу; FX-дельта облигаций — "
             "грязная MV",
    FAILED: "валютный пассив не посчитан: ошибка расчёта; FX-дельта облигаций — грязная MV",
}
RULE = ("ОВП облигации = грязная MV в валюте − валютный пассив; пассив = чистая MV + НКД при покупке (OQ-04, DEC01, "
        "правило владельца 26.10.2026); проценты фондирования — в рублях, ОВП не меняют")
PROVISIONAL = ["пассив по текущей чистой MV, а не по цене покупки (вопрос владельцу, TDM#169)"]
"""What the rule takes until the owner answers (shown in the block and the log as «предварительно»)."""
REPORT, ESTIMATED, RETURNED, UNKNOWN, DIRTY = ("report", "estimated_from_purchase_date", "returned_by_coupon",
                                               "unknown", "dirty_base")
BASIS_TEXT = {
    REPORT: "НКД при покупке — из отчёта (purchase_accrued_ccy)",
    ESTIMATED: "НКД при покупке оценён по дате покупки: текущий НКД × (покупка − начало купона) / (дата − начало "
               "купона) (предварительно)",
    RETURNED: "НКД при покупке вернулся с купоном после покупки: в пассиве 0",
    UNKNOWN: "нет НКД при покупке и даты покупки (purchase_accrued_ccy, purchase_date): в пассиве 0, весь текущий НКД "
             "в ОВП (предварительно)",
    DIRTY: "база фондирования — грязная MV: пассив покрывает весь НКД",
}
LOG_ITEMS = 50


def _day(x: Any) -> date | None:
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10]) if x else None
    except ValueError:
        return None


def purchase_accrued(pos: F.FundingPosition, x: Mapping[str, Any], terms: Any, on: date
                     ) -> tuple[float, str, str]:
    """(the accrued paid at the purchase still in the liability, in the position currency signed as the position; its
    basis; a detail) of a bond position on ``on`` (see the module docstring). ``x``: the report row's fields
    (``PositionInputs.extra``); ``terms``: the bond's ``BondTerms`` or None."""
    pdate = _day(x.get("purchase_date"))
    paid = sorted(c.pay_date for c in (terms.coupons if terms is not None else ()) if pdate and pdate < c.pay_date <= on)
    if pdate is not None and paid:
        return 0.0, RETURNED, f"купон {paid[0].isoformat()}"
    sign = -1.0 if pos.mv_clean < 0 else 1.0
    rep = funding_run._f(x.get("purchase_accrued_ccy"))
    if rep is not None:
        return sign * abs(rep), REPORT, ""
    if pdate is None:
        return 0.0, UNKNOWN, ""
    _, start = current_period(terms, on) if terms is not None else (None, None)
    if start is None or not pos.accrued or not start <= pdate <= on or on <= start:
        return 0.0, UNKNOWN, f"дата покупки {pdate.isoformat()} без купонного периода, НКД неизвестен"
    share = (pdate - start).days / (on - start).days
    return pos.accrued * share, ESTIMATED, f"доля {share:.4f} от текущего НКД"


@dataclass
class _Policy:
    policy: F.FundingPolicy | None
    entities: dict[str, F.EntityType] | None
    status: str
    text: str


def resolve_policy(con: psycopg.Connection, on: date, known_at: Any) -> _Policy:
    """The funding policy of the run's funding on ``on`` (as ``funding_run``), or why there is none."""
    rules = store.current_reference(con, "funding_rules", on, known_at)
    if not any(r.get("active", True) is not False for r in rules):
        return _Policy(None, None, NOT_CONFIGURED, STATUS_TEXT[NOT_CONFIGURED])
    entity_rows = store.current_reference(con, "entities", on, known_at)
    try:
        base = policy_from_reference(rules, entity_rows)
    except ValueError as e:
        return _Policy(None, None, INVALID, f"{STATUS_TEXT[INVALID]} ({e})")
    pol = apply_funding_policy(store.current_reference(con, "funding_policy", on, known_at), base).policy
    types, _ = entities_from_reference(entity_rows)
    return _Policy(pol, types or None, COMPUTED, STATUS_TEXT[COMPUTED])


def liabilities(rows: list[dict[str, Any]], books: Mapping[str, Mapping[str, Any]], extra: Mapping[str, Mapping],
                terms: Mapping[str, Any], on: date, pol: F.FundingPolicy, entities: Mapping[str, F.EntityType] | None,
                fx: F.FxProvider | None = None) -> dict[str, dict[str, Any]]:
    """position_id -> ``extra.fx_liability`` of every foreign-currency bond row (``state`` and, when computed, the
    liability ``amount_ccy`` (negative for a long), ``clean_mv_ccy``, ``purchase_accrued_ccy``, ``dirty_mv_ccy``, the
    FX delta before (``fx_delta_gross``) and after (``fx_delta_net``))."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("instr_type") != "BOND" or (r.get("ccy") or pol.reporting_ccy) == pol.reporting_ccy:
            continue
        pid = r["position_id"]
        p, why = funding_run._position(r, dict(books))
        if p is None:
            out[pid] = {"state": PROBLEM, "reason": why or "нет позиции фондирования"}
            continue
        base = pol.base_by_book_type.get(p.book_type)
        note = ""
        if why:  # the accrued is unknown: _position put the dirty MV as clean, the liability then covers it all
            acc, basis, detail = 0.0, UNKNOWN, ""
            note = "НКД неизвестен: пассив — от грязной MV, ОВП бумаги ≈ 0 (предварительно)"
        elif base is F.FundingBase.MV_DIRTY:
            acc, basis, detail = 0.0, DIRTY, ""
        else:
            acc, basis, detail = purchase_accrued(p, extra.get(pid) or {}, terms.get(pid), on)
        liab, probs = F.funding_liabilities([dataclasses.replace(p, purchase_accrued=acc)], policy=pol,
                                            entities=entities, fx=fx, on=on)
        if probs:
            out[pid] = {"state": PROBLEM, "reason": "; ".join(x.reason for x in probs)}
            continue
        if not liab:
            etype = (entities or {}).get(p.entity, F.EntityType.MAIN) if entities else F.EntityType.MAIN
            out[pid] = {"state": NONE, "reason": "юрлицо фондируется по всему остатку ДС" if etype is F.EntityType.OTHER
                        else f"тип книги {p.book_type or '—'}: база {base.value if base else '—'}, не по MV"}
            continue
        x = liab[0]
        gross = (r.get("fx_delta") or {}).get(x.ccy, 0.0) if x.ccy != pol.reporting_ccy else None
        if x.ccy == pol.reporting_ccy:
            out[pid] = {"state": NONE, "reason": "фондируется в рублях (out_of_currency = settlement_ccy): FX открыт"}
            continue
        out[pid] = {
            "state": COMPUTED, "ccy": x.ccy, "amount_ccy": x.amount, "clean_mv_ccy": p.mv_clean,
            "purchase_accrued_ccy": acc if basis != DIRTY else None, "accrued_ccy": p.accrued,
            "dirty_mv_ccy": p.mv_dirty, "base": base.value if base else None, "purchase_accrued_basis": basis,
            "basis_text": BASIS_TEXT[basis] + (f" ({detail})" if detail else ""),
            "fx_delta_gross": gross, "fx_delta_net": (gross or 0.0) + x.amount,
            **({"note": note} if note else {}),
        }
    return out


def fx_funding(amount_ccy: float, rates: tuple[float, float] | None, cbr: tuple[float | None, float | None]
               ) -> tuple[float | None, float | None]:
    """(``fx_funding``, ``fx_funding_cbr``): the D-1 liability x the market and the CBR rate move; None without a rate."""
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
    ``terms``: position_id -> ``BondTerms``; ``outcomes``: position_id -> ``Outcome`` (its flash PnL's FX rates)."""
    try:
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
    per = liabilities(foreign, books, inputs.extra, terms, on, pol, rp.entities, funding_run.fx_table([market]))
    notes: list[str] = []
    warn: set[str] = set()
    by_ccy: dict[str, float] = defaultdict(float)
    for pid, x in sorted(per.items()):
        if x["state"] == COMPUTED:
            by_ccy[x["ccy"]] += x["amount_ccy"]
            if x["purchase_accrued_basis"] in (ESTIMATED, UNKNOWN) or x.get("note"):
                notes.append(f"{pid}: {x.get('note') or x['basis_text']}")
        else:
            notes.append(m := f"{pid}: валютного пассива нет ({x['reason']}): FX-дельта — грязная MV")
            warn.add(m)
    pnl, pnl_notes = _fx_funding(con, on, prev, market, rows, run_books, inputs, terms, outcomes, pol, rp.entities,
                                 per)
    notes += pnl_notes
    for pid, x in per.items():
        if pid in pnl:
            x["fx_funding"], x["fx_funding_cbr"] = pnl[pid]
    block = funding_run._jsonable(_empty(COMPUTED, STATUS_TEXT[COMPUTED], by_ccy=dict(by_ccy), positions=per,
                                         notes=notes))
    n = sum(1 for x in per.values() if x["state"] == COMPUTED)
    log = [("INFO", f"валютный пассив фондирования: {n} из {len(per)} валютных облигаций, "
                    + (", ".join(f"{c} {a:,.0f}" for c, a in sorted(by_ccy.items())) or "нет")
                    + "; FX-дельта облигаций — за его вычетом; предварительно: " + "; ".join(PROVISIONAL))]
    log += [("WARN" if m in warn else "INFO", f"валютный пассив: {m}") for m in notes[:LOG_ITEMS]]
    if len(notes) > LOG_ITEMS:
        log.append(("INFO", f"валютный пассив: ещё {len(notes) - LOG_ITEMS} заметок (в блоке funding прогона)"))
    return RunFxLiability(COMPUTED, block, funding_run._jsonable(per), pnl, log)


def _fx_funding(con: psycopg.Connection, on: date, prev: MarketView | None, market: MarketView,
                rows: list[dict[str, Any]], run_books: list[dict[str, Any]], inputs: store.PositionInputs,
                terms: Mapping[str, Any], outcomes: Mapping[str, Any], pol: F.FundingPolicy,
                entities: Mapping[str, F.EntityType] | None, per: Mapping[str, dict[str, Any]]
                ) -> tuple[dict[str, tuple[float, float | None]], list[str]]:
    """position_id -> (fx_funding, fx_funding_cbr) of the foreign bonds with a flash PnL, and the notes."""
    with_pnl = [r["position_id"] for r in rows if r.get("pnl") and r["position_id"] in per]
    if not with_pnl or prev is None:
        return {}, []
    d0 = prev.business_date
    rid, facts, prev_books = funding_run._previous_rows(con, d0)
    if rid is None:
        return dict.fromkeys(with_pnl, (0.0, None)), [f"нет прогона за {d0}: FX-переоценка пассива (fx_funding) — 0"]
    books = {**_books(con, on, inputs.known_at, run_books), **{k: v for k, v in prev_books.items() if v.get("book_type")}}
    before = liabilities([f for f in facts if f["position_id"] in set(with_pnl)], books, inputs.extra, terms, d0, pol,
                         entities, funding_run.fx_table([prev]))
    out, notes = {}, []
    for pid in with_pnl:
        b = before.get(pid)
        if b is None or b.get("state") != COMPUTED:
            out[pid] = (0.0, None)
            if (per.get(pid) or {}).get("state") == COMPUTED:
                notes.append(f"{pid}: пассива на {d0} нет ({(b or {}).get('reason') or 'позиции не было'}): "
                             "fx_funding — 0")
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
