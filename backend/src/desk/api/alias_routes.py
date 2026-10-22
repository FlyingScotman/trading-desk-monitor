"""Instrument aliases (batch 1, item 3): a display name for a security, by ISIN, kept in the store
(``instrument_alias_rev``, migration 0014) and shown wherever the security's name is.

The alias is not part of a run's frozen rows: ``GET /api/v1/runs/{id}/positions`` joins it at read time
(``Position.alias``), and the pages take ``GET /api/v1/instruments/aliases`` and put it in place of the exchange
name. An empty alias clears it; every change is an append-only revision with who and when
(``GET /api/v1/instruments/aliases/{isin}/history``).

shortcut: no users yet (05 §6), so a change is a settings-like write: loopback clients only (``local_only``) and
recorded as ``changed_by = 'local'``. With users: the trader or admin role, and the user's name.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from .app import ApiError, Con, ConRW
from .settings_routes import local_only

router = APIRouter(tags=["instruments"])

ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
MAX_ALIAS = 120
LOCAL_USER = "local"
"""``changed_by`` of a change from a loopback client while there are no users."""


class InstrumentAlias(BaseModel):
    isin: str
    alias: str = Field(description="The display name to show instead of the exchange name")
    changed_by: str = Field(description="Who set it: 'local' (this computer) until users exist")
    changed_at: datetime


class AliasList(BaseModel):
    aliases: list[InstrumentAlias] = Field(description="The aliases in force, by ISIN")


class AliasIn(BaseModel):
    isin: str = Field(description="The security's ISIN (case and surrounding spaces ignored)")
    alias: str | None = Field(None, description=f"The display name, at most {MAX_ALIAS} characters; "
                                                "null or blank clears it (the exchange name again)")


class AliasState(BaseModel):
    isin: str
    alias: str | None = Field(description="In force after the request; null: none (the exchange name is shown)")
    changed_by: str | None = Field(description="Of the latest revision; null: the ISIN never had one")
    changed_at: datetime | None
    changed: bool = Field(description="False when the request set what was already in force (no revision)")


class AliasRevision(BaseModel):
    rev_id: int
    alias: str | None = Field(description="null: the alias was cleared")
    changed_by: str
    changed_at: datetime


class AliasHistory(BaseModel):
    isin: str
    revisions: list[AliasRevision] = Field(description="Newest first")


def normalize_isin(raw: str) -> str:
    isin = raw.strip().upper()
    if not ISIN_RE.fullmatch(isin):
        raise ApiError(422, "invalid_parameter", f"not an ISIN: {raw!r} (2 letters, 9 letters or digits, a digit)")
    return isin


def normalize_alias(raw: str | None) -> str | None:
    """Blank: None (clear). Inner whitespace runs become one space; control characters are refused."""
    if raw is None:
        return None
    if any(unicodedata.category(c) == "Cc" and c not in "\t\n\r" for c in raw):
        raise ApiError(422, "invalid_parameter", "the alias has control characters")
    alias = " ".join(raw.split())
    if len(alias) > MAX_ALIAS:
        raise ApiError(422, "invalid_parameter", f"the alias is longer than {MAX_ALIAS} characters")
    return alias or None


def aliases_by_isin(con, isins: Iterable[str | None] | None = None) -> dict[str, str]:
    """The aliases in force, ISIN -> alias; only of ``isins`` when given (None entries ignored)."""
    if isins is None:
        rows = con.execute("SELECT isin, alias FROM instrument_alias").fetchall()
    else:
        wanted = sorted({i for i in isins if i})
        if not wanted:
            return {}
        rows = con.execute("SELECT isin, alias FROM instrument_alias WHERE isin = ANY(%s)", (wanted,)).fetchall()
    return {r["isin"]: r["alias"] for r in rows}


def _who(request: Request) -> str:
    return LOCAL_USER  # the signed-in user once there are users (05 §6)


def _latest(con, isin: str) -> dict | None:
    return con.execute("SELECT alias, changed_by, changed_at FROM instrument_alias_rev WHERE isin = %s"
                       " ORDER BY rev_id DESC LIMIT 1", (isin,)).fetchone()


@router.get("/api/v1/instruments/aliases", response_model=AliasList)
def list_aliases(con: Con):
    rows = con.execute("SELECT isin, alias, changed_by, changed_at FROM instrument_alias ORDER BY isin").fetchall()
    return {"aliases": rows}


@router.get("/api/v1/instruments/aliases/{isin}/history", response_model=AliasHistory)
def alias_history(con: Con, isin: Annotated[str, Path(description="The ISIN")]):
    isin = normalize_isin(isin)
    rows = con.execute("SELECT rev_id, alias, changed_by, changed_at FROM instrument_alias_rev WHERE isin = %s"
                       " ORDER BY rev_id DESC", (isin,)).fetchall()
    return {"isin": isin, "revisions": rows}


@router.put("/api/v1/instruments/alias", response_model=AliasState, dependencies=[Depends(local_only)],
            description="Set or clear (blank or null) the alias of an ISIN. From this computer only until users "
                        "exist; a change is recorded with who and when, a request that changes nothing is not.")
def put_alias(con: ConRW, body: AliasIn, request: Request):
    isin, alias = normalize_isin(body.isin), normalize_alias(body.alias)
    with con.transaction():
        con.execute("SELECT pg_advisory_xact_lock(hashtext('instrument_alias:' || %s))", (isin,))
        last = _latest(con, isin)
        if (last["alias"] if last else None) == alias:
            return {"isin": isin, "alias": alias, "changed_by": last and last["changed_by"],
                    "changed_at": last and last["changed_at"], "changed": False}
        row = con.execute("INSERT INTO instrument_alias_rev (isin, alias, changed_by) VALUES (%s, %s, %s)"
                          " RETURNING changed_by, changed_at", (isin, alias, _who(request))).fetchone()
    return {"isin": isin, "alias": alias, **row, "changed": True}
