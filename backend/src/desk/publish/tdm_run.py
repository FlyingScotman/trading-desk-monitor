"""Write an EOD run into the ``tdm`` store through its writer SDK (``tdm.writer.Run``).

The store and the Desk Monitor prototype then show it through ``tdm serve`` and
``window.DESK``. This was the slice shortcut until the desk store of S5 (``desk_run``), which publishes
the same ``run_content``; ``desk eod --tdm`` keeps writing here during the transition.
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
and a WARN line with the reason; a simple PnL (all in ``other``) is counted in its own WARN line.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from rub_fixed_income import RISK_BUCKETS, RISK_PILLARS, kbd_effective_yield, portfolio_result_to_json
from rub_fixed_income import __version__ as CORE_VERSION
from tdm.writer import Run

from .. import __version__ as BACKEND_VERSION
from ..marketdata.adapter import MarketView
from ..orchestrator import EodContext, Outcome
from ..pnl.flash import METHOD
from ..settings import REPO_ROOT
from ..store import PositionInputs
from ..valuation.fallback import METHOD as SIMPLE_METHOD
from ..valuation.fallback import QUALITIES, SimplePnl, fx_rate
from ..valuation.positions import normalised

CURVE_ID = "RUB OFZ"
ASSET_CLASS = {"ofz_pd": "ОФЗ-ПД", "ofz_ad": "ОФЗ-АД", "ofz_pk": "ОФЗ-ПК", "ofz_in": "ОФЗ-ИН",
               "subfederal": "Субфедеральные", "municipal": "Муниципальные", "corporate": "Корпоративные"}
SEGMENT = {"ofz_pd": "Гос", "ofz_ad": "Гос", "ofz_pk": "Гос", "ofz_in": "Гос",
           "subfederal": "Суб", "municipal": "Суб"}
"""Segment of a bond (default ``Корп``). The Desk Monitor builds its segment chips from the data, so a segment
outside its known list (``Суб``) gets a chip of its own and is never filtered out silently."""
DIRECT_KIND = {"fx": ("FX", "Валюта", "FX"), "otc": ("OTC", "OTC-деривативы", "Деривативы"),
               "other": ("OTHER", "Прочее", "Прочее"), "bond": ("BOND", "Не сопоставлено", "Корп")}
"""asset_class of a direct row -> (instr_type, asset class, segment); an unknown one is treated as ``other``.
``OTC`` and ``OTHER`` are desk-store types: the legacy tdm store has no such instrument type (``publish``)."""
TDM_INSTR_TYPES = ("BOND", "IRS", "XCCY", "CDS", "FX")
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
UNMAPPED_ISSUER = {"issuer": "Не сопоставлено", "name": None, "issuer_group": "Не сопоставлено", "is_gov": False,
                   "is_quasi": False}
"""The issuer of the unmapped bonds (an instrument the static snapshot does not know)."""
FX_CCYS = ("USD", "EUR", "CNY")
INDICES = (("KEY_RATE", "КС"), ("RUONIA", "RUONIA"))


def _issuer_key(ctx: EodContext, o: Outcome) -> tuple[str, dict[str, Any]]:
    assert o.terms is not None
    iss = ctx.static.issuer_of(o.terms)
    is_gov = o.terms.bond_class.startswith("ofz") or bool(iss and iss.is_sovereign)
    if iss is None:
        key = f"issuer:{o.terms.issuer_id or o.secid}"
        return key, {"issuer": key, "name": None, "issuer_group": key, "is_gov": is_gov, "is_quasi": False}
    key = iss.short_name or iss.name
    return key, {"issuer": key, "name": iss.name, "issuer_group": iss.name, "sector": iss.sector,
                 "rating": None, "is_gov": is_gov, "is_quasi": False}


def curve_points(ctx: EodContext, prev: MarketView | None) -> list[dict[str, Any]]:
    prev_kbd = prev.zcyc_parameters() if prev else None
    pts = []
    for b, t in zip(RISK_BUCKETS, RISK_PILLARS):
        r = kbd_effective_yield(ctx.kbd, t) * 100
        move = (r - kbd_effective_yield(prev_kbd, t) * 100) * 100 if prev_kbd else 0.0
        pts.append({"bucket": b, "rate": round(r, 4), "move_bp": round(move, 2)})
    return pts


def store_core_results(db: str, outcomes: list[Outcome]) -> list[dict[str, str]]:
    """Write each core result of the run, hash-verified, to ``<dir of db>/results/<result_sha256>.json``."""
    out_dir = Path(db).resolve().parent / "results"
    stored = []
    for r in {o.core_result.result_sha256: o.core_result for o in outcomes if o.core_result}.values():
        path = out_dir / f"{r.result_sha256}.json"
        if not path.exists():  # content-addressed: an existing file holds the same bytes
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(portfolio_result_to_json(r))  # raises unless schema and hash verify
            tmp.replace(path)
        stored.append({"result_sha256": r.result_sha256, "schema": r.schema_version, "path": str(path)})
    return stored


def _fx_rows(ctx: EodContext, prev: MarketView | None) -> list[dict[str, Any]]:
    rows = []
    for ccy in FX_CCYS:
        now = ctx.market.fx(ccy)
        rate = now["moex_tom"] or now["cbr"]
        if rate is None:
            continue
        old = prev.fx(ccy) if prev else {}
        old_rate = (old.get("moex_tom") or old.get("cbr")) if old else None
        rows.append({
            "ccy": ccy, "rate": rate,
            "move_pct": (rate / old_rate - 1) * 100 if old_rate else 0.0,
            "rate_cbr": now["cbr"],
            "move_pct_cbr": (now["cbr"] / old["cbr"] - 1) * 100 if now["cbr"] and old.get("cbr") else None,
            "rate_cbr_next": now["cbr_next"],
        })
    return rows


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
        return BOOKS[b] if b in BOOKS else {"book_id": b, "name": b, **DEFAULT_BOOK}
    return [row(b) for b in BOOKS if b in used] + [row(b) for b in used if b not in BOOKS]


def direct_row(ctx: EodContext, book: str, pid: str, o: Outcome, x: dict[str, Any], qty: float | None
               ) -> dict[str, Any]:
    """An FX row, an unmapped bond or an OTC/other row (``valuation.positions``): PV, FX delta in currency, and
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
    if amount is None:
        amount = v.pv_rub / rate
        why.append("notional unknown: the market value is shown")
    vd = x.get("value_date")
    mat = x.get("maturity_date") or vd
    return {
        "position_id": pid, "book_id": book, "instr_type": itype, "asset_class": aclass, "segment": segment,
        "name": x.get("source_instrument_name") or (f"{ccy} {vd or 'spot'}" if fx else x.get("source_instrument_id"))
        or pid, "isin": x.get("isin"), "issuer": None, "ccy": ccy, "maturity": mat.isoformat() if mat else None,
        "tenor_y": (mat - ctx.business_date).days / 365.25 if mat and not fx else None,
        "side": ("BUY" if amount >= 0 else "SELL") if fx else "long" if amount >= 0 else "short",
        "notional": amount, "notional_rub": amount * rate, "price": _num(v.clean_pct), "mv": v.pv_rub / rate,
        "mv_rub": v.pv_rub, "dv01": v.dv01_rub, "cs01": v.spread01_rub, "ofz_spr01": 0.0,
        "risk": {CURVE_ID: v.buckets} if any(v.buckets.values()) else {}, "gamma": [],
        "instrument_id": x.get("moex_secid") or x.get("source_instrument_id"), "new_today": False,
        **({"fx_delta": {ccy: v.pv_rub / rate}} if ccy != "RUB" else {}),
        "extra": {
            "valuation": "reported" if v.quality == "reported_value" else "approximated", "quality": v.quality,
            "assumptions": why, "quantity_bonds": qty, **({"not_exact_reason": o.reason} if o.reason else {}),
            **({"fx_rate": rate} if ccy != "RUB" else {}), **reported(x),
        },
    }


def position_row(ctx: EodContext, book: str, pid: str, o: Outcome, issuer: str,
                 x: dict[str, Any] | None = None) -> dict[str, Any]:
    t, q, v = o.terms, o.quote, o.valuation
    assert t is not None and v is not None
    as_of = ctx.business_date
    spread_dur = -v.spread01_rub / v.pv_rub * 1e4 if v.pv_rub and v.spread01_rub else None
    sa = q.source_analytics if q else {}
    ccy = t.face_unit or "RUB"
    fx = fx_rate(ctx, ccy) if ccy != "RUB" else None  # foreign face: amounts in RUB, notional and FX delta in ccy
    return {
        "position_id": pid, "book_id": book, "instr_type": "BOND",
        "asset_class": ASSET_CLASS.get(t.bond_class, t.bond_class), "segment": SEGMENT.get(t.bond_class, "Корп"),
        "name": t.short_name or t.name, "isin": t.isin, "issuer": issuer, "ccy": ccy if fx else "RUB",
        "maturity": t.maturity_date.isoformat() if t.maturity_date else None,
        "tenor_y": (t.maturity_date - as_of).days / 365.25 if t.maturity_date else None,
        "coupon_type": "float" if t.coupon_type == "floating" else "fix", "coupon": v.current_coupon_pct,
        "side": "long" if v.quantity >= 0 else "short",
        "notional": v.quantity * v.face_rub / (fx[0] if fx else 1.0), "notional_rub": v.quantity * v.face_rub,
        "price": v.clean_pct, "mv": v.pv_rub / (fx[0] if fx else 1.0), "mv_rub": v.pv_rub,
        "ytm": _num(v.ytm_pct), "spread_bp": _num(v.z_spread_bp), "mdur": _num(v.modified_duration), "spr_dur": spread_dur,
        "convexity": v.curve_convexity, "dv01": v.dv01_rub,
        "cs01": v.spread01_rub if v.spread_kind == "credit" else 0.0,
        "ofz_spr01": v.spread01_rub if v.spread_kind == "ofz" else 0.0,
        "instrument_id": t.secid, "accrued": v.accrued_rub, "coupon_freq": t.coupon_frequency,
        "yield_date": v.horizon_date.isoformat() if v.horizon_kind in ("offer", "call") else None,
        "spread_type": "Z", "spread_curve": CURVE_ID, "new_today": False,
        "risk": {CURVE_ID: v.buckets},
        **({"fx_delta": {ccy: v.pv_rub / fx[0]}} if fx else {}),
        **({"pnl": o.pnl.as_tdm()} if o.pnl else {}),
        "gamma": _gamma(v.hessian),
        "extra": {
            "price_basis": v.price_basis, "horizon_kind": v.horizon_kind, "valuation_date": v.valuation_date.isoformat(),
            "settlement": v.settlement.isoformat(),
            "quantity_bonds": v.quantity, "calculation_id": v.calculation_id, "core_version": v.core_version,
            "moex_yield_close_pct": sa.get("yield_close_pct"), "moex_duration_days": sa.get("duration_days"),
            "valuation": "computed" if v.quality == "exact" else "approximated",
            "quality": v.quality, "assumptions": list(v.assumptions),
            **({"not_exact_reason": o.reason} if o.reason else {}),
            **({"fx_rate": fx[0], "fx_source": fx[1]} if fx else {}),
            **({"pnl": o.pnl.extra()} if o.pnl else {}),
            **reported(x),
        },
    }


def _factor(name: str) -> str:
    return f"IR|{CURVE_ID}|{name.rsplit('|', 1)[1]}" if name.startswith("IR|") else name


def _gamma(hessian: list[tuple[str, str, float]]) -> list[list[Any]]:
    """The upper-triangle Hessian on the one published curve: a floater's projection-curve factors
    move with the discount curve's of the same bucket (as its bucket DV01 does), so they are summed."""
    out: dict[tuple[str, str], float] = defaultdict(float)
    for a, b, g in hessian:
        fa, fb = _factor(a), _factor(b)
        out[min(fa, fb), max(fa, fb)] += 2 * g if fa == fb and a != b else g  # cross-curve term on the diagonal
    return [[a, b, g] for (a, b), g in out.items()]


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
    """What a run publishes, in one format for both stores (tdm SQLite and the desk store)."""

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
        if o.terms is None:  # valued without core (valuation.positions): no flash PnL in S7a
            if prev is not None:
                no_pnl.append(f"{pid} {label}: нет PnL (оценка без инструмента, S7a)")
            rows.append(row := direct_row(ctx, book, pid, o, x or {}, quantities.get(pid)))
            if row["instr_type"] == "BOND":  # tdm needs an issuer for a bond
                row["issuer"] = UNMAPPED_ISSUER["issuer"]
                issuers[row["issuer"]] = UNMAPPED_ISSUER
            continue
        if o.pnl is None and prev is not None:
            e = o.no_pnl
            no_pnl.append(f"{pid} {o.secid}: нет PnL ({e.reason}{': ' + e.detail if e.detail else ''})" if e
                          else f"{pid} {o.secid}: нет PnL (флэш-PnL не считался)")
        if isinstance(o.pnl, SimplePnl) and o.pnl.flag:
            no_pnl.append(f"{pid} {o.secid}: PnL {o.pnl.total:,.0f} ₽ с модельной ценой ({o.pnl.flag})")
        key, rec = _issuer_key(ctx, o)
        issuers[key] = rec
        rows.append(position_row(ctx, book, pid, o, key, x))
    used = list(dict.fromkeys(r["book_id"] for r in rows))
    indices = []
    for code, name in INDICES:
        fx = ctx.market.latest_fixing(code)
        if fx:
            indices.append({"index_name": name, "value": fx[1]})
    reference = {
        "books": book_rows(used, inputs.books if inputs else {}),
        "issuers": list(issuers.values()),
        "curves": [{"curve_id": CURVE_ID, "name": "ОФЗ (КБД MOEX)", "ccy": "RUB", "kind": "bond",
                    "points": curve_points(ctx, prev)}],
        "fx": _fx_rows(ctx, prev),
        "indices": indices,
    }
    log = [("INFO", "lineage " + json.dumps(lineage, ensure_ascii=False, default=str))]
    if ctx.point_in_time_warning:
        log.append(("WARN", "статика не на дату рынка: " + ctx.point_in_time_warning))
    log.append(("INFO", "качество оценки: " + ", ".join(f"{k} {quality[k]}" for k in QUALITIES if quality[k])
                + (f", не посчитано {len(skipped)}" if skipped else "")))
    log += [("WARN", s) for s in skipped + approx + no_pnl]
    with_pnl = [o.pnl for _, _, o in positions if o.pnl]
    simple = [x for x in with_pnl if isinstance(x, SimplePnl)]
    if simple:
        log.append(("WARN", (f"флэш-PnL без разложения ({SIMPLE_METHOD}: q x Δdirty + выплаты, всё в other) "
                             f"у {len(simple)} поз.: {sum(x.total for x in simple):,.0f} ₽")))
    if with_pnl:
        tot = {k: sum(getattr(x, k) for x in with_pnl) for k in ("carry", "rolldown", "rates", "spread", "other")}
        worst = max(abs(x.identity_error) for x in with_pnl)
        log.append(("INFO", f"флэш-PnL {with_pnl[0].prev_date} -> {with_pnl[0].as_of} ({METHOD}), "
                    f"позиций {len(with_pnl)} (без разложения {len(simple)}): "
                    + ", ".join(f"{k} {v:,.0f}" for k, v in tot.items())
                    + f"; итог {sum(x.total for x in with_pnl):,.0f} ₽, макс. ошибка тождества {worst:.2e} ₽"))
    log.append(("WARN", "JTD не считается в слайсе S1 (нет решения по recovery): поле jtd = 0"))
    log.append(("INFO", f"опубликовано позиций: {len(rows)} (точно {quality['exact']}), не посчитано: {len(skipped)}"))
    return RunContent(rows, reference, log)


def publish(db: str, ctx: EodContext, positions: list[tuple[str, str, Outcome]], *,
            prev: MarketView | None, comment: str | None = None,
            inputs: PositionInputs | None = None) -> tuple[int, str, list[str]]:
    """positions: (position_id, book_id, outcome) of ``inputs``. Returns (run_id, status, warnings)."""
    lineage = {**ctx.lineage(), "backend_version": BACKEND_VERSION, "backend_git_sha": git_sha(),
               "core_version": CORE_VERSION,
               "prev_market_snapshot_id": prev.snapshot.id if prev else None,
               "core_results": store_core_results(db, [o for _, _, o in positions])}
    c = run_content(ctx, positions, prev, lineage, inputs)
    rows = [r for r in c.rows if r["instr_type"] in TDM_INSTR_TYPES]
    if len(rows) < len(c.rows):  # the desk store holds them; the legacy tdm store has no such instrument type
        c.log.append(("WARN", "в tdm не записаны позиции без типа инструмента tdm (OTC/прочее): "
                      + ", ".join(r["position_id"] for r in c.rows if r["instr_type"] not in TDM_INSTR_TYPES)))
    with Run(db, as_of=ctx.business_date.isoformat(), run_type="EOD",
             source=f"desk-backend {BACKEND_VERSION} (slice {'S2' if any(o.pnl for _, _, o in positions) else 'S1'})",
             lib_version=f"rub-fixed-income-core {CORE_VERSION}",
             market_snapshot=ctx.market.snapshot.id, comment=comment or json.dumps(lineage, ensure_ascii=False),
             prev_date=prev.business_date.isoformat() if prev else None, jtd_recovery=0.0) as run:
        for kind in REFERENCE_KINDS:
            getattr(run, f"add_{kind}")(c.reference[kind])
        run.add_positions(rows)
        for level, message in c.log:
            run.log(level, message)
    return run.run_id, run.status, run.warnings
