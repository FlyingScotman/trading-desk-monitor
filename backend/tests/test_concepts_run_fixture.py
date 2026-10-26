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
object. Checked here on the golden answers and the fixture, and in ``loadRun`` on what a test adds.

TDM#337: an entry of ``CURVES`` has the currency of its id (``ccy`` is the id's first three letters; a currency's own
curve, id the bare code, is in RUB when the run has no rate for it: ``tdm_run.curves_reference`` publishes it so,
``concepts_run.assemble`` gives it back its currency when the rate is there, ``api/concepts_run.py:196-198``) and a
``kind`` of ``ids.curveKinds`` (``CURVE_KINDS``); the keys of ``FX`` and ``fxDelta`` and a position's ``ccy`` (or null: a
not-valued row may have none) match ``ids.ccyPattern``, a rate of ``FX`` is a finite number or null. The nested
structures of ``extra`` the pages read (``extraNested``: ``EXTRA_NESTED_KEYS``, which holds the golden answers' keys)
have known keys: ``funding`` an object, ``funding.lines[]`` and ``legs[]`` lists of objects. A curve id typed the same
way in ``CURVES`` and ``risk``, with the currency of its id and a known kind, still passes: that is what a new curve
of the server looks like."""

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

from desk.api.concepts_run import (
    BOND_KEYS,
    CURVE_KINDS,
    EXTRA_KEYS_READ,
    EXTRA_NESTED_KEYS,
    PNL_KEYS_READ,
    POSITION_KEYS,
)
from desk.valuation.derivatives import _ui_leg

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "js" / "fixtures" / "concepts-run.json"
SERVER_KEYS = FIXTURE.with_name("server-keys.json")
FLAGS = {"accrualOnly", "ocpTransfer"}
"""A position's keys set only when true, on rows the golden book does not have."""
CURVE_ID = r"^[A-Z]{3}( [A-Z][A-Z0-9]*)*$"
"""A key of ``CURVES``: its currency, then words of capitals and digits (``RUB OFZ``, ``CNY XCCY``, ``USD``)."""
CCY = r"^[A-Z]{3}$"
"""A currency: a key of ``FX`` and ``fxDelta``, a position's and a curve's ``ccy``."""


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


def is_ccy(x) -> bool:
    """A currency code."""
    return isinstance(x, str) and re.fullmatch(CCY, x) is not None


def is_curve(cid: str, v, nb: int, fx: dict) -> bool:
    """An entry of ``CURVES``: name, currency and kind as strings, the kind one of ``CURVE_KINDS``, the currency the
    id's (a bare currency id without a rate in ``fx``: RUB), a point (or null) per bucket."""
    if not (isinstance(v, dict) and all(isinstance(v.get(k), str) for k in ("name", "ccy", "kind"))):
        return False
    own = v["ccy"] == cid[:3] or (v["ccy"] == "RUB" and is_ccy(cid) and cid not in fx)
    return is_ccy(v["ccy"]) and own and v["kind"] in CURVE_KINDS and is_row(v.get("pts"), nb)


def nested_errors(extra: dict, nested: dict) -> list[str]:
    """The nested structures of ``extra`` (``EXTRA_NESTED_KEYS``: ``a`` an object, ``a[]`` a list of objects, ``a.b[]``
    within ``a``) off their keys or shape, as ``extra.<path>.<key>`` or ``extra.<path>``; absent or null is fine."""
    bad = []
    for spec, known in nested.items():
        parts, nodes = spec.split("."), [("extra", extra)]
        for i, part in enumerate(parts):
            k, many, last, nxt = part.removesuffix("[]"), part.endswith("[]"), i == len(parts) - 1, []
            for path, o in nodes:
                v, p = o.get(k), f"{path}.{k}"
                if v is None:
                    continue
                items = (v if isinstance(v, list) else None) if many else [v]
                if items is None or not all(isinstance(x, dict) for x in items):
                    if last:  # a malformed parent is its own spec's error
                        bad.append(p + ("[]" if items is not None and many else ""))
                    continue
                nxt += [(p + ("[]" if many else ""), x) for x in items]
            nodes = nxt
        bad += [f"{path}.{key}" for path, o in nodes for key in o if key not in known]
    return bad


def shape_errors(doc: dict, curves: set[str], ccys: set[str]) -> list[str]:
    """The ids and values of ``CURVES``, ``risk``, ``fxDelta`` and ``extra`` off the module's rules; ``curves`` and
    ``ccys`` are the known ids besides the document's own ``CURVES`` and ``FX``."""
    nb, fx = len(doc["BUCKETS"]), doc["FX"]
    bad = [f"CURVES.{c}" for c, v in doc["CURVES"].items() if not re.fullmatch(CURVE_ID, c) or not is_curve(c, v, nb, fx)]
    bad += [f"FX.{c}" for c, v in fx.items() if not is_ccy(c) or not (v is None or is_num(v))]
    # a position's own currency may have no rate in FX (core values it, the page says «нет курса»)
    known_curves = curves | set(doc["CURVES"])
    known_ccys = ccys | set(doc["FX"]) | {p.get("ccy") for p in doc["positions"]}
    for p in doc["positions"]:
        if p.get("ccy") is not None and not is_ccy(p["ccy"]):
            bad.append(f"ccy.{p['id']}")
        bad += [f"risk.{c}" for c, v in (p.get("risk") or {}).items() if c not in known_curves or not is_row(v, nb)]
        bad += [f"fxDelta.{c}" for c, v in (p.get("fxDelta") or {}).items()
                if c not in known_ccys or not is_ccy(c) or not is_num(v)]
        if p.get("extra") is not None and not isinstance(p["extra"], dict):
            bad.append(f"extra.{p['id']}")
        elif p.get("extra"):
            bad += nested_errors(p["extra"], EXTRA_NESTED_KEYS)
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


def test_shape_errors_catch_typos_337():
    """TDM#337: a curve's currency and kind, currency codes in FX, fxDelta and a position's ccy, the rates of FX, the
    nested structures of extra."""
    nb = 3

    def doc():
        return {"BUCKETS": ["1", "2", "3"], "FX": {"RUB": 1.0, "USD": 80.0},
                "CURVES": {"RUB OFZ": {"name": "ОФЗ", "ccy": "RUB", "kind": "bond", "pts": [1, 2, None]},
                           "OMR": {"name": "OMR", "ccy": "RUB", "kind": "bond", "pts": [None] * nb}},
                "positions": [{"id": "P", "ccy": "USD", "risk": {"RUB OFZ": [1.0, None, 0]}, "fxDelta": {"USD": 2.0},
                               "extra": {"funding": {"state": "funded", "lines": [{"kind": "POS", "rate_pct": 1.0}]},
                                         "legs": [{"leg": "near", "buy_ccy": "USD"}]}},
                              {"id": "N", "ccy": None, "risk": {}, "fxDelta": {}, "extra": None}]}

    assert shape_errors(doc(), set(), set()) == []
    cases = [
        (lambda d: d["CURVES"].__setitem__("RUB FOO", {**d["CURVES"]["RUB OFZ"], "ccy": "USD"}), "CURVES.RUB FOO"),
        (lambda d: d["CURVES"]["RUB OFZ"].update(ccy="USD"), "CURVES.RUB OFZ"),
        (lambda d: d["CURVES"]["RUB OFZ"].update(kind="bonds"), "CURVES.RUB OFZ"),
        (lambda d: d["FX"].__setitem__("OMR", 1.0), "CURVES.OMR"),  # with a rate the currency's curve is in OMR
        (lambda d: d["FX"].__setitem__("usd", 80.0), "FX.usd"),
        (lambda d: d["FX"].__setitem__("EUR\n", 90.0), "FX.EUR\n"),  # re.match с «$» такое пропускает
        (lambda d: d["positions"][0].update(ccy="USD\n"), "ccy.P"),
        (lambda d: d["FX"].__setitem__("XXX", "abc"), "FX.XXX"),
        (lambda d: d["FX"].__setitem__("XXX", float("inf")), "FX.XXX"),
        (lambda d: d["FX"].__setitem__("XXX", True), "FX.XXX"),
        (lambda d: d["positions"][0].update(ccy="usd"), "ccy.P"),
        (lambda d: d["positions"][0].update(ccy="usd", fxDelta={"usd": 1.0}), ["ccy.P", "fxDelta.usd"]),
        (lambda d: d["positions"][0]["extra"]["funding"].update(fooBar=1), "extra.funding.fooBar"),
        (lambda d: d["positions"][0]["extra"]["funding"]["lines"][0].update(fooBar=1), "extra.funding.lines[].fooBar"),
        (lambda d: d["positions"][0]["extra"]["funding"].update(lines={"kind": "POS"}), "extra.funding.lines"),
        (lambda d: d["positions"][0]["extra"]["funding"].update(lines=["x"]), "extra.funding.lines[]"),
        (lambda d: d["positions"][0]["extra"].update(funding=["x"]), "extra.funding"),
        (lambda d: d["positions"][0]["extra"]["legs"][0].update(fooBar=1), "extra.legs[].fooBar"),
        (lambda d: d["positions"][0]["extra"].update(legs={"leg": "near"}), "extra.legs"),
        (lambda d: d["positions"][0]["extra"].update(legs=[1]), "extra.legs[]"),
    ]
    for mutate, want in cases:
        d = doc()
        mutate(d)
        assert sorted(shape_errors(d, set(), set())) == sorted([want] if isinstance(want, str) else want), want
    d = doc()  # a new curve of the server with a known kind and the currency of its id
    d["CURVES"]["CNY FWD"] = {"name": "CNY implied", "ccy": "CNY", "kind": "swap", "pts": [None] * nb}
    d["FX"]["KZT"] = None
    assert shape_errors(d, set(), set()) == []


def test_ui_leg_keys_are_the_contract():
    """``EXTRA_NESTED_KEYS["legs[]"]`` is what ``_ui_leg`` gives over its branches (TDM#337): a leg without terms, a
    deliverable leg read the other way round (RUB base), an NDF with a fixing, a cross leg."""
    from datetime import date
    from types import SimpleNamespace as NS

    lv = NS(value_date=date(2026, 11, 2), pv_rub=1.0, rub_discount_factor=0.99, forward_rate=90.0, forward_points=0.5,
            payment_date=date(2026, 11, 3), base_forward_rate=7.1, quote_forward_rate=12.5, fixing_rate=90.1)

    def leg(**k):
        return NS(**{"base": "USD", "quote": "RUB", "side": "buy", "leg_id": None, "trade_id": "T1", "n_base": 1e6,
                     "n_quote": 9e7, "rate": 90.0, "ndf": False, "settle_ccy": None, "fixing_source": None,
                     "fixing_date": None, **k})

    got = set(_ui_leg(None, lv, "USD"))
    got |= set(_ui_leg(leg(base="RUB", quote="USD", side="sell"), lv, "USD"))
    got |= set(_ui_leg(leg(ndf=True, settle_ccy="RUB", fixing_source="CBR", fixing_date=date(2026, 10, 30)), lv, "USD"))
    got |= set(_ui_leg(leg(base="USD", quote="CNY"), lv, "USD", cross=True))
    assert got == set(EXTRA_NESTED_KEYS["legs[]"])


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
        assert {c["kind"] for c in d["CURVES"].values()} <= set(CURVE_KINDS)
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
    ids = {"curvePattern": CURVE_ID, "curves": sorted(curves), "ccys": sorted(ccys), "ccyPattern": CCY,
           "curveKinds": sorted(CURVE_KINDS)}
    nested = {spec: sorted(ks) for spec, ks in sorted(EXTRA_NESTED_KEYS.items())}
    text = json.dumps({**{level: sorted(ks) for level, ks in may.items()}, "extraRead": sorted(EXTRA_KEYS_READ),
                       "extraNested": nested, "ids": ids}, ensure_ascii=False, indent=1) + "\n"
    if os.environ.get("UPDATE_SERVER_KEYS") == "1":
        SERVER_KEYS.write_text(text, encoding="utf-8")
    assert SERVER_KEYS.exists() and SERVER_KEYS.read_text(encoding="utf-8") == text, (
        "tests/js/fixtures/server-keys.json differs from the server: UPDATE_SERVER_KEYS=1 pytest " + __file__)
