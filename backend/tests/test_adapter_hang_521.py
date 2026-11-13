"""TDM#521: an adapter that does not answer must not eat the autopilot cycle. ``reference()`` that timed out is paused
(1 h, then 6 h; saved books meanwhile; ends with another fingerprint or read timeout), the reads of a source that
timed out ``SOURCE_STOP_AFTER`` times in a row stop (its other files stay queued, never failed), and a scan that finds
the lock taken says in Russian how long the holder has run and what it does. Synthetic data only."""

from __future__ import annotations

import dataclasses
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from desk import backfill, daystatus, store
from desk.autopilot import Autopilot
from desk.inbox import hang, runner, scan
from desk.inbox.loader import load_adapter
from desk.inbox.spi import AdapterFailure
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
PRIV, CONFIG, SNAPS, ADAPTERS = FIX / "private" / "v1", FIX / "private" / "v1" / "config", FIX / "mdhub_v1", \
    FIX / "adapters"
GOOD = str(ADAPTERS / "good_adapter")
D = date.fromisoformat
FILES = ("positions_20261014.csv", "positions_20261015.csv", "positions_20261016.csv", "positions_20261016_2.csv",
         "positions_20261017.csv")  # five files of different content (the 17th and the 19th are copies of each other)


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    monkeypatch.setenv(scan.READ_WORKERS_ENV, "1")  # reads one after another: the order of the timeouts is fixed
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


@pytest.fixture
def con(store_url):
    with store.connect(store_url) as c:
        yield c


def cfg(tmp_path: Path, inbox: Path, adapter: str = GOOD, **kw) -> Settings:
    return Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                    private_adapter=adapter, private_adapter_config=CONFIG, home=tmp_path / "home",
                    **{"inbox_poll_seconds": 0, "inbox_lookback_days": 0, "adapter_read_timeout_s": 1, **kw})


def put(inbox: Path, *names: str) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.copy(PRIV / n, inbox / n)


class Hanging:
    """The good adapter's layout whose ``reference`` and ``read`` note each call and, while the flag of the call is set
    (``hang("read")``), do not answer: the worker is killed by the read timeout."""

    def __init__(self, tmp_path: Path, name: str = "hanging") -> None:
        self.dir, self.flags, self.calls = tmp_path / name, tmp_path / f"{name}_flags", tmp_path / f"{name}_calls.log"
        shutil.copytree(ADAPTERS / "good_adapter", self.dir)
        self.flags.mkdir()
        src = (self.dir / "__init__.py").read_text(encoding="utf-8")
        helper = (f"import os\nimport time\n\n\ndef _call(what, tag=''):\n"
                  f"    with open({str(self.calls)!r}, 'a') as f:\n        f.write(what + ' ' + tag + '\\n')\n"
                  f"    if os.path.exists(os.path.join({str(self.flags)!r}, what)):\n        time.sleep(60)\n")
        src = src.replace("import os\n", helper, 1)
        src = src.replace("def read(path, h, ctx):\n", "def read(path, h, ctx):\n    _call('read', os.path.basename(path))\n", 1)
        src = src.replace("def reference(ctx):\n", "def reference(ctx):\n    _call('reference')\n"
                          f"    if os.path.exists(os.path.join({str(self.flags)!r}, 'reference_raises')):\n"
                          "        raise ValueError('not for the screen')\n"
                          f"    if os.path.exists(os.path.join({str(self.flags)!r}, 'reference_bad')):\n"
                          "        return {'books': 'x'}\n", 1)
        (self.dir / "__init__.py").write_text(src.replace('"name": "good_adapter"', f'"name": "{name}"'),
                                              encoding="utf-8")

    def hang(self, what: str, on: bool = True) -> None:
        if on:
            (self.flags / what).touch()
        else:
            (self.flags / what).unlink(missing_ok=True)

    def count(self, what: str) -> int:
        try:
            return sum(1 for line in self.calls.read_text(encoding="utf-8").splitlines() if line.split()[0] == what)
        except OSError:
            return 0

    def change(self) -> None:
        """A new adapter fingerprint: the code changed."""
        p = self.dir / "__init__.py"
        p.write_text(p.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        scan.forget_adapters()


def statuses(con) -> dict[str, int]:
    return dict(con.execute("SELECT status, count(*) FROM inbox_file_status GROUP BY 1").fetchall())


def sightings(con) -> int:
    return con.execute("SELECT count(*) FROM inbox_sighting").fetchone()[0]


# ---- the pauses of the ledger -----------------------------------------------------------------------------------


def test_a_pause_grows_with_the_series_ends_on_an_answer_and_survives_a_restart(tmp_path):
    t = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    led = hang.Ledger(tmp_path, "fp|600")
    assert led.reference_paused(t) is None
    first = led.reference_failed(t)
    assert (datetime.fromisoformat(first["until"]) - t) == timedelta(hours=1) and first["failures"] == 1
    led.save()
    again = hang.Ledger(tmp_path, "fp|600")  # a restart: the pause is read back from the archive folder
    assert again.reference_paused(t + timedelta(minutes=59)) is not None
    assert again.reference_paused(t + timedelta(minutes=61)) is None  # lapsed: the next scan asks again
    second = again.reference_failed(t + timedelta(minutes=61))
    assert (datetime.fromisoformat(second["until"]) - (t + timedelta(minutes=61))) == timedelta(hours=6)
    assert second["since"] == first["since"] and second["failures"] == 2  # one unbroken series
    third = again.reference_failed(t + timedelta(hours=8))
    assert (datetime.fromisoformat(third["until"]) - (t + timedelta(hours=8))) == timedelta(hours=6)  # no more growth
    again.save()
    ended = hang.Ledger(tmp_path, "fp|600")
    ended.reference_answered()
    ended.save()
    assert not (tmp_path / hang.HANG_FILE).exists() and hang.Ledger(tmp_path, "fp|600").reference is None


def test_a_pause_belongs_to_the_adapter_and_the_read_timeout(tmp_path):
    t = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    led = hang.Ledger(tmp_path, hang.key("fp-a", 600))
    led.reference_failed(t)
    led.source_failed("positions_daily", t)
    led.save()
    assert hang.Ledger(tmp_path, hang.key("fp-a", 600)).reference_paused(t) is not None
    for other in (hang.key("fp-b", 600), hang.key("fp-a", 900), hang.key(None, 600)):
        gone = hang.Ledger(tmp_path, other)
        assert gone.reference_paused(t) is None and gone.source_paused("positions_daily", t) is None
        assert hang.report(tmp_path, other, t)["lines"] == []


def test_the_report_says_in_russian_what_is_paused_and_since_when(tmp_path):
    t = datetime(2026, 11, 2, 11, 5, tzinfo=UTC)  # 14:05 Moscow
    led = hang.Ledger(tmp_path, "k")
    led.reference_failed(t)
    led.source_failed("positions_daily", t)
    led.queued("positions_daily", 4947)
    led.save()
    lines = hang.report(tmp_path, "k", t + timedelta(minutes=5))["lines"]
    assert lines[0].startswith("Справочник адаптера не отвечает с 02.11 14:05.") and "15:05" in lines[0]
    assert "сохранённые книги" in lines[0]
    assert "«positions_daily»" in lines[1] and "(4947)" in lines[1] and "следующая попытка после 02.11 15:05" in lines[1]
    assert hang.report(tmp_path, "k", t + timedelta(hours=2))["lines"] == []  # lapsed: not shown


# ---- reference() ------------------------------------------------------------------------------------------------


def test_a_reference_that_timed_out_is_not_asked_again_while_paused_and_the_files_wait(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, *FILES[:3])
    ad = Hanging(tmp_path)
    ad.hang("reference")
    s, now = cfg(tmp_path, inbox, str(ad.dir)), datetime.now(UTC)
    r = scan.scan(con, s, now=now)  # no books stored yet, reference() does not answer
    assert r.notes == {"reference_timed_out": 1} and ad.count("reference") == 1, r.text()
    # without any book the book ids cannot be judged: the files are not rejected as unknown_book, they wait
    assert r.files == {} and r.skipped[scan.SKIP_NO_BOOKS] == 3, r.text()
    assert statuses(con) == {} and sightings(con) == 0
    # no book is saved, so the line must not say «берёт сохранённые книги» (there are none): it says what waits
    assert len(r.pauses) == 1 and "а книг в базе ещё нет" in r.pauses[0] and "3 файла ждут в очереди" in r.pauses[0], r.pauses
    assert "сохранённые книги" not in r.pauses[0]
    r = scan.scan(con, s, now=now + timedelta(minutes=10))  # paused: not asked, the files still wait
    assert r.notes == {"reference_paused": 1} and ad.count("reference") == 1 and r.files == {}, r.text()
    assert statuses(con) == {} and sightings(con) == 0
    ad.hang("reference", False)
    r = scan.scan(con, s, now=now + timedelta(minutes=61))  # the pause lapsed and it answers: the files come in
    assert ad.count("reference") == 2 and sum(r.files.values()) == 3 and "timed_out" not in r.files, r.text()
    assert r.pauses == [] and not (tmp_path / "archive" / hang.HANG_FILE).exists()  # books came: nothing waits any more


def test_while_reference_is_paused_the_saved_books_are_used_and_a_later_pause_is_longer(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, FILES[0])
    assert scan.scan(con, cfg(tmp_path, inbox)).files == {"accepted": 1}  # the books are saved by a working adapter
    ad = Hanging(tmp_path)
    ad.hang("reference")
    s, now = cfg(tmp_path, inbox, str(ad.dir)), datetime.now(UTC)
    put(inbox, FILES[1])
    r = scan.scan(con, s, now=now)
    assert r.notes == {"reference_timed_out": 1} and sum(r.files.values()) >= 1, r.text()  # saved books: read on
    assert "unknown_book" not in {c for (c,) in con.execute("SELECT error_code FROM inbox_file_status").fetchall()}
    put(inbox, FILES[2])
    r = scan.scan(con, s, now=now + timedelta(minutes=10))
    assert r.notes == {"reference_paused": 1} and ad.count("reference") == 1, r.text()
    assert sum(r.files.values()) >= 1 and r.skipped[scan.SKIP_NO_BOOKS] == 0  # nothing waits: the books are there
    scan.scan(con, s, now=now + timedelta(minutes=61))  # asked again, still nothing: the second pause is 6 h
    assert ad.count("reference") == 2
    pause = hang.Ledger(tmp_path / "archive", hang.key(scan.adapter(s).fingerprint, 1)).reference
    assert pause["failures"] == 2 and pause["since"] != pause["last"]
    assert datetime.fromisoformat(pause["until"]) - (now + timedelta(minutes=61)) == timedelta(hours=6)


@pytest.mark.parametrize("change", ["fingerprint", "timeout"])
def test_the_reference_pause_ends_when_the_adapter_or_the_settings_change(con, tmp_path, change):
    inbox = tmp_path / "in"
    put(inbox, FILES[0])
    ad = Hanging(tmp_path)
    ad.hang("reference")
    s, now = cfg(tmp_path, inbox, str(ad.dir)), datetime.now(UTC)
    scan.scan(con, s, now=now)
    scan.scan(con, s, now=now + timedelta(minutes=5))
    assert ad.count("reference") == 1  # paused
    if change == "fingerprint":
        ad.change()
    else:
        s = dataclasses.replace(s, adapter_read_timeout_s=2)
    scan.scan(con, s, now=now + timedelta(minutes=10))
    assert ad.count("reference") == 2  # asked at once, not after the hour


# ---- the reads of one source ---------------------------------------------------------------------------------


def hanging_reads(tmp_path: Path, monkeypatch, stop_after: int = 2):
    monkeypatch.setattr(hang, "SOURCE_STOP_AFTER", stop_after)
    inbox = tmp_path / "in"
    put(inbox, *FILES)
    ad = Hanging(tmp_path)
    ad.hang("read")
    return ad, cfg(tmp_path, inbox, str(ad.dir)), datetime.now(UTC)


def test_after_k_timeouts_of_one_source_the_rest_of_its_files_stay_queued_not_failed(con, tmp_path, monkeypatch):
    ad, s, now = hanging_reads(tmp_path, monkeypatch)
    r = scan.scan(con, s, now=now)
    assert r.files == {"timed_out": 2}, r.text()  # K = 2 timeouts, then the source is left alone
    assert r.queued == {"positions_daily": 3} and r.skipped[scan.SKIP_SOURCE] == 3, r.text()
    # the other three are neither failed nor rejected nor seen: as if the scan had not met them
    assert statuses(con) == {"timed_out": 2} and sightings(con) == 2
    assert any("«positions_daily»" in line and "(3)" in line for line in r.pauses), r.pauses
    assert r.ok and "Чтение источника" in r.text()
    ad.hang("read", False)
    r = scan.scan(con, s, now=now + timedelta(minutes=10))  # paused: not even asked
    assert r.files == {} and r.queued == {"positions_daily": 3}, r.text()
    seen = ad.count("read")
    r = scan.scan(con, s, now=now + timedelta(minutes=61))  # the pause lapsed and it answers: the queue is read
    assert sum(r.files.values()) == 3 and "timed_out" not in r.files and r.queued == {}, r.text()
    assert ad.count("read") == seen + 3 and statuses(con).get("timed_out") == 2  # the earlier two wait for a new fingerprint
    assert not (tmp_path / "archive" / hang.HANG_FILE).exists()


def test_a_new_fingerprint_reads_the_queue_at_once(con, tmp_path, monkeypatch):
    ad, s, now = hanging_reads(tmp_path, monkeypatch)
    scan.scan(con, s, now=now)
    ad.hang("read", False)
    ad.change()
    r = scan.scan(con, s, now=now + timedelta(minutes=1))  # within the hour, but not the same adapter
    assert sum(r.files.values()) == 5 and r.queued == {}, r.text()  # the three queued and the two that timed out


def test_a_source_that_timed_out_before_is_asked_once_when_its_pause_lapses(con, tmp_path, monkeypatch):
    ad, s, now = hanging_reads(tmp_path, monkeypatch)
    scan.scan(con, s, now=now)
    seen = ad.count("read")
    r = scan.scan(con, s, now=now + timedelta(minutes=61))  # still hanging: one probe stops it, not K again
    assert r.files == {"timed_out": 1} and r.queued == {"positions_daily": 2}, r.text()
    assert ad.count("read") <= seen + 3  # one probe, and at most the reads of the window started beside it
    led = hang.Ledger(tmp_path / "archive", hang.key(scan.adapter(s).fingerprint, 1))
    assert led.sources["positions_daily"]["failures"] == 2
    assert datetime.fromisoformat(led.sources["positions_daily"]["until"]) - (now + timedelta(minutes=61)) \
        == timedelta(hours=6)


def test_an_explicit_retry_ignores_the_pause_and_a_read_that_answers_ends_it(con, tmp_path, monkeypatch):
    ad, s, now = hanging_reads(tmp_path, monkeypatch)
    scan.scan(con, s, now=now)
    ad.hang("read", False)
    r = scan.retry(con, s, ("timed_out",), now=now + timedelta(minutes=5))  # the owner asks: it is read at once
    assert sum(r.files.values()) == 2 and "timed_out" not in r.files, r.text()
    assert not (tmp_path / "archive" / hang.HANG_FILE).exists()  # it answered: no pause is left
    r = scan.scan(con, s, now=now + timedelta(minutes=6))
    assert sum(r.files.values()) == 3 and r.queued == {}, r.text()


def test_the_backfill_does_not_mark_the_reports_read_while_files_wait(con, tmp_path, monkeypatch):
    ad, s, _ = hanging_reads(tmp_path, monkeypatch)
    line = backfill.inbox(con, s, D("2026-10-01"))
    assert line and "Чтение источника" in line
    assert "inbox" not in backfill.state(s)  # not done: the next cycle reads the queued files again
    ad.hang("read", False)
    ad.change()  # a new fingerprint: the queue is read at once, and now the start date is done
    backfill.inbox(con, s, D("2026-10-01"))
    assert backfill.state(s).get("inbox", {}).get("from") == "2026-10-01"


def test_the_daystatus_carries_the_lines_for_c14(con, tmp_path, monkeypatch):
    _, s, _ = hanging_reads(tmp_path, monkeypatch)
    scan.scan(con, s)
    lines = daystatus.day(con, s, D("2026-10-17"))["adapter_hang"]["lines"]
    assert len(lines) == 1 and "«positions_daily»" in lines[0], lines


# ---- the scan that holds the lock -----------------------------------------------------------------------------


def test_the_scan_leaves_a_marker_while_it_runs_and_removes_it(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, *FILES[:2])
    s, marks = cfg(tmp_path, inbox), []
    scan.scan(con, s, progress=lambda done, total, detail: marks.append(hang.read_running(s.inbox_archive)))
    assert marks and all(m and m["kind"] == "scan" for m in marks)
    assert hang.read_running(s.inbox_archive) is None  # gone with the scan


def test_a_retry_that_finds_the_lock_taken_says_in_russian_how_long_and_what(store_url, con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, FILES[0])
    s = cfg(tmp_path, inbox)
    started = datetime.now(UTC) - timedelta(minutes=82)
    marker = hang.Running(s.inbox_archive, "scan", started)  # the holder: its marker and its lock
    marker.update(120, 4947, "сопоставление файлов", force=True)
    with store.connect(store_url) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (scan.SCAN_LOCK,))
        try:
            r = scan.retry(con, s)
        finally:
            other.execute("SELECT pg_advisory_unlock(%s)", (scan.SCAN_LOCK,))
    assert r.busy and not r.ok
    text = r.text()
    assert "Другой скан уже держит блокировку входящих" in text and "ничего не сделано" in text, text
    assert "1 ч 22 мин" in text and "просмотр папок" in text, text
    assert "Сейчас: сопоставление файлов, 120 из 4947." in text, text
    assert "another scan" not in text


def test_a_busy_scan_without_a_marker_still_says_it_in_russian(store_url, con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, FILES[0])
    s = cfg(tmp_path, inbox)
    with store.connect(store_url) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (scan.SCAN_LOCK,))
        try:
            text = scan.scan(con, s).text()
        finally:
            other.execute("SELECT pg_advisory_unlock(%s)", (scan.SCAN_LOCK,))
    assert "Другой скан уже держит блокировку входящих" in text and "Сведений о нём нет" in text, text


def test_a_marker_older_than_its_last_news_warns_that_the_adapter_may_not_answer():
    t = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    info = {"kind": "scan", "started": (t - timedelta(hours=1)).isoformat(), "at": (t - timedelta(minutes=30)).isoformat(),
            "done": 0, "total": None, "detail": "сопоставление файлов"}
    text = hang.busy_text(info, t)
    assert "1 ч" in text and "Сведения обновлены" in text and "не отвечает" in text, text


# ---- TDM#521 review: the feeds, the «no books» queue, the aborted reads, the date's warning ------------------------


def seed_books(con, tmp_path: Path, inbox: Path) -> None:
    """A working adapter saves the books (and one report) first."""
    put(inbox, FILES[0])
    assert scan.scan(con, cfg(tmp_path, inbox)).files == {"accepted": 1}


def test_two_feed_files_while_reference_hangs_cost_one_timeout_and_the_other_entries_still_ask(con, tmp_path):
    inbox = tmp_path / "in"
    seed_books(con, tmp_path, inbox)
    put(inbox, *FILES[1:3])
    ad = Hanging(tmp_path)
    ad.hang("reference")
    s = cfg(tmp_path, inbox, str(ad.dir))
    h = scan.adapter(s)
    started = datetime.now(UTC)
    r1 = scan.ingest_feed(con, s, inbox / FILES[1], source="positions_daily", handle=h)  # asks, times out: the pause
    r2 = scan.ingest_feed(con, s, inbox / FILES[2], source="positions_daily", handle=h)  # paused: saved books, no wait
    assert ad.count("reference") == 1, "the second feed file asked again: it would hold the cycle lock for a whole timeout"
    assert r1.notes == {"reference_timed_out": 1} and r2.notes == {"reference_paused": 1}
    assert sum(r1.files.values()) == 1 and sum(r2.files.values()) == 1 and "rejected" not in {*r1.files, *r2.files}
    assert (datetime.now(UTC) - started).total_seconds() < 8
    # the owner's explicit entries ask whatever the pause: a retry (and replay, and the owner's acquisition, which are
    # not given the pause either)
    scan.retry(con, s, ("accepted",))
    assert ad.count("reference") == 2


def test_without_any_book_the_files_wait_and_the_screens_say_so_with_the_dates_they_wait_for(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, *FILES[:3])
    ad = Hanging(tmp_path)
    ad.hang("reference")
    s, now = cfg(tmp_path, inbox, str(ad.dir)), datetime.now(UTC)
    r = scan.scan(con, s, now=now)
    assert r.skipped[scan.SKIP_NO_BOOKS] == 3 and statuses(con) == {}
    led = hang.Ledger(tmp_path / "archive", hang.key(scan.adapter(s).fingerprint, 1))
    assert led.no_books["queued"] == 3 and led.no_books["since"] and sum(led.no_books["dates"].values()) == 3
    rep = scan.hang_report(s, now=now)
    assert len(rep["lines"]) == 1 and "а книг в базе ещё нет" in rep["lines"][0] and "3 файла ждут" in rep["lines"][0]
    assert sorted(rep["waiting"]) == sorted(led.no_books["dates"]) and rep["retry_at"] is not None
    ad.hang("reference", False)
    r = scan.scan(con, s, now=now + timedelta(minutes=61))
    assert sum(r.files.values()) == 3 and scan.hang_report(s, now=now + timedelta(minutes=62))["lines"] == []
    assert {d.isoformat() for (d,) in con.execute("SELECT DISTINCT valid_date FROM inbox_file_status").fetchall()} \
        == set(rep["waiting"]), "the dates the screens warned of are the dates the files then came in for"


def test_a_reference_that_fails_without_a_timeout_and_no_books_queue_the_files_and_say_why(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, *FILES[:3])
    ad = Hanging(tmp_path)
    ad.hang("reference_raises")
    s, now = cfg(tmp_path, inbox, str(ad.dir)), datetime.now(UTC)
    r = scan.scan(con, s, now=now)
    assert r.skipped[scan.SKIP_NO_BOOKS] == 3 and r.notes == {"reference_failed": 1} and statuses(con) == {}, r.text()
    line = r.pauses[0]
    assert "не удалось прочитать (код: ValueError)" in line and "а книг в базе ещё нет" in line, r.pauses
    assert "not for the screen" not in line and "not for the screen" not in (tmp_path / "archive" / hang.HANG_FILE).read_text()
    assert scan.hang_report(s, now=now)["lines"] == r.pauses  # the plate and C14 say the same
    scan.scan(con, s, now=now + timedelta(minutes=1))
    assert ad.count("reference") == 2, "no timeout, no pause: the next scan asks again"
    ad.hang("reference_raises", False)
    ad.hang("reference_bad")  # an answer that is no books: the codes of the check are the reason
    scan.scan(con, s, now=now + timedelta(minutes=2))
    assert "код: bad_reference" in scan.hang_report(s, now=now)["lines"][0]
    ad.hang("reference_bad", False)
    r = scan.scan(con, s, now=now + timedelta(minutes=3))
    assert sum(r.files.values()) == 3 and scan.hang_report(s, now=now)["lines"] == []


def test_the_backfill_scans_a_start_date_with_queued_files_once_not_every_cycle(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, *FILES[:3])
    ad = Hanging(tmp_path)
    ad.hang("reference_raises")
    s = cfg(tmp_path, inbox, str(ad.dir))
    assert "inbox from 2026-10-01" in (backfill.inbox(con, s, D("2026-10-01")) or "")
    asked = ad.count("reference")
    for _ in range(3):  # the next cycles: not a scan each
        assert backfill.inbox(con, s, D("2026-10-01")) is None
    assert ad.count("reference") == asked
    wait = backfill.state(s)["inbox_wait"]
    assert wait["from"] == "2026-10-01" and "inbox" not in backfill.state(s)
    # the wait ends: its time passes, the adapter or the read timeout changes, or the owner forces it
    st = backfill.state(s)
    st["inbox_wait"]["until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    backfill.save_state(s, st)
    assert backfill.inbox(con, s, D("2026-10-01")) is not None and ad.count("reference") == asked + 1
    assert backfill.inbox(con, s, D("2026-10-01"), force=True) is not None and ad.count("reference") == asked + 2
    assert backfill.inbox(con, dataclasses.replace(s, adapter_read_timeout_s=2), D("2026-10-01")) is not None
    ad.hang("reference_raises", False)  # it answers: the files are read and the start date is done
    assert backfill.inbox(con, s, D("2026-10-01"), force=True)
    assert backfill.state(s)["inbox"]["from"] == "2026-10-01" and "inbox_wait" not in backfill.state(s)


def test_a_stopped_source_gives_up_its_reads_started_ahead_and_frees_the_workers(con, tmp_path, monkeypatch):
    monkeypatch.setenv(scan.READ_WORKERS_ENV, "4")
    monkeypatch.setattr(hang, "SOURCE_STOP_AFTER", 2)
    inbox = tmp_path / "in"
    put(inbox, *FILES)
    ad = Hanging(tmp_path)
    ad.hang("read")
    s = cfg(tmp_path, inbox, str(ad.dir), adapter_read_timeout_s=3)
    aborted = []
    real = runner.abort_reads
    monkeypatch.setattr(runner, "abort_reads", lambda tag: (aborted.append(tag), real(tag))[1])
    started = datetime.now(UTC)
    r = scan.scan(con, s)
    took = (datetime.now(UTC) - started).total_seconds()
    assert r.files == {"timed_out": 2} and r.queued == {"positions_daily": 3}, r.text()
    assert aborted == ["positions_daily"]  # once, when the source stopped
    assert took < 3 * 2, f"the scan went on for the reads started ahead: {took:.1f} s"
    assert statuses(con) == {"timed_out": 2}


def test_an_aborted_read_ends_at_once_and_its_worker_serves_the_next_call(tmp_path):
    h = load_adapter(str(ADAPTERS / "read_hangs"), None, None)
    hit = {"source": "market", "report_date": "2026-10-15"}
    with runner.pooled(1) as pool, ThreadPoolExecutor(2) as ex:
        t0 = time.monotonic()
        running = ex.submit(runner.call_read, h, str(tmp_path / "x"), hit, None, 30, tag="market")
        waiting = ex.submit(runner.call_read, h, str(tmp_path / "y"), hit, None, 30, tag="market")  # no worker for it yet
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pool._tagged:
            time.sleep(0.05)
        time.sleep(1.0)  # the first call is inside the adapter
        runner.abort_reads("market")
        for fut in (running, waiting):
            with pytest.raises(AdapterFailure) as e:
                fut.result(timeout=10)
            assert (e.value.kind, e.value.type) == ("failed", "aborted")
        assert time.monotonic() - t0 < 10  # not the 30 s of the timeout
        with pytest.raises(AdapterFailure):  # a call of that tag after the abort fails without a worker
            runner.call_read(h, str(tmp_path / "z"), hit, None, 30, tag="market")
        got = runner.call_match(h, [str(tmp_path / "m")], None, 20)  # the worker is free and restarts
        assert got and not any("_error" in v for v in got.values() if isinstance(v, dict))


def test_queued_files_count_as_the_inbox_waiting_and_the_date_says_its_report_waits(con, tmp_path, monkeypatch):
    ad, s, _ = hanging_reads(tmp_path, monkeypatch)
    ap = Autopilot(settings_loader=lambda: s, today=lambda: D("2026-10-20"), out=lambda _: None)
    step = ap._scan(s, con)
    assert step[0] == "ok" and ap._inbox_waiting == 3, (step, ap._inbox_waiting)  # three files wait in the queue
    # ... which holds the eod of older dates as the files waiting for a calendar do, but not the prune: the queue of a
    # paused source may last weeks, and no replaced run would ever be deleted (TDM#634)
    assert ap._inbox_calendar == 0
    assert ap._prune(s, con, []) == ("ok", "no replaced runs to delete")
    ap._inbox_calendar = 2  # files waiting for a calendar: the backlog the prune waits for, as before
    assert ap._prune(s, con, []) == ("skipped", "after the inbox backlog")
    ap._inbox_calendar = 0
    waiting = scan.hang_report(s)["waiting"]
    assert sum(waiting.values()) == 3
    for d in waiting:
        warnings = daystatus.day(con, s, D(d))["summary"]["warnings"]
        assert any(w.startswith("отчёт не прочитан, ждёт в очереди: адаптер не отвечает; расчёт может идти на прошлых данных")
                   for w in warnings), (d, warnings)
    shown = {x["date"] for x in daystatus.days(con, s, D("2026-10-20"), 30)["days"]}
    assert set(waiting) <= shown, "the dates of waiting reports are on the strip"
    other = (D(next(iter(waiting))) + timedelta(days=60)).isoformat()
    assert not any("ждёт в очереди" in w for w in daystatus.day(con, s, D(other))["summary"]["warnings"])
    ad.hang("read", False)
    ad.change()  # a new fingerprint: the queue is read, and the warnings go
    ap2 = Autopilot(settings_loader=lambda: s, today=lambda: D("2026-10-20"), out=lambda _: None)
    ap2._scan(s, con)
    assert ap2._inbox_waiting == 0 and scan.hang_report(s)["waiting"] == {}
    assert not any("ждёт в очереди" in w for d in waiting for w in daystatus.day(con, s, D(d))["summary"]["warnings"])


def test_a_feed_file_is_left_in_the_queue_without_asking_a_paused_reference_again(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, *FILES[:2])
    ad = Hanging(tmp_path)
    ad.hang("reference")
    s = cfg(tmp_path, inbox, str(ad.dir))
    h = scan.adapter(s)
    r1 = scan.ingest_feed(con, s, inbox / FILES[0], source="positions_daily", handle=h)  # no books, no answer: queued
    r2 = scan.ingest_feed(con, s, inbox / FILES[1], source="positions_daily", handle=h)  # the retry: not asked again
    assert r1.queued == {"positions_daily": 1} and r2.queued == {"positions_daily": 1}
    assert ad.count("reference") == 1 and statuses(con) == {} and sightings(con) == 0  # nothing written: it can come again
