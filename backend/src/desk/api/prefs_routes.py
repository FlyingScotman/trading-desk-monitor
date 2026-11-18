"""A user's own display preferences: the columns of the position tables they have hidden (TDM#763).

The monitors' position tables (C01, C03, C06, C07, C09-C13: ``KIT.table`` in ``prototypes/concepts/lib/kit.js``) let a
user hide the columns they do not need and bring them back. The choice is kept here, per user of the monitor
(``user_table_columns``, migration 0025), so it follows the user to another computer and survives a restart and an
update of the program:

* ``GET /api/v1/me/table-columns``: every table of the signed-in user, ``{key: {hidden, updated_at}}``;
* ``PUT /api/v1/me/table-columns/{key}`` ``{"hidden": [column keys]}``: the hidden columns of one table; an empty list
  is the table's default (every column shown) and removes the row;
* ``DELETE /api/v1/me/table-columns/{key}``: back to the default ("как было"), also when there is nothing to remove.

``key`` is screen + table (+ the column set where a screen has sets): ``c01-positions``, ``c03-blotter:base``. Only the
hidden columns are stored: a column added by a later version shows by default, and a key the page does not know is
ignored by it. The server does not know the columns, only the shape of the keys.

Whose: always the caller's own (``auth.me_guard``): the signed-in user of any role, or ``local`` while sign-in is off
(this computer only, as the settings). There is no way to name another user. Not in the OpenAPI schema: the pages'
own request, like ``/api/v1/history/ocp``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, Field

from . import auth
from .app import ApiError, Con, ConRW

router = APIRouter(tags=["me"], include_in_schema=False)

KEY_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
COLUMN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
MAX_HIDDEN = 300
"""Hidden columns of one table: far more than any table has."""
MAX_TABLES = 300
"""Tables of one user: the monitors have a few dozen (with the column sets of C03)."""

Key = Annotated[str, Path(description="Screen + table (+ set), e.g. c03-blotter:base: a-z 0-9 . _ : -, up to 128")]


class HiddenIn(BaseModel):
    hidden: list[str] = Field(max_length=4 * MAX_HIDDEN,
                              description="The keys of the hidden columns; empty: the table's default")


class TableColumns(BaseModel):
    hidden: list[str]
    updated_at: datetime


class MyTables(BaseModel):
    user: str = Field(description="Whose they are: the signed-in user; 'local' while sign-in is off")
    tables: dict[str, TableColumns]


def check_key(key: str) -> str:
    if not KEY_RE.fullmatch(key):
        raise ApiError(422, "invalid_parameter", "the table key is a-z 0-9 . _ : - (a letter or digit first), "
                                                 "up to 128 characters")
    return key


def check_hidden(raw: list[str]) -> list[str]:
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
            raise ApiError(422, "invalid_parameter", f"at most {MAX_HIDDEN} hidden columns in a table")
    return out


@router.get("/api/v1/me/table-columns", response_model=MyTables)
def my_tables(con: Con, who: Annotated[auth.User, Depends(auth.me_guard)]):
    rows = con.execute("SELECT table_key, hidden, updated_at FROM user_table_columns WHERE username = %s"
                       " ORDER BY table_key", (who.username,)).fetchall()
    return {"user": who.username,
            "tables": {r["table_key"]: {"hidden": r["hidden"], "updated_at": r["updated_at"]} for r in rows}}


@router.put("/api/v1/me/table-columns/{key}", response_model=TableColumns)
def put_table_columns(con: ConRW, key: Key, body: HiddenIn, who: Annotated[auth.User, Depends(auth.me_guard)]):
    key, hidden = check_key(key), check_hidden(body.hidden)
    with con.transaction():
        if not hidden:
            con.execute("DELETE FROM user_table_columns WHERE username = %s AND table_key = %s", (who.username, key))
            return {"hidden": [], "updated_at": con.execute("SELECT clock_timestamp() AS t").fetchone()["t"]}
        # one user, one lock: the count of their tables cannot be passed by two requests at once
        con.execute("SELECT pg_advisory_xact_lock(hashtext('user_table_columns:' || %s))", (who.username,))
        known = con.execute("SELECT 1 FROM user_table_columns WHERE username = %s AND table_key = %s",
                            (who.username, key)).fetchone()
        n = con.execute("SELECT count(*) AS n FROM user_table_columns WHERE username = %s",
                        (who.username,)).fetchone()["n"]
        if not known and n >= MAX_TABLES:
            raise ApiError(409, "invalid_parameter", f"at most {MAX_TABLES} tables per user: reset one first")
        return con.execute(
            "INSERT INTO user_table_columns (username, table_key, hidden) VALUES (%s, %s, %s::jsonb)"
            " ON CONFLICT (username, table_key) DO UPDATE SET hidden = EXCLUDED.hidden,"
            " updated_at = clock_timestamp() RETURNING hidden, updated_at",
            (who.username, key, json.dumps(hidden))).fetchone()


@router.delete("/api/v1/me/table-columns/{key}", status_code=204)
def reset_table_columns(con: ConRW, key: Key, who: Annotated[auth.User, Depends(auth.me_guard)]):
    con.execute("DELETE FROM user_table_columns WHERE username = %s AND table_key = %s",
                (who.username, check_key(key)))
    return Response(status_code=204)
