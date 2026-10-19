"""Conformance kit (08 §8): run an adapter over folders through the same child-process runner, without a store.

``python -m desk.inbox.conformance --adapter X --dir D [--dir D2] [--stub-ctx] [--snapshots S] [--config C]
[--json]``; ``desk adapter check`` calls ``check``. Standard library and ``desk.inbox`` only, so the adapter's
tests can import it from a sibling checkout.

The summary is shareable outside work: per source and report date it holds counts and codes only, counts
below 10 are shown as ``<10``, and there are no values, file or sheet names, or exception messages.

``Report.ok`` judges the adapter, not the data: it fails on a load error, an exception or timeout in
``match``/``read``/``reference``, a result that breaks the protocol (``PROTOCOL_CODES``) or a second read
that differs. Data issues (a bad number, an unknown book, a control mismatch) are reported, not failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import desk
from desk.inbox import runner
from desk.inbox.loader import load_adapter
from desk.inbox.schema import amount, norm_ccy
from desk.inbox.spi import DATASETS_WITH_BOOK, SPI_VERSION, AdapterFailure, source_datasets
from desk.inbox.static_index import (
    CalendarUnavailable,
    Instruments,
    build_index,
    business_calendar,
    covering,
    static_snapshots,
)
from desk.inbox.timesem import report_date_suspicious, revision_key, valid_date
from desk.inbox.validate import check_hit, check_read, check_reference, unit_hash

PROTOCOL_CODES = frozenset({
    "bad_hit", "bad_result_shape", "bad_scopes", "bad_row", "bad_issue", "bad_controls", "missing_scopes",
    "row_outside_scopes", "reserved_key", "bad_reference",
})
_SKIP_PREFIX, _SKIP_SUFFIX = ("~$", "."), (".tmp", ".part")
_WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def masked(n: int) -> int | str:
    return "<10" if 0 < n < 10 else n


def list_files(dirs: list[str]) -> list[str]:
    """Files of the folders (a folder ending in ``**`` recursively), without temp and hidden files (08 §13)."""
    out = []
    for d in dirs:
        recursive = d.rstrip("/\\").endswith("**")
        root = Path(d.rstrip("/\\").removesuffix("**") if recursive else d)
        for p in sorted(root.rglob("*") if recursive else root.glob("*")):
            if p.is_file() and not p.name.startswith(_SKIP_PREFIX) and not p.name.endswith(_SKIP_SUFFIX):
                out.append(str(p))
    return out


@dataclass
class Group:
    """One (source, report date): raw counts; the masked view is built on output."""

    source: str
    report_date: str
    valid_date: str | None = None
    files: int = 0
    rows: int = 0
    units: Counter = field(default_factory=Counter)      # accepted / rejected
    statuses: Counter = field(default_factory=Counter)   # file statuses
    issues: Counter = field(default_factory=Counter)     # (severity, code) -> count
    controls: Counter = field(default_factory=Counter)
    unmapped: int = 0
    quantity_only: int = 0                                 # bonds by quantity whose face the kit does not know
    unmapped_notional: Counter = field(default_factory=Counter)  # ccy -> |face| of the unmapped bonds
    notional: Counter = field(default_factory=Counter)           # ccy -> |face| of all bonds


@dataclass
class Report:
    adapter: dict
    load_error: str | None = None
    instruments: str = "stub"
    calendar: str = "none"
    matched: int = 0
    ignored: int = 0
    match_failed: int = 0
    match_seconds: float = 0.0
    groups: dict[tuple[str, str], Group] = field(default_factory=dict)
    problems: Counter = field(default_factory=Counter)   # conformance failures by code
    notes: Counter = field(default_factory=Counter)      # kit-level info codes (same_valid_date, ...)

    @property
    def ok(self) -> bool:
        return self.load_error is None and not self.problems

    def to_json(self) -> dict:
        def week(attr: str) -> dict:
            c: Counter = Counter()
            for g in self.groups.values():
                if getattr(g, attr):
                    c[_WD[date.fromisoformat(getattr(g, attr)).weekday()]] += g.files
            return {k: masked(v) for k, v in c.items()}
        return {
            "ok": self.ok, "backend": desk.__version__, "spi": SPI_VERSION, **self.adapter,
            "load_error": self.load_error, "instruments": self.instruments, "calendar": self.calendar,
            "files": {"matched": masked(self.matched), "ignored": masked(self.ignored),
                      "match_failed": masked(self.match_failed)},
            "match_seconds": round(self.match_seconds, 2),
            "problems": {k: masked(v) for k, v in sorted(self.problems.items())},
            "notes": {k: masked(v) for k, v in sorted(self.notes.items())},
            "weekdays": {"report_date": week("report_date"), "valid_date": week("valid_date")},
            "sources": [{
                "source": g.source, "report_date": g.report_date, "valid_date": g.valid_date,
                "files": masked(g.files), "rows": masked(g.rows),
                "units": {k: masked(v) for k, v in sorted(g.units.items())},
                "statuses": {k: masked(v) for k, v in sorted(g.statuses.items())},
                "issues": [{"severity": s, "code": c, "count": masked(n)} for (s, c), n in sorted(g.issues.items())],
                "controls": sorted(g.controls),
                "unmapped": masked(g.unmapped), "quantity_only": masked(g.quantity_only),
                "unmapped_share_pct": {c: round(float(100 * g.unmapped_notional[c] / n), 1)
                                       for c, n in sorted(g.notional.items()) if n} or None,
            } for g in sorted(self.groups.values(), key=lambda g: (g.source, g.report_date))],
        }

    def text(self) -> str:
        j = self.to_json()
        lines = [f"desk adapter check: backend {j['backend']}, SPI {j['spi']}, contract {j.get('contract')}",
                 f"adapter {j.get('name')} {j.get('version')}, fingerprint {j.get('fingerprint')}",
                 f"instruments: {j['instruments']}; calendar: {j['calendar']}"]
        if self.load_error:
            return "\n".join([*lines, f"load error: {self.load_error}", "result: FAIL"])
        f = j["files"]
        lines.append(f"files: {f['matched']} matched, {f['ignored']} ignored, {f['match_failed']} match_failed "
                     f"(match {j['match_seconds']} s)")
        for s in j["sources"]:
            vd = s["valid_date"] or "waiting_calendar"
            units = ", ".join(f"{v} {k}" for k, v in s["units"].items()) or "none"
            share = "" if s["unmapped_share_pct"] is None else " (" + ", ".join(
                f"{p}% of {c} notional" for c, p in s["unmapped_share_pct"].items()) + ")"
            lines.append(f"{s['source']} {s['report_date']} -> {vd}: files {s['files']}, rows {s['rows']}, "
                         f"units {units}, controls {'/'.join(s['controls']) or '-'}, unmapped {s['unmapped']}{share}"
                         + (f", quantity only {s['quantity_only']}" if s["quantity_only"] else ""))
            lines += [f"    {i['severity']} {i['code']} {i['count']}" for i in s["issues"]]
        for kind, c in (("weekdays of report_date", j["weekdays"]["report_date"]),
                        ("weekdays of valid_date", j["weekdays"]["valid_date"])):
            lines.append(f"{kind}: " + ", ".join(f"{d} {c[d]}" for d in _WD if d in c))
        lines += [f"note {k} {v}" for k, v in j["notes"].items()]
        lines += [f"problem {k} {v}" for k, v in j["problems"].items()]
        lines.append("result: " + ("OK" if self.ok else "FAIL"))
        return "\n".join(lines)


def _instrument(row: dict, instruments: Instruments, on: str) -> dict | None:
    """The index record of the row's instrument (``instrument_id`` is a secid), or None."""
    keys = [("moex_secid", row.get("instrument_id"))] + [(k, row.get(k)) for k in ("isin", "regnum", "moex_secid")]
    return next((rec for k, v in keys if isinstance(v, str) and (rec := instruments.lookup(k, v, on))), None)


def _notional(row: dict, rec: dict | None) -> tuple[str, Decimal] | None:
    """(currency, |face|) of a bond row: its face amount, else quantity x the instrument's initial face; None for
    a quantity without a known face. Per currency, never summed across currencies.
    shortcut: initial and outstanding face amounts are added alike; upgrade when the share must be exact."""
    ccy = norm_ccy(row["ccy"]) if isinstance(row.get("ccy"), str) else "?"
    for f in ("face_amount_ccy", "face_outstanding_ccy"):
        if (v := amount(row.get(f))) is not None:
            return ccy, abs(v)
    q, face = amount(row.get("quantity")), amount((rec or {}).get("initial_face"))
    if q is None or face is None:
        return None
    return norm_ccy(rec.get("currency") or ccy), abs(q * face)  # type: ignore[union-attr]


def check(adapter_spec: str | None, dirs: list[str], *, stub_ctx: bool = False, snapshot_dir: str | None = None,
          config_dir: str | None = None, expected_sha256: str | None = None, match_timeout_s: float = 10.0,
          read_timeout_s: float = 600.0) -> Report:
    handle = load_adapter(adapter_spec, config_dir, expected_sha256)
    desc = handle.describe
    spec = adapter_spec or runner.REFERENCE_ADAPTER  # a folder by its name only: the path can name the user
    report = Report({"adapter": Path(spec.rstrip("/\\")).name if runner.is_path_spec(spec) else spec,
                     "name": handle.name, "version": handle.version, "contract": desc.get("contract"),
                     "fingerprint": handle.fingerprint})
    if handle.error:
        report.load_error = handle.error
        return report
    with tempfile.TemporaryDirectory(prefix="desk-kit-") as tmp:
        index_path = None
        refs = static_snapshots(snapshot_dir) if snapshot_dir and not stub_ctx else []
        if refs:
            index_path = os.path.join(tmp, "instruments.json")
            report.instruments = build_index(snapshot_dir, index_path)["snapshots"][-1]["id"]
        _run(report, handle, dirs, refs, index_path, snapshot_dir, match_timeout_s, read_timeout_s)
    return report


def _run(report: Report, handle, dirs: list[str], refs: list, index_path: str | None, snapshot_dir: str | None,
         match_timeout_s: float, read_timeout_s: float) -> None:
    desc = handle.describe
    books = None
    if any(d in DATASETS_WITH_BOOK for s in desc["sources"] for d in source_datasets(s)):
        try:
            books, issues = check_reference(runner.call_reference(handle, index_path, read_timeout_s))
        except AdapterFailure as f:
            report.problems[f"reference_{f.kind}"] += 1
            books, issues = None, []
        for i in (i for i in issues if i["severity"] == "error"):
            report.problems[i["code"] if i["code"] in PROTOCOL_CODES else f"reference_{i['code']}"] += 1

    files = list_files(dirs)
    t0 = time.perf_counter()
    hits = runner.call_match(handle, files, index_path, match_timeout_s) if files else {}
    report.match_seconds = time.perf_counter() - t0
    failed = {p for p, h in hits.items() if isinstance(h, dict) and "_error" in h}
    matched = {p: h for p, h in hits.items() if h is not None and p not in failed}  # a non-dict is a bad_hit below
    report.ignored = sum(1 for h in hits.values() if h is None)
    report.match_failed = len(hits) - len(matched) - report.ignored
    report.matched = len(matched)
    if report.match_failed:
        report.problems["match_failed"] += report.match_failed
    if index_path and snapshot_dir:  # lookups on old dates need the static snapshot of those dates
        dates = sorted({h["report_date"] for h in matched.values()
                        if isinstance(h, dict) and isinstance(h.get("report_date"), str)})
        if {covering(refs, d).id for d in dates} - {refs[-1].id}:
            build_index(snapshot_dir, index_path, dates)
    instruments = Instruments.from_file(index_path)

    calendars: dict[str, str] = {}
    cal_by_date: dict[str, object] = {}
    order: dict[tuple, list[tuple]] = defaultdict(list)
    valid_by_source: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path, hit in matched.items():
        decl = handle.source(hit.get("source")) if isinstance(hit, dict) else None
        why = check_hit(decl, hit)
        if why:
            report.problems["bad_hit"] += 1
            continue
        g = report.groups.setdefault((hit["source"], hit["report_date"]), Group(hit["source"], hit["report_date"]))
        g.files += 1
        try:
            cal = _calendar(snapshot_dir, hit["report_date"], calendars, cal_by_date) \
                if decl["time_basis"] == "start_of_day" else None
            vd: str | None = valid_date(hit["report_date"], decl["time_basis"], cal).isoformat()
        except CalendarUnavailable:
            vd = None
        g.valid_date = vd
        try:
            raw = runner.call_read(handle, path, hit, index_path, read_timeout_s)
        except AdapterFailure as f:
            g.statuses[f.kind] += 1
            report.problems[f"read_{f.kind}"] += 1
            continue
        c = check_read(decl, hit, raw, books=books, contract=desc["contract"])
        g.statuses[c.status if vd or decl["time_basis"] != "start_of_day" else "waiting_calendar"] += 1
        g.controls[c.controls_status] += 1
        for i in c.issues:
            g.issues[(i["severity"], i["code"])] += i["count"]
        if c.file_error in PROTOCOL_CODES:
            report.problems[c.file_error] += 1
        for u in c.units:
            g.units[u["status"]] += 1
        rows = raw["rows"] if isinstance(raw, dict) and isinstance(raw.get("rows"), list) else []
        g.rows += len(rows)
        for r in rows:
            if isinstance(r, dict) and r.get("asset_class") == "bond":
                rec = _instrument(r, instruments, vd or hit["report_date"])
                n = _notional(r, rec)
                if n is None:
                    g.quantity_only += 1
                else:
                    g.notional[n[0]] += n[1]
                if not (r.get("instrument_id") or rec):
                    g.unmapped += 1
                    if n is not None:
                        g.unmapped_notional[n[0]] += n[1]
        meta = c.meta if isinstance(c.meta, dict) else {}
        if report_date_suspicious(hit["report_date"], decl["time_basis"], meta.get("generated_at")):
            g.issues[("warning", "report_date_suspicious")] += 1
        # Determinism: a second read gives the same canonical units.
        try:
            again = check_read(decl, hit, runner.call_read(handle, path, hit, index_path, read_timeout_s),
                               books=books, contract=desc["contract"])
            if _fingerprint(again) != _fingerprint(c):
                report.problems["nondeterministic"] += 1
        except AdapterFailure:
            report.problems["nondeterministic"] += 1
        order[(hit["source"], hit["report_date"])].append(
            revision_key(hit["report_date"], hit.get("report_ts"), hit.get("seq"), meta.get("generated_at"), None))
        if vd:
            valid_by_source[(hit["source"], vd)].add(hit["report_date"])
    report.calendar = ", ".join(sorted(set(calendars.values()))) or "none"
    report.notes["order_by_mtime"] += sum(len(v) - len(set(v)) for v in order.values())
    report.notes["same_valid_date"] += sum(len(v) - 1 for v in valid_by_source.values())
    report.notes = +report.notes


def _calendar(snapshot_dir: str | None, report_date: str, seen: dict[str, str], by_date: dict[str, object]):
    """The calendar of a report date, looked up once per date (a miss is remembered too)."""
    if not snapshot_dir:
        raise CalendarUnavailable("no snapshot folder")
    if report_date not in by_date:
        try:
            by_date[report_date] = business_calendar(snapshot_dir, date.fromisoformat(report_date))
        except CalendarUnavailable as e:
            by_date[report_date] = e
    cal = by_date[report_date]
    if isinstance(cal, CalendarUnavailable):
        raise cal
    seen[cal.snapshot_id] = cal.version  # type: ignore[attr-defined]
    return cal


def _fingerprint(c) -> tuple:
    return (c.file_error, sorted((u["dataset"], json.dumps(u["scope"], sort_keys=True), u["report_date"],
                                  u["status"], unit_hash(u["rows"])) for u in c.units))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m desk.inbox.conformance", description=__doc__.split("\n\n")[0])
    ap.add_argument("--adapter", default=None, help="package directory or module name (default: reference)")
    ap.add_argument("--dir", action="append", default=[], required=True, help="a folder of reports (repeatable)")
    ap.add_argument("--stub-ctx", action="store_true", help="every ctx.instruments lookup returns None")
    ap.add_argument("--snapshots", default=None, help="cached snapshot folder (instruments and calendar)")
    ap.add_argument("--config", default=None, help="the adapter's config folder (DESK_PRIVATE_ADAPTER_CONFIG)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = check(a.adapter, a.dir, stub_ctx=a.stub_ctx, snapshot_dir=a.snapshots, config_dir=a.config)
    print(json.dumps(rep.to_json(), indent=2, ensure_ascii=False) if a.json else rep.text())
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
