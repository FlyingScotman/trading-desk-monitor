"""The embedded PostgreSQL after a crash (TDM#518): a killed server, a ``postmaster.pid`` left behind (of a gone process, of
a pid another program got), a start that pgserver cuts off at 10 s while the postmaster recovers, a failed start. Real
clusters in a temporary folder (pgserver, as the dev store); the injected part is only the timeout itself."""

from __future__ import annotations

import atexit
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import psycopg
import pytest
from pgserver import postgres_server as ps

from desk.settings import SettingsError
from desk.store import embedded

pytestmark = pytest.mark.filterwarnings("ignore")


def _kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = [*parent.children(recursive=True), parent]
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=10)


def crash(pgdata: Path) -> int:
    """kill -9 of the postmaster and its backends: ``postmaster.pid`` stays, as after a power cut."""
    pid = embedded.read_pidfile(pgdata).pid
    _kill_tree(pid)
    assert (pgdata / "postmaster.pid").is_file()
    return pid


def new_process(pgdata: Path) -> None:
    """The next ``get_server`` is a new process's: the handle of this one is dropped (and its exit hook with it)."""
    server = ps.PostgresServer._instances.pop(pgdata.resolve(), None)
    if server is not None:
        atexit.unregister(server._cleanup)


@pytest.fixture
def pgdata(tmp_path: Path) -> Iterator[Path]:
    d = tmp_path / "pg"
    new_process(d)
    yield d
    info = embedded.read_pidfile(d) if d.exists() else None
    if info is not None:
        _kill_tree(info.pid)
    new_process(d)


def _url(server) -> str:
    return server.get_uri("postgres")


def _value(server, sql: str):
    with psycopg.connect(_url(server), autocommit=True, connect_timeout=5) as con:
        return con.execute(sql).fetchone()[0]


def test_a_new_cluster_starts_and_its_log_is_beside_the_data_directory(pgdata: Path) -> None:
    said: list[str] = []
    server = embedded.get_server(pgdata, said.append)
    assert _value(server, "SELECT 1") == 1 and said == []
    log = pgdata.parent / "logs" / "postgres.log"
    assert log.is_file() and log.stat().st_size > 0
    assert not (pgdata / "log").exists()  # the data directory holds no log the recovery would have to walk past
    assert embedded.get_server(pgdata, said.append) is server  # a second call in the process: the same handle


def test_server_killed_without_a_checkpoint_recovers_and_keeps_its_data(pgdata: Path) -> None:
    said: list[str] = []
    server = embedded.get_server(pgdata, said.append)
    with psycopg.connect(_url(server), autocommit=True) as con:
        con.execute("CREATE TABLE t518 (n int)")
        con.execute("INSERT INTO t518 VALUES (1), (2), (3)")
    new_process(pgdata)  # a new process: no handle of the old one
    dead = crash(pgdata)
    server = embedded.get_server(pgdata, said.append)
    assert _value(server, "SELECT sum(n) FROM t518") == 6
    assert embedded.read_pidfile(pgdata).pid != dead and embedded.read_pidfile(pgdata).status == "ready"
    assert any("postmaster.pid" in m and "убрал" in m for m in said), said  # a gone process: its file was removed


def test_pid_file_of_another_program_is_not_a_running_server(pgdata: Path) -> None:
    """The pid in a left-over ``postmaster.pid`` went to another program (here: this process): pgserver would take the
    server for running and hand out a dead address."""
    server = embedded.get_server(pgdata, lambda m: None)
    uri_before = _url(server)
    new_process(pgdata)
    crash(pgdata)
    lines = (pgdata / "postmaster.pid").read_text(encoding="utf-8").splitlines()
    lines[0] = str(os.getpid())
    (pgdata / "postmaster.pid").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert embedded.is_postgres(os.getpid(), pgdata) is False
    said: list[str] = []
    server = embedded.get_server(pgdata, said.append)
    assert _value(server, "SELECT 1") == 1 and embedded.read_pidfile(pgdata).pid != os.getpid()
    assert any("убрал" in m for m in said) and uri_before


def test_a_live_postmaster_file_is_never_removed(pgdata: Path) -> None:
    server = embedded.get_server(pgdata, lambda m: None)
    said: list[str] = []
    assert embedded.heal(pgdata, said.append) is False and said == []
    assert (pgdata / "postmaster.pid").is_file() and _value(server, "SELECT 1") == 1
    assert embedded.is_postgres(embedded.read_pidfile(pgdata).pid, pgdata) is True


def test_empty_pid_file_is_removed_once_it_is_not_new(pgdata: Path) -> None:
    pgdata.mkdir()
    (pgdata / "postmaster.pid").write_text("", encoding="utf-8")
    assert embedded.heal(pgdata, lambda m: None) is False  # just written: a postmaster may be writing it
    old = time.time() - 60
    os.utime(pgdata / "postmaster.pid", (old, old))
    assert embedded.heal(pgdata, lambda m: None) is True and not (pgdata / "postmaster.pid").exists()


def test_start_cut_off_at_ten_seconds_waits_for_the_recovery_and_says_so(pgdata: Path, monkeypatch) -> None:
    """pgserver's ``pg_ctl -w start`` times out while the postmaster recovers in the background (the issue): no trace,
    a line saying what goes on, then the server that is up is taken. The timeout is injected after a real start."""
    first = embedded.get_server(pgdata, lambda m: None)
    with psycopg.connect(_url(first), autocommit=True) as con:
        con.execute("CREATE TABLE t518 (n int)")
        con.execute("INSERT INTO t518 VALUES (41)")
    new_process(pgdata)
    crash(pgdata)
    real, calls = ps.pg_ctl, []

    def cut_off(args, pgdata=None, **kw):
        if args and args[-1] == "start":
            calls.append(kw["timeout"])
            real([a for a in args if a != "-w"], pgdata=pgdata, **{k: v for k, v in kw.items() if k != "timeout"})
            raise subprocess.TimeoutExpired(["pg_ctl", "start"], kw["timeout"])
        return real(args, pgdata=pgdata, **kw)

    monkeypatch.setattr(ps, "pg_ctl", cut_off)
    monkeypatch.setattr(embedded, "HEARTBEAT_S", 0.2)
    monkeypatch.setattr(embedded, "POLL_S", 0.05)
    said: list[str] = []
    server = embedded.get_server(pgdata, said.append)
    assert calls == [embedded.START_TIMEOUT_S] and ps.pg_ctl is cut_off  # the wrapper is put back
    assert _value(server, "SELECT n FROM t518") == 41
    assert any("восстанавливается после аварийной остановки" in m and "Не закрывайте это окно" in m for m in said), said
    assert any(m.startswith("PostgreSQL готов") for m in said), said


def test_postmaster_still_recovering_from_an_earlier_try_is_waited_for_not_started_again(pgdata: Path, monkeypatch) -> None:
    server = embedded.get_server(pgdata, lambda m: None)
    pid = embedded.read_pidfile(pgdata).pid
    new_process(pgdata)
    # the file as a recovering postmaster writes it: the process is alive, the status is "starting" until it is ready
    text = (pgdata / "postmaster.pid").read_text(encoding="utf-8")
    starting = text.replace("ready", "starting")
    (pgdata / "postmaster.pid").write_text(starting, encoding="utf-8")
    starts: list[list[str]] = []
    real = ps.pg_ctl
    monkeypatch.setattr(ps, "pg_ctl", lambda args, pgdata=None, **kw: starts.append(args) or real(args, pgdata=pgdata, **kw))
    monkeypatch.setattr(embedded, "POLL_S", 0.05)
    monkeypatch.setattr(embedded, "HEARTBEAT_S", 0.1)
    said: list[str] = []
    done = []

    import threading

    t = threading.Thread(target=lambda: done.append(embedded.get_server(pgdata, said.append)))
    t.start()
    time.sleep(0.6)
    assert not done, "it must wait while the postmaster says «starting»"
    (pgdata / "postmaster.pid").write_text(text, encoding="utf-8")  # recovery is over
    t.join(30)
    assert done and _value(done[0], "SELECT 1") == 1 and embedded.read_pidfile(pgdata).pid == pid
    assert starts == []  # no second start of a server that is up
    assert any("ещё восстанавливается" in m for m in said) and server is not None


def test_postmaster_that_dies_while_recovering_is_reported_with_its_log(pgdata: Path, monkeypatch) -> None:
    embedded.get_server(pgdata, lambda m: None)
    pid = embedded.read_pidfile(pgdata).pid
    new_process(pgdata)
    (pgdata / "postmaster.pid").write_text((pgdata / "postmaster.pid").read_text(encoding="utf-8").replace("ready", "starting"), encoding="utf-8")
    log = pgdata.parent / "logs" / "postgres.log"
    monkeypatch.setattr(embedded, "POLL_S", 0.05)
    said: list[str] = []
    import threading

    def die() -> None:
        _kill_tree(pid)
        # written once the postmaster is dead: on Windows a live one holds its log open and refuses the write
        with log.open("a", encoding="utf-8") as f:
            f.write("FATAL:  could not write to file: No space left on device (invented line)\n")

    threading.Timer(0.3, die).start()
    with pytest.raises(embedded.StoreStartError) as e:
        embedded.get_server(pgdata, said.append)
    msg = str(e.value)
    assert isinstance(e.value, SettingsError) and "остановился во время запуска или восстановления" in msg
    assert "No space left on device (invented line)" in msg and str(log) in msg and "start.bat" in msg


def test_wait_gives_up_with_a_message_when_the_postmaster_stays_busy(pgdata: Path, monkeypatch) -> None:
    embedded.get_server(pgdata, lambda m: None)
    (pgdata / "postmaster.pid").write_text((pgdata / "postmaster.pid").read_text(encoding="utf-8").replace("ready", "starting"), encoding="utf-8")
    monkeypatch.setattr(embedded, "POLL_S", 0.05)
    with pytest.raises(embedded.StoreStartError, match="всё ещё не готов.*продолжает восстановление.*Ctrl\\+C"):
        embedded.wait_ready(pgdata, lambda m: None, wait_s=0.3)
    (pgdata / "postmaster.pid").write_text((pgdata / "postmaster.pid").read_text(encoding="utf-8").replace("starting", "ready"), encoding="utf-8")


def test_failed_start_on_a_taken_port_is_tried_again_then_reported(pgdata: Path, monkeypatch) -> None:
    real, calls = ps.pg_ctl, []

    def taken(args, pgdata=None, **kw):
        if args and args[-1] == "start":
            calls.append(1)
            if len(calls) < 3:
                log = Path(args[args.index("-l") + 1])
                log.write_text("FATAL:  could not create listen socket for \"127.0.0.1\"\n"
                               "LOG:  could not bind IPv4 address: Address already in use\n", encoding="utf-8")
                raise subprocess.CalledProcessError(1, ["pg_ctl"])
        return real(args, pgdata=pgdata, **kw)

    monkeypatch.setattr(ps, "pg_ctl", taken)
    monkeypatch.setattr(embedded.time, "sleep", lambda s: None)
    said: list[str] = []
    server = embedded.get_server(pgdata, said.append)
    assert _value(server, "SELECT 1") == 1 and len(calls) == 3
    assert sum("повторяю" in m for m in said) == 2

    # a failure that another try does not cure: no trace, the log's tail, what to do
    _kill_tree(embedded.read_pidfile(pgdata).pid)
    new_process(pgdata)
    (pgdata / "postmaster.pid").unlink()

    def broken(args, pgdata=None, **kw):
        if args and args[-1] == "start":
            Path(args[args.index("-l") + 1]).write_text("FATAL:  data directory has wrong ownership (invented)\n",
                                                        encoding="utf-8")
            raise subprocess.CalledProcessError(1, ["pg_ctl"])
        return real(args, pgdata=pgdata, **kw)

    monkeypatch.setattr(ps, "pg_ctl", broken)
    with pytest.raises(embedded.StoreStartError) as e:
        embedded.get_server(pgdata, lambda m: None)
    assert "wrong ownership (invented)" in str(e.value) and "stop.bat" in str(e.value)
    assert ps.PostgresServer._instances.get(pgdata.resolve()) is None  # a failed start is not kept as a handle


def test_resolve_url_uses_the_recovering_start(tmp_path: Path, monkeypatch) -> None:
    from desk import store
    from desk.settings import load

    monkeypatch.setenv("DESK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("DESK_STORE_URL", raising=False)
    st = load()
    seen: list[Path] = []

    class Fake:
        def psql(self, sql: str) -> str:
            return "1 row"

        def get_uri(self, db: str) -> str:
            return f"postgresql://fake/{db}"

    monkeypatch.setattr(embedded, "get_server", lambda p, *a, **k: seen.append(p) or Fake())
    assert store.resolve_url(st) == "postgresql://fake/desk" and seen == [st.pg_dir]


def test_log_is_rotated_when_big_and_no_postmaster_is_up(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(embedded, "LOG_MAX_BYTES", 10)
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "postgres.log").write_text("x" * 100, encoding="utf-8")
    assert embedded.rotate_log(tmp_path / "pg") is True
    assert not (folder / "postgres.log").exists() and (folder / "postgres.log.1").read_text(encoding="utf-8") == "x" * 100
    assert embedded.log_path(tmp_path / "pg") == folder / "postgres.log"


def test_log_of_a_live_postmaster_is_not_rotated(pgdata: Path, monkeypatch) -> None:
    """A live server holds the log open: Windows refuses the rename, Linux would let it write into the renamed file."""
    server = embedded.get_server(pgdata, lambda m: None)
    log = pgdata.parent / "logs" / "postgres.log"
    monkeypatch.setattr(embedded, "LOG_MAX_BYTES", 10)
    assert log.stat().st_size > 10
    assert embedded.rotate_log(pgdata) is False and log.is_file() and not log.with_name("postgres.log.1").exists()
    new_process(pgdata)
    again = embedded.get_server(pgdata, lambda m: None)  # the live server is taken, nothing is rotated
    assert _value(again, "SELECT 1") == 1 and _url(again) == _url(server)
    assert log.is_file() and not log.with_name("postgres.log.1").exists()


def test_pid_of_a_postgres_of_another_cluster_is_not_this_clusters_postmaster(pgdata: Path, tmp_path: Path) -> None:
    """The pid in a left-over ``postmaster.pid`` went to the postmaster of ANOTHER cluster (the name is postgres, only
    the data directory and the start time tell): the file is stale, and it is removed, the other cluster is left alone."""
    other = tmp_path / "other_home" / "pg"  # its own folder: the log (logs\\postgres.log) is one per data directory's parent
    other.parent.mkdir()
    new_process(other)
    other_server = embedded.get_server(other, lambda m: None)
    try:
        embedded.get_server(pgdata, lambda m: None)
        new_process(pgdata)
        crash(pgdata)
        other_pid = embedded.read_pidfile(other).pid
        lines = (pgdata / "postmaster.pid").read_text(encoding="utf-8").splitlines()
        lines[0] = str(other_pid)
        lines[2] = str(int(time.time()) - 3600)  # the crash was an hour ago, the pid went to the other cluster since
        (pgdata / "postmaster.pid").write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert embedded.is_postgres(other_pid, pgdata) is False
        assert embedded.is_postgres(other_pid, other) is True
        said: list[str] = []
        server = embedded.get_server(pgdata, said.append)
        assert _value(server, "SELECT 1") == 1 and embedded.read_pidfile(pgdata).pid != other_pid
        assert any("убрал" in m for m in said) and _value(other_server, "SELECT 1") == 1
    finally:
        _kill_tree(embedded.read_pidfile(other).pid)
        new_process(other)


def test_heal_does_not_remove_a_file_that_a_starting_postmaster_wrote_meanwhile(pgdata: Path, monkeypatch) -> None:
    """The reading said «stale», then another process's start wrote its fresh file: the second reading stops the removal."""
    embedded.get_server(pgdata, lambda m: None)
    new_process(pgdata)
    dead = crash(pgdata)
    fresh = (pgdata / "postmaster.pid").read_text(encoding="utf-8").replace(str(dead), str(os.getpid() + 1_000_000), 1)
    real = embedded.is_postgres

    def late(pid: int, d: Path) -> bool | None:
        answer = real(pid, d)
        (pgdata / "postmaster.pid").write_text(fresh, encoding="utf-8")  # the other start writes between the check and the removal
        return answer

    monkeypatch.setattr(embedded, "is_postgres", late)
    said: list[str] = []
    assert embedded.heal(pgdata, said.append) is False and said == []
    assert (pgdata / "postmaster.pid").read_text(encoding="utf-8") == fresh


def test_desk_db_migrate_after_a_killed_server_needs_no_manual_step(tmp_path: Path, monkeypatch, capsys) -> None:
    """The owner's path (start.bat -> desk db-migrate): the server was killed, ``postmaster.pid`` is left, the next
    ``desk db-migrate`` reaches a migrated store by itself."""
    from desk import cli

    home = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(home))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    pg = (home / "pg").resolve()
    try:
        assert cli.main(["db-migrate"]) == 0
        new_process(pg)
        crash(pg)
        capsys.readouterr()
        assert cli.main(["db-migrate"]) == 0  # nothing to apply, the store is reached
        err = capsys.readouterr().err
        assert "Traceback" not in err
        server = embedded.get_server(pg, lambda m: None)
        with psycopg.connect(server.get_uri("desk"), autocommit=True) as con:
            assert con.execute("SELECT count(*) FROM schema_migration").fetchone()[0] > 0
    finally:
        info = embedded.read_pidfile(pg)
        if info is not None:
            _kill_tree(info.pid)
        new_process(pg)
