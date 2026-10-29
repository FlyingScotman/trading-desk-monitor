"""The settings tabs' forms (08 §4 ``settings_tabs``, FND-4): the JSON Schema check of a file before it is written,
and the adapter's own verdict on it, a dry run of ``reference()`` over a copy of the config folder.

The backend still never interprets the files: the schema is the adapter's, the rules behind it the adapter's
``reference()`` and the contract checks of ``desk.inbox.validate.check_reference_tables``. This module only runs
them and reports what they say, as issues ``{severity, code, path, message, origin}`` whose codes are those of the
adapter's readers (``tda.config``: ``missing_required``, ``bad_value``, ``bad_date``, ``bad_number``, ...) so the
settings page shows one Russian text per code wherever an issue comes from. Messages carry field names only.

The schema check is the JSON-Schema (draft 2020-12) subset the adapter's tabs use: ``type`` (one or a list),
``enum``, ``const``, ``oneOf`` / ``anyOf`` of ``const`` branches (an enum with titles), ``required``, ``properties``,
``patternProperties``, ``additionalProperties`` (false or a schema),
``propertyNames``, ``items``, ``pattern``, ``minLength``, ``minimum`` / ``maximum`` (and the exclusive ones),
``format: date``. As in the adapter, null is the same as absent unless the type allows null; a property outside
the schema is a warning (the adapter strips it), never an error.

The dry run (B2-11) runs ``reference()`` twice, on the current files and on the candidate. The current files'
issues are kept until the adapter or its config folder changes (the handle's fingerprint covers both) or for
``BASE_TTL_S`` (the report folders the adapter may read are not fingerprinted), so a check costs one run, not two.
Both runs go to one long-lived child (``runner.Worker``) that stays running between checks: the import is paid once
and the adapter may keep caches of its own between calls (08 §2), such as the reports it has already read. There is
no idle timer: the child lives until the next check, which ends it and starts a new one when the previous check was
more than ``WORKER_IDLE_S`` ago or the adapter's own files changed (``reset``, at the server's exit, ends it too).
A timeout or a crash of a run kills it as well, and the next run starts a new one. The limit of one run is the setting
``adapter_check_timeout_s``; when the current files' run hits it, the candidate is not tried with the same limit.
The candidate's copy is a dot folder beside the config folder, its subfolders linked, so that paths of the files
relative to the config folder lead where they lead from the real one (``_check_dir``, TDM#130): an unchanged file
then has exactly the current files' issues, none of them ``new``.
"""

from __future__ import annotations

import atexit
import dataclasses
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from .inbox import loader, runner
from .inbox.spi import AdapterFailure
from .inbox.validate import check_reference_tables

MAX_ISSUES = 500
DRY_RUN_TIMEOUT_S = 60.0  # the default of the setting adapter_check_timeout_s: one reference() run
BASE_TTL_S = 600.0       # the current files' issues are run again after this long even if nothing changed
WORKER_IDLE_S = 600.0    # a check after a longer pause restarts the warm child (no timer: it waits for that check)
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


def _consts(node: dict) -> list | None:
    """The values of a ``oneOf`` / ``anyOf`` whose every branch is a ``const`` (an enum with a title per value, as the
    settings page shows it), else None."""
    opts = node.get("oneOf", node.get("anyOf"))
    if isinstance(opts, list) and opts and all(isinstance(o, dict) and "const" in o for o in opts):
        return [o["const"] for o in opts]
    return None


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
        if ("enum" in node and value not in node["enum"]) or ("const" in node and value != node["const"]) or (
                (consts := _consts(node)) is not None and value not in consts):
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
    code: str | None = None     # why, for the page: timeout | failed | not_loaded


_lock = threading.Lock()
_base: dict[tuple, tuple[float, frozenset]] = {}  # the current files' issue keys, see the module's docstring
_warm: dict[str, Any] = {"worker": None, "key": None, "used": 0.0}


def reset() -> None:
    """Ends the warm child and forgets the cached runs (tests; the server's exit)."""
    with _lock:
        _close_worker()
        _base.clear()


def _close_worker() -> None:
    w, _warm["worker"], _warm["key"] = _warm["worker"], None, None
    if w is not None:
        w.close()


atexit.register(reset)


def _adapter_key(handle: Any) -> tuple:
    """What the warm child holds: the adapter's own files (not the config folder, which changes with every save)."""
    try:
        return handle.spec, loader.fingerprint(handle.spec or None, None)
    except Exception:  # noqa: BLE001 - a module spec the parent cannot resolve: its name and version only
        return handle.spec, handle.version


def _worker(handle: Any) -> runner.Worker:
    key, now = _adapter_key(handle), time.monotonic()
    if _warm["worker"] is not None and (_warm["key"] != key or now - _warm["used"] > WORKER_IDLE_S):
        _close_worker()
    if _warm["worker"] is None:
        _warm["worker"], _warm["key"] = runner.Worker(), key
    _warm["used"] = now
    return _warm["worker"]


def _check_dir(cfg: Path | None) -> tempfile.TemporaryDirectory:
    """The candidate's folder, beside the config folder (the same parent) when it can be made there, so that a path
    of the files relative to the config folder (``options.json`` ``report_dirs``, say: the adapter joins it to
    ``ctx.config_dir``) reaches what it reaches from the real folder: ``../x`` the same folder, ``x`` through a link
    (``_copy_config``). A dot name, so the adapter's and the backend's file lists skip it; else the system temp."""
    if cfg is not None and cfg.is_dir():
        try:
            return tempfile.TemporaryDirectory(prefix=".desk-settings-check-", dir=cfg.parent)
        except OSError:
            pass  # a read-only parent: relative paths then miss, as before #130
    return tempfile.TemporaryDirectory(prefix="desk-settings-check-")


def _link_dir(src: Path, dest: Path) -> None:
    try:
        dest.symlink_to(src, target_is_directory=True)
    except OSError:
        if os.name == "nt":  # no right to make symlinks (no developer mode): a junction needs none
            try:
                import _winapi
                _winapi.CreateJunction(str(src), str(dest))
            except OSError:
                pass


def _copy_config(cfg: Path | None, dest: Path) -> None:
    if cfg is None or not cfg.is_dir():
        return
    for f in cfg.iterdir():
        if f.name.startswith("."):
            continue
        if f.is_file() and f.stat().st_size <= MAX_COPY_BYTES:
            shutil.copy2(f, dest / f.name)
        elif f.is_dir():  # a subfolder (reports under the config folder, say): linked, never copied
            _link_dir(f.resolve(), dest / f.name)


def _plain_issue(i: dict) -> dict:
    src = i.get("src") if isinstance(i.get("src"), str) else None
    m = _SRC_ROW.match(src or "")
    f = _FIELD.search(i.get("message") or "")
    return {"severity": i.get("severity"), "code": i.get("code"), "message": i.get("message") or "",
            "src": src, "scope": i.get("scope"), "count": i.get("count") or 1, "origin": i.get("origin") or "adapter",
            "row": int(m.group(1)) - 1 if m else None, "field": f.group(1) if f else None}


def _key(i: dict) -> tuple:
    return (i["severity"], i["code"], i["src"], i["message"], json.dumps(i["scope"], sort_keys=True))


def _reference_issues(handle: Any, config_dir: str | None, timeout_s: float) -> list[dict]:
    h = dataclasses.replace(handle, config_dir=config_dir)
    result = runner.call_reference(h, None, timeout_s, worker=_worker(handle))
    return [_plain_issue(i) for i in check_reference_tables(result)[1]]


def _failure(f: AdapterFailure) -> DryRun:
    return DryRun([], f"reference() failed: {f.kind} {f.type}", "timeout" if f.kind == "timed_out" else "failed")


def dry_run(handle: Any, cfg: Path | None, name: str, text: str, timeout_s: float = DRY_RUN_TIMEOUT_S) -> DryRun:
    """``reference()`` of the adapter over a copy of the config folder with ``name`` replaced by ``text``, and of
    the folder as it is (cached, see the module's docstring): the candidate's issues, each marked ``new`` when the
    current files do not have it. ``timeout_s`` bounds each run."""
    if not handle.ok:
        return DryRun([], f"the adapter is not loaded: {handle.error}", "not_loaded")
    with _lock:
        key = (handle.spec, handle.fingerprint, str(cfg) if cfg else None, tuple(getattr(handle, "inbox_dirs", ())))
        hit = _base.get(key)
        if hit is not None and time.monotonic() - hit[0] <= BASE_TTL_S:
            base = hit[1]
        else:
            try:
                base = frozenset(_key(i) for i in _reference_issues(handle, str(cfg) if cfg else None, timeout_s))
                _base.clear()  # one folder is edited at a time: only its latest state is worth keeping
                _base[key] = (time.monotonic(), base)
            except AdapterFailure as f:
                if f.kind == "timed_out":
                    return _failure(f)  # the candidate would hit the same limit: say so now, not after a second wait
                base = frozenset()  # the current config already fails: every issue of the candidate is worth showing
        with _check_dir(cfg) as d:
            tmp = Path(d)
            _copy_config(cfg, tmp)
            (tmp / name).write_text(text, encoding="utf-8")
            try:
                found = _reference_issues(handle, str(tmp), timeout_s)
            except AdapterFailure as f:
                return _failure(f)
    return DryRun([{**i, "new": _key(i) not in base} for i in found])
