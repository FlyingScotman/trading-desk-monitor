"""A scenario definition, its normalized form and hash, and the own spread shock of a position (S6 §2).

The definition keeps the prototype shape (``prototypes/data.js``), so the prototype ``runScenario`` reads it::

    {"curves": {"RUB OFZ": [11 bp, RISK_BUCKETS order] | bp, "RUB RUONIA": ..., "RUB KEY": ..., "USD": ...},
     "ofzSpread": bp,
     "spreads": {"_all": bp, "_mult": factor > 0, "sector": {name: bp}, "issuer": {name: bp}, "rating": {r: bp}},
     "fx": {"USD": pct, ...}}

A curve is a published curve of the run (R8): ``RUB RUONIA`` the discount curve of every RUB bond (and a RUONIA
floater's projection), ``RUB KEY`` a key-rate floater's projection, ``RUB OFZ`` the KBD (a market curve: no bond
is discounted on it unless the date has no RUONIA curve, so a shock of it alone moves nothing else), ``USD``
and the like a currency's curve; each moves only by its own vector. The preset moves of
all rates shift the three RUB curves alike; the basis preset only ``RUB KEY`` (store migration 0008).
``ofzSpread`` is the OFZ Z-spread to RUONIA (ORG_CONVENTIONS §4).
``normalize`` gives the stored, returned and hashed form: every curve a list of 11 floats, zero entries,
``_mult == 1``, empty maps and all-zero curves dropped; an unknown key, a bad length, |bp| > ``MAX_BP``
or an FX move of -100 % or less raise ``DefinitionError``. A null or missing section counts as empty.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

from rub_fixed_income import RISK_BUCKETS

MAX_BP = 5000.0
KEYS = ("curves", "ofzSpread", "spreads", "fx")
RUB_CURVES = ("RUB OFZ", "RUB RUONIA", "RUB KEY")
"""The curves core reprices (``valuation.bonds.RISK_CURVES``): a shock on them is valid for every run, also one
published before R8 with ``RUB OFZ`` only (its floaters' risk summed there)."""
SPREAD_RULES = ("sector", "issuer", "rating")


class DefinitionError(ValueError):
    pass


def _num(x: Any, what: str, limit: float | None = MAX_BP) -> float:
    try:
        ok = not isinstance(x, bool) and isinstance(x, int | float) and math.isfinite(x)
    except OverflowError:  # an int beyond float
        ok = False
    if not ok:
        raise DefinitionError(f"{what}: a finite number is required, got {x!r}")
    if limit is not None and abs(x) > limit:
        raise DefinitionError(f"{what}: {x} is beyond ±{limit:g}")
    return float(x) + 0.0  # -0.0 -> 0.0: one hash for one shock


def _map(x: Any, what: str) -> Mapping[str, Any]:
    if x is None:
        return {}
    if not isinstance(x, Mapping) or not all(isinstance(k, str) and k for k in x):
        raise DefinitionError(f"{what}: an object with non-empty string keys is required")
    return x


def _rules(x: Any, what: str) -> dict[str, float]:
    out = {k: _num(v, f"{what}.{k}") for k, v in _map(x, what).items()}
    return {k: v for k, v in out.items() if v}


def normalize(defn: Mapping[str, Any], curves: Iterable[str] | None = None) -> dict[str, Any]:
    """The normalized definition. ``curves``: the curve IDs the run publishes; a shock on another curve than
    those and ``RUB_CURVES`` raises."""
    defn = _map(defn, "definition")
    if extra := set(defn) - set(KEYS):
        raise DefinitionError(f"unknown keys {sorted(extra)}")
    out: dict[str, Any] = {}
    shocked = {}
    for cid, v in _map(defn.get("curves"), "curves").items():
        vec = [v] * len(RISK_BUCKETS) if not isinstance(v, list | tuple) else list(v)
        if len(vec) != len(RISK_BUCKETS):
            raise DefinitionError(f"curves.{cid}: {len(RISK_BUCKETS)} bucket shifts are required, got {len(vec)}")
        vec = [_num(x, f"curves.{cid}") for x in vec]
        if any(vec):
            shocked[cid] = vec
    if curves is not None and (unknown := set(shocked) - set(curves) - set(RUB_CURVES)):
        raise DefinitionError(f"curves {sorted(unknown)} are not published by the run")
    if shocked:
        out["curves"] = shocked
    if ofz := _num(defn.get("ofzSpread") or 0, "ofzSpread"):
        out["ofzSpread"] = ofz
    raw = _map(defn.get("spreads"), "spreads")
    if extra := set(raw) - {"_all", "_mult", *SPREAD_RULES}:
        raise DefinitionError(f"spreads: unknown keys {sorted(extra)}")
    spreads: dict[str, Any] = {}
    if a := _num(raw.get("_all") or 0, "spreads._all"):
        spreads["_all"] = a
    if raw.get("_mult") is not None:
        m = _num(raw["_mult"], "spreads._mult", None)
        if m <= 0:
            raise DefinitionError(f"spreads._mult: a factor > 0 is required, got {m}")
        if m != 1:
            spreads["_mult"] = m
    for k in SPREAD_RULES:
        if r := _rules(raw.get(k), f"spreads.{k}"):
            spreads[k] = r
    if spreads:
        out["spreads"] = spreads
    fx = {}
    for ccy, v in _map(defn.get("fx"), "fx").items():
        if _num(v, f"fx.{ccy}", None) <= -100:
            raise DefinitionError(f"fx.{ccy}: a move above -100 % is required, got {v}")
        if v:
            fx[ccy] = float(v) + 0.0
    if fx:
        out["fx"] = fx
    return out


def definition_hash(norm: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of the normalized definition (no id or name)."""
    return hashlib.sha256(json.dumps(norm, sort_keys=True, separators=(",", ":"), allow_nan=False)
                          .encode()).hexdigest()


def own_spread_shock(norm: Mapping[str, Any], pos: Mapping[str, Any]) -> float:
    """ds (bp) of a risk-payload position: ``ofzSpread`` for an OFZ spread; for a credit spread
    ``_all + (_mult - 1) x spread_bp + sector + issuer + rating``, summed left to right (the TS port
    sums the same way). A missing rule or a null ``spread_bp`` adds 0 (no default spread); no spread kind: 0."""
    kind = pos.get("spread_kind")
    if kind == "ofz":
        return norm.get("ofzSpread", 0.0)
    if kind != "credit":
        return 0.0
    s = norm.get("spreads", {})
    ds = s.get("_all", 0.0)
    if "_mult" in s and pos.get("spread_bp") is not None:
        ds += (s["_mult"] - 1) * pos["spread_bp"]
    for k in SPREAD_RULES:
        if pos.get(k) is not None:
            ds += s.get(k, {}).get(pos[k], 0.0)
    return ds
