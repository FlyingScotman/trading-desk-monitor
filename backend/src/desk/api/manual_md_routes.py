"""Manually entered market data (TDM#652): the CDS curves of the monitor C22 «Ручные рыночные данные».

Storage, the quotation convention and the curve in force on a date are in ``desk.marketdata.manual_cds`` (migration
0028, append-only revisions with who and when). Routes, all under ``/api/v1/market-data/manual/cds``:

* ``GET /curves?date=``: the curves in force on a date (a name's own entry of that date or one carried from an earlier
  date), with the reference names of the date's run's CDS rows (``positions``, the report's ``x_issuer``);
* ``GET /curve?name=&date=``: one name's curve in force on a date (404 without one);
* ``GET /history?name=``: every revision of a name, newest first;
* ``PUT /curve``: set (or, with ``clear``, withdraw) a name's curve from a date: the whole curve, tenors left empty are
  empty;
* ``POST /copy``: enter on a date the curves in force before it (or on ``from_date``), for all names or the ones named.

Who may change (``desk.api.auth.alias_guard``, as the alias of a security): with sign-in off, loopback clients only,
recorded as ``changed_by = 'local'``; with it on, the trader or admin role, recorded with the user's name. Every change
is also in the audit log (``manual_cds``). A request that sets what is already in force adds no revision.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..marketdata import manual_cds as M
from . import auth
from .app import ApiError, Con, ConRW

router = APIRouter(tags=["manual market data"])
BASE = "/api/v1/market-data/manual/cds"
AUDIT_ACTION = "manual_cds"


class CdsCurve(BaseModel):
    rev_id: int
    name_key: str = Field(description="The identity of the reference name (normalised: case, spaces)")
    name: str = Field(description="The reference name as written")
    valid_from: Date = Field(description="The curve holds from this date until a later one is entered")
    entered_on_date: bool | None = Field(None, description="The curve's valid_from is the date asked for (else it is "
                                                           "carried from an earlier date); null: no date asked")
    cleared: bool = Field(False, description="History only: the name was withdrawn from valid_from on")
    spreads_bp: dict[str, float | None] = Field(description="Par coupon spread in basis points by tenor "
                                                            "(3M 6M 1Y 2Y 3Y 5Y 7Y 10Y); null: not given")
    recovery: float = Field(description="Recovery rate, a fraction (0.25 is 25 %)")
    ccy: str = Field(description="Currency of the quotes")
    changed_by: str = Field(description="Who entered it: the user, or 'local' (this computer, sign-in off)")
    changed_at: datetime
    manual: bool = Field(True, description="Entered by hand (the only source until internal systems give some)")


class CdsPositionName(BaseModel):
    name: str = Field(description="The reference name of the CDS rows: the report's x_issuer, else the deal's name")
    name_key: str
    deals: list[str] = Field(description="The names of the deals (source_instrument_name) of these rows, up to 5")
    positions: int = Field(description="CDS rows of the run with this name")
    has_curve: bool = Field(description="A curve is in force on the date for this name")


class CdsCurveList(BaseModel):
    date: Date
    run_id: int | None = Field(description="The run of the date whose CDS rows are listed (null: none)")
    tenors: list[str]
    default_recovery: float
    default_ccy: str
    max_spread_bp: float
    curves: list[CdsCurve] = Field(description="By name")
    positions: list[CdsPositionName] = Field(description="CDS names of the date's run, by name")


class CdsHistory(BaseModel):
    name_key: str
    revisions: list[CdsCurve] = Field(description="Newest first")


class CdsCurveIn(BaseModel):
    name: str = Field(description=f"The reference name, at most {M.MAX_NAME} characters")
    date: Date = Field(description="The curve holds from this date (2000-01-01..2100-12-31)")
    spreads_bp: dict[str, float | None] = Field(default_factory=dict, description="Par spread in bp by tenor; a tenor "
                                                "left out or null is empty; at least one is needed unless clear")
    recovery: float | None = Field(None, description="Fraction from 0 up to (not including) 1; null: 0.25")
    ccy: str | None = Field(None, description="Currency code; null: USD")
    clear: bool = Field(False, description="Withdraw the name from this date on instead of setting a curve")


class CdsPutOut(BaseModel):
    changed: bool = Field(description="False when the request set what was already in force (no revision)")
    curve: CdsCurve | None = Field(description="In force on the date afterwards; null: none (withdrawn)")


class CdsCopyIn(BaseModel):
    to_date: Date = Field(description="Enter the curves on this date")
    from_date: Date | None = Field(None, description="Copy what was in force on this date; null: what was in force "
                                                     "before to_date")
    names: list[str] | None = Field(None, description="Only these names; null: every name that has a curve")


class CdsSkipped(BaseModel):
    name_key: str
    name: str
    why: str


class CdsCopyOut(BaseModel):
    to_date: Date
    copied: list[CdsCurve] = Field(description="The new entries of to_date")
    skipped: list[CdsSkipped]


def _out(c: M.Curve, on: Date | None = None) -> dict:
    return {"rev_id": c.rev_id, "name_key": c.name_key, "name": c.name, "valid_from": c.valid_from,
            "entered_on_date": None if on is None else c.valid_from == on, "cleared": c.cleared,
            "spreads_bp": c.spreads_bp, "recovery": c.recovery, "ccy": c.ccy, "changed_by": c.changed_by,
            "changed_at": c.changed_at, "manual": True}


def _refuse(e: M.CurveError) -> ApiError:
    return ApiError(422, "invalid_parameter", str(e))


def _cds_names_of(con, on: Date) -> tuple[int | None, list[dict]]:
    """The run shown for ``on`` (as ``store.CURRENT_RUNS``: the default EOD run, else the latest intraday one) and the
    reference names of its CDS rows (the adapter's product group ``cds``): (run_id, [{name, positions, deals}]). The name
    is the report's ``x_issuer`` (in ``position_input.x``, as for an unmapped bond), else the row's own name."""
    run = con.execute(
        "SELECT run_id, position_set_ids FROM run WHERE business_date = %s AND status = 'complete'"
        " AND (run_type = 'EOD' AND cut_type IN ('eod_final', 'eod_preliminary') OR run_type = 'INTRADAY')"
        " ORDER BY run_type = 'EOD' DESC, cut_type = 'eod_final' DESC, completed_at DESC, run_id DESC LIMIT 1",
        (on,)).fetchone()
    if run is None:
        return None, []
    rows = con.execute(
        "SELECT COALESCE(NULLIF(btrim(x->>'x_issuer'), ''), NULLIF(btrim(source_instrument_name), ''),"
        " source_instrument_id) AS name, count(*) AS n,"
        " array_remove(array_agg(DISTINCT NULLIF(btrim(source_instrument_name), '')), NULL) AS deals"
        " FROM position_input WHERE set_id = ANY(%s) AND lower(x->>'x_product_group') = 'cds'"
        " AND COALESCE(NULLIF(btrim(x->>'x_issuer'), ''), NULLIF(btrim(source_instrument_name), ''),"
        " source_instrument_id) IS NOT NULL GROUP BY 1 ORDER BY 1", (list(run["position_set_ids"]),)).fetchall()
    return run["run_id"], [{"name": r["name"], "positions": r["n"], "deals": r["deals"]} for r in rows]


@router.get(BASE + "/curves", response_model=CdsCurveList)
def list_curves(con: Con, on: Annotated[Date, Query(alias="date", description="The date")]):
    curves = M.curves_on(con, on)
    have = {c.name_key for c in curves}
    run_id, names = _cds_names_of(con, on)
    by_key: dict[str, dict] = {}
    for n in names:  # two spellings of one name are one name
        try:
            name = M.clean_name(n["name"])
        except M.CurveError:
            continue
        k = M.name_key(name)
        e = by_key.setdefault(k, {"name": name, "name_key": k, "positions": 0, "deals": [], "has_curve": k in have})
        e["positions"] += n["positions"]
        e["deals"] = sorted({*e["deals"], *n["deals"]})[:5]
    return {"date": on, "run_id": run_id, "tenors": list(M.TENORS), "default_recovery": M.DEFAULT_RECOVERY,
            "default_ccy": M.DEFAULT_CCY, "max_spread_bp": M.MAX_SPREAD_BP, "curves": [_out(c, on) for c in curves],
            "positions": sorted(by_key.values(), key=lambda e: e["name_key"])}


@router.get(BASE + "/curve", response_model=CdsCurve)
def get_curve(con: Con, name: Annotated[str, Query(description="The reference name (any spelling)")],
              on: Annotated[Date, Query(alias="date", description="The date")]):
    c = M.curve_on(con, on, name)
    if c is None:
        raise ApiError(404, "not_found", "no CDS curve in force for this name on this date")
    return _out(c, on)


@router.get(BASE + "/history", response_model=CdsHistory)
def curve_history(con: Con, name: Annotated[str, Query(description="The reference name (any spelling)")]):
    return {"name_key": M.name_key(name), "revisions": [_out(c) for c in M.history(con, name)]}


@router.put(BASE + "/curve", response_model=CdsPutOut, dependencies=[Depends(auth.alias_guard)],
            description="Set a reference name's CDS curve from a date (the whole curve), or withdraw the name from the "
                        "date with clear. A trader or admin (sign-in off: this computer only); a change is recorded "
                        "with who and when, a request that changes nothing is not.")
def put_curve(con: ConRW, body: CdsCurveIn, request: Request):
    who = auth.actor(request)
    try:
        with con.transaction():
            curve, changed = M.put_curve(con, body.date, body.name, None if body.clear else body.spreads_bp,
                                         recovery=body.recovery, ccy=body.ccy, who=who)
            if changed:
                auth.audit(con, who, AUDIT_ACTION, M.name_key(body.name), ip=auth.client_ip(request),
                           detail={"date": body.date.isoformat(), "clear": body.clear,
                                   "spreads_bp": None if body.clear else curve.spreads_bp if curve else None,
                                   "recovery": curve.recovery if curve else None, "ccy": curve.ccy if curve else None})
    except M.CurveError as e:
        raise _refuse(e) from None
    return {"changed": changed, "curve": _out(curve, body.date) if curve else None}


@router.post(BASE + "/copy", response_model=CdsCopyOut, dependencies=[Depends(auth.alias_guard)],
             description="Enter on a date the curves in force before it (or on from_date), for every name or the ones "
                         "named, in one step. A name that already has its own curve on the date is left as it is.")
def copy_curves(con: ConRW, body: CdsCopyIn, request: Request):
    who = auth.actor(request)
    try:
        with con.transaction():
            r = M.copy_curves(con, body.to_date, from_date=body.from_date, names=body.names, who=who)
            if r.copied:
                auth.audit(con, who, AUDIT_ACTION, "copy", ip=auth.client_ip(request),
                           detail={"to_date": body.to_date.isoformat(),
                                   "from_date": body.from_date.isoformat() if body.from_date else None,
                                   "names": [c.name_key for c in r.copied]})
    except M.CurveError as e:
        raise _refuse(e) from None
    return {"to_date": r.to_date, "copied": [_out(c, body.to_date) for c in r.copied],
            "skipped": [{"name_key": k, "name": n, "why": w} for k, n, w in r.skipped]}
