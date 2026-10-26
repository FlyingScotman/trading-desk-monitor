"""The monitors' JS fixture (``tests/js/fixtures/concepts-run.json``, TDM#291) stays a ``/api/v1/concepts/run`` document:
every key it has at the top, in ``source``, in a position and in a position's ``pnl`` is in the server's answer on the
golden runs of test_api. A position's flags the golden book has no row for (``accrualOnly``: an HTM lot,
``ocpTransfer``: an OCP transfer) are set only when true: those are checked against ``concepts_run.POSITION_KEYS``.
The fixture may have fewer keys (it is the demo data set), never one the server dropped or renamed. Independent of
compat: it stays when compat goes (TDM#107, step 4)."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

from store_rows import insert_run
from test_api import book, client, run  # noqa: F401 - fixtures

from desk.api.concepts_run import POSITION_KEYS

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "js" / "fixtures" / "concepts-run.json"
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
