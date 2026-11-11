"""The embedded PostgreSQL's start that survives a crash (TDM#518).

``pgserver`` starts the server with ``pg_ctl -w start`` and gives it 10 seconds. After an emergency stop (the window of
``start.bat`` closed, Ctrl+C, a killed process, a power cut) the next start first recovers the data directory: on the
work machine 40+ seconds. ``pg_ctl`` is cut off at 10 s (``subprocess.TimeoutExpired``), the postmaster goes on in the
background, and the owner, seeing a trace, pressed Ctrl+C: the recovery was killed with the console and the next start
began it again.

``get_server`` is ``pgserver.get_server`` with what a crash needs:

* a ``postmaster.pid`` of a process that is gone, or of one that is not this cluster's postgres (the pid was reused),
  is removed before the start (pgserver would take the second for a running server);
* a postmaster that is up but not ``ready`` (recovering, from an earlier try that timed out) is waited for, up to
  ``RECOVERY_WAIT_S``, with a line on the console every ``HEARTBEAT_S`` saying what is going on, never a trace; a
  postmaster that dies meanwhile is reported with the tail of its log;
* ``TimeoutExpired`` of pgserver's start is not an error: the same wait, then the server that is up is taken;
* ``CalledProcessError`` (``pg_ctl`` failed: a port taken between the choice and the bind, a lock) is tried again, then
  reported with the tail of the log, as ``StoreStartError`` (a ``SettingsError``: ``desk`` prints it, no trace);
* the server's log is ``<data dir>/../logs/postgres.log``, outside the data directory: on Windows the recovery walks
  the whole data directory, and a log it writes into itself cannot be opened by that walk (``sharing violation``, up to
  30 s more);
* on Windows ``pg_ctl`` runs in a console and a process group of its own, so Ctrl+C and the closed window of
  ``start.bat`` do not reach the database. Not provable here (no Windows); the launcher still says "stop.bat".

pgserver stays the one that starts, stops nothing (``cleanup_mode=None``: the server outlives the process, so later
commands share it) and keeps its own handle bookkeeping.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..settings import SettingsError

START_TIMEOUT_S = 10.0
"""What pgserver gives ``pg_ctl -w start`` (its own constant); tests lower it."""
RECOVERY_WAIT_S = 900.0
"""How long a postmaster that is not ready yet is waited for: recovery of a big data directory on a slow disk."""
HEARTBEAT_S = 15.0
POLL_S = 1.0
TRIES = 3
LOG_MAX_BYTES = 20_000_000
LOG_TAIL_LINES = 25
PID_LINES = 8  # pid, data dir, start time, port, socket dir, host, shared memory, status
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
TRANSIENT = ("could not bind", "address already in use", "only one usage of each socket address",
             "could not create lock file", "could not create listen socket")
"""Parts of a failed start's log that a second try (another port) can cure."""

_lock = threading.Lock()


class StoreStartError(SettingsError):
    """The embedded PostgreSQL did not start, in the owner's words (``desk`` prints it, the launcher shows it)."""


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Pid:
    pid: int
    status: str
    """``ready``, ``starting``, ``stopping``, ``standby``; empty while the postmaster is still writing the file."""
    complete: bool


def read_pidfile(pgdata: Path) -> Pid | None:
    """``postmaster.pid`` as postgres writes it, or None: no file, or one with no pid in it."""
    try:
        lines = (pgdata / "postmaster.pid").read_text(encoding="utf-8", errors="replace").splitlines()
        pid = int(lines[0].strip())
    except (OSError, IndexError, ValueError):
        return None
    status = lines[PID_LINES - 1].strip() if len(lines) >= PID_LINES else ""
    return Pid(pid, status, len(lines) >= PID_LINES)


def is_postgres(pid: int, pgdata: Path) -> bool | None:
    """Is process ``pid`` a postgres of this data directory: True, False (gone, or another program that got the pid),
    None (cannot tell: no psutil, access denied, a postgres whose folder is not this one by name)."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        p = psutil.Process(pid)
        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
            return False
        if not p.name().lower().startswith("postgres"):
            return False
        try:
            cwd = p.cwd()  # a postmaster works in its data directory
        except (psutil.AccessDenied, OSError):
            return True
        # the same folder under another spelling (case, 8.3 names, a link) is not proof of another cluster: only "cannot
        # tell" then, and a file of a live postmaster is never removed on that
        same = os.path.normcase(os.path.realpath(cwd)) == os.path.normcase(os.path.realpath(pgdata))
        return True if same else None
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return None


def log_path(pgdata: Path) -> Path:
    """The server's log, beside the data directory (module doc), rotated when big."""
    folder = pgdata.parent / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "postgres.log"
    try:
        if log.stat().st_size > LOG_MAX_BYTES:
            log.replace(log.with_name(log.name + ".1"))
    except OSError:
        pass
    _own_for_pgserver(folder, log)
    return log


def _own_for_pgserver(*paths: Path) -> None:
    """pgserver run as root (a container, CI) starts postgres as its own user ``pgserver``, which must be able to write
    the log; as any other user, and on Windows, nothing to do."""
    if sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        import pwd

        u = pwd.getpwnam("pgserver")
    except (ImportError, KeyError):
        return
    for p in paths:
        with contextlib.suppress(OSError):
            os.chown(p, u.pw_uid, u.pw_gid)


def log_tail(log: Path, lines: int = LOG_TAIL_LINES) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def heal(pgdata: Path, out: Callable[[str], None] = say) -> bool:
    """Remove a ``postmaster.pid`` that names no postgres of this data directory (the process is gone, or the pid went
    to another program): True when removed. A live postmaster's file is never touched."""
    pf = pgdata / "postmaster.pid"
    info = read_pidfile(pgdata)
    if info is None and not pf.exists():
        return False
    if info is not None and is_postgres(info.pid, pgdata) is not False:
        return False  # alive (or cannot tell): postgres decides
    if info is None:  # an empty or broken file: a postmaster being born writes it at once, so wait a moment
        try:
            if time.time() - pf.stat().st_mtime < 5:
                return False
        except OSError:
            return False
    try:
        pf.unlink()
    except OSError:
        return False
    out("PostgreSQL: найден оставшийся после аварийной остановки файл postmaster.pid (процесса уже нет): убрал его.")
    return True


def _recovering() -> str:
    return ("PostgreSQL восстанавливается после аварийной остановки (его остановили не штатно: закрыли окно, Ctrl+C, "
            "сбой или выключение питания). Это может занять несколько минут. Не закрывайте это окно и не нажимайте "
            "Ctrl+C: восстановление пойдёт заново.")


def wait_ready(pgdata: Path, out: Callable[[str], None] = say, *, wait_s: float | None = None, log: Path | None = None,
               announce: bool = False) -> None:
    """Wait until the postmaster of ``pgdata`` is ``ready``. ``StoreStartError``: it died meanwhile (with the tail of its
    log), or is still not ready after ``wait_s`` (it goes on; the next start waits for it again)."""
    wait_s = RECOVERY_WAIT_S if wait_s is None else wait_s
    log = log or log_path(pgdata)
    t0 = time.monotonic()
    last = t0
    told = False
    if announce:  # the start was cut off: the recovery is slow whatever the file says by now
        out(_recovering())
        told = True
    while True:
        info = read_pidfile(pgdata)
        if info is not None and info.status == "ready" and info.complete and is_postgres(info.pid, pgdata) is not False:
            if told:
                out(f"PostgreSQL готов (восстановление заняло {time.monotonic() - t0:.0f} с).")
            return
        alive = info is not None and is_postgres(info.pid, pgdata) is not False
        if not alive and time.monotonic() - t0 > 3:  # the file may not be written yet in the first moments
            tail = log_tail(log)
            raise StoreStartError(
                "PostgreSQL остановился во время запуска или восстановления."
                + (f" Последние строки его журнала ({log}):\n{tail}\n" if tail else f" Журнал: {log}.\n")
                + "Запустите start.bat ещё раз: если повторится, откройте Настройки -> «Диагностика» или раздел "
                  "«Если что-то не так» в INSTALL_BACKEND_WINDOWS.md.")
        if not told:
            out(_recovering())
            told = True
        now = time.monotonic()
        if now - t0 > wait_s:
            raise StoreStartError(
                f"PostgreSQL всё ещё не готов через {now - t0:.0f} с, но процесс работает: он продолжает "
                f"восстановление. Подождите несколько минут и запустите start.bat ещё раз: он дождётся его, а не "
                f"начнёт заново. Не закрывайте окно и не нажимайте Ctrl+C. Журнал: {log}.")
        if now - last >= HEARTBEAT_S:
            out(f"  PostgreSQL ещё восстанавливается, прошло {now - t0:.0f} с ...")
            last = now
        time.sleep(POLL_S)


def _server_class() -> Any:
    from pgserver.postgres_server import PostgresServer

    class Server(PostgresServer):
        """pgserver's server whose log is outside the data directory (module doc)."""

        def ensure_postgres_running(self) -> None:
            self.log = log_path(self.pgdata)
            super().ensure_postgres_running()

    return Server


@contextlib.contextmanager
def _start_options() -> Iterator[None]:
    """While pgserver starts the server: ``pg_ctl start`` gets ``START_TIMEOUT_S`` and, on Windows, a console and a
    process group of its own. pgserver's module-level ``pg_ctl`` is wrapped for the call and put back."""
    from pgserver import postgres_server as ps

    with _lock:
        orig = ps.pg_ctl

        def pg_ctl(args: list[str], pgdata: Path | None = None, **kw: Any) -> str:
            if args and args[-1] == "start":
                kw["timeout"] = START_TIMEOUT_S
                if sys.platform == "win32":
                    kw["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            return orig(args, pgdata=pgdata, **kw)

        ps.pg_ctl = pg_ctl
        try:
            yield
        finally:
            ps.pg_ctl = orig


def _forget(cls: Any, pgdata: Path) -> None:
    """A server whose start failed is not a handle: pgserver registers it before it starts it."""
    server = cls._instances.pop(pgdata, None)
    if server is not None:
        atexit.unregister(server._cleanup)


def get_server(pgdata: Path | str, out: Callable[[str], None] = say) -> Any:
    """``pgserver.get_server(pgdata, cleanup_mode=None)`` with the recovery of the module doc; ``StoreStartError``."""
    pgdata = Path(pgdata).expanduser().resolve()
    if not pgdata.parent.exists():
        raise FileNotFoundError(f"Parent directory of pgdata does not exist: {pgdata.parent}")
    pgdata.mkdir(exist_ok=True)
    cls = _server_class()
    if pgdata in cls._instances:
        return cls._instances[pgdata]
    failed = unexpected = 0
    while True:
        heal(pgdata, out)
        info = read_pidfile(pgdata)
        if info is not None and is_postgres(info.pid, pgdata) is not False and not (info.status == "ready" and info.complete):
            wait_ready(pgdata, out, announce=True)  # a postmaster of an earlier try, still recovering
        try:
            with _start_options():
                return cls(pgdata, cleanup_mode=None)
        except subprocess.CalledProcessError:
            _forget(cls, pgdata)
            tail = log_tail(log_path(pgdata))
            failed += 1
            if failed < TRIES and any(t in tail.lower() for t in TRANSIENT):
                out(f"PostgreSQL не запустился (занят порт или файл блокировки); повторяю, попытка {failed + 1} из "
                    f"{TRIES} ...")
                time.sleep(2)
                continue
            raise StoreStartError(
                "PostgreSQL не запустился."
                + (f" Последние строки его журнала ({log_path(pgdata)}):\n{tail}\n" if tail else "\n")
                + "Частые причины: антивирус блокирует postgres.exe, оставшийся процесс postgres.exe держит базу "
                  "(Диспетчер задач), программа запущена от имени администратора. Запустите stop.bat и затем "
                  "start.bat ещё раз; подробнее — «Если что-то не так» в INSTALL_BACKEND_WINDOWS.md.") from None
        except Exception as e:  # noqa: BLE001
            # TimeoutExpired: pg_ctl was cut off, the postmaster was not. Also what pgserver raises while reporting it
            # (its log in the ANSI code page that the locale cannot decode, a log not yet there) or when it reads a
            # postmaster.pid that is being written. With a live postmaster: wait for it, then take it
            _forget(cls, pgdata)
            info = read_pidfile(pgdata)
            live = info is not None and is_postgres(info.pid, pgdata) is not False
            unexpected += 1
            if not (live or isinstance(e, subprocess.TimeoutExpired)) or unexpected > TRIES:
                raise StoreStartError(
                    f"PostgreSQL не запустился ({type(e).__name__}): файлы базы в {pgdata} недоступны или "
                    "повреждены, либо запуск прерван. Запустите stop.bat и затем start.bat ещё раз; журнал "
                    f"PostgreSQL: {log_path(pgdata)}.") from None
            wait_ready(pgdata, out, announce=isinstance(e, subprocess.TimeoutExpired))
