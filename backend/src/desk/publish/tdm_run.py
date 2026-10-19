"""Write an EOD run into the ``tdm`` store through its writer SDK (``tdm.writer.Run``).

The store and the Desk Monitor prototype then show it through ``tdm serve`` and
``window.DESK``. This is the slice shortcut for the desk store and desk API (VERTICAL_SLICES.md).
Every valued position is written, also those on a fallback rung (``valuation.fallback``): its
``extra`` carries ``quality`` and ``assumptions``, and the run log counts the qualities and lists
each non-exact position with its reasons as a WARN line (the prototype has no quality column).
A position with no data at all is not written with zeros: it gets a WARN line with its reason.
The same holds for the flash PnL of slice S2: a priced position without PnL gets no ``pnl`` dict
and a WARN line with the reason; a simple PnL (all in ``other``) is counted in its own WARN line.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections import Counter, defaultdict
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
from ..valuation.fallback import METHOD as SIMPLE_METHOD
from ..valuation.fallback import QUALITIES, SimplePnl, fx_rate

CURVE_ID = "RUB OFZ"
ASSET_CLASS = {"ofz_pd": "ОФЗ-ПД", "ofz_ad": "ОФЗ-АД", "ofz_pk": "ОФЗ-ПК", "ofz_in": "ОФЗ-ИН",
               "subfederal": "Субфедеральные", "municipal": "Муниципальные", "corporate": "Корпоративные"}
SEGMENT = {"ofz_pd": "Гос", "ofz_ad": "Гос", "ofz_pk": "Гос", "ofz_in": "Гос",
           "subfederal": "Суб", "municipal": "Суб"}
BOOKS = {
    "OFZ": {"book_id": "OFZ", "name": "ОФЗ (демо)", "desk": "Rates", "trader": None, "book_type": "TRADING",
            "entity": "Демо", "ocp_to": None},
    "CORP": {"book_id": "CORP", "name": "Корпоративные (демо)", "desk": "Credit", "trader": None,
             "book_type": "TRADING", "entity": "Демо", "ocp_to": None},
    "MIXED": {"book_id": "MIXED", "name": "Смешанная (тест)", "desk": "Rates", "trader": None,
              "book_type": "TRADING", "entity": "Демо", "ocp_to": None},
}
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


def position_row(ctx: EodContext, book: str, pid: str, o: Outcome, issuer: str) -> dict[str, Any]:
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


def publish(db: str, ctx: EodContext, positions: list[tuple[str, str, Outcome]], *,
            prev: MarketView | None, comment: str | None = None) -> tuple[int, str, list[str]]:
    """positions: (position_id, book_id, outcome). Returns (run_id, status, warnings)."""
    lineage = {**ctx.lineage(), "backend_version": BACKEND_VERSION, "backend_git_sha": git_sha(),
               "core_version": CORE_VERSION,
               "prev_market_snapshot_id": prev.snapshot.id if prev else None,
               "core_results": store_core_results(db, [o for _, _, o in positions])}
    issuers: dict[str, dict[str, Any]] = {}
    rows, skipped, no_pnl, approx = [], [], [], []
    quality: Counter[str] = Counter()
    for pid, book, o in positions:
        if o.valuation is None or o.terms is None:
            skipped.append(f"{pid} {o.secid}: не посчитано, нет данных "
                           f"({o.reason}{': ' + o.detail if o.detail else ''})")
            continue
        quality[o.valuation.quality] += 1
        if o.valuation.quality != "exact":
            approx.append(f"{pid} {o.secid}: оценка {o.valuation.quality}: " + "; ".join(o.valuation.assumptions))
        if o.pnl is None and prev is not None:
            e = o.no_pnl
            no_pnl.append(f"{pid} {o.secid}: нет PnL ({e.reason}{': ' + e.detail if e.detail else ''})" if e
                          else f"{pid} {o.secid}: нет PnL (флэш-PnL не считался)")
        if isinstance(o.pnl, SimplePnl) and o.pnl.flag:
            no_pnl.append(f"{pid} {o.secid}: PnL {o.pnl.total:,.0f} ₽ с модельной ценой ({o.pnl.flag})")
        key, rec = _issuer_key(ctx, o)
        issuers[key] = rec
        rows.append(position_row(ctx, book, pid, o, key))
    used_books = [BOOKS[b] for b in BOOKS if any(r["book_id"] == b for r in rows)]
    indices = []
    for code, name in INDICES:
        fx = ctx.market.latest_fixing(code)
        if fx:
            indices.append({"index_name": name, "value": fx[1]})
    with Run(db, as_of=ctx.business_date.isoformat(), run_type="EOD",
             source=f"desk-backend {BACKEND_VERSION} (slice {'S2' if any(o.pnl for _, _, o in positions) else 'S1'})",
             lib_version=f"rub-fixed-income-core {CORE_VERSION}",
             market_snapshot=ctx.market.snapshot.id, comment=comment or json.dumps(lineage, ensure_ascii=False),
             prev_date=prev.business_date.isoformat() if prev else None, jtd_recovery=0.0) as run:
        run.add_books(used_books)
        run.add_issuers(list(issuers.values()))
        run.add_curves([{"curve_id": CURVE_ID, "name": "ОФЗ (КБД MOEX)", "ccy": "RUB", "kind": "bond",
                         "points": curve_points(ctx, prev)}])
        run.add_fx(_fx_rows(ctx, prev))
        run.add_indices(indices)
        run.add_positions(rows)
        run.log("INFO", "lineage " + json.dumps(lineage, ensure_ascii=False))
        if ctx.point_in_time_warning:
            run.log("WARN", "статика не на дату рынка: " + ctx.point_in_time_warning)
        run.log("INFO", "качество оценки: " + ", ".join(f"{k} {quality[k]}" for k in QUALITIES if quality[k])
                + (f", не посчитано {len(skipped)}" if skipped else ""))
        for s in skipped + approx + no_pnl:
            run.log("WARN", s)
        with_pnl = [o.pnl for _, _, o in positions if o.pnl]
        simple = [x for x in with_pnl if isinstance(x, SimplePnl)]
        if simple:
            run.log("WARN", f"флэш-PnL без разложения ({SIMPLE_METHOD}: q x Δdirty + выплаты, всё в other) "
                    f"у {len(simple)} поз.: {sum(x.total for x in simple):,.0f} ₽")
        if with_pnl:
            tot = {k: sum(getattr(x, k) for x in with_pnl) for k in ("carry", "rolldown", "rates", "spread", "other")}
            worst = max(abs(x.identity_error) for x in with_pnl)
            run.log("INFO", f"флэш-PnL {with_pnl[0].prev_date} -> {with_pnl[0].as_of} ({METHOD}), "
                    f"позиций {len(with_pnl)} (без разложения {len(simple)}): "
                    + ", ".join(f"{k} {v:,.0f}" for k, v in tot.items())
                    + f"; итог {sum(x.total for x in with_pnl):,.0f} ₽, макс. ошибка тождества {worst:.2e} ₽")
        run.log("WARN", "JTD не считается в слайсе S1 (нет решения по recovery): поле jtd = 0")
        run.log("INFO", f"опубликовано позиций: {len(rows)} (точно {quality['exact']}), не посчитано: {len(skipped)}")
    return run.run_id, run.status, run.warnings
