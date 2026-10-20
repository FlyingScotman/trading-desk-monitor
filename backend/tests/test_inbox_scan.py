"""desk inbox (S7a part B): the scanner on the embedded store with the canonical fixtures, positions_for over
inbox revisions, books, broken adapters, the CLI and an EOD run on inbox positions. Synthetic data only."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import cli, store
from desk.api import create_app
from desk.inbox import scan
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
PRIV, CONFIG, SNAPS, ADAPTERS = FIX / "private" / "v1", FIX / "private" / "v1" / "config", FIX / "mdhub_v1", \
    FIX / "adapters"
GOOD = str(ADAPTERS / "good_adapter")
D = date.fromisoformat


@pytest.fixture(autouse=True)
def _fresh_process():
    """Each test is a new process for the scanner: no cached adapter, no stability memory."""
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


@pytest.fixture
def con(store_url):
    with store.connect(store_url) as c:
        yield c


def cfg(tmp_path: Path, inbox: Path, adapter: str | None = None, config: Path | None = CONFIG, **kw) -> Settings:
    return Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                    private_adapter=adapter, private_adapter_config=config,
                    **{"inbox_poll_seconds": 0, "inbox_lookback_days": 0, **kw})  # fixed fixture dates: no lookback


def put(inbox: Path, *names: str, src: Path = PRIV) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.copy(src / n, inbox / n)


def qty(inputs, pid: str):
    return {p: q for p, _, _, q in inputs.rows}.get(pid)


def test_four_dates_corrections_omission_stale_copy_and_saturday(con, tmp_path):
    inbox = tmp_path / "in"
    s = cfg(tmp_path, Path(f"{inbox}/**"), GOOD)  # recursive
    put(inbox, "positions_20261014.csv", "positions_20261015.csv", "positions_20261016_2.csv",
        "positions_20261017.csv")
    r = scan.scan(con, s)
    assert r.ok and r.files == {"accepted": 3, "partially_accepted": 1}, r.text()
    assert r.revisions == 11 and r.books_revised == 4  # 4 files x B1-B3, minus B2 rejected on 14.10; 4 books
    t1 = con.execute("SELECT now()").fetchone()[0]

    put(inbox, "positions_20261016.csv", "positions_20261019.csv")  # the original after its correction; a copy
    r = scan.scan(con, s)
    assert r.files == {"superseded": 1} and r.copies == {"stale_copy": 1} and r.revisions == 0

    (inbox / "again").mkdir()
    shutil.copy(PRIV / "positions_20261014.csv", inbox / "again" / "positions_20261014.csv")
    counts = [con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("position_set", "inbox_file_status")]
    r = scan.scan(con, s)
    assert r.copies == {"duplicate": 1} and not r.files and r.revisions == 0
    assert not scan.scan(con, s).copies  # an unchanged path is not taken in again
    assert counts == [con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("position_set", "inbox_file_status")]

    monday = (PRIV / "positions_20261017.csv").read_text(encoding="utf-8-sig").replace(";13000;13000000;", ";13100;13100000;")
    (inbox / "positions_20261019_1.csv").write_text(monday, encoding="utf-8-sig")
    r = scan.scan(con, s)
    assert r.files == {"accepted": 1} and r.revisions == 1  # only B1 changed; B2, B3 equal the Saturday file

    # positions_for: per book the latest valid date <= D, the current revision by report key
    p13 = store.positions_for(con, D("2026-10-13"))
    assert qty(p13, "B1|SEC-26238") == 10000 and set(p13.valid_dates.values()) == {D("2026-10-13")}
    assert {p: s for p, _, s, _ in p13.rows}["B1|SEC-26238"] == "SU26238RMFS4"  # resolved by the adapter
    assert {p: s for p, _, s, _ in p13.rows}["B3|SEC-UNMAPPED"] is None
    p14 = store.positions_for(con, D("2026-10-14"))
    assert qty(p14, "B1|SEC-26238") == 11000
    assert p14.valid_dates == {"B1": D("2026-10-14"), "B2": D("2026-10-13"), "B3": D("2026-10-14")}  # B2 rejected
    assert qty(p14, "B2|SEC-29008") == 20000 and not [r for r in p14.rows if r[1] == "B3"]  # B3 closed by omission
    p15 = store.positions_for(con, D("2026-10-15"))
    assert qty(p15, "B1|SEC-26238") == 12500  # the correction, though the original arrived later
    assert p15.sources["B1"]["file_name"] == "positions_20261016_2.csv"
    assert p15.sources["B1"]["source"] == "positions_daily" and p15.sources["B1"]["report_date"] == D("2026-10-16")
    p16 = store.positions_for(con, D("2026-10-16"))
    assert qty(p16, "B1|SEC-26238") == 13100 and p16.sources["B1"]["file_name"] == "positions_20261019_1.csv"
    assert p16.sources["B2"]["file_name"] == "positions_20261017.csv"  # Monday equal for B2: no new revision
    assert p16.valid_dates == dict.fromkeys(("B1", "B2", "B3"), D("2026-10-16"))  # Saturday report -> Friday
    then = store.positions_for(con, D("2026-10-16"), known_at=t1)
    assert qty(then, "B1|SEC-26238") == 13000 and then.private_inputs_hash != p16.private_inputs_hash
    assert store.positions_for(con, D("2026-10-25")).valid_dates == p16.valid_dates  # carried forward
    fx = p16.extra["B1|USD"]
    assert fx == {"asset_class": "fx", "ccy": "USD", "amount_ccy": 1000000.0, "source_instrument_id": "USD",
                  "src": "R4", "x": {"x_desk_note": "spot"}}
    bond = p16.extra["B2|SEC-XR1"]
    assert bond["face_amount_ccy"] == 3000000.0 and bond["face_outstanding_ccy"] == 2400000.0
    assert isinstance(bond["face_amount_ccy"], float) and bond["isin"] == "RU000A109XR1"

    # the ledger: units, issues, events, the stale copy
    units = dict(con.execute("SELECT u.status, count(*) FROM inbox_unit u GROUP BY 1").fetchall())
    assert units == {"accepted": 14, "rejected": 1, "superseded": 3}
    issues = {(c, s["book_id"] if s else None) for c, s in con.execute("SELECT code, scope FROM inbox_issue")}
    assert {("bad_number", "B2"), ("book_closed_by_omission", "B3"), ("same_valid_date", None)} <= issues
    events = [t for (t,) in con.execute("SELECT type FROM event ORDER BY event_id")]
    assert events.count("input.accepted") == 4 and {"input.partially_accepted", "input.superseded", "input.stale",
                                                    "books.changed"} <= set(events)
    assert con.execute("SELECT kind, source, report_date, valid_date FROM inbox_sighting WHERE kind = 'stale_copy'"
                       ).fetchone() == ("stale_copy", "positions_daily", D("2026-10-19"), D("2026-10-16"))
    assert len(list((tmp_path / "archive").glob("*/positions_*.csv"))) == 6

    st = scan.status(con, s, D("2026-10-16"))
    g = {x["source"]: x for x in st["groups"]}["positions_daily"]
    assert g["files"] == {"accepted": 2} and g["copies"] == {"stale_copy": 1}
    assert {"severity": "info", "code": "same_valid_date", "count": 1} in g["issues"]
    assert {b["book_id"]: (b["valid_date"], b["age_days"], b["stale"]) for b in st["books"]} == \
        {b: ("2026-10-16", 0, False) for b in ("B1", "B2", "B3")}  # B9 is inactive
    text = scan.status_text(st)
    assert "positions_daily 2026-10-16" in text and "13100" not in text and "SEC-" not in text
    for sql in ("UPDATE inbox_file_status SET status = 'accepted'", "DELETE FROM event", "UPDATE book_rev SET active = false"):
        with pytest.raises(Exception, match="append-only"):
            con.execute(sql)


def test_stability_waits_for_a_quiet_file(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv")
    (inbox / "~$positions_20261015.csv").write_text("lock", encoding="utf-8")
    (inbox / "x.part").write_text("partial", encoding="utf-8")
    s = cfg(tmp_path, inbox, None, inbox_poll_seconds=60)
    r = scan.scan(con, s)
    assert r.skipped == {"waiting": 1} and not r.files  # just copied: not offered yet
    assert scan.scan(con, s).files == {"accepted": 1}  # unchanged since the previous scan
    scan._last_seen.clear()
    big = cfg(tmp_path, inbox, None, inbox_max_file_mb=1e-6)
    assert scan.scan(con, big).skipped == {"too_large": 1}
    put(tmp_path / "in2", "positions_20261015.csv")
    later = datetime.now(UTC) + timedelta(seconds=61)  # a one-shot scan of a file copied a minute ago
    assert scan.scan(con, cfg(tmp_path, tmp_path / "in2", None, inbox_poll_seconds=60), now=later).files == \
        {"partially_accepted": 1}


def test_fingerprint_change_reoffers_rejected_files_and_replay_adds_nothing(con, tmp_path):
    inbox, conf = tmp_path / "in", tmp_path / "conf"
    conf.mkdir()
    put(inbox, "positions_20261014.csv", "README.md")
    s = cfg(tmp_path, inbox, None, conf)
    r = scan.scan(con, s)  # no books.csv: no books, every unit unknown_book
    assert r.files == {"rejected": 1, "ignored": 1} and r.units == {"rejected": 3}
    assert scan.scan(con, s).files == {}  # same fingerprint: nothing again
    shutil.copy(CONFIG / "books.csv", conf / "books.csv")
    scan._handles.clear()  # a new process with the new mappings
    (inbox / "positions_20261014.csv").unlink()  # the bank cleaned its folder: read from the archive
    r = scan.scan(con, s)
    assert r.files == {"accepted": 1, "ignored": 1} and r.revisions == 3, r.text()

    n = con.execute("SELECT count(*) FROM position_set").fetchone()[0]
    r = scan.replay(con, s, "positions_csv", D("2026-10-13"), D("2026-10-13"))
    assert r.files == {"accepted": 1} and r.revisions == 0 and r.units == {"accepted": 3}
    assert con.execute("SELECT count(*) FROM position_set").fetchone()[0] == n
    assert scan.replay(con, s, "positions_csv", D("2026-10-14"), D("2026-10-20")).files == {}
    assert scan.retry(con, s, ("ignored",)).files == {"ignored": 1}


def test_scope_conflict_between_load_positions_and_the_inbox(con, tmp_path):
    store.load_positions(con, [("L1", "B1", "SU26238RMFS4", 5.0)], valid_date=D("2026-10-13"),
                         source_name="legacy.csv", source_sha256="0" * 64)
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv")
    r = scan.scan(con, cfg(tmp_path, inbox))
    assert r.files == {"partially_accepted": 1} and r.units == {"accepted": 2, "rejected": 1}
    assert con.execute("SELECT scope->>'book_id' FROM inbox_issue WHERE code = 'scope_conflict'").fetchone() == ("B1",)
    p = store.positions_for(con, D("2026-10-13"))
    assert [r for r in p.rows if r[1] == "B1"] == [("L1", "B1", "SU26238RMFS4", 5.0)]
    assert p.sources["B1"] == {"source": "load-positions", "report_date": None, "inbox_file_sha256": None,
                               "file_name": "legacy.csv"}
    assert p.sources["B2"]["source"] == "positions_csv" and "B2|SEC-29008" in p.extra and "L1" not in p.extra
    with pytest.raises(store.InputError, match=r"scope_conflict for 2026-10-13: B2 \(from positions_csv\)"):
        store.load_positions(con, [("L2", "B2", "SU26238RMFS4", 1.0)], valid_date=D("2026-10-13"),
                             source_name="legacy2.csv", source_sha256="1" * 64)


def test_books_are_bitemporal(con):
    books = [{"book_id": "B1", "name": "n", "desk": "Rates", "book_type": "TRADING", "entity": "E", "active": True}]
    assert store.store_books(con, books, "f1") == ["B1"] and store.store_books(con, books, "f1") == []
    t = con.execute("SELECT now()").fetchone()[0]
    assert store.store_books(con, [{**books[0], "book_type": "HTM", "valid_from": "2026-11-01"}], "f2") == ["B1"]
    assert store.current_books(con, D("2026-10-01"))[0]["book_type"] == "TRADING"  # the change applies later
    assert store.current_books(con, D("2026-11-02"))[0]["book_type"] == "HTM"
    assert store.current_books(con, D("2026-11-02"), t)[0]["book_type"] == "TRADING"  # as known before it
    assert store.current_books(con, D("2020-01-01"))[0]["book_id"] == "B1"  # the first revision: always


@pytest.mark.parametrize(("adapter", "error"), [("import_error", "import_error"), ("wrong_spi", "unsupported_spi"),
                                                ("json", "name_collision")])
def test_broken_adapters_leave_ingestion_off(con, store_url, tmp_path, adapter, error):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv")
    s = cfg(tmp_path, inbox, str(ADAPTERS / adapter))
    r = scan.scan(con, s)
    assert not r.ok and r.adapter.startswith(f"error {error}") and "ingestion is off" in r.text()
    assert con.execute("SELECT count(*) FROM inbox_file").fetchone()[0] == 0
    assert scan.status(con, s)["adapter"].startswith("error")
    c = TestClient(create_app(store_url, adapter_status=lambda: scan.adapter_line(scan.adapter(s))))
    assert c.get("/health/ready").json()["adapter"].startswith(f"error {error}")
    assert c.get("/api/v1/runs").status_code == 200


def test_match_raising_and_read_hanging_are_ledgered(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv", "README.md")
    r = scan.scan(con, cfg(tmp_path, inbox, str(ADAPTERS / "match_raises"), None))
    assert r.ok and r.files == {"match_failed": 1, "ignored": 1}
    (etype, frames), = con.execute("SELECT error_type, error_frames FROM inbox_file_status"
                                   " WHERE status = 'match_failed'").fetchall()
    assert etype == "ValueError" and frames and "4242" not in json.dumps(frames)
    scan._handles.clear()
    r = scan.scan(con, cfg(tmp_path, inbox, str(ADAPTERS / "read_hangs"), None, adapter_read_timeout_s=1))
    assert r.files == {"timed_out": 2}  # fingerprint changed: both re-offered; read hangs on each
    assert {t for (t,) in con.execute("SELECT type FROM event")} >= {"input.match_failed", "input.failed"}


def test_waiting_calendar_is_retried(con, tmp_path):
    inbox = tmp_path / "in"
    (inbox).mkdir()
    shutil.copy(PRIV / "positions_20261014.csv", inbox / "positions_20280110.csv")  # beyond the calendar
    s = cfg(tmp_path, inbox)
    assert scan.scan(con, s).files == {"waiting_calendar": 1}
    assert scan.scan(con, s).files == {}  # the same calendars: not matched again
    scan._static_seen.clear()  # as after a sync brought a static snapshot
    assert scan.scan(con, s).files == {"waiting_calendar": 1}  # retried, no second status row
    assert con.execute("SELECT count(*) FROM inbox_file_status").fetchone()[0] == 1


def _counting(monkeypatch) -> dict[str, list[str]]:
    """Count the files hashed and read by the scanner."""
    calls: dict[str, list[str]] = {"hash": [], "read": []}
    sha, read = scan._sha256, scan.runner.call_read

    def counted_sha(path):
        calls["hash"].append(Path(path).name)
        return sha(path)

    def counted_read(h, path, *a, **kw):
        calls["read"].append(Path(path).name)
        return read(h, path, *a, **kw)

    monkeypatch.setattr(scan, "_sha256", counted_sha)
    monkeypatch.setattr(scan.runner, "call_read", counted_read)
    return calls


def test_lookback_skips_old_files_unhashed_and_ignores_old_report_dates_once(con, tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    now = datetime(2026, 10, 27, 9, tzinfo=UTC)  # lookback 10 days: report dates from 2026-10-17 on
    put(inbox, "positions_20261014.csv", "positions_20261015.csv", "positions_20261017.csv", "README.md")
    years = []
    for y in range(2015, 2025):  # years of daily reports: modified long ago
        f = inbox / "archive" / f"positions_{y}0110.csv"
        f.parent.mkdir(exist_ok=True)
        shutil.copy(PRIV / "positions_20261014.csv", f)
        t = datetime(y, 1, 10, 20, tzinfo=UTC).timestamp()
        os.utime(f, (t, t))
        years.append(f.name)
    fresh = datetime(2026, 10, 26, 8, tzinfo=UTC).timestamp()  # copied recently, whatever their report date
    for n in ("positions_20261014.csv", "positions_20261015.csv", "positions_20261017.csv", "README.md"):
        os.utime(inbox / n, (fresh, fresh))
    s = cfg(tmp_path, Path(f"{inbox}/**"), inbox_lookback_days=10)
    calls = _counting(monkeypatch)
    r = scan.scan(con, s, now=now)
    assert r.skipped == {"too_old": len(years)}, r.text()
    assert not set(calls["hash"]) & set(years)  # a stat only: never hashed, matched or read
    assert r.files == {"ignored_too_old": 2, "accepted": 1, "ignored": 1}, r.text()
    assert calls["read"] == ["positions_20261017.csv"]
    assert set(calls["hash"]) == {"positions_20261017.csv"}  # an old report date or not the adapter's: never hashed
    rows = dict(con.execute("SELECT report_date, source FROM inbox_unread WHERE status = 'ignored_too_old'").fetchall())
    assert set(rows) == {D("2026-10-14"), D("2026-10-15")}  # remembered by path, size and time only
    assert not con.execute("SELECT 1 FROM inbox_file_status WHERE status LIKE 'ignored%'").fetchone()
    assert len(list((tmp_path / "archive").glob("*/positions_*.csv"))) == 1  # only the file read is archived
    assert not con.execute("SELECT 1 FROM event WHERE type LIKE '%too_old%'").fetchone()  # no event flood

    calls["hash"].clear()
    r = scan.scan(con, s, now=now)  # the second scan: nothing again, nothing hashed
    assert r.files == {} and calls == {"hash": [], "read": ["positions_20261017.csv"]}, r.text()
    scan._handles.clear()  # a new process (an adapter update reloads the same way): still too old, still quiet
    assert scan.scan(con, s, now=now + timedelta(days=1)).files == {}
    assert calls["read"] == ["positions_20261017.csv"]

    r = scan.scan(con, cfg(tmp_path, Path(f"{inbox}/**"), inbox_lookback_days=30), now=now)  # a longer lookback
    assert calls["read"][1:] and set(calls["read"][1:]) == {"positions_20261014.csv", "positions_20261015.csv"}
    assert r.files.get("ignored_too_old", 0) == 0 and r.skipped == {"too_old": len(years)}, r.text()


def test_waiting_calendar_behind_the_lookback_becomes_terminal(con, tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    shutil.copy(PRIV / "positions_20261014.csv", inbox / "positions_20280110.csv")  # beyond the calendar
    s = cfg(tmp_path, inbox, inbox_lookback_days=10)
    assert scan.scan(con, s).files == {"waiting_calendar": 1}
    later = datetime(2028, 3, 1, 9, tzinfo=UTC)  # the report date is now behind the lookback
    assert scan.scan(con, s, now=later).files == {"ignored_too_old": 1}
    assert scan.scan(con, s, now=later).files == {}  # terminal: not offered again
    assert con.execute("SELECT status, report_date, archive_path IS NOT NULL FROM inbox_file_status"
                       " ORDER BY status_id DESC LIMIT 1").fetchone() == ("ignored_too_old", D("2028-01-10"), True)
    assert scan.retry(con, s).files == {}  # not a retry status either


def test_interrupted_scan_resumes_without_redoing_finished_files(con, tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261015.csv", "positions_20261016.csv", "positions_20261017.csv")
    s = cfg(tmp_path, inbox)
    calls = _counting(monkeypatch)
    read = scan.runner.call_read  # the counting one

    def stop_on_second(h, path, *a, **kw):
        if len(calls["read"]) == 1:
            raise KeyboardInterrupt  # stop.bat in the middle of the second read
        return read(h, path, *a, **kw)

    monkeypatch.setattr(scan.runner, "call_read", stop_on_second)
    seen: list[tuple] = []
    with pytest.raises(KeyboardInterrupt):
        scan.scan(con, s, progress=lambda done, total, detail: seen.append((done, total)))
    assert seen[0] == (0, None) and (0, 3) in seen and (1, 3) in seen  # listing, then files done of offered
    assert con.execute("SELECT count(*) FROM inbox_file_status").fetchone()[0] == 1  # the second rolled back
    assert con.execute("SELECT pg_try_advisory_lock(%s)", (scan.SCAN_LOCK,)).fetchone()[0]  # lock released
    con.execute("SELECT pg_advisory_unlock(%s)", (scan.SCAN_LOCK,))
    done = calls["read"][0]
    monkeypatch.setattr(scan.runner, "call_read", read)
    r = scan.scan(con, s)  # resumes: the finished file is not read again
    assert calls["read"][0] == done and sorted(calls["read"][1:]) == sorted(
        {"positions_20261015.csv", "positions_20261016.csv", "positions_20261017.csv"} - {done}), calls
    assert sum(r.files.values()) == 2, r.text()


def test_end_to_end_scan_then_eod(store_url, tmp_path, monkeypatch, capsys):
    inbox = tmp_path / "in"
    inbox.mkdir()
    text = (PRIV / "positions_20261016.csv").read_text(encoding="utf-8-sig")
    text = text.replace("B3;;;;;;;;;;;;;", "B3;B3|SEC-XS1;bond;SEC-XS1;Foreign bond;;RUB;;1000000;;;;98.5;")
    (inbox / "positions_20261016.csv").write_text(text, encoding="utf-8-sig")
    for k, v in {"DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": store_url,
                 "DESK_INBOX_DIRS": f"{inbox};{tmp_path / 'missing'}", "DESK_INBOX_ARCHIVE": str(tmp_path / "arch"),
                 "DESK_SNAPSHOT_CACHE": str(SNAPS), "DESK_PRIVATE_ADAPTER_CONFIG": str(CONFIG),
                 "DESK_INBOX_POLL_SECONDS": "0", "DESK_INBOX_LOOKBACK_DAYS": "0"}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["inbox", "scan"]) == 0
    assert cli.main(["eod", "--date", "2026-10-15", "--snapshots", str(SNAPS)]) == 0
    out = capsys.readouterr().out
    assert "files: accepted 1" in out and "desk run 1: complete" in out
    assert "B1: positions_csv positions_20261016.csv, report 2026-10-16, valid 2026-10-15" in out
    with store.connect(store_url) as con:
        lin = con.execute("SELECT lineage FROM run WHERE run_id = 1").fetchone()[0]
        sha = con.execute("SELECT sha256 FROM inbox_file").fetchone()[0]
        q = dict(con.execute("SELECT position_id, quality FROM run_position WHERE run_id = 1").fetchall())
    src = lin["positions"]["sources"]["B1"]
    assert src["file_name"] == "positions_20261016.csv" and src["inbox_file_sha256"] == sha
    assert lin["positions"]["valid_dates"]["B3"] == "2026-10-15"
    assert q["B3|SEC-XS1"] == "reported_value" and q["B1|USD"] == "market_value" and q["B1|SEC-26238"] == "exact"

    assert cli.main(["inbox", "status", "--date", "2026-10-15", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["groups"][0]["files"] == {"accepted": 1} and st["adapter"].startswith("ok desk_reference")
    assert cli.main(["inbox", "replay", "--source", "positions_csv", "--from", "2026-10-15", "--to", "2026-10-15"]) == 0
    assert "new revisions 0" in capsys.readouterr().out
    assert cli.main(["inbox", "retry", "--status", "rejected"]) == 0
    assert cli.main(["adapter", "check", "--stub-ctx"]) == 0
    assert "result: OK" in capsys.readouterr().out
    assert cli.main(["config"]) == 0
    assert "inbox_poll_seconds       0" in capsys.readouterr().out
    assert cli.main(["runs"]) == 0


def _adapter_calls(monkeypatch) -> dict[str, int]:
    """Count the adapter calls of the scanner by kind."""
    n = {"match": 0, "read": 0, "reference": 0}
    for kind in n:
        fn = getattr(scan.runner, f"call_{kind}")

        def counted(*a, _fn=fn, _kind=kind, **kw):
            n[_kind] += 1
            return _fn(*a, **kw)

        monkeypatch.setattr(scan.runner, f"call_{kind}", counted)
    return n


def test_an_idle_scan_touches_no_file_and_calls_no_adapter(con, tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261015.csv", "positions_20261016.csv")
    for i in range(300):  # a folder of old-format reports the adapter does not take
        (inbox / f"old_report_{i:04d}.xls").write_bytes(b"x" * 64)
    s = cfg(tmp_path, inbox)
    calls, n = _counting(monkeypatch), _adapter_calls(monkeypatch)
    r = scan.scan(con, s)
    assert r.files == {"accepted": 1, "partially_accepted": 1, "ignored": 300}, r.text()
    assert set(calls["hash"]) == {"positions_20261015.csv", "positions_20261016.csv"}  # declined: never hashed
    assert n == {"match": 7, "read": 2, "reference": 1}  # 302 files: 7 match batches of 50
    assert con.execute("SELECT count(*) FROM inbox_unread WHERE status = 'ignored'").fetchone()[0] == 300
    assert not con.execute("SELECT 1 FROM event WHERE type = 'input.ignored'").fetchone()  # no event flood

    for k in n:
        n[k] = 0
    calls["hash"].clear()
    r = scan.scan(con, s)  # nothing new: a folder listing, no hash, no adapter call, no reference()
    assert r.files == {} and n == {"match": 0, "read": 0, "reference": 0} and not calls["hash"], r.text()
    assert scan.status(con, s)["groups"][0]["files"] == {"ignored": 300}

    (inbox / "old_report_0000.xls").write_bytes(b"y" * 65)  # a changed file is looked at again
    assert scan.scan(con, s).files == {"ignored": 1} and n["match"] == 1 and n["reference"] == 0

    scan._handles.clear()
    conf = tmp_path / "conf"
    shutil.copytree(CONFIG, conf)
    (conf / "note.txt").write_text("a new config file: a new fingerprint", encoding="utf-8")
    r = scan.scan(con, cfg(tmp_path, inbox, config=conf))  # the adapter changed: declined files asked again
    assert r.files == {"ignored": 300} and n["reference"] == 1 and n["read"] == 0, r.text()  # books refreshed once
    assert scan.retry(con, s, ("ignored",)).files == {"ignored": 300}


REVISIONS_4_FILES = 9  # the same in any order: processing order is not a semantic (08 §6.6 revision keys)


@pytest.mark.parametrize("workers", ["1", "3"])
def test_reads_run_in_parallel_workers_newest_report_first(con, tmp_path, monkeypatch, workers):
    monkeypatch.setenv(scan.READ_WORKERS_ENV, workers)
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv", "positions_20261015.csv", "positions_20261016.csv",
        "positions_20261017.csv")
    seen: list[tuple] = []
    r = scan.scan(con, cfg(tmp_path, inbox), progress=lambda done, total, detail: seen.append((done, total, detail)))
    assert r.files == {"accepted": 3, "partially_accepted": 1} and r.revisions == REVISIONS_4_FILES, r.text()
    order = [n for (n,) in con.execute("SELECT f.name FROM inbox_file_status s JOIN inbox_file f USING (sha256)"
                                       " ORDER BY s.status_id")]
    assert order == ["positions_20261017.csv", "positions_20261016.csv", "positions_20261015.csv",
                     "positions_20261014.csv"]
    assert (4, 4, "positions_20261014.csv") in seen


def test_a_hung_worker_is_killed_and_the_next_call_starts_a_new_one(tmp_path):
    from desk.inbox.loader import load_adapter

    hangs = load_adapter(str(ADAPTERS / "read_hangs"), None, None)
    good = load_adapter(None, None, None)
    f = str(PRIV / "positions_20261014.csv")
    with scan.runner.pooled(1) as pool:
        with pytest.raises(scan.AdapterFailure, match="timed_out"):
            scan.runner.call_read(hangs, f, {"source": "market", "report_date": "2026-10-15"}, None, 1)
        assert scan.runner.call_match(good, [f], None, 10)[f]["source"] == "positions_csv"
        w = pool._all[0]
        assert w._p is not None and w._p.poll() is None  # one live worker serves the following calls
    assert w._p is None  # ended with the block
