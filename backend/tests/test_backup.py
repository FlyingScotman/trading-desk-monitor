"""Backups of the desk store and the restore drill (desk.backup, trading-desk-monitor#78), on the embedded
PostgreSQL of the tests (pgserver, whose pg_dump / pg_restore the backups use too): a backup holds the exact row
counts of its snapshot, the drill restores into a scratch database and compares them, a restore replaces the live
database only confirmed, with no other connection and after a pre-restore backup; retention, the schedule's
catch-up, the scheduler thread, the CLI, the settings page's routes and section. Synthetic data only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from desk import backup, cli, store
from desk.api import create_app
from desk.api.auth import audit
from desk.settings import Settings, SettingsError, load

PROTO = Path(__file__).resolve().parents[2] / "prototypes"


def _settings(tmp_path: Path, **kw) -> Settings:
    return Settings(home=tmp_path / "home", backup_dir=tmp_path / "backups", **kw)


def _fill(url: str, n: int = 3, start: int = 0) -> None:
    """A few rows in append-only tables (positions: triggers reject updates) and the audit log."""
    with store.connect(url) as con:
        rows = [(f"P{start + i}", "BOOK-A", f"SU{26200 + start + i}RMFS0", 10.0 * (i + 1)) for i in range(n)]
        store.load_positions(con, rows, valid_date=date(2026, 10, 15 + start % 10), source_name=f"t{start}.csv",
                             source_sha256=f"{start:064d}")
        for i in range(n):
            audit(con, "tester", "test_event", f"row {start + i}")


def _count(url: str, table: str) -> int:
    with store.connect(url) as con:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _dbs(url: str) -> set[str]:
    with backup._admin(url) as con:
        return {r[0] for r in con.execute("SELECT datname FROM pg_database")}


# ---- backup ----------------------------------------------------------------------------------------------------


def test_backup_writes_a_dump_with_its_snapshot_counts(store_url, tmp_path):
    _fill(store_url)
    s = _settings(tmp_path)
    b = backup.create(s, store_url, "manual", now=datetime(2026, 10, 23, 20, 30, 5))
    assert b.name == "desk-20261023-203005-manual.dump" and b.path.is_file() and b.size > 0
    meta = json.loads(b.path.with_suffix(".json").read_text(encoding="utf-8"))
    with store.connect(store_url) as con:
        assert meta["tables"] == backup.table_counts(con)
    assert meta["tables"]["audit_log"] == 3 and meta["tables"]["position_input"] == 3
    assert meta["schema"] == max(f.stem for f in store.MIGRATIONS.glob("*.sql"))
    assert meta["sha256"] == backup._sha256(b.path) and meta["entries"] == backup.archive_entries(b.path)
    assert not list(b.path.parent.glob("*.partial"))
    [listed] = backup.list_backups(s.backup_dir)
    assert listed.name == b.name and listed.to_json()["rows"] == sum(meta["tables"].values())
    last = backup.read_state(s.home)["last"]
    assert last["ok"] and last["file"] == b.name


def test_backup_failure_leaves_nothing_and_is_recorded(store_url, tmp_path, monkeypatch):
    """A pg_dump that fails (here a stand-in that exits 1): no file, not even a .partial; the state says why."""
    fake = tmp_path / "pg_dump"
    fake.write_text("#!/bin/sh\necho 'pg_dump: error: connection failed' >&2\nexit 1\n")
    fake.chmod(0o755)
    real = backup.tool
    monkeypatch.setattr(backup, "tool", lambda name: fake if name == "pg_dump" else real(name))
    s = _settings(tmp_path)
    with pytest.raises(backup.BackupError, match="pg_dump failed: pg_dump: error: connection failed"):
        backup.create(s, store_url)
    assert list(s.backup_dir.iterdir()) == []
    last = backup.read_state(s.home)["last"]
    assert last["ok"] is False and "connection failed" in last["error"]


def test_one_backup_at_a_time(store_url, tmp_path):
    s = _settings(tmp_path)
    lock = backup._lock(s.home)
    assert lock.acquire()
    try:
        with pytest.raises(backup.BackupError, match="another backup is running"):
            backup.create(s, store_url)
    finally:
        lock.release()


def test_retention_keeps_the_newest_and_the_pre_restore_dumps_apart(tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    for day in range(1, 21):
        (folder / f"desk-202610{day:02d}-203000-nightly.dump").write_bytes(b"x")
        (folder / f"desk-202610{day:02d}-203000-nightly.json").write_text("{}")
    for day in (2, 9):
        (folder / f"desk-202610{day:02d}-120000-manual.dump").write_bytes(b"x")
    for day in range(1, 6):
        (folder / f"desk-202610{day:02d}-100000-pre-restore.dump").write_bytes(b"x")
    (folder / "notes.txt").write_text("kept")
    removed = backup.prune(folder, 14)
    left = backup.list_backups(folder)
    regular = [b.name for b in left if b.kind != "pre-restore"]
    assert len(regular) == 14 and regular[0] == "desk-20261020-203000-nightly.dump"
    assert regular[-1] == "desk-20261008-203000-nightly.dump"  # nightly 20..8 and the manual of the 9th
    assert [b.name for b in left if b.kind == "pre-restore"] == [
        f"desk-202610{d:02d}-100000-pre-restore.dump" for d in (5, 4, 3)]
    assert len(removed) == 22 + 5 - 14 - 3
    assert not (folder / "desk-20261001-203000-nightly.json").exists() and (folder / "notes.txt").exists()


# ---- schedule --------------------------------------------------------------------------------------------------


def _touch(folder: Path, when: datetime, kind: str = "nightly") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"desk-{when:%Y%m%d-%H%M%S}-{kind}.dump").write_bytes(b"x")


def test_due_after_the_time_and_catch_up_after_a_night_off(tmp_path):
    s = _settings(tmp_path, backup_time="20:30")
    assert backup.due(s, datetime(2026, 10, 23, 9, 0))  # never backed up: at once
    _touch(s.backup_dir, datetime(2026, 10, 22, 20, 31))
    assert not backup.due(s, datetime(2026, 10, 23, 20, 29))
    assert backup.due(s, datetime(2026, 10, 23, 20, 30))
    assert backup.next_at(s, datetime(2026, 10, 23, 12, 0)) == datetime(2026, 10, 23, 20, 30)
    # the computer was off at 20:30 on the 23rd and the 24th: due as soon as it runs on the 25th
    assert backup.due(s, datetime(2026, 10, 25, 8, 0))
    _touch(s.backup_dir, datetime(2026, 10, 25, 8, 0), "pre-restore")  # a pre-restore dump is not the daily one
    assert backup.due(s, datetime(2026, 10, 25, 8, 1))
    _touch(s.backup_dir, datetime(2026, 10, 25, 8, 2), "manual")  # a manual one is
    assert not backup.due(s, datetime(2026, 10, 25, 8, 3))
    assert not backup.due(_settings(tmp_path, backup=False), datetime(2026, 10, 30, 23, 0))
    assert backup.next_at(_settings(tmp_path, backup=False)) is None


def test_scheduler_makes_the_due_backup_once_and_waits_after_a_failure(store_url, tmp_path, monkeypatch):
    s = _settings(tmp_path)
    now = [datetime(2026, 10, 23, 21, 0)]
    sch = backup.Scheduler(settings_loader=lambda: s, store_url=store_url, clock=lambda: now[0])
    b = sch.tick()
    assert b is not None and b.kind == "nightly" and b.name.startswith("desk-20261023-210000")
    assert sch.tick() is None  # done for today
    now[0] = datetime(2026, 10, 24, 20, 31)
    monkeypatch.setattr(backup, "tool", lambda name: (_ for _ in ()).throw(backup.BackupError(f"{name} not found")))
    assert sch.tick() is None and sch._retry_after > time.monotonic() + backup.RETRY_S - 60
    monkeypatch.undo()
    assert sch.tick() is None  # not before RETRY_S
    sch._retry_after = 0.0
    assert sch.tick() is not None


def test_scheduler_thread_starts_and_stops(store_url, tmp_path):
    s = _settings(tmp_path)
    sch = backup.Scheduler(settings_loader=lambda: s, store_url=store_url, check_s=0.05)
    sch.start()
    try:
        for _ in range(200):
            if backup.list_backups(s.backup_dir):
                break
            time.sleep(0.05)
    finally:
        sch.stop()
    assert [b.kind for b in backup.list_backups(s.backup_dir)] == ["nightly"]


# ---- restore drill and restore ---------------------------------------------------------------------------------


def test_restore_check_restores_into_a_scratch_database_and_compares_counts(store_url, tmp_path):
    _fill(store_url)
    s = _settings(tmp_path)
    b = backup.create(s, store_url)
    _fill(store_url, 2, start=10)  # written after the backup: the drill compares with the backup, not the live data
    rep = backup.restore_check(s, store_url, b.path)
    assert rep["ok"], rep["problems"]
    assert rep["meta"] and rep["tables"] == b.meta["tables"] and rep["rows"] == sum(b.meta["tables"].values())
    assert rep["schema"] == b.meta["schema"] and rep["migrated"] == []
    assert backup.db_name(store_url) + backup.SCRATCH_SUFFIX not in _dbs(store_url)  # dropped again
    assert _count(store_url, "audit_log") == 5  # the live data untouched
    assert backup.read_state(s.home)["last_check"]["ok"] is True


def test_restore_check_finds_a_damaged_or_altered_backup(store_url, tmp_path):
    _fill(store_url)
    s = _settings(tmp_path)
    b = backup.create(s, store_url)
    meta_path = b.path.with_suffix(".json")
    meta = json.loads(meta_path.read_text())
    meta["tables"]["audit_log"] += 1
    meta_path.write_text(json.dumps(meta))
    rep = backup.restore_check(s, store_url, b.path)
    assert not rep["ok"] and any("audit_log: 4 at backup, 3 restored" in p for p in rep["problems"])
    meta["tables"]["audit_log"] -= 1
    meta_path.write_text(json.dumps(meta))
    data = b.path.read_bytes()
    b.path.write_bytes(data[:-200] + bytes(200))  # same size, other bytes
    rep = backup.restore_check(s, store_url, b.path)
    assert not rep["ok"] and any("SHA-256 differs" in p for p in rep["problems"])
    b.path.write_bytes(data[: len(data) // 2])  # a copy cut short
    rep = backup.restore_check(s, store_url, b.path)
    assert not rep["ok"] and any("cut short" in p for p in rep["problems"])
    assert any("pg_restore" in p for p in rep["problems"])
    assert backup.db_name(store_url) + backup.SCRATCH_SUFFIX not in _dbs(store_url)


def test_restore_check_refuses_a_backup_of_a_newer_program(store_url, tmp_path):
    s = _settings(tmp_path)
    with store.connect(store_url) as con:
        con.execute("INSERT INTO schema_migration (version) VALUES ('9999_from_the_future')")
    b = backup.create(s, store_url)
    rep = backup.restore_check(s, store_url, b.path)
    assert not rep["ok"] and any("newer program version" in p and "9999_from_the_future" in p for p in rep["problems"])


def test_restore_replace_swaps_in_the_checked_copy_after_a_pre_restore_backup(store_url, tmp_path):
    _fill(store_url)
    s = _settings(tmp_path)
    b = backup.create(s, store_url)
    _fill(store_url, 2, start=10)
    seen = {}

    def confirm(report):
        seen.update(report)
        return True

    rep = backup.restore_replace(s, store_url, b.path, confirm=confirm, actor="cli:tester")
    assert seen["ok"] and seen["live_tables"]["audit_log"] == 5
    pre = Path(rep["pre_restore"])
    assert pre.is_file() and backup.parse_name(pre.name)[1] == "pre-restore"
    assert backup.read_meta(pre)["tables"]["audit_log"] == 5  # the data written after the backup is kept there
    assert _count(store_url, "position_input") == 3  # the live database is the backup's again
    with store.connect(store_url) as con:
        actions = [r[0] for r in con.execute("SELECT action FROM audit_log ORDER BY audit_id")]
    assert actions == ["test_event"] * 3 + ["backup_restore"]
    live = backup.db_name(store_url)
    assert live + backup.PREVIOUS_SUFFIX in _dbs(store_url) and live + backup.SCRATCH_SUFFIX not in _dbs(store_url)
    assert backup.read_state(s.home)["last_restore"]["by"] == "cli:tester"
    # the restored copy is a working store: this program's migrations and writes go on
    assert store.migrate(store_url) == []
    _fill(store_url, 1, start=20)
    # a second restore replaces the earlier "before restore" database
    rep2 = backup.restore_replace(s, store_url, pre, confirm=lambda r: True)
    assert rep2["ok"] and _count(store_url, "position_input") == 5


def test_restore_replace_changes_nothing_unconfirmed_or_with_the_server_running(store_url, tmp_path):
    _fill(store_url)
    s = _settings(tmp_path)
    b = backup.create(s, store_url)
    _fill(store_url, 2, start=10)
    with pytest.raises(backup.BackupError, match="not confirmed"):
        backup.restore_replace(s, store_url, b.path, confirm=lambda r: False)
    assert _count(store_url, "audit_log") == 5
    assert [x.kind for x in backup.list_backups(s.backup_dir)] == ["manual"]  # no pre-restore dump either
    live = backup.db_name(store_url)
    assert live + backup.SCRATCH_SUFFIX not in _dbs(store_url)
    serving = backup._lock(s.home, backup.SERVE_LOCK)  # desk serve runs (between its connections, too)
    assert serving.acquire()
    try:
        with pytest.raises(backup.BackupError, match="Desk Monitor is running"):
            backup.restore_replace(s, store_url, b.path, confirm=lambda r: pytest.fail("asked"))
    finally:
        serving.release()
    cycle = backup._lock(s.home, "cycle.lock")  # update_daily.bat's cycle
    assert cycle.acquire()
    try:
        with pytest.raises(backup.BackupError, match="autopilot cycle is running"):
            backup.restore_replace(s, store_url, b.path, confirm=lambda r: pytest.fail("asked"))
    finally:
        cycle.release()
    with psycopg.connect(store_url) as held:  # another client of the database
        held.execute("SELECT 1")
        with pytest.raises(backup.BackupError, match="stop the server first"):
            backup.restore_replace(s, store_url, b.path, confirm=lambda r: pytest.fail("asked"))
    assert _count(store_url, "audit_log") == 5


def test_restore_replace_refuses_a_failed_check(store_url, tmp_path):
    s = _settings(tmp_path)
    b = backup.create(s, store_url)
    b.path.write_bytes(b"PGDMP not really")
    with pytest.raises(backup.BackupError, match="the restore check failed, the live data is unchanged"):
        backup.restore_replace(s, store_url, b.path, confirm=lambda r: pytest.fail("asked"))
    assert backup.db_name(store_url) + backup.SCRATCH_SUFFIX not in _dbs(store_url)


# ---- helpers, settings -----------------------------------------------------------------------------------------


def test_urls_and_disks(tmp_path):
    assert backup.db_name("postgresql://postgres:@/desk?host=/tmp/x") == "desk"
    assert backup.with_db("postgresql://postgres:@/desk?host=/tmp/x", "desk_restore_check") == \
        "postgresql://postgres:@/desk_restore_check?host=/tmp/x"
    assert backup.with_db("postgresql://u:p@h:5432/desk", "postgres") == "postgresql://u:p@h:5432/postgres"
    assert backup.db_name("postgresql://u@h/?dbname=other") == "other"
    assert "dbname=x" in backup.with_db("postgresql://u@h/?dbname=other", "x")
    assert backup.same_disk(tmp_path / "a" / "b", tmp_path) is True
    assert backup.parse_name("desk-20261023-203000-nightly.dump") == (datetime(2026, 10, 23, 20, 30), "nightly")
    assert backup.parse_name("desk-20261023-203000-weekly.dump") is None


def test_backup_settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(home))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for k in ("DESK_BACKUP", "DESK_BACKUP_DIR", "DESK_BACKUP_KEEP", "DESK_BACKUP_TIME"):
        monkeypatch.delenv(k, raising=False)
    st = load()
    assert (st.backup, st.backup_dir, st.backup_keep, st.backup_time) == (True, None, 14, "20:30")
    assert backup.backup_dir(st) == home / "backups"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"backup_dir": "copies", "backup_keep": 30, "backup_time": "7:05",
                                                    "backup": False}))
    st = load()
    assert (st.backup, st.backup_dir, st.backup_keep, st.backup_time) == (False, home / "copies", 30, "07:05")
    monkeypatch.setenv("DESK_BACKUP_TIME", "25:00")
    with pytest.raises(SettingsError, match="backup_time"):
        load()
    monkeypatch.setenv("DESK_BACKUP_TIME", "21:15")
    assert dict(load().describe())["backup"] == "off" and load().backup_time == "21:15"


# ---- the command line ------------------------------------------------------------------------------------------


@pytest.fixture
def desk_env(store_url, tmp_path, monkeypatch):
    for k, v in {"DESK_HOME": str(tmp_path / "home"), "DESK_ENV_FILE": str(tmp_path / "none.env"),
                 "DESK_STORE_URL": store_url, "DESK_BACKUP_DIR": str(tmp_path / "backups")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DESK_BACKUP_TIME", raising=False)
    return tmp_path


def test_cli_backup_restore_check_and_replace(desk_env, store_url, capsys, monkeypatch):
    _fill(store_url)
    assert cli.main(["backup"]) == 0
    out = capsys.readouterr().out
    assert "backup " in out and "rows" in out
    assert cli.main(["backup", "--list"]) == 0
    assert "1 backups" in capsys.readouterr().out
    _fill(store_url, 2, start=10)
    assert cli.main(["restore"]) == 0
    out = capsys.readouterr().out
    assert "restore check ok" in out and "equal to the counts at backup time" in out and "--replace" in out
    assert _count(store_url, "audit_log") == 5
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert cli.main(["restore", "--replace"]) == 1
    assert "not confirmed" in capsys.readouterr().err and _count(store_url, "audit_log") == 5
    monkeypatch.setattr("builtins.input", lambda prompt: "restore")
    assert cli.main(["restore", "--replace"]) == 0
    assert "RESTORED" in capsys.readouterr().out and _count(store_url, "audit_log") == 4  # 3 + backup_restore
    assert cli.main(["restore", "nope.dump"]) == 1
    assert "backup file not found" in capsys.readouterr().err


# ---- the settings page -----------------------------------------------------------------------------------------


def test_backup_routes_run_and_check(desk_env, store_url):
    _fill(store_url)
    app = create_app(store_url)
    local = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    st = local.get("/api/v1/backup/status").json()
    assert st["enabled"] and st["backups"] == [] and st["stale"] and st["tools"] and st["keep"] == 14

    def wait() -> dict:
        for _ in range(600):
            j = local.get("/api/v1/backup/status").json()["job"]
            if j and j["state"] != "running":
                return j
            time.sleep(0.05)
        raise AssertionError("job did not finish")

    r = local.post("/api/v1/backup/run", json={})
    assert r.status_code == 202, r.text
    job = wait()
    assert job["state"] == "done" and job["result"]["file"].endswith("-manual.dump")
    st = local.get("/api/v1/backup/status").json()
    assert len(st["backups"]) == 1 and not st["stale"] and st["last"]["ok"]
    r = local.post("/api/v1/backup/check", json={})
    assert r.status_code == 202, r.text
    job = wait()
    assert job["state"] == "done" and job["result"]["ok"] and job["file"] == st["backups"][0]["name"]
    assert local.get("/api/v1/backup/status").json()["last_check"]["ok"]
    assert local.post("/api/v1/backup/check", json={"file": "../../etc/passwd"}).status_code == 422
    assert local.post("/api/v1/backup/check", json={"file": "desk-20000101-000000-manual.dump"}).status_code == 404
    with store.connect(store_url) as con:
        rows = con.execute("SELECT action, ok FROM audit_log WHERE action LIKE 'backup%' ORDER BY audit_id").fetchall()
    assert rows == [("backup_run", True), ("backup_check", True)]
    remote = TestClient(app, base_url="http://127.0.0.1:8000", client=("10.1.2.3", 50000))
    assert remote.get("/api/v1/backup/status").status_code == 403
    assert remote.post("/api/v1/backup/run", json={}).status_code == 403


def test_settings_page_hosts_the_backup_section():
    html = (PROTO / "settings.html").read_text(encoding="utf-8")
    assert 'href="#backup" data-sec="backup"' in html and 'id="sec-backup" data-sec="backup"' in html
    assert '<script src="desk-backup.js"></script>' in html
    js = (PROTO / "desk-backup.js").read_text(encoding="utf-8")
    for url in ("/api/v1/backup/status", "/api/v1/backup/run", "/api/v1/backup/check", "/api/v1/settings"):
        assert url in js
    for key in ("backup_dir", "backup_keep", "backup_time"):
        assert key in js
    assert "restore" in js and "--replace" in js  # the live restore is shown as the command, not a button
    if shutil.which("node"):
        r = subprocess.run(["node", "--check", str(PROTO / "desk-backup.js")], capture_output=True, text=True,
                           check=False)
        assert r.returncode == 0, r.stderr


def test_diagnostics_section_has_no_paths(tmp_path):
    s = _settings(tmp_path)
    _touch(s.backup_dir, datetime(2026, 10, 23, 20, 30))
    doc = backup.diagnostics(s, now=datetime(2026, 10, 23, 21, 30))
    assert doc["count"] == 1 and doc["newest_age_hours"] == 1.0 and doc["dir_set"] is True
    assert str(tmp_path) not in json.dumps(doc) and "desk-2026" not in json.dumps(doc)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_tools_come_from_the_embedded_postgres(tmp_path):
    assert backup.hold_serve_lock(tmp_path) and not backup._lock(tmp_path, backup.SERVE_LOCK).acquire()
    assert backup.tool("pg_dump").parent.name == "bin" and os.access(backup.tool("pg_restore"), os.X_OK)
