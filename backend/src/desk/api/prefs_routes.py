"""A user's own display preferences: the columns of the position tables they have hidden and added (TDM#763, TDM#778),
and the format of the numbers of a column (TDM#789).

The monitors' position tables (C01, C03, C06, C07, C09-C13: ``KIT.table`` in ``prototypes/concepts/lib/kit.js``) let a
user hide the columns they do not need, bring them back, and add a standard position column the table does not have
(ISIN, Z-spread...). The choice is kept here, per user of the monitor (``user_table_columns``, migrations 0025 and
0027), so it follows the user to another computer and survives a restart and an update of the program:

* ``GET /api/v1/me/table-columns``: every table of the signed-in user, ``{key: {hidden, added, updated_at}}``;
* ``PUT /api/v1/me/table-columns/{key}`` ``{"hidden": [column keys], "added": [column keys]}``: the hidden and the
  added columns of one table, ``added`` in the order they were added. ``added`` may be left out (a page of the version
  before TDM#778 sends only ``hidden``): the added columns then stay as they are. Both lists empty is the table's
  default (the columns the page gives it) and removes the row;
* ``DELETE /api/v1/me/table-columns/{key}``: back to the default ("как было"), also when there is nothing to remove.

``key`` is screen + table (+ the column set where a screen has sets): ``c01-positions``, ``c03-blotter:base``. Only
the changes to the default are stored: a column added to the table by a later version shows by default, and a key the
page does not know (in either list) is ignored by it. The server does not know the columns, only the shape of the keys.

One window at an update: a page of the version before TDM#778 that is still open sends ``DELETE`` when the user brings
back the last hidden column, and that also removes the columns added in the new version (no ``added`` is left to keep);
the page of the new version sends ``added`` explicitly and is not affected.

The format of a column's numbers (TDM#789, ``user_column_formats``, migration 0029): the digits after the point and the
units, which the user sets in the "Колонки ▾" menu of a position table, for a column of the page's catalog of position
columns (``KIT.posCols``: ``mv``, ``dv01``, ``carry``...). It belongs to the column, not to a table: one format holds in
every position table of every screen that has the column, and the reset of a table's columns ("Как было") does not touch
it. The server keeps it as the user chose and does not know the columns, only the shape of the keys:

* ``GET /api/v1/me/column-formats``: every format of the signed-in user, ``{column: {decimals, scale, updated_at}}``;
* ``PUT /api/v1/me/column-formats/{col}`` ``{"decimals": 0..6 | null, "scale": "1" | "k" | "mm" | "bn" | null}``: the
  whole format of one column, replaced; both keys are required and no other key is accepted (a body with a misspelt key
  or an empty one is a 422, never a reset). ``null`` is the column's own default for that part: ``decimals``
  the page's digits for the units shown, ``scale`` the column's own units. Both ``null`` is the default format and removes
  the row. ``"1"`` is the number as it is (no units), ``"k"`` thousands (тыс), ``"mm"`` millions, ``"bn"`` billions;
* ``DELETE /api/v1/me/column-formats/{col}``: back to the default, also when there is nothing to remove.

At most ``MAX_FORMATS`` columns with a format per user: a column that already has one is replaced; a new one over the
limit is refused (409) until another is reset. A bad key or value is refused (422) and changes nothing.

Whose: always the caller's own (``auth.me_guard``): the signed-in user of any role, or ``local`` while sign-in is off
(this computer only, as the settings). There is no way to name another user. Not in the OpenAPI schema: the pages'
own request, like ``/api/v1/history/ocp``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from . import auth
from .app import ApiError, Con, ConRW

router = APIRouter(tags=["me"], include_in_schema=False)

KEY_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
COLUMN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
MAX_HIDDEN = 300
"""Hidden columns of one table: far more than any table has. The same bound for the added columns."""
MAX_TABLES = 300
"""Tables of one user: the monitors have a few dozen (with the column sets of C03)."""

Key = Annotated[str, Path(description="Screen + table (+ set), e.g. c03-blotter:base: a-z 0-9 . _ : -, up to 128")]
MAX_FORMATS = 200
"""Columns with a format of one user: the catalog has a few dozen, far more than any user sets."""

Col = Annotated[str, Path(description="Key of the column in the catalog of position columns, e.g. dv01: a-z A-Z 0-9 . _ : -, up to 64")]


class HiddenIn(BaseModel):
    hidden: list[str] = Field(max_length=4 * MAX_HIDDEN,
                              description="The keys of the hidden columns; empty (and no added): the table's default")
    added: list[str] | None = Field(default=None, max_length=4 * MAX_HIDDEN,
                                    description="The keys of the added columns, in the order added; left out: as they "
                                                "are (a page of the version before TDM#778)")


class TableColumns(BaseModel):
    hidden: list[str]
    added: list[str]
    updated_at: datetime


class FormatIn(BaseModel):
    """Both keys are required and no other is accepted: a misspelt key (``{"digits": 3}``) or an empty body is a 422, not
    a silent reset of the format. The reset is an explicit ``{"decimals": null, "scale": null}`` or ``DELETE``."""

    model_config = ConfigDict(extra="forbid")
    decimals: StrictInt | None = Field(..., ge=0, le=6, description="Digits after the point, 0..6; null: the column's default")
    scale: Literal["1", "k", "mm", "bn"] | None = Field(...,
                                                        description="Units: 1 as is, k thousands, mm millions, bn billions;"
                                                                    " null: the column's own units")


class ColumnFormat(BaseModel):
    decimals: int | None
    scale: str | None
    updated_at: datetime


class MyFormats(BaseModel):
    user: str = Field(description="Whose they are: the signed-in user; 'local' while sign-in is off")
    formats: dict[str, ColumnFormat]


class MyTables(BaseModel):
    user: str = Field(description="Whose they are: the signed-in user; 'local' while sign-in is off")
    tables: dict[str, TableColumns]


def check_key(key: str) -> str:
    if not KEY_RE.fullmatch(key):
        raise ApiError(422, "invalid_parameter", "the table key is a-z 0-9 . _ : - (a letter or digit first), "
                                                 "up to 128 characters")
    return key


def check_column(col: str) -> str:
    if not COLUMN_RE.fullmatch(col):
        raise ApiError(422, "invalid_parameter", "a column key is a-z A-Z 0-9 . _ : - (a letter or digit first), "
                                                 "up to 64 characters")
    return col


def check_columns(raw: list[str], what: str = "hidden") -> list[str]:
    """The column keys without repeats, in the order given. Linear: a set for the repeats, the limit checked as the
    keys come (a body is bounded by ``HiddenIn`` as well)."""
    out: list[str] = []
    seen: set[str] = set()
    for c in raw:
        if c in seen:
            continue
        if not COLUMN_RE.fullmatch(c):
            raise ApiError(422, "invalid_parameter", "a column key is a-z A-Z 0-9 . _ : - (a letter or digit first), "
                                                     "up to 64 characters")
        seen.add(c)
        out.append(c)
        if len(out) > MAX_HIDDEN:
            raise ApiError(422, "invalid_parameter", f"at most {MAX_HIDDEN} {what} columns in a table")
    return out


@router.get("/api/v1/me/table-columns", response_model=MyTables)
def my_tables(con: Con, who: Annotated[auth.User, Depends(auth.me_guard)]):
    rows = con.execute("SELECT table_key, hidden, added, updated_at FROM user_table_columns WHERE username = %s"
                       " ORDER BY table_key", (who.username,)).fetchall()
    return {"user": who.username,
            "tables": {r["table_key"]: {"hidden": r["hidden"], "added": r["added"], "updated_at": r["updated_at"]}
                       for r in rows}}


@router.put("/api/v1/me/table-columns/{key}", response_model=TableColumns)
def put_table_columns(con: ConRW, key: Key, body: HiddenIn, who: Annotated[auth.User, Depends(auth.me_guard)]):
    key, hidden = check_key(key), check_columns(body.hidden)
    added = None if body.added is None else check_columns(body.added, "added")
    with con.transaction():
        # one user, one lock: the count of their tables cannot be passed by two requests at once, and the added
        # columns left out of the body are read and kept under the same lock
        con.execute("SELECT pg_advisory_xact_lock(hashtext('user_table_columns:' || %s))", (who.username,))
        known = con.execute("SELECT added FROM user_table_columns WHERE username = %s AND table_key = %s",
                            (who.username, key)).fetchone()
        if added is None:
            added = known["added"] if known else []
        if not hidden and not added:
            con.execute("DELETE FROM user_table_columns WHERE username = %s AND table_key = %s", (who.username, key))
            return {"hidden": [], "added": [],
                    "updated_at": con.execute("SELECT clock_timestamp() AS t").fetchone()["t"]}
        n = con.execute("SELECT count(*) AS n FROM user_table_columns WHERE username = %s",
                        (who.username,)).fetchone()["n"]
        if not known and n >= MAX_TABLES:
            raise ApiError(409, "invalid_parameter", f"at most {MAX_TABLES} tables per user: reset one first")
        return con.execute(
            "INSERT INTO user_table_columns (username, table_key, hidden, added) VALUES (%s, %s, %s::jsonb, %s::jsonb)"
            " ON CONFLICT (username, table_key) DO UPDATE SET hidden = EXCLUDED.hidden, added = EXCLUDED.added,"
            " updated_at = clock_timestamp() RETURNING hidden, added, updated_at",
            (who.username, key, json.dumps(hidden), json.dumps(added))).fetchone()


@router.delete("/api/v1/me/table-columns/{key}", status_code=204)
def reset_table_columns(con: ConRW, key: Key, who: Annotated[auth.User, Depends(auth.me_guard)]):
    con.execute("DELETE FROM user_table_columns WHERE username = %s AND table_key = %s",
                (who.username, check_key(key)))
    return Response(status_code=204)


@router.get("/api/v1/me/column-formats", response_model=MyFormats)
def my_formats(con: Con, who: Annotated[auth.User, Depends(auth.me_guard)]):
    rows = con.execute("SELECT col_key, decimals, scale, updated_at FROM user_column_formats WHERE username = %s"
                       " ORDER BY col_key", (who.username,)).fetchall()
    return {"user": who.username,
            "formats": {r["col_key"]: {"decimals": r["decimals"], "scale": r["scale"], "updated_at": r["updated_at"]}
                        for r in rows}}


@router.put("/api/v1/me/column-formats/{col}", response_model=ColumnFormat)
def put_column_format(con: ConRW, col: Col, body: FormatIn, who: Annotated[auth.User, Depends(auth.me_guard)]):
    col = check_column(col)
    with con.transaction():
        # one user, one lock: the count of their formats cannot be passed by two requests at once
        con.execute("SELECT pg_advisory_xact_lock(hashtext('user_column_formats:' || %s))", (who.username,))
        if body.decimals is None and body.scale is None:  # the default format: no row
            con.execute("DELETE FROM user_column_formats WHERE username = %s AND col_key = %s", (who.username, col))
            return {"decimals": None, "scale": None,
                    "updated_at": con.execute("SELECT clock_timestamp() AS t").fetchone()["t"]}
        known = con.execute("SELECT 1 FROM user_column_formats WHERE username = %s AND col_key = %s",
                            (who.username, col)).fetchone()
        n = con.execute("SELECT count(*) AS n FROM user_column_formats WHERE username = %s",
                        (who.username,)).fetchone()["n"]
        if not known and n >= MAX_FORMATS:
            raise ApiError(409, "invalid_parameter", f"at most {MAX_FORMATS} columns with a format per user: reset one first")
        return con.execute(
            "INSERT INTO user_column_formats (username, col_key, decimals, scale) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (username, col_key) DO UPDATE SET decimals = EXCLUDED.decimals, scale = EXCLUDED.scale,"
            " updated_at = clock_timestamp() RETURNING decimals, scale, updated_at",
            (who.username, col, body.decimals, body.scale)).fetchone()


@router.delete("/api/v1/me/column-formats/{col}", status_code=204)
def reset_column_format(con: ConRW, col: Col, who: Annotated[auth.User, Depends(auth.me_guard)]):
    con.execute("DELETE FROM user_column_formats WHERE username = %s AND col_key = %s", (who.username, check_column(col)))
    return Response(status_code=204)
