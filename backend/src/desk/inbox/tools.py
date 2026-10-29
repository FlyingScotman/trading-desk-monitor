"""The adapter's optional tools of SPI 1 (08 §3, §4, since 28.10.2026): ``status(ctx)``, the adapter's own state for the
settings page, and ``describe()["actions"]`` with ``run_action(action_id, params, ctx)``, jobs the owner starts there
(a mapping export, a rebuild of a lookup file), whose files the backend writes into ``<home>/adapter-output/<action>/``
for the inbox to read like any report. Neither bumps the SPI: an adapter without them loads as before.

This module checks what crosses the boundary and writes the files; it runs nothing (``desk.adapter_jobs`` calls the
child through ``desk.inbox.runner``). Standard library and ``desk.inbox`` only, as the rest of the kit. Like every
answer of the adapter, panels, lines, tables, summaries and issue messages must hold no cell values (08 §6.3): the
backend cannot tell, so it bounds their size and shows them only on the local settings page; the files are another
matter, they are reports the adapter made and go through ``match`` / ``read``.

Shapes (08 §3):

* ``status(ctx) -> {"panels": [{"id", "title", "level", "lines", "table"?}], "issues"?: [...]}``, ``level`` one of
  ``LEVELS``, ``table`` ``{"columns": [str], "rows": [[str | number | null]]}``, issues as §6.3;
* an action ``{"id": "^[a-z0-9_]{1,40}$", "title", "description", "params"?: JSON Schema object, "output_dir"?: bool}``;
* ``run_action(...) -> {"ok": bool, "summary": [str], "table"?, "files": [{"name", "text"}], "issues"?}``. Files are
  written only when ``ok`` (``ok`` false: the action's previous files stay); an action that declares ``output_dir``
  replaces its previous files even with an empty list, one that does not only when it returns some. A file name is a plain name (no folder, at most
  ``MAX_NAME`` characters, none of ``BAD_NAME_CHARS``, not hidden), the texts together at most ``MAX_OUTPUT_BYTES``
  of UTF-8.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from desk.inbox.validate import _issue_ok

ACTION_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
LEVELS = ("ok", "info", "warning", "error")
MAX_PANELS, MAX_LINES, MAX_TEXT = 50, 200, 2000          # panels, lines per panel or summary, characters per line
MAX_COLUMNS, MAX_ROWS = 50, 5000                           # of a table
MAX_ISSUES = 500
MAX_FILES = 500
MAX_NAME = 255
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
BAD_NAME_CHARS = set('/\\:*?"<>|\0')
STAGING = ".adapter-output-staging"  # next to adapter-output in <home>, never inside the scanned tree
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


# ---- describe()["actions"] ---------------------------------------------------------------------------------------


def check_actions(desc: object, has_run_action: bool) -> tuple[list[dict], list[str]]:
    """The usable ``describe()["actions"]`` ``{id, title, description, params, output_dir}`` and warnings for the rest.
    Never fatal: a bad action is left out; without ``run_action`` the whole list is."""
    raw = desc.get("actions") if isinstance(desc, dict) else None
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["actions: not a list, ignored"]
    if not has_run_action:
        return [], ["actions: the adapter has no run_action(), ignored"]
    out: list[dict] = []
    warnings: list[str] = []
    for i, a in enumerate(raw):
        aid = a.get("id") if isinstance(a, dict) else None
        if not (isinstance(aid, str) and ACTION_ID_RE.match(aid)):
            warnings.append(f"actions[{i}]: id must match ^[a-z0-9_]{{1,40}}$, ignored")
            continue
        if any(o["id"] == aid for o in out):
            warnings.append(f"actions[{i}]: {aid} declared twice, ignored")
            continue
        params = a.get("params")
        if params is not None and not (isinstance(params, dict) and params.get("type") == "object"):
            warnings.append(f"actions[{i}]: params must be a JSON Schema of type object, ignored")
            continue
        for key, want in (("title", str), ("description", str), ("output_dir", bool)):
            if a.get(key) is not None and not isinstance(a[key], want):
                warnings.append(f"actions[{i}]: {key} must be {want.__name__}")
        out.append({"id": aid, "title": a["title"] if isinstance(a.get("title"), str) else aid,
                    "description": a["description"] if isinstance(a.get("description"), str) else "",
                    "params": params or {"type": "object", "properties": {}},
                    "output_dir": a.get("output_dir") is True})
    return out, warnings


# ---- what comes back ---------------------------------------------------------------------------------------------


class BadResult(ValueError):
    """A result that breaks the shapes above; the message says where (field names only)."""


def _text(v: Any, where: str) -> str:
    if not isinstance(v, str):
        raise BadResult(f"{where}: must be a string")
    return v[:MAX_TEXT]


def _lines(v: Any, where: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise BadResult(f"{where}: must be a list of strings")
    return [_text(x, f"{where}[{i}]") for i, x in enumerate(v[:MAX_LINES])]


def check_table(t: Any, where: str = "table") -> dict | None:
    """``{"columns": [str], "rows": [[str | number | null]]}`` with rows as long as the columns, or None."""
    if t is None:
        return None
    if not isinstance(t, dict) or not isinstance(t.get("columns"), list) or not isinstance(t.get("rows"), list):
        raise BadResult(f"{where}: must be {{columns, rows}}")
    cols = [_text(c, f"{where}.columns[{i}]") for i, c in enumerate(t["columns"])]
    if not cols or len(cols) > MAX_COLUMNS:
        raise BadResult(f"{where}.columns: 1 to {MAX_COLUMNS} names")
    rows = []
    for i, r in enumerate(t["rows"][:MAX_ROWS]):
        if not isinstance(r, list) or len(r) != len(cols):
            raise BadResult(f"{where}.rows[{i}]: a list as long as the columns")
        for j, c in enumerate(r):
            if c is not None and (isinstance(c, bool) or not isinstance(c, (str, int, float))):
                raise BadResult(f"{where}.rows[{i}][{j}]: a string, a number or null")
        rows.append([c[:MAX_TEXT] if isinstance(c, str) else c for c in r])
    return {"columns": cols, "rows": rows, "truncated": len(t["rows"]) > MAX_ROWS}


def _issues(v: Any, where: str = "issues") -> list[dict]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(_issue_ok(i) for i in v):
        raise BadResult(f"{where}: issues as 08 §6.3 (severity, code, src, scope, message, count)")
    return [{"severity": i["severity"], "code": i["code"], "src": i.get("src"), "scope": i.get("scope"),
             "message": (i.get("message") or "")[:MAX_TEXT], "count": i.get("count") or 1, "origin": "adapter"}
            for i in v[:MAX_ISSUES]]


def check_status(result: Any) -> dict:
    """``status(ctx)`` as the page shows it: ``{panels, issues, problems}``. A bad panel is left out with a problem
    (``problems``: what was wrong, field names only); a result that is not an object raises ``BadResult``."""
    if not isinstance(result, dict) or not isinstance(result.get("panels"), list):
        raise BadResult("status() must return {panels: [...], issues?: [...]}")
    panels, problems, seen = [], [], set()
    for i, p in enumerate(result["panels"][:MAX_PANELS]):
        try:
            if not isinstance(p, dict):
                raise BadResult(f"panels[{i}]: must be an object")
            pid, level = _text(p.get("id"), f"panels[{i}].id"), p.get("level")
            if pid in seen:
                raise BadResult(f"panels[{i}]: id {pid} repeats")
            if level not in LEVELS:
                raise BadResult(f"panels[{i}].level: one of {', '.join(LEVELS)}")
            panels.append({"id": pid, "title": _text(p.get("title", pid), f"panels[{i}].title"), "level": level,
                           "lines": _lines(p.get("lines"), f"panels[{i}].lines"),
                           "table": check_table(p.get("table"), f"panels[{i}].table")})
            seen.add(pid)
        except BadResult as e:
            problems.append(str(e))
    try:
        issues = _issues(result.get("issues"))
    except BadResult as e:
        issues, problems = [], [*problems, str(e)]
    return {"panels": panels, "issues": issues, "problems": problems}


def check_file_name(name: Any) -> str | None:
    """Why ``name`` cannot be an output file's name, or None."""
    if not isinstance(name, str) or not name.strip():
        return "a non-empty string"
    if len(name) > MAX_NAME:
        return f"at most {MAX_NAME} characters"
    if any(c in BAD_NAME_CHARS or ord(c) < 32 for c in name):
        return "a plain file name: no folder, none of \\ / : * ? \" < > |"
    if name in (".", "..") or name.startswith((".", "~$")) or name.endswith((" ", ".", ".tmp", ".part")):
        return "not hidden or temporary (the inbox skips those)"
    if name.split(".")[0].upper() in _RESERVED:
        return "not a reserved Windows device name"
    return None


def check_action_result(result: Any, decl: dict) -> dict:
    """``run_action(...)`` as the job keeps it: ``{ok, summary, table, files, issues, warnings}``; ``files`` keep their
    text. Raises ``BadResult`` on a broken shape or a file that cannot be written (then nothing is written)."""
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise BadResult("run_action() must return {ok, summary, table?, files, issues?}")
    files = result.get("files") or []
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise BadResult(f"files: a list of at most {MAX_FILES} {{name, text}}")
    names: set[str] = set()
    total = 0
    for i, f in enumerate(files):
        if not isinstance(f, dict) or not isinstance(f.get("text"), str):
            raise BadResult(f"files[{i}]: must be {{name, text}} with text a string")
        why = check_file_name(f.get("name"))
        if why:
            raise BadResult(f"files[{i}].name: {why}")
        if f["name"].lower() in names:  # Windows: names differing in case only are one file
            raise BadResult(f"files[{i}].name: repeats another file's name")
        names.add(f["name"].lower())
        total += len(f["text"].encode("utf-8"))
    if total > MAX_OUTPUT_BYTES:
        raise BadResult(f"files: more than {MAX_OUTPUT_BYTES // 1024 // 1024} MB together")
    warnings: list[str] = []
    return {"ok": result["ok"], "summary": _lines(result.get("summary"), "summary"),
            "table": check_table(result.get("table")), "issues": _issues(result.get("issues")),
            "files": [{"name": f["name"], "text": f["text"]} for f in files], "warnings": warnings}


# ---- the output folder -------------------------------------------------------------------------------------------


def write_outputs(output_root: Path, action_id: str, files: list[dict]) -> list[Path]:
    """The action's files as ``<output_root>/<action_id>/<name>`` (UTF-8, as given), replacing that action's previous
    files: written into a staging folder next to ``output_root`` (outside the scanned tree), then put in place by
    renames, so a scan sees the old set or the new one, never half of it. Returns the new paths."""
    if not ACTION_ID_RE.match(action_id):
        raise ValueError("bad action id")
    output_root.mkdir(parents=True, exist_ok=True)
    staging = output_root.parent / STAGING
    staging.mkdir(exist_ok=True)
    token = uuid.uuid4().hex[:12]
    new, old, target = staging / f"{action_id}-{token}", staging / f"{action_id}-{token}.old", output_root / action_id
    new.mkdir()
    try:
        for f in files:
            (new / f["name"]).write_bytes(f["text"].encode("utf-8"))
        if target.exists():
            os.replace(target, old)
        os.replace(new, target)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        if old.exists() and not target.exists():
            os.replace(old, target)  # put the previous set back
        raise
    shutil.rmtree(old, ignore_errors=True)
    return [target / f["name"] for f in files]
