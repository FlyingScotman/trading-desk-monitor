"""Day status (S7b, ROADMAP §0.2 B5; issue #25): per business date what has arrived (positions, OCP, cash, official
PnL, trade terms; the hub's closes), what was rejected and why, which runs exist and which superseded which; and the
event feed.

``day(con, settings, on)`` is ``GET /api/v1/inbox?on=``: the date's inputs by dataset, the market snapshots of the
cache, the report files that touched the date with their units and issue codes (a rejection says why), the copies
(``stale_copy``: yesterday's file under today's name, 08 §13), the date's runs with what superseded what and what
changed between them, its events, and per book where the positions went (``inbox.scan.reconcile``, reused, not
repeated). ``days(con, settings, to, n)`` is ``GET /api/v1/inbox/days``: one line per date for the screen's strip.
Codes, counts, book ids and file names: the owner's own view (local only, like ``/api/v1/inbox/reconcile``); no row
values and no issue messages (08 §6.8).

``events(con, ...)`` is ``GET /api/v1/events``: the ``event`` table (inbox status changes since S7a, runs since
migration 0019) in ``event_id`` order. ``event_id`` is the one cursor: ``after`` pages forward (what a poll or the
SSE stream of #28 sends: ``id:`` = ``event_id``, a reconnect resumes from ``Last-Event-ID`` with ``after``),
``before`` pages back. Each event carries a ``level`` and a Russian ``text`` for notifications.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from . import store
from .settings import Settings

DATASETS = ("positions", "ocp", "cash_balances", "pnl_official", "trade_terms")
DATASET_NAMES = {"positions": "Позиции", "ocp": "ОВП", "cash_balances": "Остатки на счетах",
                 "pnl_official": "Официальный PnL", "trade_terms": "Условия сделок"}

FILE_STATUS = {
    "accepted": "принят",
    "partially_accepted": "принят частично",
    "superseded": "заменён более новым отчётом",
    "rejected": "отклонён",
    "failed": "ошибка чтения",
    "timed_out": "чтение не уложилось в таймаут",
    "match_failed": "адаптер не распознал файл",
    "waiting_calendar": "ждёт календарь",
    "ignored": "не формат адаптера",
    "ignored_too_old": "старше окна загрузки",
    "duplicate": "повтор",
    "stale_copy": "старая копия",
}
"""What a file's status means, briefly; the losses' longer explanations are ``inbox.scan.LOSS_REASONS``."""

ISSUE_TEXT = {
    "bad_number": "число не разобрано",
    "duplicate_key": "повтор ключа строки",
    "unknown_book": "книги нет в справочнике",
    "missing_required": "нет обязательных полей",
    "book_closed_by_omission": "книги нет в отчёте, который покрывает все книги: закрыта",
    "scope_conflict": "та же книга и дата от другого источника",
    "report_date_suspicious": "время формирования не сходится с датой отчёта",
    "same_valid_date": "другая дата отчёта даёт тот же рабочий день",
    "pnl_parts_mismatch": "части PnL не сходятся с итогом",
}

LEVELS = {"input.rejected": "error", "input.failed": "error", "input.match_failed": "error", "run.failed": "error",
          "input.partially_accepted": "warn", "input.stale": "warn", "input.waiting_calendar": "warn"}

EVENT_TEXT = {
    "input.accepted": "принят",
    "input.partially_accepted": "принят частично",
    "input.superseded": "пришёл, но заменён более новым отчётом",
    "input.rejected": "отклонён",
    "input.failed": "ошибка чтения",
    "input.match_failed": "адаптер не распознал файл",
    "input.waiting_calendar": "ждёт календарь",
    "input.ignored": "не формат адаптера",
    "input.stale": "старая копия под новым именем: свежего отчёта нет",
    "input.duplicate": "повтор",
}

UNIT_STATUS = {"accepted": "принято", "rejected": "отклонено", "superseded": "заменено"}
"""A file's parts (books, entities) by status, as the feed counts them."""

RUN_CHANGES = {"positions": "позиции", "market": "снимок рынка", "prev_market": "снимок прошлого дня",
               "program": "версия программы", "cut": "срез (окончательное закрытие вместо предварительного)"}

LATEST = "(SELECT DISTINCT ON (sha256) * FROM inbox_file_status ORDER BY sha256, status_id DESC)"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date | datetime) else v


def _rows(con: psycopg.Connection, sql: str, args: Any = None) -> list[dict[str, Any]]:
    with con.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, args).fetchall()


# ---- events -----------------------------------------------------------------------------------------------------

def event_text(e: dict[str, Any]) -> str:
    """One line for the feed and a notification: «Позиции за 20.10 · источник: принят · книг 3, строк 120»."""
    t, d = e["type"], e.get("detail") or {}
    vd = e.get("valid_date")
    on = f" за {date.fromisoformat(vd):%d.%m}" if isinstance(vd, str) else ""
    if t.startswith("run."):
        what = {"EOD": "Прогон закрытия", "INTRADAY": "Прогон внутри дня"}.get(d.get("run_type"), "Прогон")
        cut = "" if d.get("cut_type") == "intraday" else store.CUT_LABELS.get(d.get("cut_type") or "", d.get("cut_type") or "")
        s = f"{what} #{e.get('run_id')}{on}" + (f" ({cut})" if cut else "")
        if t == "run.published":
            return s + " опубликован" + (f", заменил #{d['supersedes_run_id']}" if d.get("supersedes_run_id") else "")
        return s + " не удался"
    if t == "books.changed":
        return f"Справочник книг обновлён: изменено книг {(e.get('counts') or {}).get('books', '?')}"
    if t == "reference.changed":
        return "Справочники адаптера обновлены: " + ", ".join(f"{k} {v}" for k, v in (e.get("counts") or {}).items())
    name = DATASET_NAMES.get(e.get("dataset") or "", "Файл")
    s = f"{name}{on}" + (f" · {e['source']}" if e.get("source") else "") + ": " + EVENT_TEXT.get(t, t)
    c = e.get("counts") or {}
    units = c.get("units") or {}
    parts = [f"{UNIT_STATUS.get(k, k)} {n}" for k, n in units.items()]
    if c.get("rows") is not None:
        parts.append(f"строк {c['rows']}")
    if t == "input.stale" and c.get("content_report_date"):
        parts.append(f"содержимое отчёта за {c['content_report_date']}")
    return s + (" · " + ", ".join(parts) if parts else "")


def _event(r: dict[str, Any]) -> dict[str, Any]:
    e = {"id": r["event_id"], "at": _iso(r["at"]), "type": r["type"], "level": LEVELS.get(r["type"], "info"),
         "source": r["source"], "dataset": r["dataset"], "valid_date": _iso(r["valid_date"]), "run_id": r["run_id"],
         "file": r["sha256"][:12] if r["sha256"] else None, "counts": r["counts"], "detail": r["detail"]}
    e["text"] = event_text(e)
    return e


def events(con: psycopg.Connection, *, after: int | None = None, before: int | None = None, on: date | None = None,
           types: list[str] | None = None, limit: int = 100) -> dict[str, Any]:
    """A page of the feed in ``event_id`` order (module doc). ``after``: the events after it, oldest first, at most
    ``limit`` (``has_more``: more follow); otherwise the newest ``limit`` (before ``before``), still oldest first
    (``has_more``: older ones remain). ``on``: of this business date only; ``types``: type prefixes, each matched
    as the start of the type (``run`` and ``run.`` give ``run.published`` and ``run.failed``; ``input.rejected``). ``last_id``: the newest event in the store, whatever the filter: where a stream starts."""
    where, args = [], []
    if after is not None:
        where.append("event_id > %s")
        args.append(after)
    if before is not None:
        where.append("event_id < %s")
        args.append(before)
    if on is not None:
        where.append("valid_date = %s")
        args.append(on)
    if types:
        where.append("(" + " OR ".join("type LIKE %s" for _ in types) + ")")
        args += [t.replace("%", "").replace("_", r"\_") + "%" for t in types]
    sql = ("SELECT event_id, at, type, source, dataset, valid_date, sha256, counts, run_id, detail FROM event"
           + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY event_id {'ASC' if after is not None else 'DESC'} LIMIT %s")
    rows = _rows(con, sql, (*args, limit + 1))
    more = len(rows) > limit
    rows = rows[:limit]
    if after is None:
        rows.reverse()
    last = con.execute("SELECT max(event_id) FROM event").fetchone()[0]
    return {"events": [_event(r) for r in rows], "has_more": more, "last_id": last}


# ---- one date -----------------------------------------------------------------------------------------------------

def _market(settings: Settings, con: psycopg.Connection, day: date) -> dict[str, Any]:
    """The hub's market snapshots of ``day`` in the cache: the final and the preliminary close (the current one of
    each: corrections supersede), the intraday cuts; and whether the store loaded the final close's rows."""
    from .marketdata.bundle import SnapshotDir

    d = day.isoformat()
    out: dict[str, Any] = {"eod_final": None, "eod_preliminary": None, "intraday": {"count": 0, "latest_cut_ts": None}}
    try:
        sd = SnapshotDir(Path(settings.snapshot_cache))
    except (OSError, ValueError) as e:
        return {**out, "error": type(e).__name__}
    mine = {sid: m for sid, m in sd.manifests().items() if m.get("kind") == "market" and m.get("business_date") == d}
    for cut in ("eod_final", "eod_preliminary"):
        if any(m.get("cut_type") == cut for m in mine.values()):
            sid = sd.market_id(d, cut)
            m, prov = mine[sid], sd.provenance(sid) or {}
            out[cut] = {"snapshot_id": sid, "cut_ts": m.get("cut_ts"), "known_at": m.get("known_at"),
                        "published_at": prov.get("published_at"),
                        "corrections": sum(1 for x in mine.values() if x.get("cut_type") == cut) - 1}
    cuts = sorted(str(m.get("cut_ts")) for m in mine.values() if m.get("cut_type") == "intraday")
    out["intraday"] = {"count": len(cuts), "latest_cut_ts": cuts[-1] if cuts else None}
    if out["eod_final"]:
        row = con.execute("SELECT status, n_rows FROM md_snapshot_load WHERE snapshot_id = %s",
                          (out["eod_final"]["snapshot_id"],)).fetchone()
        out["eod_final"]["store"] = {"status": row[0], "rows": row[1]} if row else None
    return out


UNITS_SQL = (
    f"WITH u AS (SELECT u.unit_id, u.dataset, u.valid_date, u.scope, u.status, u.row_count, l.source FROM {LATEST} l"
    " JOIN inbox_unit u USING (status_id) WHERE u.valid_date BETWEEN %(lo)s AND %(hi)s)"
    " SELECT valid_date, dataset, status, count(*), sum(row_count), array_agg(DISTINCT source) FROM"
    " (SELECT DISTINCT ON (dataset, valid_date, scope) * FROM u WHERE status <> 'superseded'"
    "  ORDER BY dataset, valid_date, scope, unit_id DESC) last GROUP BY 1, 2, 3"
    " UNION ALL SELECT valid_date, dataset, status, count(*), sum(row_count), array_agg(DISTINCT source) FROM u"
    "  WHERE status = 'superseded' GROUP BY 1, 2, 3")
"""Per (business date, dataset, status) the units and rows of the latest reading of each file. A scope unit (a book,
an entity) counts once, by its last reading that was not superseded: a correction that replaced the same books does
not count them twice; superseded units are counted as they are (what a newer report replaced). A book accepted
from one file and rejected in a later one counts as rejected (the dataset then reads ``partial`` or ``rejected``)
although the earlier positions stay in force: the signal is that the latest report of the book was lost; a book only
in an older file keeps that file's reading."""


def _units(con: psycopg.Connection, lo: date, hi: date) -> dict[tuple[date, str], dict[str, Any]]:
    out: dict[tuple[date, str], dict[str, Any]] = {}
    for d, ds, st, n, rows, srcs in con.execute(UNITS_SQL, {"lo": lo, "hi": hi}):
        x = out.setdefault((d, ds), {"n": Counter(), "rows": Counter(), "sources": set()})
        x["n"][st] += n
        x["rows"][st] += int(rows or 0)
        x["sources"].update(v for v in srcs if v)
    return out


def _input_state(n: Counter) -> str:
    """A dataset's state for a date from its scope units: ``missing``, ``rejected`` (only rejected), ``partial``,
    ``ok`` or ``superseded`` (only superseded)."""
    acc, rej, sup = n["accepted"], n["rejected"], n["superseded"]
    return ("missing" if not (acc or rej or sup) else "rejected" if rej and not acc
            else "partial" if rej else "ok" if acc else "superseded")


def _inputs(con: psycopg.Connection, day: date) -> list[dict[str, Any]]:
    """Per dataset what arrived for ``day`` (``UNITS_SQL``): scope units accepted, rejected, superseded, their rows,
    the sources; the sets stored for the date (positions: the books whose set is of this date)."""
    units = {ds: x for (_, ds), x in _units(con, day, day).items()}
    stored = {"positions": con.execute(
        "WITH s AS (SELECT DISTINCT ON (book_id) set_id FROM position_set WHERE valid_date = %s"
        f" ORDER BY book_id, {store.CURRENT_SET})"
        " SELECT (SELECT count(*) FROM s), (SELECT count(*) FROM position_input WHERE set_id IN (SELECT set_id FROM s))",
        (day,)).fetchone()}
    for ds, n, rows in con.execute(
            "SELECT dataset, count(*), sum(row_count) FROM (SELECT DISTINCT ON (dataset, scope_key) dataset, row_count"
            f" FROM dataset_set WHERE valid_date = %s ORDER BY dataset, scope_key, {store.CURRENT_SET}) s GROUP BY 1",
            (day,)):
        stored[ds] = (n, int(rows or 0))
    out = []
    for ds in [*DATASETS, *sorted(set(units) - set(DATASETS))]:
        u = units.get(ds) or {"n": Counter(), "rows": Counter(), "sources": set()}
        n_sets, n_rows = stored.get(ds) or (0, 0)
        out.append({"dataset": ds, "name": DATASET_NAMES.get(ds, ds), "state": _input_state(u["n"]),
                    "units": {k: u["n"][k] for k in ("accepted", "rejected", "superseded")},
                    "rows": {"accepted": u["rows"]["accepted"], "rejected": u["rows"]["rejected"]},
                    "sources": sorted(u["sources"]), "stored_sets": n_sets or 0, "stored_rows": n_rows or 0})
    return out


def _files(con: psycopg.Connection, day: date) -> list[dict[str, Any]]:
    """The files whose latest reading is of ``day`` (the file's business date, or a unit's: a multi-date file), with
    their units, issue codes and, for a loss, why."""
    from .inbox.scan import LOSS_REASONS

    files = _rows(con, "SELECT l.status_id, l.sha256, f.name, l.status, l.at, l.source, l.report_date, l.valid_date,"
                       f" l.error_code, l.error_type FROM {LATEST} l JOIN inbox_file f USING (sha256)"
                       " WHERE l.valid_date = %(d)s OR EXISTS (SELECT 1 FROM inbox_unit u"
                       "  WHERE u.status_id = l.status_id AND u.valid_date = %(d)s) ORDER BY l.at, l.status_id",
                  {"d": day})
    ids = [f["status_id"] for f in files]
    units: dict[int, list] = defaultdict(list)
    for u in _rows(con, "SELECT unit_id, status_id, dataset, valid_date, scope, status, row_count FROM inbox_unit"
                        " WHERE status_id = ANY(%s) ORDER BY unit_id", (ids,)):
        units[u["status_id"]].append(u)
    issues: dict[tuple, Counter] = defaultdict(Counter)
    for sid, uid, sev, code, n in con.execute(
            "SELECT status_id, unit_id, severity, code, sum(count) FROM inbox_issue WHERE status_id = ANY(%s)"
            " GROUP BY 1, 2, 3, 4", (ids,)):
        issues[(sid, uid)][(sev, code)] += int(n)

    def codes(c: Counter) -> list[dict[str, Any]]:
        order = {"error": 0, "warning": 1, "info": 2}
        return [{"severity": s, "code": k, "count": n, "text": ISSUE_TEXT.get(k)}
                for (s, k), n in sorted(c.items(), key=lambda x: (order.get(x[0][0], 3), -x[1], x[0][1]))]

    out = []
    for f in files:
        us = []
        for u in units.get(f["status_id"], []):
            iss = codes(issues.get((f["status_id"], u["unit_id"]), Counter()))
            why = None
            if u["status"] == "rejected":
                why = "; ".join(i["text"] or i["code"] for i in iss if i["severity"] == "error") or "отклонена"
            elif u["status"] == "superseded":
                why = LOSS_REASONS["unit_superseded"]
            us.append({"dataset": u["dataset"], "valid_date": _iso(u["valid_date"]), "scope": u["scope"],
                       "status": u["status"], "rows": u["row_count"], "issues": iss, "why": why})
        st = f["status"]
        why = LOSS_REASONS.get(st) if st not in ("accepted", "superseded", "partially_accepted") else None
        if st == "rejected" and f["error_code"]:
            why = f"{why}: {f['error_code']}"
        elif st in ("failed", "timed_out", "match_failed") and (f["error_type"] or f["error_code"]):
            why = f"{why} ({f['error_type'] or f['error_code']})"
        out.append({"file": f["sha256"][:12], "name": f["name"], "status": st, "status_text": FILE_STATUS.get(st, st),
                    "at": _iso(f["at"]), "source": f["source"], "report_date": _iso(f["report_date"]),
                    "valid_date": _iso(f["valid_date"]),
                    "datasets": sorted({u["dataset"] for u in us}),
                    "units": us, "issues": codes(issues.get((f["status_id"], None), Counter())), "why": why})
    return out


def _copies(con: psycopg.Connection, day: date) -> list[dict[str, Any]]:
    """Copies of known content seen with this business date: a stale copy means the date's own report is missing."""
    return [{"name": Path(str(r["path"]).replace("\\", "/")).name, "kind": r["kind"], "source": r["source"],
             "report_date": _iso(r["report_date"]), "content_report_date": _iso(r["content_report_date"]),
             "at": _iso(r["at"]), "file": r["sha256"][:12]}
            for r in _rows(con, "SELECT s.path, s.kind, s.source, s.report_date, s.at, s.sha256,"
                                " (SELECT report_date FROM inbox_file_status x WHERE x.sha256 = s.sha256"
                                "  AND x.report_date IS NOT NULL ORDER BY status_id DESC LIMIT 1) AS content_report_date"
                                " FROM inbox_sighting s WHERE s.kind <> 'new' AND s.valid_date = %s ORDER BY s.at",
                           (day,))]


def _runs(con: psycopg.Connection, day: date) -> list[dict[str, Any]]:
    """The runs of ``day``, oldest first: type, cut, status, default or shown, which run each superseded and which
    superseded it, and what changed against the run it superseded (positions, market, previous close, program)."""
    rows = _rows(con, "SELECT r.run_id, r.run_type, r.cut_type, r.cut_ts, r.status, r.started_at, r.completed_at,"
                      " r.supersedes_run_id, r.error, r.position_set_ids, r.market_snapshot_id,"
                      " r.prev_market_snapshot_id, r.lineage->>'logic_version' AS logic_version,"
                      " r.coverage->'_total' AS coverage,"
                      f" r.run_id IN ({store.DEFAULT_RUNS}) AS is_default,"
                      f" r.run_id IN ({store.CURRENT_RUNS}) AS is_current"
                      " FROM run r WHERE r.business_date = %s ORDER BY r.run_id", (day,))
    by_id = {r["run_id"]: r for r in rows}
    later: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        if r["supersedes_run_id"]:
            later[r["supersedes_run_id"]].append(r["run_id"])
    out = []
    for r in rows:
        old = by_id.get(r["supersedes_run_id"])
        changed = []
        if old:
            if old["cut_type"] != r["cut_type"]:
                changed.append("cut")
            if sorted(old["position_set_ids"] or []) != sorted(r["position_set_ids"] or []):
                changed.append("positions")
            if old["market_snapshot_id"] != r["market_snapshot_id"] and old["cut_type"] == r["cut_type"]:
                changed.append("market")
            if old["prev_market_snapshot_id"] != r["prev_market_snapshot_id"]:
                changed.append("prev_market")
            if old["logic_version"] != r["logic_version"]:
                changed.append("program")
        err = (r["error"] or "").splitlines()[0][:300] if r["error"] else None
        out.append({"run_id": r["run_id"], "run_type": r["run_type"], "cut_type": r["cut_type"],
                    "label": store.slice_label(day, r["cut_type"], r["cut_ts"], r["run_type"]),
                    "status": r["status"], "started_at": _iso(r["started_at"]), "completed_at": _iso(r["completed_at"]),
                    "is_default": r["is_default"], "is_current": r["is_current"],
                    "supersedes_run_id": r["supersedes_run_id"], "superseded_by": later.get(r["run_id"], []),
                    "changed": changed, "changed_text": [RUN_CHANGES[c] for c in changed],
                    "position_sets": len(r["position_set_ids"] or []), "coverage": r["coverage"], "error": err})
    return out


LOST = ("rejected", "failed", "timed_out", "match_failed")
"""File statuses that lost a report (a file that touched the date and ended so is a problem of the date)."""


def _facts_empty() -> dict[str, Any]:
    return {"inputs": {}, "files_lost": 0, "stale_copies": 0, "runs": 0, "runs_failed": 0, "default_run_id": None,
            "current_run_id": None, "default_set_ids": None, "eod_final": False, "positions_in_force": False,
            "run_behind": False, "books_no_positions": 0, "books_rows_differ": 0}


ROWS_DIFFER_SQL = (
    f"WITH r AS (SELECT run_id, business_date, position_set_ids FROM run WHERE run_id IN ({store.DEFAULT_RUNS})"
    "  AND business_date BETWEEN %s AND %s),"
    " a AS (SELECT r.business_date AS d, s.book_id, count(i.set_id) AS n FROM r"
    "  JOIN position_set s ON s.set_id = ANY(r.position_set_ids) LEFT JOIN position_input i ON i.set_id = s.set_id"
    "  GROUP BY 1, 2),"
    " b AS (SELECT r.business_date AS d, p.book_id, count(*) AS n FROM r JOIN run_position p USING (run_id) GROUP BY 1, 2)"
    " SELECT coalesce(a.d, b.d), count(*) FROM a FULL JOIN b ON a.d = b.d AND a.book_id = b.book_id"
    " WHERE coalesce(a.n, 0) <> coalesce(b.n, 0) GROUP BY 1")
"""Per date the books whose rows in the default run differ from the rows of the position sets the run read: the
run lost or added rows (``rows_differ`` of ``inbox.scan.reconcile``)."""


def _facts(con: psycopg.Connection, settings: Settings, lo: date, hi: date, *,
           every: bool = False) -> dict[date, dict[str, Any]]:
    """What the state of each business date from ``lo`` to ``hi`` is judged on (``_rate``), the same for one date
    (``day``) and for the strip (``days``): dataset states (``_units``), files lost (``LOST``, by the file's or a
    unit's date, as ``_files``), stale copies, runs (count, failed and not superseded, default, shown, whether the
    default run read the position sets in force now), the final close in the cache, positions in force at all, the
    active books without positions and the books whose rows the default run lost (``reconcile``'s ``no_positions``
    and ``rows_differ``). Only dates with anything on them, unless ``every`` (``day``: a date without inputs or runs
    still has positions carried forward from earlier dates)."""
    from .marketdata.bundle import SnapshotDir

    out: dict[date, dict[str, Any]] = {}

    def g(d: date) -> dict[str, Any]:
        return out.setdefault(d, _facts_empty())

    if every:
        for i in range((hi - lo).days + 1):
            g(lo + timedelta(days=i))

    for (d, ds), x in _units(con, lo, hi).items():
        g(d)["inputs"][ds] = x["n"]
    for d, k in con.execute(
            f"SELECT d, count(DISTINCT sha256) FROM (SELECT l.sha256, l.valid_date AS d FROM {LATEST} l"
            "  WHERE l.status = ANY(%(st)s) AND l.valid_date BETWEEN %(lo)s AND %(hi)s"
            f" UNION SELECT l.sha256, u.valid_date FROM {LATEST} l JOIN inbox_unit u USING (status_id)"
            "  WHERE l.status = ANY(%(st)s) AND u.valid_date BETWEEN %(lo)s AND %(hi)s) x GROUP BY 1",
            {"st": list(LOST), "lo": lo, "hi": hi}):
        g(d)["files_lost"] = k
    for d, k in con.execute("SELECT valid_date, count(*) FROM inbox_sighting WHERE kind = 'stale_copy'"
                            " AND valid_date BETWEEN %s AND %s GROUP BY 1", (lo, hi)):
        g(d)["stale_copies"] = k
    for d, k, failed, dflt, cur in con.execute(
            "SELECT business_date, count(*), count(*) FILTER (WHERE status = 'failed' AND NOT EXISTS (SELECT 1 FROM run"
            " x WHERE x.supersedes_run_id = r.run_id)),"
            f" max(run_id) FILTER (WHERE run_id IN ({store.DEFAULT_RUNS})),"
            f" max(run_id) FILTER (WHERE run_id IN ({store.CURRENT_RUNS}))"
            " FROM run r WHERE business_date BETWEEN %s AND %s AND run_type <> 'TEST' GROUP BY 1", (lo, hi)):
        g(d).update(runs=k, runs_failed=failed, default_run_id=dflt, current_run_id=cur)
    try:
        finals = set(SnapshotDir(Path(settings.snapshot_cache)).market_dates("eod_final"))
    except (OSError, ValueError):
        finals = set()
    for s in finals:
        d = date.fromisoformat(s)
        if lo <= d <= hi:
            g(d)["eod_final"] = True
    for d, k in con.execute(ROWS_DIFFER_SQL, (lo, hi)):
        if d in out:
            out[d]["books_rows_differ"] = k
    first_set = dict(con.execute("SELECT book_id, min(valid_date) FROM position_set GROUP BY 1").fetchall())
    first = min(first_set.values(), default=None)
    for d, x in out.items():
        x["positions_in_force"] = first is not None and first <= d
        x["books_no_positions"] = sum(1 for b in store.current_books(con, d) if b.get("active", True)
                                      and (first_set.get(b["book_id"]) is None or first_set[b["book_id"]] > d))
        if x["default_run_id"]:
            ids = con.execute("SELECT position_set_ids FROM run WHERE run_id = %s", (x["default_run_id"],)).fetchone()[0]
            x["default_set_ids"] = sorted(ids or [])
            x["run_behind"] = x["default_set_ids"] != store.current_sets(con, d)[0]
    return out


def _rate(x: dict[str, Any]) -> dict[str, Any]:
    """The state of a date from its ``_facts``: ``bad`` (a dataset only rejected, no positions in force, a file lost,
    a failed run not superseded, no run shown), ``warn`` (a dataset partly rejected, a stale copy, no default run
    while an intraday run is shown, the default run on older position sets, an active book without positions, a book
    whose rows the run lost, no final close), else ``ok``; with the reasons. One rule for the strip and the date's
    headline."""
    bad, warn = [], []
    for ds in [*DATASETS, *sorted(set(x["inputs"]) - set(DATASETS))]:
        st = _input_state(x["inputs"].get(ds) or Counter())
        name = DATASET_NAMES.get(ds, ds)
        if st == "rejected":
            bad.append(f"{name}: отклонено")
        elif st == "partial":
            warn.append(f"{name}: принято частично")
    if not x["positions_in_force"]:
        bad.append("нет позиций")
    if x["files_lost"]:
        bad.append(f"файлов с ошибкой: {x['files_lost']}")
    if x["stale_copies"]:
        warn.append("старая копия отчёта под новым именем")
    if x["runs_failed"]:
        bad.append("прогон не удался")
    if not x["default_run_id"]:
        (warn if x["current_run_id"] else bad).append("нет основного прогона")
    elif x["run_behind"]:
        warn.append("позиции загружены после расчёта: прогон не пересчитан")
    if x["positions_in_force"] and x["books_no_positions"]:
        warn.append(f"книг без позиций: {x['books_no_positions']}")
    if x["books_rows_differ"]:
        warn.append(f"в прогоне не все строки позиций: книг {x['books_rows_differ']}")
    if not x["eod_final"]:
        warn.append("нет окончательного закрытия хаба")
    return {"state": "bad" if bad else "warn" if warn else "ok", "problems": bad, "warnings": warn}


def day(con: psycopg.Connection, settings: Settings, on: date | None = None) -> dict[str, Any]:
    """``GET /api/v1/inbox`` (module doc). ``on``: the business date; default as ``reconcile``'s (the date of the
    latest default run, else today, Moscow)."""
    from .inbox import scan

    rec = scan.reconcile(con, settings, on)
    d = date.fromisoformat(rec["date"])
    inputs, market = _inputs(con, d), _market(settings, con, d)
    files, copies, runs = _files(con, d), _copies(con, d), _runs(con, d)
    feed = events(con, on=d, limit=200)
    return {"date": d.isoformat(), "adapter": scan.adapter_line(scan.adapter(settings)),
            "summary": {**_rate(_facts(con, settings, d, d, every=True)[d]),
                        "default_run_id": next((r["run_id"] for r in runs if r["is_default"]), None),
                        "current_run_id": next((r["run_id"] for r in runs if r["is_current"]), None)},
            "inputs": inputs, "market": market, "runs": runs, "files": files, "copies": copies,
            "positions": {k: rec[k] for k in ("run_id", "since", "books", "totals", "losses")},
            "events": feed["events"], "events_more": feed["has_more"], "last_event_id": feed["last_id"],
            "generated_at": datetime.now(UTC).isoformat()}


def days(con: psycopg.Connection, settings: Settings, to: date | None = None, n: int = 30) -> dict[str, Any]:
    """``GET /api/v1/inbox/days``: the ``n`` calendar days ending on ``to`` (default today, Moscow), those with
    anything (an input unit, a file lost, a stale copy, a run, a cached close), newest first: per dataset the scope
    units accepted, rejected and superseded, the runs, whether the final close is cached, and the date's ``state``
    with its reasons, by the same rule as ``day`` (``_facts``, ``_rate``)."""
    to = to or datetime.now(UTC).astimezone(store.MSK).date()
    lo = to - timedelta(days=n - 1)
    rows = []
    for d, x in sorted(_facts(con, settings, lo, to).items(), reverse=True):
        rows.append({"date": d.isoformat(), "inputs": {ds: {k: c[k] for k in ("accepted", "rejected", "superseded")}
                                                       for ds, c in sorted(x["inputs"].items())},
                     "runs": x["runs"], "runs_failed": x["runs_failed"], "default_run_id": x["default_run_id"],
                     "eod_final": x["eod_final"], "files_lost": x["files_lost"], **_rate(x)})
    return {"from": lo.isoformat(), "to": to.isoformat(), "days": rows}
