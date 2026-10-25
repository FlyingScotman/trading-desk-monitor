"""The T2 scenario result: exact repricing of a stored run by core, beside the T1 Taylor (S6 §4).

``reprice_run`` (no DB) rebuilds the run's context from its own market snapshot and reprices its exact
and assumed positions through core ``value_portfolio`` scenarios, one ``value`` call per distinct own
spread shock (core takes one spread shift per scenario). A position is covered when its base PV
reproduces the run's within ``BASE_TOLERANCE_RUB`` and core repriced it; its residual is exact minus
the Taylor total the browser shows. Fallback rungs and rows without a bond (an FX forward, ``assumed``) have no
cashflows: they stay Taylor only, with ``code`` = their quality. ``blended`` = exact of the covered positions + Taylor of the rest.

Curves (R8): ``RUB RUONIA`` and ``RUB KEY`` move the RUONIA OIS curve (every RUB bond's discount curve, a RUONIA
floater's projection) and the key-rate projection by name (``curve_shifts_bp``), each only by its own vector, as
the Taylor moves each curve's factors: a floater sees the basis when one curve moves without the other. The shock
of ``RUB OFZ`` is the shock's own curve: it moves the KBD of a bond discounted on it (a date without RUONIA). A foreign
curve core values on (``curves.fx``: ``CNY FX``, ``CNY XCCY``, ``USD UST``; its core ID is its published name) moves by name
too: a foreign bond's discount curve, an FX derivative's foreign leg. A currency's curve without points (``USD``, a duration
proxy, ``bonds.ccy_curve``) has no core curve; only Taylor positions carry risk on it.

FX derivatives valued by core from their terms (``valuation.derivatives``) are repriced from the legs the run published
(``extra.legs``), with the curves, the FX moves (``fx``) and the FX basis shifts (``basis``: ``FXBASIS|<ccy>``, bp; the
Taylor has no basis factor, so its effect is all in the residual) on the run's market and the FX settings' defaults
(a run made with other FX settings may not reproduce its base PV: ``base_mismatch``, Taylor only).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from rub_fixed_income import RISK_BUCKETS, BondScenario, PortfolioScenario

from ..marketdata.bundle import SnapshotDir
from ..orchestrator import EodContext, prepare, value
from ..valuation import derivatives as dv
from ..valuation.bonds import OFZ_CURVE, RISK_CURVES, BondValuation
from .definition import definition_hash, normalize, own_spread_shock
from .taylor import PARTS, risk_payload, run_taylor

METHOD = "desk-scenario-exact-v2"
"""v2 (R8): each curve moves only by its own shifts; v1 moved a floater's projection curve with the KBD."""
BASE_TOLERANCE_RUB = 1.0
REPRICED = ("exact", "assumed")
CTX_CACHE_SIZE = 2
NAMED_CURVES = tuple((core, published) for core, published in RISK_CURVES.items() if published != OFZ_CURVE)
"""(core curve ID, published curve) moved by name; the KBD is the shock's own curve (a bond whose discount curve
is named moves it by its name)."""
KBD_SHORT_BUCKETS = ("1M", "3M", "6M")
"""The KBD's buckets without an instrument (``rub_ofz_kbd_v3``, par yields 1Y-20Y+): core 0.38 refuses a scenario whose
shift there is neither 0 nor the 1Y shift (``InvalidTerms``; 0.37 dropped it), so ``kbd_shock`` sets them to 0."""


def kbd_shock(shifts: tuple[float, ...]) -> tuple[float, ...]:
    """``RUB OFZ`` shifts as core takes them on the KBD: 1M-6M set to 0, 1Y and beyond as given. Below 1Y the KBD's
    par overlay follows the 1Y shift whatever they are, and the Taylor has no KBD risk there, so the scenario means the
    same."""
    return tuple(0.0 if b in KBD_SHORT_BUCKETS else x for b, x in zip(RISK_BUCKETS, shifts))


def core_scenario(name: str, norm: Mapping[str, Any], ds: float) -> PortfolioScenario:
    """The core scenario of a normalized definition for an own spread shock ``ds``: every curve core values with
    moves by its published curve's shifts only (0 when the definition has none); ``RUB OFZ`` (the KBD) without its
    1M-6M shifts (``kbd_shock``)."""
    curves, zero = norm.get("curves", {}), (0.0,) * len(RISK_BUCKETS)
    rub = {c for _, c in NAMED_CURVES} | {OFZ_CURVE}
    # a foreign curve's core ID is its published name (curves.fx: CNY FX, CNY XCCY, USD UST); a currency's curve
    # without points (USD: a duration proxy) has no core curve, and core ignores a shift of a curve it does not have
    foreign = tuple((c, tuple(v)) for c, v in curves.items() if c not in rub)
    return PortfolioScenario(name=name, shock=BondScenario(kbd_shock(tuple(curves.get(OFZ_CURVE, zero))), ds),
                             fx_shocks_pct=tuple(norm.get("fx", {}).items()),
                             curve_shifts_bp=(*((core, tuple(curves.get(c, zero))) for core, c in NAMED_CURVES),
                                              *foreign),
                             basis_shifts_bp=tuple((f"FXBASIS|{c}", bp) for c, bp in norm.get("basis", {}).items()))


def _context(snapshots: SnapshotDir, run: Mapping[str, Any], ctx_cache: dict | None) -> EodContext:
    """The run's context on its own snapshot, with its risk in the unit the run's was (``quote_curve_risk`` of its
    lineage, issue #171: a curve the run took in its quotes is shifted in its quotes, as its Taylor factors)."""
    if ctx_cache is not None and run["run_id"] in ctx_cache:
        return ctx_cache[run["run_id"]]
    ctx = prepare(snapshots, str(run["business_date"]), run["cut_type"], market_snapshot_id=run["market_snapshot_id"])
    if run.get("quote_curve_risk"):
        ctx = replace(ctx, quote_curve_risk=True)
    if ctx_cache is not None:
        # shortcut: at most CTX_CACHE_SIZE prepared runs, oldest out; upgrade to an LRU sized by memory when
        # the worker serves many runs at once.
        ctx_cache[run["run_id"]] = ctx
        while len(ctx_cache) > CTX_CACHE_SIZE:
            del ctx_cache[next(iter(ctx_cache))]
    return ctx


def reprice_run(snapshots: SnapshotDir, run: Mapping[str, Any], rows: list[Mapping[str, Any]],
                reference: Mapping[str, list], definition: Mapping[str, Any],
                ctx_cache: dict | None = None) -> dict[str, Any]:
    """``run``: ``run_id``, ``business_date`` (a date), ``cut_type``, ``market_snapshot_id`` (and ``quote_curve_risk``,
    the run's lineage flag: absent is off); ``rows``:
    ``run_position`` dicts (``position_id``, ``book_id``, ``secid``, ``quantity``, ``quality``, ``pv_rub``,
    ``fact``); ``reference``: ``run_reference`` data by kind. Raises ``DefinitionError`` on a bad definition,
    ``SnapshotError`` when the run's snapshots are not in ``snapshots``. Returns the result JSON."""
    payload = risk_payload(run, rows, reference)
    norm = normalize(definition, payload["curves"])
    h = definition_hash(norm)
    taylor = run_taylor(payload, norm)
    qty = {r["position_id"]: r["quantity"] for r in rows}
    exact: dict[str, float] = {}
    code: dict[str, str] = {}
    groups: dict[float, list[Mapping[str, Any]]] = {}
    facts = {r["position_id"]: r.get("fact") or {} for r in rows}
    fx_reqs: list[dv.Request] = []
    for p in payload["positions"]:
        ex = facts[p["position_id"]].get("extra") or {}
        if p["quality"] in REPRICED and p["secid"] is not None:  # an FX forward without terms has no bond
            groups.setdefault(own_spread_shock(norm, p), []).append(p)
        elif p["quality"] in REPRICED and ex.get("core") and (legs := dv.legs_from_fact(ex)):
            fx_reqs.append(dv.Request(p["position_id"], p["book_id"], {"ccy": facts[p["position_id"]].get("ccy")},
                                      legs))  # a derivative valued from its terms: repriced from its legs
        else:
            code[p["position_id"]] = p["quality"]
    if fx_reqs:
        ctx = _context(snapshots, run, ctx_cache)
        scenario = core_scenario(h[:12], norm, 0.0)
        valued, result = dv.value(ctx, fx_reqs, options=ctx.fx_options, second_order=False, scenarios=(scenario,))
        pnl: dict[str, float | None] = {}
        for s in result.scenarios[0].positions if result is not None else ():
            pid = s.position_id.rsplit("#", 1)[0]
            pnl[pid] = None if s.pnl_rub is None or pnl.get(pid, 0.0) is None else pnl.get(pid, 0.0) + s.pnl_rub
        base = {p["position_id"]: p["pv_rub"] for p in payload["positions"]}
        for r in fx_reqs:
            pid, v = r.position_id, valued.get(r.position_id)
            if not isinstance(v, BondValuation):
                code[pid] = v.reason if v is not None else "not_valued"
            elif abs(v.pv_rub - base[pid]) > BASE_TOLERANCE_RUB:
                code[pid] = "base_mismatch"
            elif pnl.get(pid) is None:
                code[pid] = "core_failed"
            else:
                exact[pid] = pnl[pid]  # type: ignore[assignment]
    if groups:
        ctx = _context(snapshots, run, ctx_cache)
        for ds, ps in groups.items():  # shortcut: one core call per distinct ds; upgrade: a per-position spread shift in core
            pids = [p["position_id"] for p in ps]
            scenario = core_scenario(h[:12], norm, ds)
            outs = value(ctx, [(p["secid"], qty[p["position_id"]]) for p in ps], position_ids=pids, fallback=False,
                         scenarios=(scenario,))
            for p, o in zip(ps, outs):
                pid = p["position_id"]
                if o.valuation is None or o.core_result is None:
                    code[pid] = o.reason or "not_valued"
                elif abs(o.valuation.pv_rub - p["pv_rub"]) > BASE_TOLERANCE_RUB:
                    code[pid] = "base_mismatch"
                else:
                    s = next(x for x in o.core_result.scenarios[0].positions if x.position_id == pid)
                    if s.pnl_rub is None:
                        code[pid] = f"core_{(s.code or 'failed').lower()}"
                    else:
                        exact[pid] = s.pnl_rub
    positions, totals = [], {}
    for p, t in zip(payload["positions"], taylor["positions"]):
        pid = p["position_id"]
        e = exact.get(pid)
        res = None if e is None else e - t["total"]
        positions.append({"position_id": pid, "book_id": p["book_id"], "quality": p["quality"],
                          "taylor": {k: t[k] for k in PARTS}, "exact": e, "residual": res, "code": code.get(pid)})
        for b in ("_total", p["book_id"]):
            s = totals.setdefault(b, {"taylor": 0.0, "taylor_covered": 0.0, "exact": None, "residual": None,
                                      "blended": 0.0, "n_positions": 0, "n_exact": 0})
            s["taylor"] += t["total"]
            s["n_positions"] += 1
            if e is None:
                s["blended"] += t["total"]
                continue
            s["taylor_covered"] += t["total"]
            s["exact"] = (s["exact"] or 0.0) + e
            s["residual"] = (s["residual"] or 0.0) + res
            s["blended"] += e
            s["n_exact"] += 1
    totals.setdefault("_total", {"taylor": 0.0, "taylor_covered": 0.0, "exact": None, "residual": None,
                                 "blended": 0.0, "n_positions": 0, "n_exact": 0})
    missing = [{"position_id": x["position_id"], "reason": x["code"]} for x in positions if x["exact"] is None]
    return {"method": METHOD, "definition_hash": h,
            "coverage": {"status": "partial" if missing else "complete", "n_positions": len(positions),
                         "n_exact": len(positions) - len(missing), "missing": missing},
            "totals": totals, "positions": positions}
