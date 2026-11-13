"""What the monitor remembers about an adapter that does not answer (TDM#521), and about the scan that holds the lock.

A ``reference()`` or a ``read()`` that does not answer within ``adapter_read_timeout_s`` costs the whole timeout, and
a scan that asked again at every autopilot cycle spent its whole cycle waiting. So the scan remembers, in the inbox
archive folder next to ``reference.done`` (the fingerprint the books were last refreshed with), a **pause** per thing
that did not answer: ``reference()``, and each source whose files kept timing out (``SOURCE_STOP_AFTER`` in a row in
one scan; a source that timed out before is stopped by its first timeout). The pause grows with the failures of
one unbroken series, ``PAUSE_H`` hours (1 h, then 6 h and on), and ends at once when the adapter's fingerprint or the
read timeout changes (``key``: the ledger of another key is not read), or when the thing answers. Only ``scan`` honours
a pause of a source (the autopilot, the backfill's reports), and ``scan`` and a feed's file (``ingest_feed``, under the
cycle lock) that of ``reference()``; ``retry``, ``replay`` and the owner's acquisition are explicit requests and always
ask, but record what they find. The files of a paused source are not touched: no status, no sighting, so the next scan
that is not paused offers them again.

Files also wait when ``reference()`` gave no books and the store has none (it timed out, raised, or answered with no
valid books): judged by no book they would all be rejected as ``unknown_book``, so they stay in the queue too, and the
ledger's ``no_books`` entry says since when, how many and why (the adapter's error class or the codes of its answer).
The dates the queued files wait for (``waiting``) are shown on the day's status: its positions may be earlier ones.

``Running`` is the other half: the scan that holds the inbox lock (a Postgres advisory lock, held by another process
for ``desk inbox retry`` or the page) writes ``scan.running`` (kind, start, the step it is in), so the one that finds
the lock taken can say how long the scan has run and what it is doing, in Russian (``busy_text``).

Times are written in UTC and shown in Moscow time. No file name or value of a report goes into the ledger; the
running marker holds the step label the progress line already shows.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import fsretry
from .timesem import MSK

log = logging.getLogger(__name__)

HANG_FILE = "adapter-hang.json"
RUNNING_FILE = "scan.running"
PAUSE_H = (1, 6)            # the pause after the 1st, the 2nd and every later failure of one series
SOURCE_STOP_AFTER = 3       # timeouts of one source in a row, in one scan, that stop its reads in that scan
RUNNING_EVERY_S = 3.0       # the running marker is rewritten at most this often (a progress call per file)
REFERENCE = "reference"

KINDS = {"scan": "просмотр папок", "retry": "повторное чтение файлов", "replay": "перечитывание принятых файлов",
         "feed": "файл сервиса адаптера", "acquisition": "файл загрузки"}


def key(fingerprint: str | None, read_timeout_s: float) -> str:
    """What a pause belongs to: the adapter (its fingerprint covers the code and the config files) and the read timeout."""
    return f"{fingerprint or '-'}|{read_timeout_s:g}"


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def _when(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m %H:%M") if iso else "—"


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        fsretry.replace(tmp, path)
    except OSError as e:  # a note for the owner, never a reason to fail a scan
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        log.warning("inbox: %s not written: %s", path.name, e)


class Ledger:
    """The pauses of one adapter under one set of settings: ``reference`` (an entry or None), ``sources`` and
    ``no_books``. An entry: ``failures`` (of the unbroken series), ``since`` (its first), ``last``, ``until`` (UTC ISO)
    and, for a source, ``queued`` (the files the scans left unread) and ``dates`` (their business dates, or report
    dates where no calendar is cached: date -> files). ``no_books``: files wait in the queue because ``reference()``
    gave no books and the store has none: ``since``, ``last``, ``queued``, ``dates`` and ``reason`` (the adapter's
    error class or the codes of its invalid answer, never a message or a value)."""

    def __init__(self, folder: Path | str, ledger_key: str) -> None:
        self.path = Path(folder) / HANG_FILE
        self.key = ledger_key
        self.reference: dict[str, Any] | None = None
        self.sources: dict[str, dict[str, Any]] = {}
        self.no_books: dict[str, Any] | None = None
        self._dirty = False
        doc = read_doc(self.path)
        if doc.get("key") == ledger_key:
            ref, srcs, nb = doc.get("reference"), doc.get("sources"), doc.get("no_books")
            self.reference = ref if isinstance(ref, dict) else None
            self.sources = {k: v for k, v in srcs.items() if isinstance(v, dict)} if isinstance(srcs, dict) else {}
            self.no_books = nb if isinstance(nb, dict) else None

    # ---- the question

    @staticmethod
    def _live(entry: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
        try:
            return entry if entry and datetime.fromisoformat(entry["until"]) > now else None
        except (KeyError, TypeError, ValueError):
            return None

    def reference_paused(self, now: datetime) -> dict[str, Any] | None:
        return self._live(self.reference, now)

    def source_paused(self, source: str, now: datetime) -> dict[str, Any] | None:
        return self._live(self.sources.get(source), now)

    def stop_after(self, source: str) -> int:
        """Timeouts in a row that stop the source in this scan: a source that timed out before is asked once."""
        return 1 if source in self.sources else SOURCE_STOP_AFTER

    # ---- what happened

    @staticmethod
    def _failed(entry: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
        n = (entry or {}).get("failures", 0) + 1
        return {**(entry or {}), "failures": n, "since": (entry or {}).get("since") or _iso(now), "last": _iso(now),
                "until": _iso(now + timedelta(hours=PAUSE_H[min(n, len(PAUSE_H)) - 1]))}

    def reference_failed(self, now: datetime) -> dict[str, Any]:
        self.reference, self._dirty = self._failed(self.reference, now), True
        return self.reference

    def source_failed(self, source: str, now: datetime) -> dict[str, Any]:
        self.sources[source], self._dirty = self._failed(self.sources.get(source), now), True
        return self.sources[source]

    def reference_answered(self) -> None:
        if self.reference is not None:
            self.reference, self._dirty = None, True

    def source_answered(self, source: str) -> None:
        if self.sources.pop(source, None) is not None:
            self._dirty = True

    @staticmethod
    def _merged(entry: dict[str, Any], files: int, dates: dict[str, int]) -> bool:
        """The queue as the scans saw it: the most of each, so a scan that looked at fewer files (the autopilot's
        lookback against the backfill's start date) does not make the figures jump. True when ``entry`` changed."""
        before = (entry.get("queued"), dict(entry.get("dates") or {}))
        entry["queued"] = max(files, entry.get("queued") or 0)
        entry["dates"] = {d: max(dates.get(d, 0), (entry.get("dates") or {}).get(d, 0)) for d in {*dates, *(entry.get("dates") or {})}}
        return before != (entry["queued"], entry["dates"])

    def queued(self, source: str, files: int, dates: dict[str, int] | None = None) -> None:
        """The files of a stopped source this scan left unread, and their dates (shown next to its pause)."""
        if source in self.sources and self._merged(self.sources[source], files, dates or {}):
            self._dirty = True

    def books_missing(self, now: datetime, files: int, dates: dict[str, int], reason: str | None) -> None:
        """Files wait because there are no books at all (``reference()`` gave none, the store has none)."""
        if self.no_books is None:
            self.no_books = {"since": _iso(now)}
        self.no_books["last"], self.no_books["reason"] = _iso(now), reason
        self._merged(self.no_books, files, dates)
        self._dirty = True

    def books_present(self) -> None:
        if self.no_books is not None:
            self.no_books, self._dirty = None, True

    def save(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if self.reference is None and not self.sources and self.no_books is None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as e:
                log.warning("inbox: %s not removed: %s", self.path.name, e)
            return
        _write(self.path, json.dumps({"key": self.key, "reference": self.reference, "sources": self.sources,
                                      "no_books": self.no_books}, indent=1, ensure_ascii=False))


def read_doc(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(fsretry.retry(lambda: path.read_text(encoding="utf-8"), fsretry.REPLACE_WAIT_S))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _files(n: int) -> str:
    """«1 файл ждёт», «3 файла ждут», «5 файлов ждут»."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} файл ждёт"
    return f"{n} файла ждут" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else f"{n} файлов ждут"


def _add_dates(into: dict[str, int], entry: dict[str, Any]) -> None:
    dates = entry.get("dates")
    for d, n in (dates.items() if isinstance(dates, dict) else ()):
        if isinstance(d, str) and isinstance(n, int):
            into[d] = max(into.get(d, 0), n)


def report(folder: Path | str, ledger_key: str, now: datetime | None = None) -> dict[str, Any]:
    """What the screens show (the settings «Обзор» of the adapter, C14): ``reference``, ``sources`` and ``no_books`` as
    saved, ``lines`` in Russian, one per thing that is paused or waiting now (empty: nothing is), ``waiting`` (the dates
    of the files left in the queue -> how many, for the warning of those dates) and ``retry_at`` (the soonest end of a
    pause, UTC ISO, else None). A pause that has ended is not shown (the next scan asks again). Without any book in the
    store the line says so instead of «пока берёт сохранённые книги»."""
    now = now or datetime.now(UTC)
    led = Ledger(folder, ledger_key)
    lines: list[str] = []
    waiting: dict[str, int] = {}
    ref, nb = led.reference_paused(now), led.no_books
    if nb:
        n, since = nb.get("queued") or 0, _when(nb.get("since"))
        queue = _files(n) if n else "файлы ждут"
        if ref:
            lines.append(f"Справочник адаптера не отвечает с {_when(ref.get('since'))}, а книг в базе ещё нет: {queue} в очереди. "
                         f"Следующая попытка после {_when(ref.get('until'))}.")
        else:
            why = f" (код: {nb['reason']})" if nb.get("reason") else ""
            lines.append(f"Справочник адаптера не удалось прочитать{why}, а книг в базе ещё нет: {queue} в очереди с {since}. "
                         "Монитор спрашивает адаптер при каждом скане.")
        _add_dates(waiting, nb)
    elif ref:
        lines.append(f"Справочник адаптера не отвечает с {_when(ref.get('since'))}. Монитор не спрашивает его до "
                     f"{_when(ref.get('until'))} и пока берёт сохранённые книги.")
    srcs = {s: e for s in sorted(led.sources) if (e := led.source_paused(s, now))}
    for s, e in srcs.items():
        n = e.get("queued")
        lines.append(f"Чтение источника «{s}» не укладывается в таймаут с {_when(e.get('since'))}: его файлы ждут в очереди"
                     + (f" ({n})" if n else "") + f", следующая попытка после {_when(e.get('until'))}.")
        _add_dates(waiting, e)
    ends = sorted(e["until"] for e in [ref, *srcs.values()] if e and e.get("until"))
    return {"reference": ref, "sources": [{"source": s, **e} for s, e in srcs.items()], "no_books": nb,
            "lines": lines, "waiting": waiting, "retry_at": ends[0] if ends else None}


# ---- the scan that holds the lock ------------------------------------------------------------------------------


class Running:
    """The marker of the scan in progress, in the archive folder; rewritten as the scan goes (at most every
    ``RUNNING_EVERY_S``), removed when it ends."""

    def __init__(self, folder: Path | str, kind: str, now: datetime) -> None:
        self.path = Path(folder) / RUNNING_FILE
        self.kind, self.started = kind, now
        self._last = -RUNNING_EVERY_S
        self.update(0, None, "запуск", force=True)

    def update(self, done: int, total: int | None, detail: str, *, force: bool = False) -> None:
        t = time.monotonic()
        if not force and t - self._last < RUNNING_EVERY_S:
            return
        self._last = t
        _write(self.path, json.dumps({"kind": self.kind, "started": _iso(self.started), "at": _iso(datetime.now(UTC)),
                                      "done": done, "total": total, "detail": detail}, ensure_ascii=False))

    def close(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def read_running(folder: Path | str) -> dict[str, Any] | None:
    doc = read_doc(Path(folder) / RUNNING_FILE)
    return doc if doc.get("started") else None


def _span(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    if m < 1:
        return "меньше минуты"
    h, m = divmod(m, 60)
    return " ".join(([f"{h} ч"] if h else []) + ([f"{m} мин"] if m or not h else []))


def busy_text(info: dict[str, Any] | None, now: datetime | None = None) -> str:
    """The word of a scan that finds the lock taken (``desk inbox retry`` and the others), in Russian: how long the
    holder has been running and the step it is in; without its marker (another program version, another computer)
    only that it runs."""
    head = "Другой скан уже держит блокировку входящих: ничего не сделано, повторите позже."
    if not info:
        return head + " Сведений о нём нет."
    now = now or datetime.now(UTC)
    try:
        started = datetime.fromisoformat(info["started"])
        at = datetime.fromisoformat(info["at"]) if info.get("at") else None
    except (KeyError, TypeError, ValueError):
        return head + " Сведений о нём нет."
    what = KINDS.get(info.get("kind") or "", "скан")
    out = [f"{head} Он идёт {_span((now - started).total_seconds())} (с {_when(info['started'])} МСК): {what}."]
    step = str(info.get("detail") or "").strip()
    if step:
        total = info.get("total")
        out.append(f"Сейчас: {step}" + (f", {info.get('done', 0)} из {total}" if total else "") + ".")
    if at is not None and (now - at).total_seconds() > 120:
        out.append(f"Сведения обновлены {_when(info['at'])}: если шаг тот же, адаптер, скорее всего, не отвечает.")
    return " ".join(out)

