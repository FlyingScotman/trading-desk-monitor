"""The monitors' JS fixture (``tests/js/fixtures/concepts-run.json``, TDM#291) stays a ``/api/v1/concepts/run`` document:
every key it has at the top, in ``source``, in a position and in a position's ``pnl`` is in the server's answer on the
golden runs of test_api. A position's flags the golden book has no row for (``accrualOnly``: an HTM lot,
``ocpTransfer``: an OCP transfer) are set only when true: those are checked against ``concepts_run.POSITION_KEYS``.
The fixture may have fewer keys (it is the demo data set), never one the server dropped or renamed. Independent of
compat: it stays when compat goes (TDM#107, step 4).

``tests/js/fixtures/server-keys.json`` (TDM#310) lists, per level, the keys the server may send: those of the golden
answers plus the contract (``POSITION_KEYS``, ``BOND_KEYS``, ``PNL_KEYS_READ``). The JS tests' ``loadRun`` checks
against it the keys a test adds to the fixture (``mutate``), so a field the server does not send fails there. This test
keeps the file equal to the server; ``UPDATE_SERVER_KEYS=1 pytest tests/test_concepts_run_fixture.py`` rewrites it.

Nested levels (TDM#324): ``extra`` (a position's: the union over the golden answers plus the contract
``EXTRA_KEYS_READ``, which the file also lists alone as ``extraRead``) and ``curve`` (an entry of ``CURVES``) list keys.
The keys of ``CURVES``, a position's ``risk`` and ``fxDelta`` are curve ids and currencies, not fields. ``ids.curves``
lists the curve ids of the golden answers, ``ids.ccys`` their currencies (``FX`` and ``fxDelta``): a key of ``risk`` is
one of ``ids.curves`` or of the document's ``CURVES``, a key of ``fxDelta`` one of ``ids.ccys``, of its ``FX`` or a
currency of one of its positions (which may have no rate). ``loadRun`` also knows the fixture's own curves: a test
may drop one from ``CURVES`` and keep the risk on it (an older server's run). A key
of ``CURVES`` matches ``ids.curvePattern``. The values have a fixed shape (``shape_errors``): ``risk`` and
``CURVES[id].pts`` one finite number or null per bucket of ``BUCKETS``, ``fxDelta`` a finite number, ``extra`` an
object. Checked here on the golden answers and the fixture, and in ``loadRun`` on what a test adds."""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import os
import re
from pathlib import Path

from store_rows import insert_run
from test_api import book, client, run  # noqa: F401 - fixtures

from desk.api.concepts_run import BOND_KEYS, EXTRA_KEYS_READ, PNL_KEYS_READ, POSITION_KEYS

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "js" / "fixtures" / "concepts-run.json"
SERVER_KEYS = FIXTURE.with_name("server-keys.json")
FLAGS = {"accrualOnly", "ocpTransfer"}
"""A position's keys set only when true, on rows the golden book does not have."""
CURVE_ID = r"^[A-Z]{3}( [A-Z][A-Z0-9]*)*$"
"""A key of ``CURVES``: its currency, then words of capitals and digits (``RUB OFZ``, ``CNY XCCY``, ``USD``)."""


def keys(doc: dict) -> dict[str, set[str]]:
    """The keys of a run document per level: top, ``source``, a position (any), its ``pnl`` (any), its ``extra``
    (any, when an object), an entry of ``CURVES`` (any)."""
    ps = doc["positions"]
    return {"top": set(doc), "source": set(doc["source"]), "position": set().union(*(set(p) for p in ps)),
            "pnl": set().union(*(set(p.get("pnl") or {}) for p in ps)),
            "extra": set().union(*(set(p["extra"]) for p in ps if isinstance(p.get("extra"), dict))),
            "curve": set().union(*(set(c) for c in doc["CURVES"].values()))}


def is_num(x) -> bool:
    """A finite number (JSON has no NaN, but a bool is an int in Python)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def is_row(v, nb: int) -> bool:
    """One finite number or null per bucket."""
    return isinstance(v, list) and len(v) == nb and all(x is None or is_num(x) for x in v)


def is_curve(v, nb: int) -> bool:
    """An entry of ``CURVES``: name, currency and kind as strings, a point (or null) per bucket."""
    return (isinstance(v, dict) and all(isinstance(v.get(k), str) for k in ("name", "ccy", "kind"))
            and re.match(r"^[A-Z]{3}$", v["ccy"]) is not None and is_row(v.get("pts"), nb))


def shape_errors(doc: dict, curves: set[str], ccys: set[str]) -> list[str]:
    """The ids and values of ``CURVES``, ``risk``, ``fxDelta`` and ``extra`` off the module's rules; ``curves`` and
    ``ccys`` are the known ids besides the document's own ``CURVES`` and ``FX``."""
    nb = len(doc["BUCKETS"])
    bad = [f"CURVES.{c}" for c, v in doc["CURVES"].items() if not re.match(CURVE_ID, c) or not is_curve(v, nb)]
    # a position's own currency may have no rate in FX (core values it, the page says «нет курса»)
    known_curves = curves | set(doc["CURVES"])
    known_ccys = ccys | set(doc["FX"]) | {p.get("ccy") for p in doc["positions"]}
    for p in doc["positions"]:
        bad += [f"risk.{c}" for c, v in (p.get("risk") or {}).items() if c not in known_curves or not is_row(v, nb)]
        bad += [f"fxDelta.{c}" for c, v in (p.get("fxDelta") or {}).items() if c not in known_ccys or not is_num(v)]
        if p.get("extra") is not None and not isinstance(p["extra"], dict):
            bad.append(f"extra.{p['id']}")
    return bad


def test_shape_errors_catch_typos():
    """The rules on a hand-made document: a typo in an id or a value of the wrong shape is reported."""
    nb = 3

    def doc(**p):
        pos = {"id": "P", "risk": {"RUB OFZ": [1.0, None, 0]}, "fxDelta": {"USD": 2.0}, "extra": {}}
        pos.update(p)
        return {"BUCKETS": ["1", "2", "3"], "FX": {"RUB": 1.0, "USD": 80.0},
                "CURVES": {"RUB OFZ": {"name": "ОФЗ", "ccy": "RUB", "kind": "bond", "pts": [1, 2, None]}},
                "positions": [pos]}

    assert shape_errors(doc(), set(), set()) == []
    assert shape_errors(doc(risk={"RUB KEY": [0] * nb}), {"RUB KEY"}, set()) == []
    for p, want in [({"risk": {"RUB FOO": [0] * nb}}, "risk.RUB FOO"), ({"risk": {"RUB OFZ ": [0] * nb}}, "risk.RUB OFZ "),
                    ({"risk": {"RUB OFZ": [True, 0, 0]}}, "risk.RUB OFZ"), ({"risk": {"RUB OFZ": {"1Y": 1}}}, "risk.RUB OFZ"),
                    ({"fxDelta": {"XXX": 1.0}}, "fxDelta.XXX"), ({"fxDelta": {"USD": float("nan")}}, "fxDelta.USD"),
                    ({"fxDelta": {"USD": True}}, "fxDelta.USD"), ({"extra": ["x"]}, "extra.P")]:
        assert shape_errors(doc(**p), set(), set()) == [want], p
    d = doc()
    d["CURVES"]["RUB OFZ"]["pts"] = {}
    assert shape_errors(d, set(), set()) == ["CURVES.RUB OFZ"]
    d = doc()
    d["CURVES"]["RUB OFZ "] = d["CURVES"]["RUB OFZ"]
    assert shape_errors(d, set(), set()) == ["CURVES.RUB OFZ "]


def test_js_fixture_keys_are_in_the_servers_answer(store_url, book, client):  # noqa: F811
    ctx, positions, view = book
    market = copy.copy(ctx.market)
    market.business_date = view.business_date
    early = insert_run(store_url, dataclasses.replace(ctx, market=market), positions, None)
    rid = run(store_url, book, cut_type="eod_preliminary")
    server: dict[str, set[str]] = {}
    curves: set[str] = set()
    ccys: set[str] = set()
    for r in (early, rid):
        got = client.get(f"/api/v1/concepts/run?run_id={r}")
        assert got.status_code == 200, got.text
        d = got.json()
        assert d["CURVES"] and any(p["risk"] for p in d["positions"])
        # the answer keeps the rules on its own: every risk curve is in its CURVES, every currency in its FX
        assert shape_errors(d, set(), set()) == []
        assert {c for p in d["positions"] for c in p["risk"]} <= set(d["CURVES"])
        curves |= set(d["CURVES"])
        ccys |= set(d["FX"]) | {c for p in d["positions"] for c in p["fxDelta"]}
        for level, ks in keys(d).items():
            server.setdefault(level, set()).update(ks)
    assert FLAGS <= set(POSITION_KEYS)
    server["position"] |= FLAGS
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture = keys(doc)
    assert fixture["top"] and fixture["position"] and fixture["pnl"] and fixture["curve"]
    for level, ks in fixture.items():
        assert ks <= server[level], (level, sorted(ks - server[level]))
    assert shape_errors(doc, curves, ccys) == []
    # the keys a JS test may add (loadRun, TDM#310, TDM#324): the answers plus the contract the pages read
    may = {**server, "position": server["position"] | set(POSITION_KEYS) | set(BOND_KEYS),
           "pnl": server["pnl"] | set(PNL_KEYS_READ), "extra": server["extra"] | set(EXTRA_KEYS_READ)}
    ids = {"curvePattern": CURVE_ID, "curves": sorted(curves), "ccys": sorted(ccys)}
    text = json.dumps({**{level: sorted(ks) for level, ks in may.items()}, "extraRead": sorted(EXTRA_KEYS_READ),
                       "ids": ids}, ensure_ascii=False, indent=1) + "\n"
    if os.environ.get("UPDATE_SERVER_KEYS") == "1":
        SERVER_KEYS.write_text(text, encoding="utf-8")
    assert SERVER_KEYS.exists() and SERVER_KEYS.read_text(encoding="utf-8") == text, (
        "tests/js/fixtures/server-keys.json differs from the server: UPDATE_SERVER_KEYS=1 pytest " + __file__)
