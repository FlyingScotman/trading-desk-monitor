"""Short names of securities (TDM#776): a name a user gives a security by its ISIN for the narrow columns («Кор. имя») and
the card's «Короткое имя», kept in the store (``instrument_short_name_rev``, migration 0026) apart from the full name
(``alias_routes``, the alias). Without one the pages show the exchange's short name (the hub's MOEX SHORTNAME, in a run's
rows as ``fact.extra.short_name``).

Same shape and rules as the alias: not part of a run's frozen rows (``GET /api/v1/runs/{id}/positions`` joins it at read
time as ``Position.short_name``, old runs too; the pages take ``GET /api/v1/instruments/short-names``), an empty name
clears it (the exchange's short name again), every change is an append-only revision with who and when
(``GET /api/v1/instruments/short-names/{isin}/history``), at most ``MAX_SHORT_NAME`` characters, recorded in the audit
log (``short_name``). Who may change it: ``desk.api.auth.alias_guard`` (with sign-in off, this computer only; with it
on, the trader or admin role from any computer, the viewer role reads only).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from . import auth
from .alias_routes import normalize_isin, normalize_name
from .app import Con, ConRW

router = APIRouter(tags=["instruments"])

MAX_SHORT_NAME = 40


class InstrumentShortName(BaseModel):
    isin: str
    short_name: str = Field(description="The short name to show instead of the exchange's")
    changed_by: str = Field(description="Who set it: the user, or 'local' (this computer, sign-in off)")
    changed_at: datetime


class ShortNameList(BaseModel):
    short_names: list[InstrumentShortName] = Field(description="The short names in force, by ISIN")


class ShortNameIn(BaseModel):
    isin: str = Field(description="The security's ISIN (case and surrounding spaces ignored)")
    short_name: str | None = Field(None, description=f"The short name, at most {MAX_SHORT_NAME} characters; null or "
                                                     "blank clears it (the exchange's short name again)")


class ShortNameState(BaseModel):
    isin: str
    short_name: str | None = Field(description="In force after the request; null: none (the exchange's is shown)")
    changed_by: str | None = Field(description="Of the latest revision; null: the ISIN never had one")
    changed_at: datetime | None
    changed: bool = Field(description="False when the request set what was already in force (no revision)")


class ShortNameRevision(BaseModel):
    rev_id: int
    short_name: str | None = Field(description="null: the short name was cleared")
    changed_by: str
    changed_at: datetime


class ShortNameHistory(BaseModel):
    isin: str
    revisions: list[ShortNameRevision] = Field(description="Newest first")


def normalize_short_name(raw: str | None) -> str | None:
    return normalize_name(raw, MAX_SHORT_NAME, "the short name")


def _latest(con, isin: str) -> dict | None:
    return con.execute("SELECT short_name, changed_by, changed_at FROM instrument_short_name_rev WHERE isin = %s"
                       " ORDER BY rev_id DESC LIMIT 1", (isin,)).fetchone()


@router.get("/api/v1/instruments/short-names", response_model=ShortNameList)
def list_short_names(con: Con):
    rows = con.execute("SELECT isin, short_name, changed_by, changed_at FROM instrument_short_name"
                       " ORDER BY isin").fetchall()
    return {"short_names": rows}


@router.get("/api/v1/instruments/short-names/{isin}/history", response_model=ShortNameHistory)
def short_name_history(con: Con, isin: Annotated[str, Path(description="The ISIN")]):
    isin = normalize_isin(isin)
    rows = con.execute("SELECT rev_id, short_name, changed_by, changed_at FROM instrument_short_name_rev"
                       " WHERE isin = %s ORDER BY rev_id DESC", (isin,)).fetchall()
    return {"isin": isin, "revisions": rows}


@router.put("/api/v1/instruments/short-name", response_model=ShortNameState,
            dependencies=[Depends(auth.alias_guard)],
            description="Set or clear (blank or null) the short name of an ISIN. A trader or admin (sign-in off: this "
                        "computer only); a change is recorded with who and when, a request that changes nothing is "
                        "not.")
def put_short_name(con: ConRW, body: ShortNameIn, request: Request):
    isin, name = normalize_isin(body.isin), normalize_short_name(body.short_name)
    with con.transaction():
        con.execute("SELECT pg_advisory_xact_lock(hashtext('instrument_short_name:' || %s))", (isin,))
        last = _latest(con, isin)
        if (last["short_name"] if last else None) == name:
            return {"isin": isin, "short_name": name, "changed_by": last and last["changed_by"],
                    "changed_at": last and last["changed_at"], "changed": False}
        row = con.execute("INSERT INTO instrument_short_name_rev (isin, short_name, changed_by) VALUES (%s, %s, %s)"
                          " RETURNING changed_by, changed_at", (isin, name, auth.actor(request))).fetchone()
        auth.audit(con, row["changed_by"], "short_name", isin, ip=auth.client_ip(request),
                   detail={"short_name": name, "was": last["short_name"] if last else None})
    return {"isin": isin, "short_name": name, **row, "changed": True}
