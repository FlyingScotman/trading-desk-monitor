"""What the backend checks in a ``read`` result before anything is stored (08 §6, §10). Pure, standard library.

``check_read`` splits the file into replacement units (dataset, scope unit, report date) and decides each:

* a malformed result, a row that cannot be placed in a declared unit, an adapter ``error`` without ``scope``
  or a control total that does not re-sum rejects the **file** (``file_error``);
* a row that fails its schema, a duplicate dataset key, an unknown book or an adapter ``error`` with
  ``scope`` rejects **that unit** only; it keeps its previous revision (08 §6.8);
* under ``covers = all_active_books`` every active book missing from ``scopes`` becomes an accepted empty
  unit with a ``book_closed_by_omission`` warning (08 §4).

Backend issues follow the adapter's issue shape (no values in messages, only canonical field names) and are
aggregated per (severity, code, unit) with a count and the first ``_src``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from desk.inbox.schema import amount, is_date, load_schema, validate_row
from desk.inbox.spi import (
    CODE_RE,
    CONTRACT,
    CONTRACT_MAJOR,
    CONTRACT_MINOR,
    DATASET_KEYS,
    HIT_KEYS,
    MULTI_DATE_DATASETS,
    READ_KEYS,
    RESERVED_KEYS,
    SEVERITIES,
    parse_contract,
    scope_keys,
    source_datasets,
)
from desk.inbox.timesem import parse_ts

BOND_SIZE_FIELDS = ("quantity", "face_amount_ccy", "face_outstanding_ccy")


@dataclass
class Checked:
    file_error: str | None                  # an error code rejecting the whole file, else None
    units: list[dict] = field(default_factory=list)   # {dataset, scope, report_date, status, rows}
    issues: list[dict] = field(default_factory=list)  # {severity, code, src, scope, message, count, origin}
    controls_status: str = "no_controls"    # ok | mismatch | no_controls
    meta: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        """File status of 08 §6.8 (``failed``/``timed_out`` come from the runner, not from here)."""
        if self.file_error:
            return "rejected"
        states = {u["status"] for u in self.units}
        if "rejected" in states:
            return "partially_accepted" if "accepted" in states else "rejected"
        return "accepted"


def unit_hash(rows: list[dict]) -> str:
    """Change hash of a unit (08 §6.6): canonical fields only (``_*`` and ``x_*`` excluded, absent == null),
    independent of row order."""
    canon = sorted(json.dumps({k: v for k, v in r.items() if v is not None and not k.startswith(("_", "x_"))},
                              sort_keys=True, separators=(",", ":"), ensure_ascii=False) for r in rows)
    return hashlib.sha256("\n".join(canon).encode("utf-8")).hexdigest()


def check_hit(source_decl: dict | None, hit: Any) -> str | None:
    """Why a ``match`` hit is unusable for its declared source (08 §5), or None."""
    if not isinstance(hit, dict) or not set(hit) <= HIT_KEYS:
        return "bad_hit: not an object with the keys of 08 §5"
    if source_decl is None or hit.get("source") != source_decl.get("source"):
        return "bad_hit: source not declared by describe()"
    if not is_date(hit.get("report_date")):
        return "bad_hit: report_date must be YYYY-MM-DD"
    if hit.get("date_from") not in (None, "name", "header"):
        return "bad_hit: date_from must be name or header"
    ts = hit.get("report_ts")
    if source_decl.get("time_basis") == "intraday" and ts is None:
        return "bad_hit: report_ts is required for intraday"
    if ts is not None and not _is_ts(ts):
        return "bad_hit: report_ts must be ISO 8601"
    if hit.get("seq") is not None and type(hit["seq"]) is not int:
        return "bad_hit: seq must be an int"
    return None


def _is_ts(v: Any) -> bool:
    try:
        return isinstance(v, str) and parse_ts(v) is not None
    except ValueError:
        return False


def _ukey(dataset: str, scope: dict, report_date: str) -> tuple:
    return dataset, tuple(sorted(scope.items())), report_date


class _Reject(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code, self.message = code, message


def check_read(source_decl: dict, hit: dict, result: Any, *, books: list[dict] | None,
               contract: str = CONTRACT) -> Checked:
    """Check one ``read`` result. ``books``: the validated ``reference()["books"]`` rows in force (``active``
    defaults to true), or None to skip the book checks (no ``unknown_book``, no completion of
    ``all_active_books``). ``contract``: the adapter's ``describe()["contract"]``; a newer minor drops unknown
    fields with ``field_from_newer_contract`` warnings instead of rejecting them (08 §9)."""
    issues: list[dict] = []
    units: dict[tuple, dict] = {}
    try:
        try:
            status = _check(source_decl, hit, result, books, contract, issues, units)
        except TypeError:  # an unhashable or mistyped value somewhere in the result: a protocol break
            raise _Reject("bad_result_shape", "a value of the result has the wrong type") from None
    except _Reject as r:
        issues.append(_issue("error", r.code, None, None, r.message))
        meta = result.get("meta") if isinstance(result, dict) else None
        return Checked(r.code, [{**u, "status": "rejected", "rows": []} for u in units.values()], issues,
                       "mismatch" if r.code == "control_mismatch" else "no_controls",
                       meta if isinstance(meta, dict) else {})
    return Checked(None, list(units.values()), issues, status, result.get("meta") or {})


def _issue(severity: str, code: str, scope: dict | None, src: str | None, message: str, count: int = 1,
           origin: str = "backend") -> dict:
    return {"severity": severity, "code": code, "src": src, "scope": scope, "message": message, "count": count,
            "origin": origin}


def _issue_ok(i: Any) -> bool:
    opt = lambda k, t: i.get(k) is None or isinstance(i[k], t)
    return (isinstance(i, dict) and set(i) <= {"severity", "code", "src", "scope", "message", "count"}
            and isinstance(i.get("severity"), str) and i["severity"] in SEVERITIES and isinstance(i.get("code"), str) and bool(CODE_RE.match(i["code"]))
            and opt("message", str) and opt("src", str) and opt("scope", dict)
            and (i.get("count") is None or (type(i["count"]) is int and i["count"] >= 1)))


def _check(decl: dict, hit: dict, result: Any, books: list[dict] | None, contract: str,
           issues: list[dict], units: dict[tuple, dict]) -> str:
    why = check_hit(decl, hit)
    if why:
        raise _Reject("bad_hit", why.split(": ", 1)[1])
    datasets = source_datasets(decl)
    if any(load_schema(d) is None for d in datasets):
        raise _Reject("dataset_not_supported", "dataset not supported by this backend yet")
    if not isinstance(result, dict) or not set(result) <= READ_KEYS or not isinstance(result.get("rows"), list):
        raise _Reject("bad_result_shape", "read() must return rows, scopes, issues, controls, meta (08 §6)")
    rows, adapter_issues = result["rows"], result.get("issues") or []
    controls, meta = result.get("controls") or {}, result.get("meta") or {}
    if not (all(isinstance(r, dict) for r in rows) and isinstance(adapter_issues, list)
            and isinstance(controls, dict) and isinstance(meta, dict)):
        raise _Reject("bad_result_shape", "rows must be objects; issues a list; controls and meta objects")
    if meta.get("generated_at") is not None and not _is_ts(meta["generated_at"]):
        raise _Reject("bad_result_shape", "meta.generated_at must be ISO 8601")

    multi = "datasets" in decl
    full = decl["completeness"] == "full_snapshot"
    hit_date = hit["report_date"]
    parsed = parse_contract(contract)
    newer = bool(parsed and parsed[0] == CONTRACT_MAJOR and parsed[1] > CONTRACT_MINOR)

    def scope_of(item: dict, what: str) -> tuple[str, dict, str]:
        """(dataset, scope, report_date) of a scopes item, an issue scope or a row; raises on a bad shape."""
        ds = item.get("_dataset") if multi else datasets[0]
        if ds not in datasets:
            raise _Reject(f"bad_{what}", f"{what}: _dataset missing or not a dataset of the source")
        keys = scope_keys(decl, ds)
        rd = item.get("_report_date", hit_date)
        if "_report_date" in item and (ds not in MULTI_DATE_DATASETS or not is_date(rd)):
            raise _Reject(f"bad_{what}", f"{what}: _report_date only in multi-date datasets, as YYYY-MM-DD")
        scope = {k: item.get(k) for k in keys}
        if not all(isinstance(v, str) and v for v in scope.values()):
            raise _Reject(f"bad_{what}", f"{what}: every scope field ({', '.join(keys)}) must be a string")
        return ds, scope, rd

    # Scopes: the units this file replaces (08 §6.2).
    if full:
        scopes = result.get("scopes")
        if scopes is None:
            raise _Reject("missing_scopes", "a full_snapshot read must list its scope units")
        if not isinstance(scopes, list) or not all(isinstance(s, dict) for s in scopes):
            raise _Reject("bad_scopes", "scopes must be a list of objects")
        for item in scopes:
            ds, scope, rd = scope_of(item, "scopes")
            if set(item) - {"_dataset", "_report_date"} != set(scope):
                raise _Reject("bad_scopes", f"scopes: items have exactly the scope keys ({', '.join(scope)})")
            units.setdefault(_ukey(ds, scope, rd), {"dataset": ds, "scope": scope, "report_date": rd,
                                                    "status": "accepted", "rows": []})

    # Adapter issues: shape first, effects after the rows.
    checked_issues = []
    for i in adapter_issues:
        if not _issue_ok(i):
            raise _Reject("bad_issue", "issues need severity, a code matching ^[a-z][a-z0-9_]{1,40}$, "
                                       "optional src, scope, message, count (08 §6.3)")
        if i.get("scope") is not None and not all(isinstance(v, str) for v in i["scope"].values()):
            raise _Reject("bad_issue", "an issue scope is shaped like a scopes item")
        checked_issues.append(i)
        issues.append(_issue(i["severity"], i["code"], i.get("scope"), i.get("src"), i.get("message") or "",
                             i.get("count") or 1, origin="adapter"))

    # Rows.
    agg: dict[tuple, list] = {}
    rejected: set[tuple] = set()

    def flag(severity: str, code: str, uk: tuple, src: Any, message: str) -> None:
        a = agg.setdefault((severity, code, uk, message), [0, src if isinstance(src, str) else None])
        a[0] += 1
        if severity == "error":
            rejected.add(uk)

    keys_seen: set[tuple] = set()
    by_dataset: dict[str, list[dict]] = {d: [] for d in datasets}
    known = None if books is None else {b["book_id"] for b in books}
    for n, row in enumerate(rows, 1):
        bad = [k for k in row if k.startswith("_") and k not in RESERVED_KEYS]
        if bad:
            raise _Reject("reserved_key", f"row {n}: keys starting with _ are reserved (08 §3)")
        ds, scope, rd = scope_of(row, "row")
        uk = _ukey(ds, scope, rd)
        if uk not in units:
            if full:
                raise _Reject("row_outside_scopes", f"row {n}: its scope unit is not listed in scopes")
            units[uk] = {"dataset": ds, "scope": scope, "report_date": rd, "status": "accepted", "rows": []}
        by_dataset[ds].append(row)
        src = row.get("_src")
        norm, errs, dropped = validate_row(ds, {k: v for k, v in row.items() if k != "_report_date"},
                                           allow_newer=newer)
        for e in errs:
            flag("error", e.code, uk, src, f"field {e.field}" if e.field else "field not in the contract")
        if dropped:
            flag("warning", "field_from_newer_contract", uk, src, "field of a newer contract minor dropped")
        if ds == "positions" and not errs:
            if norm["asset_class"] == "bond" and not any(f in norm for f in BOND_SIZE_FIELDS):
                flag("error", "missing_required", uk, src, "a bond needs quantity, face_amount_ccy or "
                                                           "face_outstanding_ccy")
            if norm["asset_class"] == "fx" and "amount_ccy" not in norm:
                flag("error", "missing_required", uk, src, "an fx row needs amount_ccy")
        if ds in DATASET_KEYS:  # normalised values where valid (1.0 == "1"), repr so a list cannot break the set
            k = (ds, rd, tuple(repr({**row, **norm}.get(f)) for f in DATASET_KEYS[ds]))
            if k in keys_seen:
                flag("error", "duplicate_key", uk, src, f"key ({', '.join(DATASET_KEYS[ds])}) repeated")
            keys_seen.add(k)
        if (known is not None and "book_id" not in scope and isinstance(row.get("book_id"), str)
                and row["book_id"] not in known):
            flag("error", "unknown_book", uk, src, "book_id not in reference() books")
        if not errs:
            units[uk]["rows"].append(norm)
    if known is not None:
        for uk, u in units.items():
            if "book_id" in u["scope"] and u["scope"]["book_id"] not in known:
                flag("error", "unknown_book", uk, None, "book_id not in reference() books")

    # Adapter errors take effect: unit-scoped rejects matching units, otherwise the file. A scope naming no
    # unit of the file is a unit of its own, rejected (it keeps its previous revision, never closed by omission).
    for i in checked_issues:
        if i["severity"] != "error":
            continue
        if not i.get("scope"):
            raise _Reject(i["code"], "the adapter reported an error for the whole file")
        s = i["scope"]
        hits = [uk for uk, u in units.items()
                if s.get("_dataset", u["dataset"]) == u["dataset"] and s.get("_report_date", u["report_date"])
                == u["report_date"] and all(u["scope"].get(k) == v for k, v in s.items() if not k.startswith("_"))]
        if not hits:
            try:
                ds, scope, rd = scope_of(s, "issue")
            except _Reject:
                ds = None
            if ds is None or set(s) - {"_dataset", "_report_date"} != set(scope):
                raise _Reject(i["code"], "the adapter reported an error for a scope that is not a unit")
            uk = _ukey(ds, scope, rd)
            units[uk] = {"dataset": ds, "scope": scope, "report_date": rd, "status": "rejected", "rows": []}
            hits = [uk]
        rejected.update(hits)

    # covers = all_active_books: the active books not listed are listed and empty (08 §4).
    if full and decl.get("covers") == "all_active_books" and books is not None:
        for ds in datasets:
            keys = scope_keys(decl, ds)
            dates = {u["report_date"] for u in units.values() if u["dataset"] == ds} or {hit_date}
            for rd in sorted(dates):
                listed = {u["scope"]["book_id"] for u in units.values()
                          if u["dataset"] == ds and u["report_date"] == rd}
                for b in books:
                    if b.get("active", True) and b["book_id"] not in listed:
                        scope = {k: b.get(k) for k in keys}
                        uk = _ukey(ds, scope, rd)
                        units[uk] = {"dataset": ds, "scope": scope, "report_date": rd, "status": "accepted",
                                     "rows": []}
                        flag("warning", "book_closed_by_omission", uk, None, "active book missing from scopes")

    for (severity, code, uk, message), (count, src) in agg.items():
        u = units[uk]
        scope = {**u["scope"], **({"_dataset": u["dataset"]} if multi else {}),
                 **({"_report_date": u["report_date"]} if u["report_date"] != hit_date else {})}
        issues.append(_issue(severity, code, scope, src, message, count))
    for uk in rejected:
        if uk in units:
            units[uk].update(status="rejected", rows=[])

    # Controls: re-sum what the report states (08 §6.4); a difference rejects the file.
    if not controls:
        return "no_controls"
    for ds, c in controls.items():
        schema = load_schema(ds) if ds in datasets else None
        if schema is None or not isinstance(c, dict) or not set(c) <= {"row_count", "sums"}:
            raise _Reject("bad_controls", "controls are keyed by the source's datasets: row_count, sums")
        rc, sums = c.get("row_count"), c.get("sums") or {}
        numeric = {k for k, v in schema["properties"].items() if v.get("$ref") == "#/$defs/amount"}
        if (rc is not None and (type(rc) is not int or rc < 0)) or not isinstance(sums, dict) \
                or not set(sums) <= numeric or any(amount(v) is None for v in sums.values()):
            raise _Reject("bad_controls", "row_count is an int; sums are amounts keyed by numeric fields")
        if rc is not None and rc != len(by_dataset[ds]):
            raise _Reject("control_mismatch", f"{ds}: row count differs from the stated one")
        for f, stated in sums.items():
            total = amount(stated)
            assert total is not None
            got = sum((amount(r.get(f)) or Decimal(0) for r in by_dataset[ds]), Decimal(0))
            if abs(got - total) > max(Decimal("0.01"), Decimal("1e-8") * abs(total)):
                raise _Reject("control_mismatch", f"{ds}: sum of {f} differs from the stated total")
    return "ok"


def check_reference(result: Any) -> tuple[list[dict] | None, list[dict]]:
    """Validate ``reference(ctx)`` (08 §7, §10 books): (valid normalised books, issues). A malformed result
    gives (None, [error]); an invalid or repeated book row is left out with an error issue, so its positions
    meet ``unknown_book`` instead of a half-described book. The adapter's own ``issues`` (optional, the shape
    of a read's issues) are passed on with ``origin`` adapter."""
    raw_issues = result.get("issues") or [] if isinstance(result, dict) else []
    if not isinstance(result, dict) or not isinstance(result.get("books"), list) \
            or not all(isinstance(b, dict) for b in result["books"]) \
            or not isinstance(raw_issues, list) or not all(_issue_ok(i) for i in raw_issues):
        return None, [_issue("error", "bad_reference", None, None,
                             "reference() must return {books: [...], issues: [...]}")]
    valid = []
    issues = [_issue(i["severity"], i["code"], i.get("scope"), i.get("src"), i.get("message") or "",
                     i.get("count") or 1, origin="adapter") for i in raw_issues]
    for row in result["books"]:
        bad_keys = [k for k in row if k.startswith("_") and k != "_src"]
        norm, errs, _ = validate_row("books", row)
        src = row.get("_src") if isinstance(row.get("_src"), str) else None
        if bad_keys:
            issues.append(_issue("error", "reserved_key", None, src, "books rows carry only _src"))
        for e in errs:
            issues.append(_issue("error", e.code, None, src, f"field {e.field}" if e.field else "field not in the contract"))
        if not errs and not bad_keys:
            valid.append(norm)
    counts = Counter(b["book_id"] for b in valid)
    for book_id in sorted(b for b, n in counts.items() if n > 1):  # two definitions: neither is trusted
        issues.append(_issue("error", "duplicate_key", {"book_id": book_id}, None, "book_id repeated"))
    return [b for b in valid if counts[b["book_id"]] == 1], issues
