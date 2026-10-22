"""The settings tabs' forms (08 §4 ``settings_tabs``, FND-4): the JSON Schema check of a file before it is written,
and the adapter's own verdict on it, a dry run of ``reference()`` over a copy of the config folder.

The backend still never interprets the files: the schema is the adapter's, the rules behind it the adapter's
``reference()`` and the contract checks of ``desk.inbox.validate.check_reference_tables``. This module only runs
them and reports what they say, as issues ``{severity, code, path, message, origin}`` whose codes are those of the
adapter's readers (``tda.config``: ``missing_required``, ``bad_value``, ``bad_date``, ``bad_number``, ...) so the
settings page shows one Russian text per code wherever an issue comes from. Messages carry field names only.

The schema check is the JSON-Schema (draft 2020-12) subset the adapter's tabs use: ``type`` (one or a list),
``enum``, ``required``, ``properties``, ``patternProperties``, ``additionalProperties`` (false or a schema),
``propertyNames``, ``items``, ``pattern``, ``minLength``, ``minimum`` / ``maximum`` (and the exclusive ones),
``format: date``. As in the adapter, null is the same as absent unless the type allows null; a property outside
the schema is a warning (the adapter strips it), never an error.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .inbox import runner
from .inbox.spi import AdapterFailure
from .inbox.validate import check_reference_tables

MAX_ISSUES = 500
DRY_RUN_TIMEOUT_S = 30.0
MAX_COPY_BYTES = 8 * 1024 * 1024  # a config file larger than this is not copied for the dry run
_SRC_ROW = re.compile(r"^R([0-9]+)$")
_FIELD = re.compile(r"\bfield ([A-Za-z_][A-Za-z0-9_]*)")

Path_ = list[str | int]


def issue(severity: str, code: str, path: Path_, message: str, origin: str = "schema") -> dict:
    return {"severity": severity, "code": code, "path": path, "message": message, "origin": origin}


def _types(node: dict) -> list[str]:
    t = node.get("type")
    return [t] if isinstance(t, str) else list(t) if isinstance(t, list) else []


def _is_type(value: Any, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return type(value) is int or (type(value) is float and value.is_integer())
    if t == "number":
        return type(value) in (int, float)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return True


def _is_date_node(node: dict) -> bool:
    return node.get("format") == "date"


def _bad_code(node: dict) -> str:
    """The issue code of a value that does not fit ``node``: as the adapter's readers name it."""
    if _is_date_node(node):
        return "bad_date"
    if any(t in ("number", "integer") for t in _types(node)):
        return "bad_number"
    return "bad_value"


def _name(path: Path_) -> str:
    keys = [p for p in path if isinstance(p, str)]
    return keys[-1] if keys else "the file"


class _Checker:
    def __init__(self) -> None:
        self.issues: list[dict] = []

    def add(self, *a: Any) -> None:
        if len(self.issues) < MAX_ISSUES:
            self.issues.append(issue(*a))

    def check(self, node: Any, value: Any, path: Path_) -> None:
        if not isinstance(node, dict) or not node:
            return
        types = _types(node)
        if types and not any(_is_type(value, t) for t in types):
            self.add("error", _bad_code(node), path, f"field {_name(path)}: wrong type")
            return
        if "enum" in node and value not in node["enum"]:
            self.add("error", "bad_value", path, f"field {_name(path)}: not one of the allowed values")
            return
        if isinstance(value, str):
            self._string(node, value, path)
        elif type(value) in (int, float):
            self._number(node, value, path)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self.check(node.get("items"), v, [*path, i])
        elif isinstance(value, dict):
            self._object(node, value, path)

    def _string(self, node: dict, value: str, path: Path_) -> None:
        if isinstance(node.get("minLength"), int) and len(value) < node["minLength"]:
            self.add("error", "bad_value", path, f"field {_name(path)}: empty")
        elif isinstance(node.get("pattern"), str) and not re.search(node["pattern"], value):
            self.add("error", _bad_code(node), path, f"field {_name(path)}: does not match the format")
        elif _is_date_node(node):
            try:
                date.fromisoformat(value)
            except ValueError:
                self.add("error", "bad_date", path, f"field {_name(path)}: not a date")

    def _number(self, node: dict, value: float, path: Path_) -> None:
        bounds = (("minimum", lambda b: value < b), ("exclusiveMinimum", lambda b: value <= b),
                  ("maximum", lambda b: value > b), ("exclusiveMaximum", lambda b: value >= b))
        for key, out in bounds:
            b = node.get(key)
            if type(b) in (int, float) and out(b):
                self.add("error", "bad_number", path, f"field {_name(path)}: out of range ({key} {b})")
                return

    def _object(self, node: dict, value: dict, path: Path_) -> None:
        props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        patterns = node.get("patternProperties") if isinstance(node.get("patternProperties"), dict) else {}
        for key in node.get("required") or []:
            if value.get(key) is None:
                self.add("error", "missing_required", [*path, key], f"field {key}: required")
        names = node.get("propertyNames")
        for key, v in value.items():
            if isinstance(names, dict) and (("enum" in names and key not in names["enum"]) or (
                    isinstance(names.get("pattern"), str) and not re.search(names["pattern"], key))):
                self.add("error", "bad_value", [*path, key], f"key of {_name(path)}: not one of the allowed names")
                continue
            sub = props.get(key)
            if v is None and not (isinstance(sub, dict) and "null" in _types(sub)):
                continue  # absent and null are the same
            if sub is not None:
                self.check(sub, v, [*path, key])
                continue
            matched = [s for p, s in patterns.items() if re.search(p, key)]
            if matched:
                for s in matched:
                    self.check(s, v, [*path, key])
                continue
            extra = node.get("additionalProperties", True)
            if extra is False:
                code = "unknown_config_key" if not path else "unknown_field"
                self.add("warning", code, [*path, key], f"{_name(path)}: a key outside the form is ignored")
            elif isinstance(extra, dict):
                self.check(extra, v, [*path, key])


def check_schema(schema: dict, data: Any) -> list[dict]:
    """Issues of ``data`` against a settings tab's schema (errors block the write, warnings do not)."""
    c = _Checker()
    if not isinstance(data, dict):
        c.add("error", "bad_config", [], "the file must be a JSON object")
    else:
        c.check(schema, data, [])
    return c.issues


def file_text(data: Any) -> str:
    """What a form writes: two-space JSON, UTF-8 as is, a final newline (diff-friendly, readable in Notepad)."""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# ---- the adapter's verdict -------------------------------------------------------------------------------------


@dataclasses.dataclass
class DryRun:
    issues: list[dict]          # of the candidate, each with ``new``: not in the current config's issues
    error: str | None = None    # the dry run could not be made (the adapter failed, no reference(), ...)


def _copy_config(cfg: Path | None, dest: Path) -> None:
    if cfg is None or not cfg.is_dir():
        return
    for f in cfg.iterdir():
        if f.is_file() and not f.name.startswith(".") and f.stat().st_size <= MAX_COPY_BYTES:
            shutil.copy2(f, dest / f.name)


def _plain_issue(i: dict) -> dict:
    src = i.get("src") if isinstance(i.get("src"), str) else None
    m = _SRC_ROW.match(src or "")
    f = _FIELD.search(i.get("message") or "")
    return {"severity": i.get("severity"), "code": i.get("code"), "message": i.get("message") or "",
            "src": src, "scope": i.get("scope"), "count": i.get("count") or 1, "origin": i.get("origin") or "adapter",
            "row": int(m.group(1)) - 1 if m else None, "field": f.group(1) if f else None}


def _key(i: dict) -> tuple:
    return (i["severity"], i["code"], i["src"], i["message"], json.dumps(i["scope"], sort_keys=True))


def _reference_issues(handle: Any, config_dir: str | None) -> list[dict]:
    h = dataclasses.replace(handle, config_dir=config_dir)
    result = runner.call_reference(h, None, DRY_RUN_TIMEOUT_S)
    return [_plain_issue(i) for i in check_reference_tables(result)[1]]


def dry_run(handle: Any, cfg: Path | None, name: str, text: str) -> DryRun:
    """``reference()`` of the adapter over a copy of the config folder with ``name`` replaced by ``text``, and of
    the folder as it is: the candidate's issues, each marked ``new`` when the current files do not have it."""
    if not handle.ok:
        return DryRun([], f"the adapter is not loaded: {handle.error}")
    try:
        base = {_key(i) for i in _reference_issues(handle, str(cfg) if cfg else None)}
    except AdapterFailure:
        base = set()  # the current config already fails: every issue of the candidate is worth showing
    with tempfile.TemporaryDirectory(prefix="desk-settings-check-") as d:
        tmp = Path(d)
        _copy_config(cfg, tmp)
        (tmp / name).write_text(text, encoding="utf-8")
        try:
            found = _reference_issues(handle, str(tmp))
        except AdapterFailure as f:
            return DryRun([], f"reference() failed: {f.kind} {f.type}")
    return DryRun([{**i, "new": _key(i) not in base} for i in found])
