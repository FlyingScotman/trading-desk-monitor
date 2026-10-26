"""Forward carry of a stored run (TDM#66, TDM#72): what each position earns to a horizon, its funding as a separate
line (TDM#168), per position, book, entity, currency and instrument kind. «Кэрри» is the whole change of value as the
day moves to the horizon with the curves carried there: the coupons received plus the change of the dirty price (the
owner's answer of 26.10: how it splits into accrued and clean price is secondary). The screen shows one «Кэрри»
column, then «Фондирование», then «Нетто» (their sum); ``coupon``, ``pull``, ``rolldown`` and ``spreadRoll`` stay in
the API for attribution, ``carry`` their sum.

**Spread** (``spread``): ``held`` (default), each bond's spread held; ``curve``, it rolls along the spread curve of its
peers (its rating grade's median G-spread over tenor, ``spread_curve``) as it shortens: ``spreadRoll`` (CS01 x Δ, first
order) is added to its carry and shown apart.

**Carry** (``bond_carry``): core ``horizon_portfolio`` (``orchestrator.horizon``) on the run's own market, rebuilt as
the exact scenarios rebuild it (``scenario.exact._context``: the run's snapshot, its ``quote_curve_risk`` and
``ofz_scheme`` lineage), from the run's date to the horizon. Each bond's Z-spread is calibrated to the run's quote and
held, the curves are static (core's forwards realised for the carry, the shape held for the roll-down). The split
(``valuation.bonds.BondCarry``): ``coupon`` (coupons received + the change of the accrued coupon), ``pull`` (the rest
of core's ``carry_rub``: the clean price's drift on today's forwards at the held spread), ``rolldown`` (core's
``rolldown_rub``); ``carry = coupon + pull + rolldown`` (core's ``horizon_pnl_rub``). Covered: the run's positions core
valued (quality ``exact`` / ``assumed``, with a secid) whose start PV core reproduces within
``exact.BASE_TOLERANCE_RUB``. Every other position has no forecast (null, never 0) and a ``code``: its quality (a
fallback rung, a report value), ``not_a_bond`` (a derivative: core's swap and REPO carry is not wired here yet),
``base_mismatch``, or core's refusal (``core_<code>``). A bond whose last flow is paid by the horizon, which core does
not carry, is carried as cash by the backend (``carryMethod`` ``redeemed``: what it receives to the horizon less its
dirty value at the start; ``coupon`` the coupons received less the accrued bought, ``pull`` the rest, no roll-down).

**Funding** (``forward_funding``): the funding engine (``rules.funding.compute_funding``) over the future period (run
date, horizon] with the run's funding inputs as ``publish.funding_run`` reads them (the adapter's ``funding_rules`` in
force on each day of the period, ``entities`` and ``funding_policy`` as known at the run's ``positions_known_at``, the
books, the bond rows' MV and accrued, the HTM lots, the cash balances), every position and balance held to the horizon
(the previous snapshot is the run's own: nothing new, nothing closed), FX at the run's CBR rates (``HeldFx``). The rate
of a line and the policy are the run's own (``publish.funding_run``, TDM#237): the rules are the adapter's and the
app settings' (``funding_settings.FundingSettings.rules``) and the rule of the lowest tier wins (``RuleTier``: a
settings rule marked override, the adapter's, a settings rule, the settings' market default rate), within it the
most specific; the policy is ``funding_run.run_policy`` (the adapter's bases and entities, the settings' under them,
DEC01's clean MV for TRADING, the adapter's ``funding_policy`` over all). Each position says which rule priced it
(``fundingSource``: the line's ``rule_source``, ``fundingRuleId``; the default rate is named
``rules.funding.DEFAULT_RATE_LABEL``). The index values (``ks``):

- ``flat``: every index at its value in force on the run date for the whole period (``rate_index.HeldRates``);
- ``steps``: the key rate stepped at each CBR meeting after the run date (``md_cbr_meeting``, else the run snapshot's
  ``cbr_meetings``), ``steps`` bp per meeting in order, from its effective date (``rate_index.KeyRatePath``); RUONIA and
  the other RUB money-market indices follow the key rate with today's spread to it (an assumption, in ``notes``).

**No rule from any source and the default rate off: funding status ``not_configured``** (``funding_run.configured``),
every funding null (not 0) and no market rate put in its place. A problem of the engine (no rule for a line, an index without a value, no HTM lot) leaves that position's
funding null with its reason.

Not here (TDM#66 plan): the spread's roll to a sector curve (the spread is held), floaters repriced along the key-rate
path (their coupons are the run's curve forecast; the path moves the funding only), the carry of swaps and REPO.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from rub_fixed_income.dates import add_months

from .. import funding_settings, store
from ..marketdata.adapter import OFZ_CLASSES, bond_currency
from ..marketdata.bundle import SnapshotDir
from ..marketdata.ratings import GRADES
from ..orchestrator import EodContext, horizon
from ..publish import funding_run as FR
from ..rules import funding as F
from ..rules.rate_index import HeldFx, HeldRates, KeyRatePath
from ..rules.reference import funding_rules_from_reference
from . import spread_curve
from .exact import BASE_TOLERANCE_RUB, REPRICED, _context

METHOD = "desk-forward-carry-v1"
HORIZONS = (("1M", 1), ("3M", 3), ("6M", 6), ("1Y", 12))
"""The named horizons (window.DESK ``CARRY_HORIZONS``): months after the run date."""
KS_MODES = ("flat", "steps")
MEETINGS_MONTHS = 12
"""The CBR meetings listed (``meetings``): those taking effect within this many months of the run date, or to the
horizon when it is later; the steps apply to those within the horizon."""
SPREAD_MODES = ("held", "curve")
"""``held``: each bond's spread is held (core's horizon carry); ``curve``: it rolls along its peers' curve
(``spread_rolls``, the owner's answer of 26.10)."""
MAX_HORIZON_DAYS = 3 * 366
"""A horizon at most this far after the run date (the funding engine accrues day by day)."""
FUNDED, NOT_FUNDED, PROBLEM, NOT_VALUED = FR.FUNDED, FR.NOT_FUNDED, FR.PROBLEM, "not_valued"
COMPUTED, NOT_CONFIGURED, INVALID, FAILED = FR.COMPUTED, FR.NOT_CONFIGURED, FR.INVALID, FR.FAILED
CACHE_SIZE = 16
"""Forward carry bodies kept in process (``api.app``: by run, horizon date and key-rate path, at a store state)."""


class CarryError(ValueError):
    """A request the forward carry cannot answer: a bad horizon, mode or path."""


def horizon_date(run_date: date, h: str) -> date:
    """``1M`` / ``3M`` / ``6M`` / ``1Y`` (``HORIZONS``: months after ``run_date``, core's ``add_months``) or an ISO date
    after ``run_date`` and within ``MAX_HORIZON_DAYS``."""
    months = dict(HORIZONS).get(h.upper())
    if months is not None:
        return add_months(run_date, months)
    try:
        d = date.fromisoformat(h)
    except ValueError:
        raise CarryError(f"horizon {h!r}: one of {', '.join(k for k, _ in HORIZONS)} or an ISO date") from None
    if not run_date < d <= run_date + timedelta(days=MAX_HORIZON_DAYS):
        raise CarryError(f"horizon {d} must be after the run date {run_date} and within {MAX_HORIZON_DAYS} days")
    return d


def parse_steps(text: str | None) -> tuple[float, ...]:
    """``steps`` of the query: bp per future CBR meeting, comma-separated, in meeting order (empty: none)."""
    try:
        out = tuple(float(x) for x in (text or "").split(",") if x.strip())
    except ValueError:
        raise CarryError(f"steps {text!r}: comma-separated numbers of bp") from None
    if any(not math.isfinite(x) or abs(x) > 2000 for x in out):
        raise CarryError("steps: finite bp, at most 2000 each")
    return out


# ---------------------------------------------------------------------------------------------- CBR meetings
@dataclass(frozen=True)
class Meeting:
    date: date
    effective: date
    """From when the decision applies: the hub's ``effective_date``, else the next weekday after the meeting."""
    estimated: bool = False


def _effective(meeting: date, effective: date | None) -> date:
    if effective is not None:
        return effective
    d = meeting + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _d(v: Any) -> date | None:
    return v if isinstance(v, date) or v is None else date.fromisoformat(str(v))


def meetings(rows: Sequence[Mapping[str, Any]], after: date) -> list[Meeting]:
    """The CBR meetings that take effect after ``after``, in order: ``md_cbr_meeting`` rows or the snapshot's
    ``cbr_meetings`` (``meeting_date``, ``effective_date``, ``is_estimated``)."""
    out = []
    for r in rows:
        m = _d(r["meeting_date"])
        assert m is not None
        eff = _effective(m, _d(r.get("effective_date")))
        if eff > after:
            out.append(Meeting(m, eff, bool(r.get("is_estimated"))))
    return sorted(out, key=lambda x: (x.effective, x.date))


def meeting_rows(con: psycopg.Connection | None, ctx: EodContext | None) -> list[Mapping[str, Any]]:
    """``md_cbr_meeting`` (the newest market snapshot's meetings, ``static_facts.load_cbr``), else the run's own market
    snapshot's ``cbr_meetings`` (a store whose read model has none yet)."""
    rows: list[Mapping[str, Any]] = []
    if con is not None:
        rows = [dict(zip(("meeting_date", "effective_date", "is_estimated"), r) if not isinstance(r, dict) else r)
                for r in con.execute("SELECT meeting_date, effective_date, is_estimated FROM md_cbr_meeting"
                                     " ORDER BY meeting_date").fetchall()]
    if not rows and ctx is not None:
        rows = list(ctx.market.snapshot.data.get("cbr_meetings") or [])
    return rows


# ---------------------------------------------------------------------------------------------- carry
@dataclass
class PositionCarry:
    position_id: str
    book_id: str
    secid: str | None
    quality: str | None
    fact: dict[str, Any] | None
    carry: float | None = None
    coupon: float | None = None
    pull: float | None = None
    rolldown: float | None = None
    code: str | None = None
    detail: str = ""
    method: str | None = None
    """``core`` or ``redeemed`` (``valuation.bonds.BondCarry.method``); None without a forecast."""
    spread_roll: float | None = None
    """``spread=curve``: CS01 x ``spread_roll_bp``, in ``carry``; None when held (``spread_curve`` says why)."""
    spread_roll_bp: float | None = None
    spread_curve: str | None = None
    """The grade whose peer curve the spread rolled on, or why it is held (``spread_curve.HELD_REASONS``)."""


def bond_carry(ctx: EodContext, rows: Sequence[Mapping[str, Any]], hdate: date) -> list[PositionCarry]:
    """Core's carry of the run's ``rows`` (``run_position``: ``position_id``, ``book_id``, ``secid``, ``quantity``,
    ``quality``, ``pv_rub``, ``fact``) to ``hdate`` (see the module docstring): one per row, in order."""
    out = [PositionCarry(r["position_id"], r["book_id"], r.get("secid"), r.get("quality"), r.get("fact"))
           for r in rows]
    todo = []
    for p, r in zip(out, rows):
        if p.quality not in REPRICED:
            p.code = p.quality or NOT_VALUED
        elif p.secid is None or r.get("quantity") is None:
            p.code = "not_a_bond"
        else:
            todo.append((p, r))
    if not todo:
        return out
    outs, _ = horizon(ctx, [(p.secid, float(r["quantity"])) for p, r in todo], hdate,
                      position_ids=[p.position_id for p, _ in todo])
    for (p, r), o in zip(todo, outs):
        c = o.carry
        if c is None:
            p.code, p.detail = o.reason or "not_valued", o.detail
        elif r.get("pv_rub") is not None and abs(c.pv_start - float(r["pv_rub"])) > BASE_TOLERANCE_RUB:
            p.code = "base_mismatch"
            p.detail = f"core's start PV {c.pv_start:,.2f} vs the run's {float(r['pv_rub']):,.2f}"
        else:
            p.carry, p.coupon, p.pull, p.rolldown, p.method = c.total, c.coupon, c.pull, c.rolldown, c.method
    return out


def spread_rolls(ctx: EodContext, carries: Sequence[PositionCarry], hdate: date,
                 curves: Mapping[str, spread_curve.PeerCurve] | None = None,
                 grade_of: Callable[[Any], str | None] | None = None) -> dict[str, spread_curve.PeerCurve]:
    """``spread=curve`` (module ``spread_curve``): each covered fixed-coupon RUB corporate's spread rolls along its
    grade's peer curve to the horizon; ``spreadRoll = cs01 x Δ`` (first order) is added to its ``carry``. ``curves``
    (default: built from the run's market, ``spread_curve.build_curves``) and ``grade_of(terms)`` (default: the desk
    rating, ``reference_facts.ratings``) for tests. Returns the curves."""
    on = ctx.business_date
    if grade_of is None:
        book = ctx.reference_facts.ratings

        def grade_of(t: Any) -> str | None:
            r = book.bond(t.secid, t.issuer_id)
            return r["grade"] if r else None
    if curves is None:
        curves = spread_curve.build_curves(spread_curve.peers(ctx.static.bonds, ctx.market.quote, grade_of, on))
    years = (hdate - on).days / spread_curve.YEAR_DAYS
    for c in carries:
        terms = ctx.static.bonds.get(c.secid) if c.secid else None
        quote = ctx.market.quote(terms) if terms is not None else None
        cs01 = (c.fact or {}).get("cs01")
        why = ("not_covered" if c.carry is None or terms is None else "redeemed" if c.method == "redeemed"
               else "ofz_held" if terms.bond_class in OFZ_CLASSES else "floater_held"
               if terms.coupon_type == "floating" else "not_rub" if bond_currency(terms) != "RUB"
               else "no_quote" if quote is None else None)
        if why is None:
            curve, why = spread_curve.curve_for(curves, grade_of(terms))
            ten = spread_curve.tenor(terms, quote, on)
            if curve is not None and ten is not None and cs01 is not None:
                c.spread_roll_bp = spread_curve.roll(curve, ten, years)
                c.spread_roll = float(cs01) * c.spread_roll_bp
                c.carry = (c.carry or 0.0) + c.spread_roll
            elif curve is not None:
                why = "no_cs01" if cs01 is None else "no_quote"
        c.spread_curve = why
    return dict(curves)


# ---------------------------------------------------------------------------------------------- funding
def settings_digest(fs: funding_settings.FundingSettings) -> str:
    """A digest of the funding settings in force (``funding_settings.load``): part of the forward carry's cache key, so
    a change of the settings' rules or default rate is a new body."""
    doc = [fs.default_rate, fs.default_rate_by_ccy, list(fs.rule_rows), fs.policy, fs.error]
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class ForwardFunding:
    status: str
    text: str
    per_position: dict[str, dict[str, Any]]
    """position_id -> ``state`` (funded / not_funded / problem / not_configured / not_valued), ``amount`` (RUB; 0 for
    not_funded, None otherwise without a line), ``source``, ``reason``, ``note``."""
    book_level: list[dict[str, Any]] = field(default_factory=list)
    """Lines without a position of the run (cash, an entity's cash): book, entity, ccy, amount (RUB)."""
    problems: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    provisional: list[str] = field(default_factory=list)


def _rates(ks: str, table: Any, on: date, steps: Sequence[float], ms: Sequence[Meeting]) -> Any:
    if ks == "flat":
        return HeldRates(table, on)
    return KeyRatePath(table, on, [(m.effective, bp / 1e4) for m, bp in zip(ms, steps)])


def forward_funding(con: psycopg.Connection, run: Mapping[str, Any], ctx: EodContext,
                    rows: Sequence[Mapping[str, Any]], hdate: date, rates: Any,
                    settings: funding_settings.FundingSettings | None = None) -> ForwardFunding:
    """Funding of the run's positions over (run date, ``hdate``] (see the module docstring); ``rates``: the period's
    ``RateIndexProvider`` (``HeldRates`` or ``KeyRatePath``); ``settings``: the funding settings (default: the current
    ones, ``funding_settings.load``). Never raises: an error is status ``failed``."""
    try:
        fs = settings if settings is not None else funding_settings.load()
        return _forward_funding(con, run, ctx, rows, hdate, rates, fs)
    except Exception as e:  # noqa: BLE001 - the carry is shown without its funding, saying why
        text = f"{FR.STATUS_TEXT[FAILED]}: {type(e).__name__}: {e}"
        return ForwardFunding(FAILED, text, {r["position_id"]: {"state": PROBLEM, "amount": None, "source": "none",
                                                               "reason": text} for r in rows})


def _forward_funding(con: psycopg.Connection, run: Mapping[str, Any], ctx: EodContext,
                     rows: Sequence[Mapping[str, Any]], hdate: date, rates: Any,
                     fs: funding_settings.FundingSettings) -> ForwardFunding:
    on, known_at = run["business_date"], run.get("positions_known_at")
    period = F.AccrualPeriod(on, hdate)
    rule_rows = FR._rules_in_period(con, period, known_at)
    pids = [r["position_id"] for r in rows]
    rp = FR.run_policy(con, on, known_at, fs, rule_rows)
    if rp.status in (NOT_CONFIGURED, INVALID):
        state = NOT_CONFIGURED if rp.status == NOT_CONFIGURED else PROBLEM
        return ForwardFunding(rp.status, rp.text, {p: {"state": state, "amount": None, "source": "none",
                                                       "reason": rp.text} for p in pids})
    assert rp.policy is not None
    pol, entities = rp.policy, rp.entities
    rules = F.FundingRules(rates=(*funding_rules_from_reference(rule_rows).rates, *fs.rules().rates))
    books = run_books(con, run)
    facts = [dict(r["fact"]) for r in rows if r.get("fact")]
    problems: list[F.FundingProblem] = [F.FundingProblem(F.LineKind.POS, "*", c) for c in rp.conflicts]
    current, unknown = [], set()
    for f in facts:
        p, why = FR._position(f, books)
        if p is not None:
            current.append(p)
            if why:
                unknown.add(p.position_id)
    clean = {p.position_id for p in current if p.position_id in unknown
             and pol.base_by_book_type.get(p.book_type) is F.FundingBase.MV_CLEAN}
    problems += [F.FundingProblem(F.LineKind.POS, pid, "НКД неизвестен, база — чистая MV") for pid in sorted(clean)]
    cur = [p for p in current if p.position_id not in clean]
    inputs = store.positions_for(con, on, known_at=known_at)
    terms = {r["position_id"]: ctx.static.bonds.get(r["secid"]) for r in rows if r.get("secid")}
    lots = FR._lots(facts, inputs, {k: v for k, v in terms.items() if v is not None}, books, pol, problems)
    cash, _ = FR._cash(con, on, known_at, books, problems)
    res = F.compute_funding(period, rules, rates, HeldFx(FR.fx_table([ctx.market]), on), positions=cur,
                            previous_positions=cur, cash=cash or [], previous_cash=cash or [], htm_lots=lots,
                            entities=entities, policy=pol)
    current_ids = {p.position_id for p in current}
    ours = {(p.kind, p.ref) for p in problems}
    problems += [p for p in res.problems if not (p.kind is F.LineKind.HTM and (p.kind, p.ref) in ours)]
    by_pos: dict[str, list[F.FundingLine]] = defaultdict(list)
    book_level = []
    for x in res.lines:
        if x.position_id in current_ids:
            by_pos[x.position_id].append(x)
        else:
            book_level.append({"book": x.book_id, "entity": x.entity, "ccy": x.ccy, "kind": x.kind.value,
                               "amount": x.interest_reporting, "rateExpr": x.rate_expr, "ruleId": x.rule_id,
                               "ruleSource": x.rule_source, "defaultRate": x.default_rate})
    prob_by_ref: dict[str, list[str]] = defaultdict(list)
    for p in problems:
        prob_by_ref[p.ref].append(p.reason)
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        pid = r["position_id"]
        if by_pos.get(pid):
            lines = by_pos[pid]
            ccy = (r.get("fact") or {}).get("ccy")
            st = {"state": FUNDED, "amount": math.fsum(x.interest_reporting for x in lines),
                  "amountCcy": math.fsum(x.interest for x in lines) if ccy and all(x.ccy == ccy for x in lines)
                  else None,
                  "source": lines[0].rule_source, "ruleId": "; ".join(dict.fromkeys(x.rule_id or "" for x in lines))
                  or None, "rateExpr": "; ".join(dict.fromkeys(x.rate_expr for x in lines)),
                  "ratePct": lines[-1].days[-1].rate * 100}
            if any(x.default_rate for x in lines):
                st["note"] = F.DEFAULT_RATE_LABEL
        elif pid in prob_by_ref:
            st = {"state": PROBLEM, "amount": None, "source": "none", "reason": "; ".join(prob_by_ref[pid])}
        elif not r.get("fact"):
            st = {"state": NOT_VALUED, "amount": None, "source": "none", "reason": "позиция не оценена: нет MV"}
        else:
            m = books.get(r["book_id"]) or {}
            etype = (entities or {}).get(m.get("entity") or "") if entities else F.EntityType.MAIN
            bt = m.get("book_type") or ""
            b = pol.base_by_book_type.get(bt)
            why = ("юрлицо фондируется по всему остатку ДС" if etype is F.EntityType.OTHER
                   else f"тип книги {bt or '—'}: база {b.value if b else '—'}"
                   if (r.get("fact") or {}).get("instr_type") == "BOND"
                   else "не облигация: фондируется по правилам книги (остатки ДС), а не по MV")
            st = {"state": NOT_FUNDED, "amount": 0.0, "amountCcy": 0.0, "source": "none", "reason": why}
        per[pid] = st
    notes = [(f"фондирование {on} -> {hdate} ({len(period.days)} дн.): позиции и остатки ДС держатся до горизонта, "
              "новых и закрытых нет; FX по курсам ЦБ на дату прогона; ставки — как в прогоне: правила адаптера и "
              "настроек, рыночная ставка по умолчанию"),
             "РЕПО: условия сделок РЕПО движок пока не читает: не фондируются", *res.notes]
    if any(x.default_rate for x in res.lines):
        notes.append(f"часть строк по ставке из настроек ({F.DEFAULT_RATE_LABEL}), не по трансфертной")
    if fs.error:
        notes.append(f"настройки фондирования не прочитаны, взяты умолчания: {fs.error}")
    source = dict(rp.source)
    for bt in pol.base_by_book_type:  # as the run's block (funding_run._compute)
        source.setdefault(f"base_by_book_type[{bt}]", "funding_rules" if bt in {
            r.get("book_type") for r in rule_rows if r.get("basis")} else "default")
    return ForwardFunding(COMPUTED, FR.STATUS_TEXT[COMPUTED], per, book_level,
                          [{"kind": p.kind.value, "ref": p.ref, "reason": p.reason} for p in problems], notes,
                          FR._provisional(source, rule_rows, rp.entity_rows))


# ---------------------------------------------------------------------------------------------- the response
SUMS = {"carry": "carry", "coupon": "coupon", "pull": "pull", "rolldown": "rolldown", "spreadRoll": "spread_roll"}
"""Response key -> ``PositionCarry`` field summed over a group's covered positions."""
CCY_KEYS = ("carry", "coupon", "pull", "rolldown", "spreadRoll", "funding", "net")
"""A position's amounts also given in its currency (``inCcy``)."""


def _group() -> dict[str, Any]:
    return {**dict.fromkeys((*SUMS, "dv01", "cs01", "funding", "fundingNoCarry", "fundingBookLevel", "fundingTotal",
                             "net", "netKnown")),
            "nPositions": 0, "nCovered": 0, "nNotCovered": 0, "nFunded": 0, "nFundingMissing": 0,
            "nCoveredNoFunding": 0, "nNoCarryNoFunding": 0}


def _add(g: dict[str, Any], k: str, v: float | None) -> None:
    if v is not None:
        g[k] = (g[k] or 0.0) + v


def _close(g: dict[str, Any], funding_ok: bool) -> dict[str, Any]:
    """The group's funding total and net (``assemble``): ``fundingTotal = funding + fundingNoCarry +
    fundingBookLevel`` and ``net = carry + fundingTotal``, null (rule B1) while the funding is not computed, nothing is
    covered, a covered position's funding is unknown (``nCoveredNoFunding``) or a position without a carry forecast
    has an unknown funding (``nNoCarryNoFunding``: then ``fundingNoCarry`` and ``fundingTotal`` are null too, the
    owner's rule B1 applied strictly); ``netKnown`` still sums the positions with both."""
    if funding_ok:
        g["fundingBookLevel"] = g["fundingBookLevel"] or 0.0
        if g["nNoCarryNoFunding"]:
            g["fundingNoCarry"] = None
        else:
            g["fundingTotal"] = math.fsum(v or 0.0 for v in (g["funding"], g["fundingNoCarry"], g["fundingBookLevel"]))
    if funding_ok and g["carry"] is not None and g["funding"] is not None and not g["nCoveredNoFunding"] \
            and not g["nNoCarryNoFunding"]:
        g["net"] = g["carry"] + g["fundingTotal"]
    return g


def _fx_rate(fact: Mapping[str, Any]) -> float | None:
    """RUB per unit of the position's currency the run used (its ``mv_rub`` / ``mv`` pair, as the monitor's
    ``mvRub`` / ``mv``); 1 for RUB, None when the row says neither."""
    if (fact.get("ccy") or "RUB") == "RUB":
        return 1.0
    mv, rub = fact.get("mv"), fact.get("mv_rub")
    return float(rub) / float(mv) if mv and rub is not None else None


def assemble(run: Mapping[str, Any], hdate: date, ks: str, used: Sequence[tuple[Meeting, float, bool]],
             carries: Sequence[PositionCarry], funding: ForwardFunding, entity_of: Mapping[str, str | None],
             kind_of: Callable[[dict[str, Any] | None], str | None], key_rate: list[dict[str, Any]],
             notes: Sequence[str]) -> dict[str, Any]:
    """The response body: per position, the totals by book, entity, currency and instrument kind and overall.

    A group's ``carry`` (and its parts) sums its covered positions; its funding is split in three, so that
    ``net = carry + funding + fundingNoCarry + fundingBookLevel`` (``fundingTotal`` the last three, a null part 0):
    ``funding`` of its covered positions, ``fundingNoCarry`` of its positions without a carry forecast,
    ``fundingBookLevel`` of the lines without a position (cash balances; a book, an entity, a currency and the total
    have them). ``net`` is null (rule B1) while the funding is not computed or a covered position's funding is unknown
    (``nCoveredNoFunding``); ``netKnown`` sums ``carry + funding`` of the positions that have both. ``dv01`` and
    ``cs01`` sum every position of the group that has them, as the monitor's totals. ``used``: (meeting, step bp,
    within the horizon)."""
    ok = funding.status == COMPUTED
    positions = []
    groups: dict[str, dict[str, dict[str, Any]]] = {k: defaultdict(_group) for k in ("book", "entity", "ccy", "kind")}
    total = _group()
    for c in carries:
        f = funding.per_position.get(c.position_id) or {}
        fact = c.fact or {}
        amount = f.get("amount") if ok else None
        net = None if c.carry is None or amount is None else c.carry + amount
        ent, ccy, kind = entity_of.get(c.book_id), fact.get("ccy"), kind_of(c.fact) if c.fact else None
        dv01 = fact.get("dv01")
        cs01 = fact.get("cs01") or fact.get("ofz_spr01") if c.fact else None
        rate = _fx_rate(fact) if c.fact else None
        rub = {"carry": c.carry, "coupon": c.coupon, "pull": c.pull, "rolldown": c.rolldown,
               "spreadRoll": c.spread_roll, "funding": amount, "net": net}
        in_ccy = {k: None if v is None or not rate else v / rate for k, v in rub.items()}
        if ok and f.get("amountCcy") is not None:  # the funding lines' own interest in the currency
            in_ccy["funding"] = f["amountCcy"]
            in_ccy["net"] = None if in_ccy["carry"] is None else in_ccy["carry"] + f["amountCcy"]
        positions.append({"positionId": c.position_id, "bookId": c.book_id, "entity": ent, "secid": c.secid,
                          "ccy": ccy, "kind": kind, "quality": c.quality, "carry": c.carry, "coupon": c.coupon,
                          "pull": c.pull, "rolldown": c.rolldown, "funding": amount, "net": net,
                          "dv01": dv01, "cs01": cs01, "fxRate": rate, "inCcy": in_ccy,
                          "fundingState": f.get("state") or NOT_CONFIGURED, "fundingSource": f.get("source") or "none",
                          "fundingRate": f.get("rateExpr"), "fundingRuleId": f.get("ruleId"),
                          "fundingNote": f.get("note") or f.get("reason"),
                          "spreadRoll": c.spread_roll, "spreadRollBp": c.spread_roll_bp, "spreadCurve": c.spread_curve,
                          "carryMethod": c.method, "code": c.code, "detail": c.detail or None})
        for g in (total, groups["book"][c.book_id], groups["entity"][ent or "—"], groups["ccy"][ccy or "—"],
                  groups["kind"][kind or "—"]):
            g["nPositions"] += 1
            g["nCovered" if c.carry is not None else "nNotCovered"] += 1
            for k, attr in SUMS.items():
                _add(g, k, getattr(c, attr))
            _add(g, "dv01", dv01)
            _add(g, "cs01", cs01)
            if amount is not None:
                _add(g, "funding" if c.carry is not None else "fundingNoCarry", amount)
                g["nFunded"] += f.get("state") == FUNDED
                if c.carry is not None:
                    _add(g, "netKnown", c.carry + amount)
            else:
                g["nFundingMissing"] += 1
                g["nCoveredNoFunding"] += c.carry is not None
                g["nNoCarryNoFunding"] += c.carry is None and ok
    if ok:
        for x in funding.book_level:
            for g in (total, groups["book"][x["book"]], groups["entity"][x["entity"] or "—"],
                      groups["ccy"][x["ccy"] or "—"]):
                g["fundingBookLevel"] = (g["fundingBookLevel"] or 0.0) + x["amount"]
    return {
        "method": METHOD, "runId": run["run_id"], "asOf": run["business_date"].isoformat(),
        "horizonDate": hdate.isoformat(), "days": (hdate - run["business_date"]).days, "reportingCcy": "RUB",
        "ks": ks, "meetings": [{"date": m.date.isoformat(), "effective": m.effective.isoformat(),
                                "estimated": m.estimated, "stepBp": bp, "inHorizon": inside}
                               for m, bp, inside in used],
        "keyRatePath": key_rate,
        "fundingStatus": funding.status, "fundingText": funding.text,
        "positions": positions,
        "totals": _close(total, ok),
        "byBook": {k: _close(v, ok) for k, v in sorted(groups["book"].items())},
        "byEntity": {k: _close(v, ok) for k, v in sorted(groups["entity"].items())},
        "byCcy": {k: _close(v, ok) for k, v in sorted(groups["ccy"].items())},
        "byKind": {k: _close(v, ok) for k, v in sorted(groups["kind"].items())},
        "fundingBookLevel": funding.book_level if ok else [],
        "fundingProblems": funding.problems, "provisional": funding.provisional,
        "notes": [*notes, *funding.notes],
    }


def run_for_context(run: Mapping[str, Any]) -> dict[str, Any]:
    """The run's fields ``exact._context`` reads, from a ``run`` row (its lineage's ``quote_curve_risk`` and
    ``ofz_scheme``)."""
    lineage = run.get("lineage") or {}
    return {"run_id": run["run_id"], "business_date": run["business_date"], "cut_type": run["cut_type"],
            "market_snapshot_id": run["market_snapshot_id"], "quote_curve_risk": lineage.get("quote_curve_risk"),
            "ofz_scheme": lineage.get("ofz_scheme")}


def forward_carry(con: psycopg.Connection, snapshots: SnapshotDir, run: Mapping[str, Any], h: str, ks: str = "flat",
                  steps: Sequence[float] = (), *, kind_of: Callable[[dict[str, Any] | None], str | None] = lambda f: None,
                  ctx_cache: dict | None = None, settings: funding_settings.FundingSettings | None = None,
                  spread: str = "held") -> dict[str, Any]:
    """The forward carry of ``run`` (a ``run`` row) to the horizon ``h`` (``horizon_date``) with the key-rate path
    ``ks`` (``KS_MODES``; ``steps``: bp per future meeting for ``steps``) and the spread ``spread`` (``SPREAD_MODES``:
    held, or rolled along its peers' curve, ``spread_rolls``). ``kind_of``: a position's instrument kind
    from its fact (``api.concepts.instr_kind``). Raises ``CarryError`` on a bad request, ``SnapshotError`` when the
    run's snapshots are not in ``snapshots``. ``con`` may have any row factory: the store's helpers read tuples, so
    the call reads with ``tuple_row`` and gives the connection back as it was."""
    saved = con.row_factory
    con.row_factory = tuple_row
    try:
        return _forward_carry(con, snapshots, run, h, ks, steps, kind_of, ctx_cache, settings, spread)
    finally:
        con.row_factory = saved


def _forward_carry(con: psycopg.Connection, snapshots: SnapshotDir, run: Mapping[str, Any], h: str, ks: str,
                   steps: Sequence[float], kind_of: Callable[[dict[str, Any] | None], str | None],
                   ctx_cache: dict | None, settings: funding_settings.FundingSettings | None,
                   spread: str) -> dict[str, Any]:
    if ks not in KS_MODES:
        raise CarryError(f"ks {ks!r}: one of {', '.join(KS_MODES)}")
    if spread not in SPREAD_MODES:
        raise CarryError(f"spread {spread!r}: one of {', '.join(SPREAD_MODES)}")
    hdate = horizon_date(run["business_date"], h)
    ctx = _context(snapshots, run_for_context(run), ctx_cache)
    if ctx.business_date != run["business_date"]:  # not expected: prepare checks the snapshot's date
        raise CarryError(f"the run's market is of {ctx.business_date}, not {run['business_date']}")
    rows = con.execute("SELECT position_id, book_id, secid, quantity, quality, pv_rub, fact FROM run_position"
                       " WHERE run_id = %s ORDER BY ord", (run["run_id"],)).fetchall()
    rows = [dict(zip(("position_id", "book_id", "secid", "quantity", "quality", "pv_rub", "fact"), r)) for r in rows]
    carries = bond_carry(ctx, rows, hdate)
    curves = spread_rolls(ctx, carries, hdate) if spread == "curve" else {}
    on = run["business_date"]
    # the meetings of a year from the run date (or to the horizon when it is later), in order: step i is the i-th
    # meeting's; those after the horizon are listed (inHorizon false) and their steps do not apply
    year = max(hdate, add_months(on, MEETINGS_MONTHS))
    every = [m for m in meetings(meeting_rows(con, ctx), on) if m.effective <= year]
    listed = [(m, float(steps[i]) if ks == "steps" and i < len(steps) else 0.0, m.effective <= hdate)
              for i, m in enumerate(every)]
    ms = [m for m, _, inside in listed if inside]
    table = FR.fixing_table([ctx.market])
    rates = _rates(ks, table, on, [bp for _, bp, inside in listed if inside], ms)
    funding = forward_funding(con, run, ctx, rows, hdate, rates, settings)
    ks0 = table.rate("KEY_RATE", on)
    key_rate = [{"date": on.isoformat(), "keyRatePct": None if ks0 is None else round(ks0 * 100, 6)}]
    if isinstance(rates, KeyRatePath):
        key_rate += [{"date": d.isoformat(), "keyRatePct": round(k * 100, 6)} for d, k in rates.path]
    notes = [("кэрри: ядро horizon_portfolio на рынке прогона; спред каждой бумаги откалиброван к цене прогона и "
             "держится, кривые стоят (форварды кривой реализуются, roll-down — форма кривой сохраняется)"),
             ("купон = полученные купоны + изменение НКД; pull = остальное изменение цены по форвардам при том же "
             "спреде; " + ("кэрри = купон + pull + roll-down + spreadRoll (roll по кривой спредов)"
                           if spread == "curve" else "кэрри = купон + pull + roll-down")),
             "купоны флоатеров — прогноз по кривой прогона; путь КС влияет только на фондирование",
             ("без прогноза: оценки ниже точной, отчётные позиции, деривативы (кэрри свопов и РЕПО пока не "
             "подключено)")]
    if any(c.method == "redeemed" for c in carries):
        notes.append("бумаги, погашаемые до горизонта (carryMethod redeemed): кэрри = полученные до горизонта купоны "
                     "и погашения (без реинвестирования) минус грязная стоимость на дату прогона; купон = полученные "
                     "купоны минус НКД на дату прогона, pull — остальное, roll-down 0")
    if ks == "flat":
        notes.append("КС и индексы — на уровне даты прогона весь период")
    else:
        notes.append("КС по заседаниям ЦБ: шаг с даты вступления решения, не ниже 0; RUONIA и другие рублёвые "
                     "индексы денежного рынка идут за КС с сегодняшним спредом к ней (допущение), прочие стоят")
    if any(m.estimated for m in ms):
        notes.append("часть дат заседаний ЦБ ориентировочная")
    books = run_books(con, run)
    entity_of = {r["book_id"]: (books.get(r["book_id"]) or {}).get("entity") for r in rows}
    if spread == "curve":
        notes.append(f"спред по кривой эмитентов того же рейтинга (spread=curve): медианный G-спред MOEX рублёвых "
                     f"фикс-корпоратов на дату прогона по рейтингу и сроку (как C09), корзина от "
                     f"{spread_curve.MIN_BUCKET} выпусков, кривая — от двух корзин; бумага сдвигается по ней к сроку на "
                     "горизонте, spreadRoll = CS01 x Δ входит в кэрри: Δ по кривой G-спредов переносится на Z-спред "
                     "через CS01, первое приближение; нет кривой своего рейтинга — "
                     "соседний рейтинг, иначе спред держится (spreadCurve — почему); ОФЗ (спред к КБД) и флоатеры "
                     "(DM) — спред держится")
    else:
        notes.append("спред держится (spread=held): spreadRoll не считается")
    out = assemble(run, hdate, ks, listed, carries, funding, entity_of, kind_of, key_rate, notes)
    out["spread"] = spread
    out["spreadCurves"] = [c.json() for _, c in sorted(curves.items(), key=lambda x: GRADES.index(x[0]))]
    return out


def run_books(con: psycopg.Connection, run: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The run's books (``run_reference`` kind ``books``) over the books in force on its date."""
    books = {b["book_id"]: b for b in store.current_books(con, run["business_date"], run.get("positions_known_at"))}
    row = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'books'",
                      (run["run_id"],)).fetchone()
    data = row[0] if row else []
    books.update({b["book_id"]: b for b in data or []})
    return books
