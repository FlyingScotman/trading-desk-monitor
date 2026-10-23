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
    parts = [f"{FILE_STATUS.get(k, k) if k != 'accepted' else 'принято'} {n}" for k, n in units.items()]
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
    (``has_more``: older ones remain). ``on``: of this business date only; ``types``: type prefixes (``run.``,
    ``input.rejected``). ``last_id``: the newest event in the store, whatever the filter: where a stream starts."""
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
        args += [t.replace("%", "").replace("_", r"\_") + ("%" if t.endswith(".") else "") for t in types]
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


def _inputs(con: psycopg.Connection, day: date) -> list[dict[str, Any]]:
    """Per dataset what the latest reading of each file gave for ``day``: units accepted, rejected, superseded, their
    rows, the sources; the sets stored for the date (positions: the books whose set is of this date)."""
    units: dict[str, Counter] = defaultdict(Counter)
    sources: dict[str, set] = defaultdict(set)
    for ds, st, n, rows, src in con.execute(
            f"SELECT u.dataset, u.status, count(*), sum(u.row_count), l.source FROM {LATEST} l"
            " JOIN inbox_unit u USING (status_id) WHERE u.valid_date = %s GROUP BY 1, 2, 5", (day,)):
        units[ds][st] += n
        units[ds][f"{st}_rows"] += int(rows or 0)
        if src:
            sources[ds].add(src)
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
        u = units.get(ds, Counter())
        acc, rej, sup = u["accepted"], u["rejected"], u["superseded"]
        state = ("missing" if not (acc or rej or sup) else "rejected" if rej and not acc
                 else "partial" if rej else "ok" if acc else "superseded")
        n_sets, n_rows = stored.get(ds) or (0, 0)
        out.append({"dataset": ds, "name": DATASET_NAMES.get(ds, ds), "state": state,
                    "units": {"accepted": acc, "rejected": rej, "superseded": sup},
                    "rows": {"accepted": u["accepted_rows"], "rejected": u["rejected_rows"]},
                    "sources": sorted(sources.get(ds, ())), "stored_sets": n_sets or 0, "stored_rows": n_rows or 0})
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


def _summary(inputs: list[dict], market: dict, runs: list[dict], files: list[dict], copies: list[dict],
             books: list[dict]) -> dict[str, Any]:
    """The headline: ``state`` bad (a rejected input, a failed run, no run), warn (partial, a stale copy, a book
    flagged, no final close), ok; with the reasons."""
    bad, warn = [], []
    by = {i["dataset"]: i for i in inputs}
    for i in inputs:
        if i["state"] == "rejected":
            bad.append(f"{i['name']}: отклонено")
        elif i["state"] == "partial":
            warn.append(f"{i['name']}: принято частично")
    if by["positions"]["state"] == "missing" and not any(b.get("valid_date") for b in books):
        bad.append("нет позиций")
    lost = [f for f in files if f["status"] in ("rejected", "failed", "timed_out", "match_failed")]
    if lost:
        bad.append(f"файлов с ошибкой: {len(lost)}")
    if any(c["kind"] == "stale_copy" for c in copies):
        warn.append("старая копия отчёта под новым именем")
    if any(r["status"] == "failed" and not r["superseded_by"] for r in runs):
        bad.append("прогон не удался")
    if not any(r["is_default"] for r in runs):
        (warn if any(r["is_current"] for r in runs) else bad).append("нет основного прогона")
    if not market.get("eod_final"):
        warn.append("нет окончательного закрытия хаба")
    flagged = sum(1 for b in books if b.get("flags"))
    if flagged:
        warn.append(f"книг с замечаниями: {flagged}")
    return {"state": "bad" if bad else "warn" if warn else "ok", "problems": bad, "warnings": warn,
            "default_run_id": next((r["run_id"] for r in runs if r["is_default"]), None),
            "current_run_id": next((r["run_id"] for r in runs if r["is_current"]), None)}


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
            "summary": _summary(inputs, market, runs, files, copies, rec["books"]),
            "inputs": inputs, "market": market, "runs": runs, "files": files, "copies": copies,
            "positions": {k: rec[k] for k in ("run_id", "since", "books", "totals", "losses")},
            "events": feed["events"], "events_more": feed["has_more"], "last_event_id": feed["last_id"],
            "generated_at": datetime.now(UTC).isoformat()}


def days(con: psycopg.Connection, settings: Settings, to: date | None = None, n: int = 30) -> dict[str, Any]:
    """``GET /api/v1/inbox/days``: the business dates from ``to`` (default today, Moscow) ``n`` calendar days back
    that have anything (an input unit, a run, a cached close), newest first: per dataset the units accepted and
    rejected, the runs (default, failed), whether the final close is cached, and a ``state`` like ``day``'s."""
    from .marketdata.bundle import SnapshotDir

    to = to or datetime.now(UTC).astimezone(store.MSK).date()
    lo = to - timedelta(days=n)
    out: dict[date, dict[str, Any]] = {}

    def g(d: date) -> dict[str, Any]:
        return out.setdefault(d, {"date": d.isoformat(), "inputs": {}, "runs": 0, "runs_failed": 0,
                                  "default_run_id": None, "eod_final": False, "files_lost": 0})

    for d, ds, st, k in con.execute(
            f"SELECT u.valid_date, u.dataset, u.status, count(*) FROM {LATEST} l JOIN inbox_unit u USING (status_id)"
            " WHERE u.valid_date BETWEEN %s AND %s GROUP BY 1, 2, 3", (lo, to)):
        g(d)["inputs"].setdefault(ds, Counter())[st] += k
    for d, k in con.execute(f"SELECT valid_date, count(*) FROM {LATEST} l WHERE valid_date BETWEEN %s AND %s"
                            " AND status IN ('rejected', 'failed', 'timed_out') GROUP BY 1", (lo, to)):
        g(d)["files_lost"] = k
    for d, k, failed, dflt in con.execute(
            "SELECT business_date, count(*), count(*) FILTER (WHERE status = 'failed' AND NOT EXISTS (SELECT 1 FROM run"
            " x WHERE x.supersedes_run_id = r.run_id)),"
            f" max(run_id) FILTER (WHERE run_id IN ({store.DEFAULT_RUNS})) FROM run r"
            " WHERE business_date BETWEEN %s AND %s AND run_type <> 'TEST' GROUP BY 1", (lo, to)):
        x = g(d)
        x.update(runs=k, runs_failed=failed, default_run_id=dflt)
    try:
        finals = set(SnapshotDir(Path(settings.snapshot_cache)).market_dates("eod_final"))
    except (OSError, ValueError):
        finals = set()
    for s in finals:
        d = date.fromisoformat(s)
        if lo <= d <= to:
            g(d)["eod_final"] = True
    rows = []
    for d in sorted(out, reverse=True):
        x = out[d]
        ins = {ds: {"accepted": c["accepted"], "rejected": c["rejected"], "superseded": c["superseded"]}
               for ds, c in sorted(x["inputs"].items())}
        rejected = any(c["rejected"] for c in ins.values())
        bad = x["files_lost"] or x["runs_failed"] or any(c["rejected"] and not c["accepted"] for c in ins.values())
        warn = rejected or not x["eod_final"] or not x["default_run_id"]
        rows.append({**x, "inputs": ins, "state": "bad" if bad else "warn" if warn else "ok"})
    return {"from": lo.isoformat(), "to": to.isoformat(), "days": rows}
