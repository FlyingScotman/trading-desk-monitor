"""The monitors' run (``prototypes/concepts/``, ``lib/real.js``): ``/concepts-run.js`` sets ``window.CONCEPTS_RUN``,
``/api/v1/concepts/run`` is the same as JSON (TDM#289, step 1 of removing window.DESK, TDM#107).

``assemble`` is ``compat.assemble`` cut down to what ``lib/real.js`` reads, in the same shape and with the same values:
the run's dates and PnL state (R10: ``pnlAvailable`` false with ``pnlReasonText``), the buckets, curves and their
moves, the FX rates (market and CBR, their moves and the market rate's source, R26), the indices, issuers and books,
``riskNotes`` (R14, R15), ``fundingStatus`` (B1), ``source`` (the run, its market slice R11, ``ofzScheme`` #32,
``curveUnits`` #171) and ``positions`` with the fields the pages read (``POSITION_KEYS``; a bond's terms in
``BOND_KEYS``, a position's PnL in ``PNL_KEYS_READ``). A position the run did not value is listed after the valued
ones with ``notValued``, no PV and no risk, its reason in ``extra`` (as compat). What the tdm prototype's screens read
and the monitors do not (history, scenarios, OCP transfers, funding lines, gamma, the CBR meetings: those are in
``concepts.cbr``'s ``schedule``) is not here.

No run: ``window.CONCEPTS_RUN_EMPTY`` = ``{reason, message}`` (``no_runs``, or ``not_found`` for the date or run asked
for), as window.DESK_EMPTY; the JSON route answers 404 with the same reason in ``details``."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from rub_fixed_income import RISK_BUCKETS, RISK_PILLARS

from ..marketdata import ratings
from ..pnl.reasons import METHOD_TEXT, REPORTED_METHOD, RUN_NO_POSITION_PNL, RUN_NO_PREV_MARKET, no_pnl_text
from ..publish.funding_run import NOT_COMPUTED, STATUS_TEXT
from .runs import PNL_KEYS, risk_notes, run_slice

RUB = {"ccy": "RUB", "rate": 1.0, "move_pct": 0.0, "rate_cbr": 1.0, "move_pct_cbr": 0.0}
"""The rouble's own rate row, first in ``FX`` (as compat)."""

POSITION_KEYS = (
    "id", "book", "assetClass", "segment", "instrType", "name", "isin", "issuer", "issuerGroup", "sector", "rating",
    "ratingInfo", "ccy", "maturity", "tenorY", "couponType", "coupon", "index", "margin", "notional", "notionalRub",
    "price", "mv", "mvRub", "ytm", "spreadBp", "mdur", "sprDur", "cs01", "ofzSpr01", "risk", "fxDelta", "counterparty",
    "newToday", "entity", "portfolio", "pnl", "accrualOnly", "ocpTransfer", "extra", "notValued")
"""A position's fields ``lib/real.js`` reads (``ratingInfo``, ``accrualOnly``, ``ocpTransfer``, ``extra`` and
``notValued`` only when there is one), plus a bond's ``BOND_KEYS``."""
BOND_KEYS = ("instrumentId", "accrued", "couponFreq", "yieldDate", "spreadCurve")
PNL_AMOUNTS = ("carry", "rolldown", "rates", "spread", "fx", "funding", "convexity", "newTrades", "other", "reported",
               "explained")
PNL_KEYS_READ = (*PNL_AMOUNTS, "available", "reasonText", "method", "methodText")
"""A position's ``pnl`` as the pages read it: the amounts (all null without PnL, R10), whether it has one, why not and
by which method."""
NOT_VALUED_KIND = {"otc": ("OTC-деривативы", "Деривативы"), "other": ("Прочее", "Прочее"), "fx": ("Валюта", "FX")}
"""asset_class of an unvalued report row -> (asset class, segment), as compat."""
INPUT_FIELDS = ("asset_class", "ccy", "source_instrument_id", "source_instrument_name", "isin", "deal_id",
                "value_date", "maturity_date", "amount_ccy", "face_amount_ccy", "face_outstanding_ccy",
                "mark_price_pct", "mv_rub", "risk_dv01_rub", "risk_cs01_rub", "src", "x")
"""What an unvalued position shows of its report row (position_input of the run's position sets)."""


def _years(d0: str, d1: str | None) -> float | None:
    try:
        return (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _json(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date) else float(v) if isinstance(v, Decimal) else v


def no_pnl(code: str) -> dict[str, Any]:
    """A position's ``pnl`` without PnL (R10): every amount null, not 0, and why (``pnl.reasons``; the pages read
    only its text, not compat's ``reasonDetail``)."""
    return {**dict.fromkeys(PNL_AMOUNTS), "available": False, "reasonText": no_pnl_text(code), "method": None,
            "methodText": None}


def _pnl(q: dict[str, Any]) -> dict[str, Any]:
    """A position's ``pnl`` from its components (``run_position.pnl``): the explained PnL is their sum; a PnL by the
    reports (``REPORTED_METHOD``) is in ``reported``, not in ``other``."""
    comp = {k: q.get(k) or 0.0 for k in PNL_KEYS}
    method = (q.get("extra") or {}).get("method")
    reported = comp["other"] if method == REPORTED_METHOD else 0.0
    return {"carry": comp["carry"], "rolldown": comp["rolldown"], "rates": comp["rates"], "spread": comp["spread"],
            "fx": comp["fx_gross"] + comp["fx_funding"], "funding": q.get("funding"), "convexity": comp["convexity"],
            "newTrades": comp["new_trades"], "other": comp["other"] - reported, "reported": reported,
            "explained": sum(comp.values()), "available": True, "reasonText": None, "method": method,
            "methodText": METHOD_TEXT.get(method, method) if method else None}


def _no_pnl_code(stored: dict[str, Any] | None, prev_date: date | None) -> str:
    """Why a valued position has no PnL: run_position.no_pnl, else the run's own reason."""
    if stored:
        return stored.get("reason") or "not_computed"
    return RUN_NO_PREV_MARKET if prev_date is None else "not_computed"


def funding_status(block: dict[str, Any] | None) -> dict[str, Any]:
    """``fundingStatus`` of the run's funding block (``publish.funding_run``, B1): its state and text, totals by book
    and entity, problems; a run before B1: ``not_computed``."""
    if not block:
        block = {"status": NOT_COMPUTED, "text": STATUS_TEXT[NOT_COMPUTED]}
    totals = block.get("totals")
    return {
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


def _not_valued(r: dict[str, Any], books: dict[str, dict], x: dict[str, Any] | None,
                portfolio: str | None) -> dict[str, Any]:
    """A run_position without fact: visible, PV empty, nothing in any total; ``x`` its report row."""
    x = {k: _json(v) for k, v in (x or {}).items() if v is not None}
    kind = (x.get("asset_class") or "").lower()
    aclass, segment = (None, None) if kind in ("", "bond") else NOT_VALUED_KIND.get(kind, NOT_VALUED_KIND["other"])
    return {
        "id": r["position_id"], "book": r["book_id"], "assetClass": aclass, "segment": segment, "instrType": None,
        "name": x.get("source_instrument_name") or x.get("source_instrument_id") or r["secid"] or r["position_id"],
        "isin": x.get("isin"), "issuer": None, "issuerGroup": None, "sector": None, "rating": None,
        "ccy": x.get("ccy"), "maturity": x.get("maturity_date") or x.get("value_date"), "tenorY": None,
        "notional": None, "notionalRub": None, "price": None, "mv": None, "mvRub": None, "cs01": 0.0, "ofzSpr01": 0.0,
        "risk": {}, "fxDelta": {}, "entity": books.get(r["book_id"], {}).get("entity", "—"), "portfolio": portfolio,
        "newToday": False, "notValued": True, "pnl": no_pnl("not_valued"),
        "extra": {"quality": "not_valued", "valuation": "not_valued", "reason": r["reason"],
                  "quantity": r["quantity"], **({"reported": x} if x else {})},
    }


def assemble(con, run: dict[str, Any]) -> dict[str, Any]:
    """``window.CONCEPTS_RUN`` of a run (the module's docstring)."""
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

    prows = con.execute("SELECT position_id, fact, pnl, no_pnl FROM run_position WHERE run_id = %s"
                        " AND fact IS NOT NULL ORDER BY ord", (rid,)).fetchall()
    fxr = list({"RUB": RUB, **{r["ccy"]: r for r in ref.get("fx", [])}}.values())
    # a run that published only USD, EUR and CNY (before R27): another currency's rate is the one its bonds were
    # valued at (``extra.fx_rate``), its move unknown (0)
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
        if r.get("rating_info"):  # the desk's rating view (R25)
            x["ratingInfo"] = r["rating_info"]
        if r.get("is_gov"):
            x["gov"] = True
        if r.get("is_quasi"):
            x["quasi"] = True
        issuers[disp[r["issuer"]]] = x

    # the report's Portfolio column (x_portfolio) of every position, valued or not
    portfolio = {r["position_id"]: r["portfolio"] for r in con.execute(
        "SELECT position_id, x->>'x_portfolio' AS portfolio FROM position_input WHERE set_id = ANY(%s)"
        " AND x ? 'x_portfolio'", (run["position_set_ids"] or [],))}

    positions = []
    for row in prows:
        p, q = row["fact"], row["pnl"]
        pid = row["position_id"]
        iname = disp.get(p["issuer"], p["issuer"]) if p.get("issuer") else None
        iss = issuers.get(iname) if iname else None
        own = (p.get("extra") or {}).get("rating") if isinstance(p.get("extra"), dict) else None
        rating_info = own or (iss or {}).get("ratingInfo")  # a bond's own desk rating (R25), else its issuer's
        risk: dict[str, list[float]] = {}
        for curve, bk in (p.get("risk") or {}).items():
            for b, dv in bk.items():
                if dv:
                    risk.setdefault(curve, [0.0] * len(B))[bidx[b]] = dv
        pos = {
            "id": pid, "book": p["book_id"], "assetClass": p.get("asset_class"), "segment": p.get("segment"),
            "instrType": p.get("instr_type"), "name": p.get("name"), "isin": p.get("isin"), "issuer": iname,
            "issuerGroup": iss["group"] if iss else None, "sector": iss["sector"] if iss else None,
            "rating": ratings.position_rating(p, iss), **({"ratingInfo": rating_info} if rating_info else {}),
            "ccy": p.get("ccy"), "maturity": p.get("maturity"),
            "tenorY": p["tenor_y"] if p.get("tenor_y") is not None else _years(as_of, p.get("maturity")),
            "couponType": p.get("coupon_type"), "coupon": p.get("coupon"), "index": p.get("index_name"),
            "margin": p.get("margin"), "notional": p.get("notional"), "notionalRub": p.get("notional_rub"),
            "price": p.get("price"), "mv": p.get("mv"), "mvRub": p.get("mv_rub"), "ytm": p.get("ytm"),
            "spreadBp": p.get("spread_bp"), "mdur": p.get("mdur"), "sprDur": p.get("spr_dur"),
            "cs01": p.get("cs01") or 0.0, "ofzSpr01": p.get("ofz_spr01") or 0.0, "risk": risk,
            "fxDelta": {c: d for c, d in (p.get("fx_delta") or {}).items() if d},
            "counterparty": p.get("counterparty"), "newToday": bool(p.get("new_today")),
            "entity": books.get(p["book_id"], {}).get("entity", "—"), "portfolio": portfolio.get(pid),
            "pnl": _pnl(q) if q else no_pnl(_no_pnl_code(row["no_pnl"], run["prev_date"])),
        }
        if p.get("accrual_only"):
            pos["accrualOnly"] = True
        if p.get("ocp_transfer") or (p.get("extra") or {}).get("ocp_transfer"):
            pos["ocpTransfer"] = True
        if p.get("instr_type") == "BOND":
            pos.update({"instrumentId": p.get("instrument_id") or p.get("isin"), "accrued": p.get("accrued"),
                        "couponFreq": p.get("coupon_freq"), "yieldDate": p.get("yield_date"),
                        "spreadCurve": p.get("spread_curve")})
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

    n_pnl = sum(p["pnl"]["available"] for p in positions)
    code = RUN_NO_PREV_MARKET if run["prev_date"] is None else RUN_NO_POSITION_PNL if not n_pnl else None
    # no previous market: prevDate null (R10), the accrual still over the previous weekday
    prev = run["prev_date"] or _prev_weekday(run["business_date"])
    completed = run["completed_at"]
    lineage = run.get("lineage") or {}
    return {
        "asOf": as_of, "prevDate": run["prev_date"].isoformat() if run["prev_date"] else None,
        "pnlAvailable": code is None, "pnlReasonText": no_pnl_text(code) if code else None,
        "accrualDays": max(1, (run["business_date"] - prev).days),
        "BUCKETS": B, "PILLARS": list(RISK_PILLARS), "CURVES": curves, "CURVE_MOVES": moves,
        "FX": fx, "FX_MOVE_PCT": fx_move, "FX_CBR": fx_cbr, "FX_CBR_MOVE_PCT": fx_cbr_move, "FX_SOURCE": fx_source,
        "INDICES": {r["index_name"]: r["value"] for r in ref.get("indices", [])},
        "ISSUERS": issuers, "BOOKS": books, "riskNotes": risk_notes(run),
        "fundingStatus": funding_status(ref.get("funding")), "positions": positions,
        "source": {"runId": rid, "runType": run["run_type"],
                   "publishedAt": completed.isoformat() if completed else None,
                   "ofzScheme": lineage.get("ofz_scheme"), "curveUnits": lineage.get("curve_units"),
                   **{k: v for k, v in run_slice(run).items() if k != "prevCutType"}},
    }


def empty(as_of: date | None, run_id: int | None) -> dict[str, str]:
    """``window.CONCEPTS_RUN_EMPTY`` when no run answers: ``no_runs``, or ``not_found`` for the date or run asked for."""
    msg = f"прогон {run_id} не найден" if run_id else "нет опубликованных прогонов" + (f" на {as_of}" if as_of else "")
    return {"reason": "not_found" if as_of or run_id else "no_runs", "message": msg}


SCRIPT_PREFIX = b"window.CONCEPTS_RUN = "
"""``/concepts-run.js`` is this, ``/api/v1/concepts/run``'s JSON body as it is (``app.run_script``) and ``;\\n``."""


def empty_script(missing: dict[str, str]) -> str:
    """``/concepts-run.js`` without a run: ``window.CONCEPTS_RUN_EMPTY`` (``empty``)."""
    return (f"/* {missing['message']}: empty state */\n"
            f"window.CONCEPTS_RUN_EMPTY = {json.dumps(missing, ensure_ascii=False, default=str)};\n")
