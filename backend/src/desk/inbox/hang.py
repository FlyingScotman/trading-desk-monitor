"""What the monitor remembers about an adapter that does not answer (TDM#521), and about the scan that holds the lock.

A ``reference()`` or a ``read()`` that does not answer within ``adapter_read_timeout_s`` costs the whole timeout, and
a scan that asked again at every autopilot cycle spent its whole cycle waiting. So the scan remembers, in the inbox
archive folder next to ``reference.done`` (the fingerprint the books were last refreshed with), a **pause** per thing
that did not answer: ``reference()``, and each source whose files kept timing out (``SOURCE_STOP_AFTER`` in a row in
one scan; a source that timed out before is stopped by its first timeout). The pause grows with the failures of
one unbroken series, ``PAUSE_H`` hours (1 h, then 6 h and on), and ends at once when the adapter's fingerprint or the
read timeout changes (``key``: the ledger of another key is not read), or when the thing answers. Only ``scan`` honours
a pause (the autopilot, the backfill's reports); ``retry`` and ``replay`` are the owner's explicit requests and always
ask, but record what they find. The files of a paused source are not touched: no status, no sighting, so the next scan
that is not paused offers them again.

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
    """The pauses of one adapter under one set of settings: ``reference`` (an entry or None) and ``sources``.
    An entry: ``failures`` (of the unbroken series), ``since`` (its first), ``last``, ``until`` (UTC ISO) and, for a
    source, ``queued`` (the files the last scan left unread)."""

    def __init__(self, folder: Path | str, ledger_key: str) -> None:
        self.path = Path(folder) / HANG_FILE
        self.key = ledger_key
        self.reference: dict[str, Any] | None = None
        self.sources: dict[str, dict[str, Any]] = {}
        self._dirty = False
        doc = read_doc(self.path)
        if doc.get("key") == ledger_key:
            ref, srcs = doc.get("reference"), doc.get("sources")
            self.reference = ref if isinstance(ref, dict) else None
            self.sources = {k: v for k, v in srcs.items() if isinstance(v, dict)} if isinstance(srcs, dict) else {}

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

    def queued(self, source: str, files: int) -> None:
        """The files of a stopped source this scan left unread (shown next to its pause)."""
        if source in self.sources and self.sources[source].get("queued") != files:
            self.sources[source]["queued"], self._dirty = files, True

    def save(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if self.reference is None and not self.sources:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as e:
                log.warning("inbox: %s not removed: %s", self.path.name, e)
            return
        _write(self.path, json.dumps({"key": self.key, "reference": self.reference, "sources": self.sources},
                                     indent=1, ensure_ascii=False))


def read_doc(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(fsretry.retry(lambda: path.read_text(encoding="utf-8"), fsretry.REPLACE_WAIT_S))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def report(folder: Path | str, ledger_key: str, now: datetime | None = None) -> dict[str, Any]:
    """What the screens show (the settings «Обзор» of the adapter, C14): ``reference`` and ``sources`` as saved, and
    ``lines`` in Russian, one per thing that is paused now; empty ``lines``: nothing is. A pause that has ended is
    not shown (the next scan asks again)."""
    now = now or datetime.now(UTC)
    led = Ledger(folder, ledger_key)
    lines: list[str] = []
    ref = led.reference_paused(now)
    if ref:
        lines.append(f"Справочник адаптера не отвечает с {_when(ref.get('since'))}. Монитор не спрашивает его до "
                     f"{_when(ref.get('until'))} и пока берёт сохранённые книги.")
    srcs = {s: e for s in sorted(led.sources) if (e := led.source_paused(s, now))}
    for s, e in srcs.items():
        n = e.get("queued")
        lines.append(f"Чтение источника «{s}» не укладывается в таймаут с {_when(e.get('since'))}: его файлы ждут в очереди"
                     + (f" ({n})" if n else "") + f", следующая попытка после {_when(e.get('until'))}.")
    return {"reference": ref, "sources": [{"source": s, **e} for s, e in srcs.items()], "lines": lines}


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

