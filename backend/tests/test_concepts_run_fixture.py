"""The monitors' JS fixture (``tests/js/fixtures/concepts-run.json``, TDM#291) stays a ``/api/v1/concepts/run`` document:
every key it has at the top, in ``source``, in a position and in a position's ``pnl`` is in the server's answer on the
golden runs of test_api. A position's flags the golden book has no row for (``accrualOnly``: an HTM lot,
``ocpTransfer``: an OCP transfer) are set only when true: those are checked against ``concepts_run.POSITION_KEYS``.
The fixture may have fewer keys (it is the demo data set), never one the server dropped or renamed. Independent of
compat: it stays when compat goes (TDM#107, step 4).

``tests/js/fixtures/server-keys.json`` (TDM#310) lists, per level, the keys the server may send: those of the golden
answers plus the contract (``POSITION_KEYS``, ``BOND_KEYS``, ``PNL_KEYS_READ``). The JS tests' ``loadRun`` checks
against it the keys a test adds to the fixture (``mutate``), so a field the server does not send fails there. This test
keeps the file equal to the server; ``UPDATE_SERVER_KEYS=1 pytest tests/test_concepts_run_fixture.py`` rewrites it."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
from pathlib import Path

from store_rows import insert_run
from test_api import book, client, run  # noqa: F401 - fixtures

from desk.api.concepts_run import BOND_KEYS, PNL_KEYS_READ, POSITION_KEYS

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "js" / "fixtures" / "concepts-run.json"
SERVER_KEYS = FIXTURE.with_name("server-keys.json")
FLAGS = {"accrualOnly", "ocpTransfer"}
"""A position's keys set only when true, on rows the golden book does not have."""


def keys(doc: dict) -> dict[str, set[str]]:
    """The keys of a run document per level: top, ``source``, a position (any), its ``pnl`` (any)."""
    ps = doc["positions"]
    return {"top": set(doc), "source": set(doc["source"]), "position": set().union(*(set(p) for p in ps)),
            "pnl": set().union(*(set(p.get("pnl") or {}) for p in ps))}


def test_js_fixture_keys_are_in_the_servers_answer(store_url, book, client):  # noqa: F811
    ctx, positions, view = book
    market = copy.copy(ctx.market)
    market.business_date = view.business_date
    early = insert_run(store_url, dataclasses.replace(ctx, market=market), positions, None)
    rid = run(store_url, book, cut_type="eod_preliminary")
    server: dict[str, set[str]] = {}
    for r in (early, rid):
        got = client.get(f"/api/v1/concepts/run?run_id={r}")
        assert got.status_code == 200, got.text
        for level, ks in keys(got.json()).items():
            server.setdefault(level, set()).update(ks)
    assert FLAGS <= set(POSITION_KEYS)
    server["position"] |= FLAGS
    fixture = keys(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert fixture["top"] and fixture["position"] and fixture["pnl"]
    for level, ks in fixture.items():
        assert ks <= server[level], (level, sorted(ks - server[level]))
    # the keys a JS test may add (loadRun, TDM#310): the answers plus the contract the pages read
    may = {**server, "position": server["position"] | set(POSITION_KEYS) | set(BOND_KEYS),
           "pnl": server["pnl"] | set(PNL_KEYS_READ)}
    text = json.dumps({level: sorted(ks) for level, ks in may.items()}, ensure_ascii=False, indent=1) + "\n"
    if os.environ.get("UPDATE_SERVER_KEYS") == "1":
        SERVER_KEYS.write_text(text, encoding="utf-8")
    assert SERVER_KEYS.exists() and SERVER_KEYS.read_text(encoding="utf-8") == text, (
        "tests/js/fixtures/server-keys.json differs from the server: UPDATE_SERVER_KEYS=1 pytest " + __file__)
