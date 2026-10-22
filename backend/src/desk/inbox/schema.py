"""A standard-library validator for ``contract/private/v1/*.schema.json`` and the row normaliser.

Only the JSON-Schema subset those files use: ``type``, ``enum``, ``required``, ``properties``,
``patternProperties``, ``additionalProperties: false``, ``pattern``, ``minLength``, ``format: date`` and local
``$ref``. Error codes are the 08 §6.3 ones and messages carry field names only, never values, because issues
are shown and shared (08 §6.3). A validated row is normalised so that equal data hashes equally: amounts become
plain decimal strings (a float through its shortest repr, so nothing is lost), integers (``type`` integer, since
private/1.2) become ints, absent and null are the same (null keys are dropped), ``ccy`` and every field of the
``#/$defs/ccy`` type is upper case (``RUR`` is ``RUB``) and ``""`` is invalid.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from typing import Any

from desk.inbox.spi import AMOUNT_RE, CODE_RE, SCHEMA_DIR

_JSON_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}
_CCY_ALIAS = {"RUR": "RUB"}
_CCY_REF = "#/$defs/ccy"
_INT_RE = re.compile(r"-?[0-9]+")


@dataclass(frozen=True)
class RowError:
    code: str      # missing_required | bad_number | bad_date | bad_value | unknown_field | dataset_not_supported
    field: str | None  # a canonical field name; None for a key outside the contract (its name may be data)


def load_schema(dataset: Any) -> dict | None:
    """The dataset's schema, or None when this backend has none yet (or ``dataset`` is not a dataset name)."""
    return _load(dataset) if isinstance(dataset, str) and CODE_RE.match(dataset) else None


@cache
def _load(dataset: str) -> dict | None:
    p = SCHEMA_DIR / f"{dataset}.schema.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def norm_ccy(code: str) -> str:
    """A currency code as the backend keys it: upper case, ``RUR`` (the pre-1998 code) as ``RUB``."""
    return _CCY_ALIAS.get(code.upper(), code.upper())


def schema_fields(dataset: str) -> tuple[list[str], list[str]]:
    """(all canonical fields, required fields) of a dataset, reserved ``_`` keys excluded."""
    s = load_schema(dataset)
    if s is None:
        return [], []
    return ([k for k in s["properties"] if not k.startswith("_")],
            [k for k in s["required"] if not k.startswith("_")])


def amount(value: Any) -> Decimal | None:
    """A contract amount as Decimal, or None when it is not one (bool, NaN, infinity, separators, commas)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str) and AMOUNT_RE.fullmatch(value):
        return Decimal(value)
    return None


def amount_str(d: Decimal) -> str:
    """Plain decimal text without exponent or trailing zeros: 1.50 and 1.5 hash alike."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def is_date(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _resolve(schema: dict, node: dict) -> dict:
    while "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"only local $ref is supported: {ref}")
        node = schema
        for part in ref[2:].split("/"):
            node = node[part]
    return node


def _check_value(node: dict, value: Any) -> tuple[str | None, Any]:
    """(error code or None, normalised value) of one present, non-null value."""
    types = node.get("type")
    types = [types] if isinstance(types, str) else (types or [])
    if "number" in types:  # amounts: number or decimal string (pattern applies to the string form)
        d = amount(value)
        return ("bad_number", None) if d is None else (None, amount_str(d))
    if "integer" in types:  # an int, or a string of digits; never a bool or a float
        if type(value) is int:
            return None, value
        if isinstance(value, str) and _INT_RE.fullmatch(value):
            return None, int(value)
        return "bad_number", None
    if "enum" in node:
        return (None, value) if value in node["enum"] else ("bad_value", None)
    if types and not any(isinstance(value, _JSON_TYPES[t]) for t in types if t in _JSON_TYPES):
        return "bad_value", None
    if isinstance(value, str):
        if value == "" or len(value) < node.get("minLength", 0):
            return "bad_value", None
        if node.get("format") == "date" and not is_date(value):
            return "bad_date", None
        if "pattern" in node and not re.search(node["pattern"], value):
            return "bad_value", None
    return None, value


def validate_row(dataset: str, row: dict, *, allow_newer: bool = False) -> tuple[dict, list[RowError], int]:
    """Validate one row against its dataset schema.

    Returns (normalised row, errors, number of dropped newer-minor fields). With ``allow_newer`` (the adapter
    declares a newer minor of our major, 08 §9) an unknown non-``x_`` field is dropped and counted instead of
    being an ``unknown_field`` error. Reserved ``_`` keys are the caller's business and pass unchanged.
    """
    schema = load_schema(dataset)
    if schema is None:
        return {}, [RowError("dataset_not_supported", None)], 0
    props, patterns = schema["properties"], schema.get("patternProperties", {})
    out: dict[str, Any] = {}
    errors: list[RowError] = []
    dropped = 0
    for key, value in row.items():
        if value is None:
            continue
        if key in props:
            code, norm = _check_value(_resolve(schema, props[key]), value)
            if code:
                errors.append(RowError(code, key))
            else:
                is_ccy = key == "ccy" or props[key].get("$ref") == _CCY_REF
                out[key] = norm_ccy(norm) if is_ccy and isinstance(norm, str) else norm
        elif any(re.search(p, key) for p in patterns):
            if value == "":
                errors.append(RowError("bad_value", None))
            else:
                out[key] = value  # x_* stored as they are
        elif key.startswith("_"):
            out[key] = value
        elif allow_newer:
            dropped += 1
        else:
            errors.append(RowError("unknown_field", None))
    for key in schema["required"]:
        if row.get(key) is None:
            errors.append(RowError("missing_required", key))
    for key, node in props.items():  # defaults (books.active) so absent and default hash alike
        if key not in out and "default" in node and not any(e.field == key for e in errors):
            out[key] = node["default"]
    return out, errors, dropped
