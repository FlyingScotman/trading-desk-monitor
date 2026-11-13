"""TDM#521: an adapter that does not answer must not eat the autopilot cycle. ``reference()`` that timed out is paused
(1 h, then 6 h; saved books meanwhile; ends with another fingerprint or read timeout), the reads of a source that
timed out ``SOURCE_STOP_AFTER`` times in a row stop (its other files stay queued, never failed), and a scan that finds
the lock taken says in Russian how long the holder has run and what it does. Synthetic data only."""

from __future__ import annotations

import dataclasses
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from desk import backfill, daystatus, store
from desk.inbox import hang, scan
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
        src = src.replace("def reference(ctx):\n", "def reference(ctx):\n    _call('reference')\n", 1)
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
    assert any("Справочник адаптера не отвечает" in line for line in r.pauses), r.pauses
    r = scan.scan(con, s, now=now + timedelta(minutes=10))  # paused: not asked, the files still wait
    assert r.notes == {"reference_paused": 1} and ad.count("reference") == 1 and r.files == {}, r.text()
    assert statuses(con) == {} and sightings(con) == 0
    ad.hang("reference", False)
    r = scan.scan(con, s, now=now + timedelta(minutes=61))  # the pause lapsed and it answers: the files come in
    assert ad.count("reference") == 2 and sum(r.files.values()) == 3 and "timed_out" not in r.files, r.text()
    assert r.pauses == [] and not (tmp_path / "archive" / hang.HANG_FILE).exists()


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
