"""Compatibility adapter (05 §4): ``window.DESK`` from the desk store, in the shape ``tdm.assemble.assemble``
produces. A port of that function onto the store's rows (run_position.fact/pnl, run_reference); the tdm
writer's defaults (zero cs01/jtd, nonzero risk/FX/gamma only, RUB at 1) are applied here. What the store
does not hold yet (OCP, scenario results, official PnL, CBR meetings, bond quotes) is rendered as the tdm
assembler renders a run without it. ``history`` is the default runs of the earlier dates, where tdm's
desk_history holds the latest published EOD run of each date (any cut). A position the run did not value (no
fact: no data at all, an OTC row without the report's value) is listed after the valued ones with ``notValued``,
no PV (``mvRub`` null), zero risk and its reason in ``extra``, so the positions list shows it and no total
moves; it has no segment and no instrument type (the one addition beyond tdm's assembler, which drops such a
position).

Honest absence of PnL (R10), the other departure from tdm, which shows a missing PnL as 0: ``prevDate`` is null
for a run without a previous market, the run says ``pnlAvailable`` (false with ``pnlReason`` and its Russian
``pnlReasonText``) and ``pnlCoverage``; a position without PnL has every ``pnl`` amount null with ``available``
false, ``reason``, ``reasonText`` and ``reasonDetail``; one with PnL has its ``method``/``methodText``/``flag``,
and a PnL by the reports (``pnl.reasons.REPORTED_METHOD``) is in ``pnl.reported``, not in ``other``. ``history``
has null PnL for a run where no position has one. ``riskNotes`` (R14, R15): what the run's risk by curve lacks (a
rates curve missing from the hub's snapshot, a run made before the RUONIA discounting), so the risk views say it
instead of showing a curve as silently zero. Funding (B1, ``publish.funding_run``): ``funding`` (the lines, in tdm's
shape; a bond's line has its currency liability in ``fxLiability`` and its FX revaluation, in the position's PnL, in
``pnl.fxAllocated``, TDM#54), ``fundingPnl`` and ``fundingPnlByBook`` (the book-level lines;
``inPositions``: what is in the positions' PnL), ``FUNDING_RULES`` (the adapter's rules the run used), a position's
``pnl.funding`` (null, not 0, without funding: not configured, a problem; the pages show it apart from the carry,
TDM#168) and ``fundingStatus`` (``fundingStatus``:
state ``computed`` / ``not_configured`` / ``invalid_config`` / ``failed`` / ``not_computed``, its text, totals by
book and entity, problems), the one key compat adds for it. OCP (B2, ``publish.ocp_run``): ``OCP_TRANSFERS`` from the
run's transfer deals and a transfer deal's ``ocpTransfer``; the OCP screen reads ``/api/v1/runs/{id}/ocp``. compat,
removed with window.DESK (M7); never extended otherwise."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from rub_fixed_income import RISK_BUCKETS, RISK_PILLARS

from ..marketdata import ratings
from ..pnl.reasons import METHOD_TEXT, REPORTED_METHOD, RUN_NO_POSITION_PNL, RUN_NO_PREV_MARKET, no_pnl_text
from ..publish.funding_run import NOT_COMPUTED, STATUS_TEXT
from ..publish.summary import fx_net
from ..scenario.carry import HORIZONS as carry_horizons
from ..scenario.carry import meeting_rows, meetings
from ..store import CURRENT_RUNS, DEFAULT_RUNS, slice_label  # noqa: F401 - re-exported, as before
from .runs import PNL_KEYS, RUONIA_DISCOUNT_VERSION, resolve_run, risk_notes, run_slice  # noqa: F401 - moved (TDM#289)

CARRY_HORIZONS = [list(h) for h in carry_horizons]  # the forward carry's (scenario.carry.HORIZONS)
DEFAULT_CARRY = [{"id": "flat", "name": "КС без изменений", "ks": {"mode": "flat"}}]
RATINGS = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B",
           "B-", "CCC", "D"]  # tdm ref_rating seed order
RATING_RANK = {g: i for i, g in enumerate([*ratings.GRADES, ratings.NOT_RATED, ratings.NO_DATA])}
"""The order of ``RATING_ORDER``: the grades best first (``marketdata.ratings.GRADES``), then б/р and нет данных."""
RUB = {"ccy": "RUB", "rate": 1.0, "move_pct": 0.0, "rate_cbr": 1.0, "move_pct_cbr": 0.0, "rate_cbr_next": 1.0}
CONTRACT_VERSION = "1.3"
"""The tdm bundle contract (``tdm.CONTRACT_VERSION``, shared with tdm). Its shape did not change in 0.2.6, but
``ofzSpr01`` of a fixed-coupon OFZ is its spread to the KBD from then on, no longer its basis to RUONIA: a reader
tells them apart by ``source.ofzScheme`` (``kbd_par``; null before) and finds the basis in ``extra.ofz_basis01``
(``contract/bundle.schema.json`` says so on ``ofz_spr01``). Nor in 0.2.7 (``ruonia_basis``, TDM#213): a fixed OFZ's
``risk`` also has ``RUB RUONIA`` (its RUONIA risk minus its basis, ``valuation.bonds.ofz_levels``) beside ``RUB OFZ``
(its basis risk, the KBD's par risk) and ``dv01`` is their sum, its RUONIA risk (``extra.ofz_ruonia01``); its ``gamma``
is over the same factors. The reader tells such a row by ``extra.ofz_ruonia01``."""
HISTORY_DAYS = 22  # tdm.assemble.HISTORY_DAYS

def _years(d0: str, d1: str | None) -> float | None:
    try:
        return (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _zero(v: float | None) -> float:
    return 0.0 if v is None else v


def _prev_weekday(d: date) -> date:
    # shortcut: Mon-Fri as tdm without ref_calendar; a run without prev_date is an S1-style run
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def gamma_summary(rows, mv_rub, convexity, cs01) -> dict[str, Any]:
    """As tdm.assemble.gamma_summary: RUB per bp^2 to parallel IR, to the spread and mixed."""
    ir = cs = ircs = 0.0
    if rows:
        for a, b, v in rows:
            k = 1 if a == b else 2
            ta, tb = a.split("|")[0], b.split("|")[0]
            if ta == "IR" and tb == "IR":
                ir += k * v
            elif ta == "CS" and tb == "CS":
                cs += v
            elif {ta, tb} == {"IR", "CS"}:
                ircs += v
        src = "matrix"
    elif convexity and mv_rub:
        ir = mv_rub * convexity * 1e-8
        if cs01:
            cs = ircs = ir
        src = "convexity"
    else:
        src = None
    return {"gammaIr": ir, "gammaCs": cs, "gammaIrCs": ircs, "gammaSource": src}


def _gamma(rows: list[list[Any]] | None) -> list[list[Any]]:
    """The writer's position_gamma: nonzero pairs, factors sorted, ordered by factor."""
    out: dict[tuple[str, str], float] = {}
    for a, b, v in rows or []:
        if v:
            out[min(a, b), max(a, b)] = v
    return [[a, b, v] for (a, b), v in sorted(out.items())]


def run_totals(rows) -> tuple[float | None, float | None, float, float]:
    """As tdm.writer.run_totals on rows (fact, pnl) of a run without funding deals: (official, explained,
    dv01, cs01). A position without official PnL counts its explained PnL plus the FX basis (CBR rates),
    one without PnL nothing; official and explained are None (not 0) when no position has a PnL (R10). A position
    with an impossible risk (``extra.risk_problem``, TDM#170) is in neither DV01 nor CS01 (TDM#460), as in the run's
    summary; its PnL is."""
    off, exp = [], []
    for r in rows:
        if q := r["pnl"]:
            e = sum(q.get(k) or 0.0 for k in PNL_KEYS)  # tdm EXPLAINED_SQL, the same order
            basis = (fx_net(q, cbr=True) or 0.0) - (fx_net(q) or 0.0)  # the net FX by the CBR rates - the market
            off.append(q["official"] if q.get("official") is not None else e + basis)
            exp.append(e)
    # SQLite SUM is compensated too
    facts = [r["fact"] for r in rows if not (r["fact"].get("extra") or {}).get("risk_problem")]
    return (math.fsum(off) if off else None, math.fsum(exp) if exp else None,
            math.fsum(p.get("dv01") or 0.0 for p in facts),
            math.fsum(p.get("cs01") or 0.0 for p in facts))


def official_received(rows) -> bool:
    """Whether any row (fact, pnl) of a run carries the official PnL itself (TDM#106), not the fallback of
    ``run_totals`` (explained plus the FX basis at the CBR rates)."""
    return any((r["pnl"] or {}).get("official") is not None for r in rows)


NOT_VALUED_KIND = {"otc": ("OTC-деривативы", "Деривативы"), "other": ("Прочее", "Прочее"), "fx": ("Валюта", "FX")}
"""asset_class of an unvalued report row -> (asset class, segment), as ``tdm_run.DIRECT_KIND`` names a valued one;
a bond and a legacy row get none (segment ``—`` in the Desk Monitor)."""
INPUT_FIELDS = ("asset_class", "ccy", "source_instrument_id", "source_instrument_name", "isin", "deal_id",
                "value_date", "maturity_date", "amount_ccy", "face_amount_ccy", "face_outstanding_ccy",
                "mark_price_pct", "mv_rub", "risk_dv01_rub", "risk_cs01_rub", "src", "x")
"""What an unvalued position shows of its report row (position_input of the run's position sets)."""


PNL_FIELDS = ("carry", "rolldown", "rates", "spread", "fx", "funding", "convexity", "newTrades", "other", "reported",
              "fxGross", "fxFunding", "fxGrossCbr", "fxFundingCbr", "fxCbr", "explained", "explainedCbr", "fxBasis",
              "unexplained")
"""The amounts of a Desk Monitor position's ``pnl``: all null for a position without PnL."""


def no_pnl(code: str, detail: str | None = None) -> dict[str, Any]:
    """A position's ``pnl`` without PnL (R10): every amount null, not 0, and why (``pnl.reasons``)."""
    return {**dict.fromkeys(PNL_FIELDS), "official": None, "breakNote": None, "available": False, "reason": code,
            "reasonText": no_pnl_text(code), "reasonDetail": detail or None, "method": None, "methodText": None,
            "flag": None}


def _json(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date) else float(v) if isinstance(v, Decimal) else v


def _not_valued(r: dict[str, Any], books: dict[str, dict], x: dict[str, Any] | None,
                portfolio: str | None) -> dict[str, Any]:
    """A run_position without fact as a Desk Monitor position: visible, PV empty, nothing in any total. ``x``: its
    report row (``INPUT_FIELDS``, None for a legacy row): name, currency, maturity and the raw x_* fields."""
    x = {k: _json(v) for k, v in (x or {}).items() if v is not None}
    kind = (x.get("asset_class") or "").lower()
    aclass, segment = (None, None) if kind in ("", "bond") else NOT_VALUED_KIND.get(kind, NOT_VALUED_KIND["other"])
    return {
        "id": r["position_id"], "book": r["book_id"], "assetClass": aclass, "segment": segment, "instrType": None,
        "name": x.get("source_instrument_name") or x.get("source_instrument_id") or r["secid"] or r["position_id"],
        "isin": x.get("isin"), "issuer": None, "issuerGroup": None, "sector": None, "rating": None,
        "ccy": x.get("ccy"), "maturity": x.get("maturity_date") or x.get("value_date"), "tenorY": None, "side": None,
        "notional": None,
        "notionalRub": None, "price": None, "mv": None, "mvRub": None, "dv01": 0.0, "cs01": 0.0, "ofzSpr01": 0.0,
        "risk": {}, "fxDelta": {}, "jtd": 0.0, "gamma": [], **gamma_summary([], None, None, 0.0),
        "entity": books.get(r["book_id"], {}).get("entity", "—"), "portfolio": portfolio, "newToday": False,
        "notValued": True,
        "pnl": no_pnl("not_valued", r["reason"]),
        "extra": {"quality": "not_valued", "valuation": "not_valued", "reason": r["reason"],
                  "quantity": r["quantity"], **({"reported": x} if x else {})},
    }


def _no_pnl_of(stored: dict[str, Any] | None, prev_date: date | None) -> tuple[str, str | None]:
    """(code, detail) of a valued position without PnL: run_position.no_pnl, else the run's own reason."""
    if stored:
        return stored.get("reason") or "not_computed", stored.get("detail")
    return (RUN_NO_PREV_MARKET, None) if prev_date is None else ("not_computed", None)


def pnl_state(run: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any]:
    """The run-level PnL fields of window.DESK (R10): ``pnlAvailable`` false with a code and its text when the run
    has no previous market or no position has a PnL; ``pnlCoverage``: positions with PnL of all the listed ones."""
    n = sum(p["pnl"]["available"] for p in positions)
    code = RUN_NO_PREV_MARKET if run["prev_date"] is None else RUN_NO_POSITION_PNL if not n else None
    return {"pnlAvailable": code is None, "pnlReason": code, "pnlReasonText": no_pnl_text(code) if code else None,
            "pnlCoverage": {"withPnl": n, "positions": len(positions)}}


def assemble(con, run: dict[str, Any]) -> dict[str, Any]:
    rid, as_of = run["run_id"], run["business_date"].isoformat()
    ref = {r["kind"]: r["data"]
           for r in con.execute("SELECT kind, data FROM run_reference WHERE run_id = %s", (rid,))}
    B = list(RISK_BUCKETS)
    bidx = {b: i for i, b in enumerate(B)}

    curves, moves = {}, {}
    for c in ref.get("curves", []):
        pts, mv = [None] * len(B), [0.0] * len(B)
        for p in c["points"]:
            pts[bidx[p["bucket"]]], mv[bidx[p["bucket"]]] = p["rate"], p.get("move_bp") or 0
        curves[c["curve_id"]] = {"name": c["name"], "ccy": c["ccy"], "kind": c["kind"], "pts": pts}
        moves[c["curve_id"]] = mv

    indices = {r["index_name"]: r["value"] for r in ref.get("indices", [])}

    prows = con.execute("SELECT position_id, fact, pnl, no_pnl FROM run_position WHERE run_id = %s"
                        " AND fact IS NOT NULL ORDER BY ord", (rid,)).fetchall()
    fxr = list({"RUB": RUB, **{r["ccy"]: r for r in ref.get("fx", [])}}.values())  # RUB first, as in tdm
    # a run that published only USD, EUR and CNY (before R27): another currency's rate is the one its bonds were
    # valued at (``extra.fx_rate``), its move unknown (0), so that its amounts convert and its curve is its own
    have = {r["ccy"] for r in fxr}
    for r in prows:
        ccy, ex = r["fact"].get("ccy"), r["fact"].get("extra") or {}
        if ccy and ccy not in have and isinstance(ex, dict) and ex.get("fx_rate"):
            have.add(ccy)
            fxr.append({"ccy": ccy, "rate": ex["fx_rate"], "rate_source": ex.get("fx_source"), "move_pct": 0.0})
    fx = {r["ccy"]: r["rate"] for r in fxr}
    fx_move = {r["ccy"]: r.get("move_pct") or 0 for r in fxr}
    fx_cbr = {r["ccy"]: r["rate_cbr"] if r.get("rate_cbr") is not None else r["rate"] for r in fxr}
    fx_cbr_move = {r["ccy"]: r["move_pct_cbr"] if r.get("move_pct_cbr") is not None
                   else (fx_move[r["ccy"]] if r.get("rate_cbr") is None else 0.0) for r in fxr}
    fx_cbr_next = {r["ccy"]: r["rate_cbr_next"] if r.get("rate_cbr_next") is not None else fx_cbr[r["ccy"]]
                   for r in fxr}
    # the market rate's source (R26: MOEX 19:00, else 14:00, else CETS close, else CBR); a run before it: unknown
    fx_source = {r["ccy"]: r.get("rate_source") for r in fxr if r["ccy"] != "RUB"}
    for cid, c in curves.items():  # a currency's curve published as RUB for want of its rate (before R27): its own
        if c["ccy"] == "RUB" and cid != "RUB" and cid in fx:
            c["ccy"] = cid
    has_bonds = {r["fact"]["book_id"] for r in prows if r["fact"].get("instr_type") == "BOND"}
    books = {}
    for r in ref.get("books", []):
        b = {"name": r["name"], "desk": r["desk"], "trader": r.get("trader"),
             "bookType": r.get("book_type") or ("TRADING" if r["book_id"] in has_bonds else "DERIVS"),
             "entity": r.get("entity") or "—"}
        if r.get("ocp_to"):
            b["ocpTo"] = r["ocp_to"]
        books[r["book_id"]] = b
    issuers, disp = {}, {}
    for r in ref.get("issuers", []):
        disp[r["issuer"]] = r.get("name") or r["issuer"]
        x = {"group": r["issuer_group"], "sector": r.get("sector"), "rating": r.get("rating")}
        if r.get("rating_info"):  # the desk's rating view (R25): agency, the rating as written, date, basis
            x["ratingInfo"] = r["rating_info"]
        if r.get("is_gov"):
            x["gov"] = True
        if r.get("is_quasi"):
            x["quasi"] = True
        issuers[disp[r["issuer"]]] = x
    def pos_rating(p: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        """A bond's own desk rating (``extra.rating``, R25), else its issuer's."""
        own = (p.get("extra") or {}).get("rating") if isinstance(p.get("extra"), dict) else None
        iss = issuers.get(disp.get(p["issuer"], p["issuer"])) if p.get("issuer") else None
        return ratings.position_rating(p, iss), own or (iss or {}).get("ratingInfo")

    used = list(dict.fromkeys([x["rating"] for x in issuers.values() if x["rating"]]
                              + [g for r in prows if (g := pos_rating(r["fact"])[0])]))
    rating_order = sorted(used, key=lambda r: RATING_RANK.get(r, len(RATING_RANK) + used.index(r)))

    # the report's Portfolio column (the adapter's x_portfolio: HTM, trading, ...) of every position, valued or
    # not, for the Desk Monitor's portfolio filter; None for a row without it (a legacy load)
    portfolio = {r["position_id"]: r["portfolio"] for r in con.execute(
        "SELECT position_id, x->>'x_portfolio' AS portfolio FROM position_input WHERE set_id = ANY(%s)"
        " AND x ? 'x_portfolio'", (run["position_set_ids"] or [],))}

    positions = []
    for row in prows:
        p, q = row["fact"], row["pnl"]
        pid = row["position_id"]
        iname = disp.get(p["issuer"], p["issuer"]) if p.get("issuer") else None
        iss = issuers.get(iname) if iname else None
        comp = {k: (q.get(k) or 0.0) if q else 0.0 for k in PNL_KEYS}
        explained = sum(comp.values())
        gross_cbr = q["fx_gross_cbr"] if q and q.get("fx_gross_cbr") is not None else comp["fx_gross"]
        fund_cbr = q["fx_funding_cbr"] if q and q.get("fx_funding_cbr") is not None else comp["fx_funding"]
        # the pages show FX only net (one source «FX (нетто ОВП)», summary.fx_net, TDM#414); fxGross / fxFunding stay
        # in the API for the explain's arithmetic, no page shows them apart
        fxn, fxn_cbr = (fx_net(q), fx_net(q, cbr=True)) if q else (None, None)
        fx_basis = (fxn_cbr or 0.0) - (fxn or 0.0)
        explained_cbr = explained + fx_basis
        official = q.get("official") if q else None
        method = ((q or {}).get("extra") or {}).get("method")
        reported = comp["other"] if method == REPORTED_METHOD else 0.0  # the reports' difference: its own step
        cs01 = _zero(p.get("cs01"))
        gamma = _gamma(p.get("gamma"))
        risk = {}
        for curve, bk in (p.get("risk") or {}).items():
            for b, dv in bk.items():
                if dv:
                    risk.setdefault(curve, [0.0] * len(B))[bidx[b]] = dv
        pos = {
            "id": pid, "book": p["book_id"], "assetClass": p.get("asset_class"), "segment": p.get("segment"),
            "instrType": p.get("instr_type"), "name": p.get("name"), "isin": p.get("isin"), "issuer": iname,
            "issuerGroup": iss["group"] if iss else None, "sector": iss["sector"] if iss else None,
            "rating": pos_rating(p)[0], **({"ratingInfo": ri} if (ri := pos_rating(p)[1]) else {}),
            "ccy": p.get("ccy"), "maturity": p.get("maturity"),
            "tenorY": p["tenor_y"] if p.get("tenor_y") is not None else _years(as_of, p.get("maturity")),
            "couponType": p.get("coupon_type"), "coupon": p.get("coupon"), "index": p.get("index_name"),
            "margin": p.get("margin"), "side": p.get("side"),
            "notional": p.get("notional"), "notionalRub": p.get("notional_rub"), "price": p.get("price"),
            "mv": p.get("mv"), "mvRub": p.get("mv_rub"), "ytm": p.get("ytm"), "spreadBp": p.get("spread_bp"),
            "mdur": p.get("mdur"), "sprDur": p.get("spr_dur"), "convexity": p.get("convexity"),
            "dv01": p.get("dv01"), "cs01": cs01, "ofzSpr01": p.get("ofz_spr01") or 0.0, "risk": risk,
            "fxDelta": {c: d for c, d in (p.get("fx_delta") or {}).items() if d}, "jtd": _zero(p.get("jtd")),
            "wal": p.get("wal"), "cpr": p.get("cpr"), "counterparty": p.get("counterparty"),
            "newToday": bool(p.get("new_today")),
            "gamma": gamma, **gamma_summary(gamma, p.get("mv_rub"), p.get("convexity"), cs01),
            "entity": books.get(p["book_id"], {}).get("entity", "—"), "portfolio": portfolio.get(pid),
            "pnl": {
                "carry": comp["carry"], "rolldown": comp["rolldown"], "rates": comp["rates"],
                "spread": comp["spread"],
                "fx": fxn, "funding": q.get("funding"),
                "convexity": comp["convexity"], "newTrades": comp["new_trades"], "other": comp["other"] - reported,
                "reported": reported,
                "fxGross": comp["fx_gross"], "fxFunding": comp["fx_funding"], "fxGrossCbr": gross_cbr,
                "fxFundingCbr": fund_cbr, "fxCbr": fxn_cbr, "explained": explained,
                "explainedCbr": explained_cbr, "fxBasis": fx_basis,
                "unexplained": 0.0 if official is None else official - explained_cbr, "official": official,
                "breakNote": q.get("break_note"), "available": True, "reason": None, "reasonText": None,
                "reasonDetail": None, "method": method, "methodText": METHOD_TEXT.get(method, method) if method else None,
                "flag": (q.get("extra") or {}).get("flag"),
            } if q else no_pnl(*_no_pnl_of(row["no_pnl"], run["prev_date"])),
        }
        if p.get("accrual_only"):
            pos.update({"accrualOnly": True, "purchaseDate": p.get("purchase_date"),
                        "purchasePrice": p.get("purchase_price"), "purchaseYield": p.get("purchase_yield"),
                        "contractRate": p.get("contract_rate")})
        if p.get("ocp_transfer") or (p.get("extra") or {}).get("ocp_transfer"):
            pos["ocpTransfer"] = True
        if p.get("instr_type") == "BOND":
            # shortcut: no bond quotes in the store (tdm md_bond_quote); the market proxy is a later slice
            pos.update({"instrumentId": p.get("instrument_id") or p.get("isin"), "accrued": p.get("accrued"),
                        "couponFreq": p.get("coupon_freq"), "yieldDate": p.get("yield_date"),
                        "spreadType": p.get("spread_type"), "spreadCurve": p.get("spread_curve"),
                        "quote": None})
        if p.get("extra") is not None:
            pos["extra"] = p["extra"]
        positions.append(pos)
    unvalued = con.execute("SELECT position_id, book_id, secid, quantity, reason FROM run_position WHERE run_id = %s"
                           " AND fact IS NULL ORDER BY ord", (rid,)).fetchall()
    inputs = {i["position_id"]: i for i in con.execute(
        f"SELECT position_id, {', '.join(INPUT_FIELDS)} FROM position_input WHERE set_id = ANY(%s)"
        " AND position_id = ANY(%s)", (run["position_set_ids"], [r["position_id"] for r in unvalued]))} \
        if unvalued else {}
    for r in unvalued:
        i = inputs.get(r["position_id"])
        positions.append(_not_valued(r, books, {k: v for k, v in i.items() if k != "position_id"} if i else None,
                                     portfolio.get(r["position_id"])))
    for c in dict.fromkeys(c for p in positions for c in p["risk"]):
        if c not in curves:  # a risk curve the run's reference lacks: shown without points, its risk never dropped
            curves[c] = {"name": c, "ccy": c.split()[0], "kind": "bond", "pts": [None] * len(B)}
            moves[c] = [0.0] * len(B)

    scenarios = [{"id": s["scenario_id"], "name": s["name"], "kind": s["kind"], "description": s["description"],
                  **s["definition"]} for s in con.execute(
        "SELECT * FROM scenario_def WHERE active ORDER BY ord, scenario_id")]
    hist = con.execute(f"SELECT run_id, business_date FROM run WHERE run_id IN ({DEFAULT_RUNS})"
                       " AND business_date < %s ORDER BY business_date DESC LIMIT %s",
                       (run["business_date"], HISTORY_DAYS - 1)).fetchall()[::-1]
    history, cum = [], 0.0
    for d, rows in [(h["business_date"].isoformat(), con.execute(
            "SELECT jsonb_build_object('dv01', fact->'dv01', 'cs01', fact->'cs01', 'extra',"
            " jsonb_build_object('risk_problem', fact->'extra'->'risk_problem')) AS fact, pnl FROM run_position"
            " WHERE run_id = %s AND fact IS NOT NULL", (h["run_id"],)).fetchall()) for h in hist] + [(as_of, prows)]:
        off, exp, dv01, cs01 = run_totals(rows)
        cum += off or 0.0
        # officialReceived (TDM#106): pnlOfficial is the official PnL only when one arrived; else it is the explained
        # PnL plus the FX basis (run_totals), and the pages must not call it official
        history.append({"date": d, "pnlOfficial": off, "pnlExplained": exp, "pnlCum": cum, "dv01": dv01,
                        "cs01": cs01, "officialReceived": official_received(rows)})
    # no previous market: prevDate null (R10), the funding accrual still over the previous weekday
    prev = run["prev_date"] or _prev_weekday(run["business_date"])
    zero_book = {"carry": 0.0, "fx": 0.0, "fxCbr": 0.0, "fxAllocated": 0.0, "inPositions": 0.0}
    funding = funding_view(ref.get("funding"), fx)
    completed = run["completed_at"]
    return {
        "asOf": as_of, "prevDate": run["prev_date"].isoformat() if run["prev_date"] else None,
        **pnl_state(run, positions),
        "accrualDays": max(1, (run["business_date"] - prev).days), "reportingCcy": "RUB",
        "BUCKETS": B, "PILLARS": list(RISK_PILLARS), "CURVES": curves, "CURVE_MOVES": moves,
        "FX": fx, "FX_MOVE_PCT": fx_move, "FX_CBR": fx_cbr, "FX_CBR_MOVE_PCT": fx_cbr_move,
        "FX_CBR_NEXT": fx_cbr_next, "FX_SOURCE": fx_source,
        "INDICES": indices, "ISSUERS": issuers, "RATING_ORDER": rating_order,
        "BOOKS": books, "SCENARIOS": scenarios, "scenarioResults": {},
        "JTD_RECOVERY": 0.0, "FUNDING_RULES": funding["rules"], "OCP_TRANSFERS": ocp_transfers(ref.get("ocp")),
        "CBR_MEETINGS": cbr_meetings(con, run["business_date"]), "CARRY_SCENARIOS": DEFAULT_CARRY,
        "CARRY_HORIZONS": CARRY_HORIZONS,
        "official": {"asOf": as_of, "received": any(p["pnl"]["official"] is not None for p in positions),
                     "basis": "CBR", "receivedAt": None},
        "positions": positions, "riskNotes": risk_notes(run),
        "funding": funding["lines"], "fundingPnl": funding["pnl"],
        "fundingPnlByBook": {**{b: dict(zero_book) for b in books}, **funding["by_book"]},
        "fundingStatus": funding["status"], "history": history,
        "source": {"kind": "db", "runId": rid, "runType": run["run_type"],
                   "publishedAt": completed.isoformat() if completed else None,
                   "libVersion": f"rub-fixed-income-core {run['core_version']}",
                   "marketSnapshot": run["market_snapshot_id"], "contractVersion": CONTRACT_VERSION,
                   # how the run values OFZ (valuation.bonds.OFZ_SCHEME): "ruonia_basis" since 0.2.7, "kbd_par" in
                   # 0.2.6 (and a run without RUONIA OIS quotes), null before
                   "ofzScheme": (run.get("lineage") or {}).get("ofz_scheme"),
                   # the unit of each curve's buckets (orchestrator.EodContext.curve_units, #171): null before
                   "curveUnits": (run.get("lineage") or {}).get("curve_units"),
                   **run_slice(run)},
    }


def cbr_meetings(con, as_of: date) -> list[dict[str, Any]]:
    """window.DESK ``CBR_MEETINGS``: the CBR key-rate meetings that take effect after ``as_of`` (``md_cbr_meeting``, the
    newest market snapshot's; ``scenario.carry.meetings``), in order, as the Scenario & PnL Lab's key-rate path reads
    them (``desk-util.js`` ``ksPath``, ``scenario-lab.html`` ``cyMeetings``: ``date`` the decision, ``effective`` from
    when it applies; ``estimated`` the hub's estimated date). Empty before the read model has the meetings."""
    return [{"date": m.date.isoformat(), "effective": m.effective.isoformat(), "estimated": m.estimated}
            for m in meetings(meeting_rows(con, None), as_of)]


def ocp_transfers(block: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The run's OCP transfers (``publish.ocp_run`` block ``transfers``, B2) as window.DESK's ``OCP_TRANSFERS``,
    in tdm's shape (``run_ocp_transfer``, ``prototypes/data.js``): one row per **source** book (its ``ocp_to`` set in
    the books) and currency with a transfer deal; ``amount`` the OCP moved, signed as the OCP before the transfer
    (= -the deals' amount); the receiving book's mirror deal has no row. A run before B2, a book with no deal or
    without ``ocp_to``: none."""
    return [{"from": t["book_id"], "to": t["ocp_to"], "ccy": t["ccy"], "amount": -t["transferred"],
             "before": t["before"], "rate": t.get("rate"), "valueDate": t.get("value_date")}
            for t in (block or {}).get("transfers") or [] if t.get("deals") and t.get("source") and t.get("ocp_to")]


FUNDING_TYPES = {"POS": "Фондирование позиции (MV)", "HTM": "FTP HTM (контракт)", "CASH": "Остаток ДС",
                 "ENTITY_CASH": "Остаток ДС юрлица", "REPO": "РЕПО"}
"""A funding line's ``type`` in the Desk Monitor (tdm's ``funding_deal.type``)."""


def funding_view(block: dict[str, Any] | None, fx: dict[str, float]) -> dict[str, Any]:
    """The run's funding block (``run_reference`` kind ``funding``) as window.DESK's ``funding``, ``fundingPnl``,
    ``fundingPnlByBook``, ``FUNDING_RULES`` and ``fundingStatus``. A run before B1 has no block: state
    ``not_computed``; a run whose adapter gave no rules: ``not_configured``; both with no lines and zero book sums,
    as tdm shows a run without funding deals, and the state saying why."""
    if not block:
        block = {"status": NOT_COMPUTED, "text": STATUS_TEXT[NOT_COMPUTED]}
    lines, by_book, pnl = [], {}, {"carry": 0.0, "fx": 0.0, "fxCbr": 0.0}
    liab = (block.get("fx_liability") or {}).get("positions") or {}
    for x in block.get("lines") or []:
        in_pos = bool(x.get("in_position"))
        # a bond's currency liability (publish.fx_liability): already inside the position's fxDelta, which is net of it,
        # so the line's fxDelta stays empty (the pages add the lines' fxDelta to the positions': it would count twice)
        lb = liab.get(x.get("position_id") or "") if x["kind"] == "POS" else None
        lb = lb if lb and lb.get("state") == "computed" else None
        fxa = (lb.get("fx_funding") or 0.0) if lb and in_pos else 0.0
        fxa_cbr = lb.get("fx_funding_cbr") if lb and in_pos and lb.get("fx_funding_cbr") is not None else fxa
        lines.append({
            "id": x["key"], "book": x["book_id"], "kind": x["kind"], "positionId": x.get("position_id"),
            "type": FUNDING_TYPES.get(x["kind"], x["kind"]), "ccy": x["ccy"], "amount": x["body_ccy"],
            "amountRub": x["body_ccy"] * fx.get(x["ccy"], 1.0), "index": None, "spreadBp": None, "fixed": None,
            "rate": x["rate_pct"], "rateExpr": x["rate_expr"], "benchmark": None,
            "dayBasis": 360 if x.get("day_basis") == "ACT/360" else 365, "start": x["first_day"],
            "maturity": None, "tenorY": 1 / 365, "counterparty": None, "collateral": None, "fxDelta": {},
            **({"fxLiability": {lb["ccy"]: lb["amount_ccy"]}} if lb else {}),
            "pnl": {"carry": x["interest_rub"], "fx": fxa, "fxCbr": fxa_cbr, "fxGross": fxa, "fxGrossCbr": fxa_cbr,
                    "fxAllocated": fxa, "inPosition": in_pos},
            **({"ruleNote": x["note"]} if x.get("note") else {}),
            "rule": x.get("rule_id"), "ruleSource": x.get("rule_source"), "defaultRate": bool(x.get("default_rate")),
        })
        b = by_book.setdefault(x["book_id"], {"carry": 0.0, "fx": 0.0, "fxCbr": 0.0, "fxAllocated": 0.0,
                                              "inPositions": 0.0})
        if in_pos:
            b["inPositions"] += x["interest_rub"]
            b["fxAllocated"] += fxa
            continue
        b["carry"] += x["interest_rub"]
        pnl["carry"] += x["interest_rub"]
    rules = []
    for r in block.get("rules") or []:
        if r.get("active", True) is False or r.get("source") == "default":  # the default: status.defaultRate
            continue
        x = {"bookType": r.get("book_type"), "book": r.get("book_id"), "entity": r.get("entity"),
             "instrument": r.get("instrument_id"), "ccy": r.get("ccy"), "index": r.get("index_code"),
             "spreadBp": float(r["spread_bp"]) if r.get("spread_bp") is not None else None,
             "fixed": float(r["fixed_rate_pct"]) if r.get("fixed_rate_pct") is not None else None,
             "note": r.get("rule_id"), "basis": r.get("basis"), "rateKind": r.get("rate_kind"),
             "validFrom": r.get("valid_from"), "source": r.get("source"),
             "securityClass": r.get("security_class"), "kind": r.get("kind"), "override": r.get("override")}
        rules.append({k: v for k, v in x.items() if v is not None and v != ""})
    totals = block.get("totals")
    status = {
        "state": block.get("status"), "text": block.get("text"), "period": block.get("period"),
        "total": totals["total"] if totals else None, "inPositions": totals["in_positions"] if totals else None,
        "bookLevel": totals["book_level"] if totals else None,
        "byBook": {b: {"total": v["total"], "inPositions": v["in_positions"], "bookLevel": v["book_level"],
                       "problems": v.get("problems", 0), "base": v.get("base"), "writeOff": v.get("write_off")}
                   for b, v in (block.get("by_book") or {}).items()},
        "byEntity": block.get("by_entity") or {},
        "problems": [{"kind": p["kind"], "ref": p["ref"], "book": p.get("book_id"), "reason": p["reason"]}
                     for p in block.get("problems") or []],
        "notes": block.get("notes") or [], "provisional": (block.get("policy") or {}).get("provisional") or [],
        "defaultRate": block.get("default_rate"),
    }
    return {"lines": lines, "pnl": pnl, "by_book": by_book, "rules": rules, "status": status}
