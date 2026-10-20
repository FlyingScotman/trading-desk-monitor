"""Desk Monitor launcher of the Windows bundle: stdlib only, run by the bundled Python (``-I``).

    python\\python.exe -I app\\deskctl.py start [--port N]  install if needed, desk serve, open the browser
    python\\python.exe -I app\\deskctl.py stop               stop this bundle's desk processes and the embedded store
    python\\python.exe -I app\\deskctl.py cycle [args]       install if needed, then desk cycle (hub, inbox, EOD)
    python\\python.exe -I app\\deskctl.py desk [args]        install if needed, then any desk command

The bundle folder (``DeskMonitor\\``, anywhere): ``python\\`` the runtime, ``wheelhouse\\`` every wheel and
``requirements.txt``, ``app\\`` this file, the prototypes and the built frontend, ``adapter\\`` the private adapter
when the build had one, ``VERSION``. The venv ``env\\`` is made from the runtime and the wheelhouse, offline,
whenever ``env\\desk-bundle.json`` does not name this version and this folder (a venv cannot move). Data and
settings live in DESK_HOME (default ``%LOCALAPPDATA%\\DeskMonitor``, or ``C:\\DeskMonitorData`` when that path is not
plain ASCII, see ``default_home``), outside the bundle, so they survive updates.
The .bat files only find a Python and call this; the logic is here, away from cmd.exe quoting.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
WHEELHOUSE = ROOT / "wheelhouse"
ADAPTER = ROOT / "adapter"
ENV = ROOT / "env"
WIN = os.name == "nt"  # elsewhere only to test the launcher
SCRIPTS = ENV / ("Scripts" if WIN else "bin")
EXE = ".exe" if WIN else ""
MARKER = ENV / "desk-bundle.json"
# exit codes of a server that was stopped, not failed: Ctrl+C (ours, STATUS_CONTROL_C_EXIT), stop.bat (15)
STOPPED = (0, 130, 15, -15, 0xC000013A, -1073741510)
LOG_MAX_BYTES = 20_000_000
DEFAULT_PORT = 8000
# pgserver gives pg_ctl 10 s to start PostgreSQL; on the first start an antivirus scanning postgres.exe can take
# longer, and the next try usually finds the files already checked
MIGRATE_TRIES = 3
MIGRATE_PAUSE_S = 5.0


def say(msg: str = "") -> None:
    print(msg, flush=True)


class Fail(Exception):
    """A message for the person at the console; the .bat pauses on it."""


def version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def adapter_package() -> Path | None:
    """The private adapter's package folder in ``adapter\\`` (the build puts exactly one there), if any."""
    if not ADAPTER.is_dir():
        return None
    pkgs = sorted(d for d in ADAPTER.iterdir() if d.is_dir() and (d / "__init__.py").is_file())
    return pkgs[0] if pkgs else None


def default_home(env: dict[str, str]) -> Path:
    """``%LOCALAPPDATA%\\DeskMonitor``; ``C:\\DeskMonitorData`` instead when that path is not plain ASCII (a Cyrillic
    user name). PostgreSQL writes its paths in the ANSI code page (``postmaster.pid``, initdb's output) and pgserver
    reads them as UTF-8 (PYTHONUTF8), so the embedded store cannot live under a non-ASCII path."""
    local = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    home = Path(local) / "DeskMonitor"
    if WIN and not str(home).isascii():
        return Path((env.get("SystemDrive") or "C:") + "\\") / "DeskMonitorData"
    return home


def desk_environ() -> dict[str, str]:
    """The environment of every desk process: defaults only where the variable is not already set."""
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONPATH"):  # a machine-wide Python must not leak into the bundle's
        env.pop(k, None)
    for k in [k for k in env if k.upper().startswith("PIP_")]:  # nor this machine's pip settings (PIP_USER, ...)
        env.pop(k)
    env.setdefault("DESK_HOME", str(default_home(env)))
    home = Path(env["DESK_HOME"])
    env["DESK_BUNDLE_VERSION"] = version()  # in each run's lineage: a new bundle recomputes the latest date
    env.setdefault("DESK_STATIC_ROOT", str(APP))  # prototypes\ and frontend\dist\ that desk serve serves
    pkg = adapter_package()
    if pkg is not None:  # a fallback: DESK_PRIVATE_ADAPTER and settings.json private_adapter win over it
        env.setdefault("DESK_DEFAULT_PRIVATE_ADAPTER", str(pkg))
    if (home / "desk.env").is_file():
        env.setdefault("DESK_ENV_FILE", str(home / "desk.env"))
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_CONFIG_FILE"] = os.devnull  # no pip.ini of this machine: the install is offline
    env["PIP_NO_INPUT"] = "1"
    return env


def home_dir(env: dict[str, str]) -> Path:
    home = Path(env["DESK_HOME"])
    fresh = not home.exists()
    try:
        (home / "logs").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Fail(f"Не удалось создать папку данных {home}: {e}\nЗадайте другую (латиницей): в командной строке  "
                   "setx DESK_HOME D:\\DeskData  , затем снова start.bat.") from e
    if fresh and WIN and not _under(str(home), Path(env.get("USERPROFILE") or Path.home())):
        _private(home)  # e.g. C:\DeskMonitorData: it would inherit the drive root's "Authenticated Users: Modify"
    return home


def _private(folder: Path) -> None:
    """Best effort: only this user, SYSTEM and Administrators may open ``folder`` (it holds the hub token)."""
    try:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, timeout=30,
                             check=False).stdout.decode("latin-1")  # the OEM code page: only the SID matters
        sid = re.search(r"S-1-[0-9-]+", out)
        if sid and not subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r",
                                       f"*{sid.group(0)}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
                                      capture_output=True, timeout=60, check=False).returncode:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    say(f"  ВНИМАНИЕ: не удалось закрыть папку {folder} от других пользователей этого компьютера (в ней токен хаба).")


def check_paths(env: dict[str, str]) -> None:
    """The embedded PostgreSQL (pgserver) on Windows needs plain paths: its ``psql`` runs through cmd.exe unquoted
    (a space in the program folder breaks it), and it decodes PostgreSQL's ANSI output as UTF-8 (non-ASCII folders)."""
    if not WIN or env.get("DESK_STORE_URL"):
        return
    if " " in str(ROOT) or not str(ROOT).isascii():
        raise Fail(f"В пути к папке программы есть пробел или не латинские буквы: {ROOT}\n"
                   "Встроенный PostgreSQL в такой папке не работает. Перенесите папку целиком (вместе с python\\, "
                   "wheelhouse\\, app\\) в короткий путь латиницей без пробелов, например C:\\DeskMonitor, "
                   "и запустите start.bat оттуда.")
    if not env["DESK_HOME"].isascii():
        raise Fail(f"В пути к папке данных (DESK_HOME) есть не латинские буквы: {env['DESK_HOME']}\n"
                   "Встроенный PostgreSQL там не работает. Задайте другую: в командной строке  "
                   "setx DESK_HOME C:\\DeskMonitorData  , закройте это окно и снова start.bat.")


def desk_exe() -> Path:
    return SCRIPTS / f"desk{EXE}"


def pg_ctl() -> Path | None:
    """pg_ctl of this venv's pgserver, if installed."""
    found = sorted(ENV.glob(f"[Ll]ib/**/site-packages/pgserver/pginstall/bin/pg_ctl{EXE}"))
    return found[0] if found else None


# ---- processes of this bundle (Windows only; ctypes, no PowerShell: it is often locked down)

def _processes() -> dict[int, str]:
    """pid -> image path of every process this user may query."""
    if not WIN:  # tests: a venv's python is a symlink out of it, so take the script (argv[1]) when there is one
        out = {}
        for d in Path("/proc").glob("[0-9]*"):
            try:
                argv = (d / "cmdline").read_bytes().split(b"\0")
                script = argv[1].decode() if len(argv) > 1 and argv[1].startswith(b"/") else ""
                out[int(d.name)] = script or os.readlink(d / "exe")
            except OSError:
                pass
        return out
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    n = 4096
    while True:
        pids = (wintypes.DWORD * n)()
        got = wintypes.DWORD()
        if not k32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(got)):
            return {}
        if got.value < ctypes.sizeof(pids):
            break
        n *= 2
    out: dict[int, str] = {}
    for pid in pids[: got.value // ctypes.sizeof(wintypes.DWORD)]:
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                out[int(pid)] = buf.value
        finally:
            k32.CloseHandle(h)
    return out


def _under(path: str, folder: Path) -> bool:
    p, f = os.path.normcase(os.path.abspath(path)), os.path.normcase(str(folder))
    return p.startswith(f.rstrip("\\/") + os.sep)


def bundle_processes() -> tuple[list[int], list[int]]:
    """(desk/python pids, postgres pids) running from this bundle's runtime or venv, this process excluded."""
    desk, pg = [], []
    for pid, image in _processes().items():
        if pid == os.getpid() or not (_under(image, ENV) or _under(image, ROOT / "python")):
            continue
        name = os.path.basename(image).lower()
        if name in (f"postgres{EXE}", f"pg_ctl{EXE}"):
            pg.append(pid)
        else:
            desk.append(pid)
    return desk, pg


def _kill(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 15)  # TerminateProcess on Windows
        except OSError:
            pass


def stop_store(env: dict[str, str]) -> None:
    """Stop the embedded PostgreSQL clusters served by this venv's pgserver (pg_ctl, then by force)."""
    ctl = pg_ctl()
    home = Path(env["DESK_HOME"])
    for data in (home / "pg", ENV / ".devdata" / "desk_pg"):  # DESK_HOME\pg; the pre-DESK_HOME place
        if ctl and (data / "postmaster.pid").is_file():
            say(f"Останавливаю встроенный PostgreSQL ({data}) ...")
            subprocess.run([str(ctl), "stop", "-D", str(data), "-m", "fast", "-w", "-t", "30"], env=env,
                           check=False)
    for _ in range(10):
        _, pg = bundle_processes()
        if not pg:
            return
        time.sleep(1)
    _kill(bundle_processes()[1])


# ---- the venv

def _rmtree(p: Path) -> None:
    def retry(fn, path, _exc):
        os.chmod(path, 0o700)
        fn(path)

    shutil.rmtree(p, onerror=retry)


def apply_patch_delete() -> None:
    """A patch zip (``build_windows_bundle.py --patch-base``) lists in ``PATCH_DELETE.txt`` the files the new version
    no longer has; delete them (inside this folder only), then the list."""
    listing = ROOT / "PATCH_DELETE.txt"
    if not listing.is_file():
        return
    for line in listing.read_text(encoding="utf-8").splitlines():
        parts = line.strip().replace("\\", "/").split("/")
        if not line.strip() or ".." in parts or ":" in line or parts[0] in ("", "env"):
            continue
        try:
            ROOT.joinpath(*parts).unlink(missing_ok=True)
        except OSError:
            pass
    listing.unlink(missing_ok=True)


def ensure_env(env: dict[str, str], *, force: bool = False) -> None:
    """Create ``env\\`` from the bundled runtime and the wheelhouse unless it already belongs to this bundle."""
    apply_patch_delete()
    check_paths(env)
    want = {"version": version(), "root": str(ROOT)}
    try:
        have = json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        have = None
    if have == want and desk_exe().is_file() and not force:
        return
    say(f"Устанавливаю Desk Monitor {want['version']} в {ENV}")
    say("  (один раз после распаковки или обновления, без интернета, 1-3 минуты) ...")
    if ENV.exists():
        desk, _ = bundle_processes()
        if desk:
            raise Fail("Desk Monitor из этой папки ещё работает. Закройте его окно (или запустите stop.bat) "
                       "и запустите снова.")
        stop_store(env)
        legacy = ENV / ".devdata"
        if legacy.is_dir():  # data of a build before DESK_HOME: keep it, never delete
            keep = Path(env["DESK_HOME"]) / f"legacy-devdata-{datetime.now():%Y%m%d-%H%M%S}"
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(keep))
            say(f"  старые данные из env\\.devdata перенесены в {keep}")
        try:
            _rmtree(ENV)
        except OSError as e:
            raise Fail(f"Не удалось удалить старое окружение {ENV}: {e}\n"
                       "Закройте окна Desk Monitor и программы, открывшие файлы в этой папке, и повторите.") from e
    base = Path(sys.executable)
    r = subprocess.run([str(base), "-I", "-m", "venv", str(ENV)], env=env, check=False)
    py = SCRIPTS / f"python{EXE}"
    if r.returncode or not py.is_file():
        raise Fail(f"Не удалось создать окружение Python в {ENV} ({base}).")
    r = subprocess.run([str(py), "-m", "pip", "install", "--no-index", "--find-links",
                        str(WHEELHOUSE), "--no-warn-script-location", "-r", str(WHEELHOUSE / "requirements.txt")],
                       env=env, check=False)
    if r.returncode or not desk_exe().is_file():
        raise Fail("pip не смог установить пакеты из wheelhouse (сообщения выше). Архив распакован целиком?")
    # desk serve finds prototypes\ and frontend\dist\ through DESK_STATIC_ROOT (app\, desk_environ); check
    # that the installed package imports
    r = subprocess.run([str(py), "-c", "import desk.api"], env=env, capture_output=True, text=True, check=False)
    if r.returncode:
        raise Fail(f"Установленный пакет desk не импортируется:\n{r.stderr}")
    MARKER.write_text(json.dumps(want, ensure_ascii=False), encoding="utf-8")
    migrate_store(env)
    say("Установка завершена.")


def migrate_store(env: dict[str, str]) -> None:
    """``desk db-migrate``, up to ``MIGRATE_TRIES`` times: a first start of the embedded PostgreSQL can exceed
    pgserver's 10 s while an antivirus checks its files."""
    say("Создаю или обновляю базу (desk db-migrate); при первом запуске PostgreSQL создаёт кластер, до минуты ...")
    for n in range(1, MIGRATE_TRIES + 1):
        if not subprocess.run([str(desk_exe()), "db-migrate"], env=env, cwd=str(home_dir(env)),
                              check=False).returncode:
            return
        if n < MIGRATE_TRIES:
            say(f"PostgreSQL не успел запуститься (при первом запуске его файлы может проверять антивирус). "
                f"Повтор {n + 1} из {MIGRATE_TRIES} через {MIGRATE_PAUSE_S:g} с ...")
            time.sleep(MIGRATE_PAUSE_S)
    raise Fail(f"desk db-migrate не прошёл за {MIGRATE_TRIES} попытки (сообщения выше). Журнал PostgreSQL: "
               f"{Path(env['DESK_HOME']) / 'pg' / 'log'}; частые причины и решения: "
               "app\\INSTALL_BACKEND_WINDOWS.md, раздел «Если что-то не так». "
               "Часто помогает просто запустить ещё раз.")


# ---- running desk with a log

def _open_log(path: Path):
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    return open(path, "ab")


def run_logged(cmd: list[str], env: dict[str, str], log: Path, on_start=None) -> int:
    """Run ``cmd``; its output goes to the console and to ``log``. Ctrl+C reaches it too (same console)."""
    with _open_log(log) as f:
        f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} {version()} {ROOT}: {' '.join(cmd[1:])}\n".encode())
        f.flush()
        proc = subprocess.Popen(cmd, env=env, cwd=env["DESK_HOME"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        if on_start:
            threading.Thread(target=on_start, args=(proc,), daemon=True).start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                f.write(line)
                f.flush()
                sys.stdout.write(line.decode("utf-8", "replace"))
                sys.stdout.flush()
            return proc.wait()
        except KeyboardInterrupt:
            try:
                return proc.wait(15)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                proc.kill()
                return 130


def _no_proxy_get(url: str, timeout: float = 2.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via the corporate proxy
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.status == 200
    except (OSError, http.client.HTTPException):  # URLError, timeouts, resets are OSError
        return False


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return True
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def cmd_start(args: list[str]) -> int:
    port = int(os.environ.get("DESK_PORT") or DEFAULT_PORT)
    if args[:1] == ["--port"] and len(args) > 1:
        port = int(args[1])
    env = desk_environ()
    home = home_dir(env)
    say(f"Desk Monitor {version()}")
    say(f"  программа: {ROOT}")
    say(f"  данные и настройки (DESK_HOME): {home}")
    ensure_env(env)
    page = "desk-monitor.html" if (home / "settings.json").is_file() else "settings.html"
    url = f"http://127.0.0.1:{port}/{page}"
    if port_busy(port):
        ours = bool(bundle_processes()[0])
        if _no_proxy_get(f"http://127.0.0.1:{port}/health/live") and ours:
            say(f"Desk Monitor уже запущен: открываю {url}")
            webbrowser.open(url)
            return 0
        if _no_proxy_get(f"http://127.0.0.1:{port}/health/live"):
            raise Fail(f"Порт {port} занят другим экземпляром Desk Monitor (старая версия из другой папки?). "
                       "Закройте его окно или запустите stop.bat в его папке, затем снова start.bat.")
        raise Fail(f"Порт {port} занят другой программой. Кто занял: netstat -ano | findstr :{port} "
                   f"(последняя колонка - PID в Диспетчере задач); если там пусто, порт зарезервирован Windows: "
                   f"netsh interface ipv4 show excludedportrange protocol=tcp. Другой порт: в командной строке "
                   f"set DESK_PORT=8001, затем start.bat из того же окна.")
    log = home / "logs" / "serve.log"
    say(f"  журнал: {log}")
    say(f"  адрес: {url}")
    say("Остановить: закройте это окно (или Ctrl+C, или stop.bat).")
    say()

    def open_when_ready(proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + 300  # the first start of PostgreSQL may take a while
        while time.monotonic() < deadline and proc.poll() is None:
            if _no_proxy_get(f"http://127.0.0.1:{port}/health/live"):
                webbrowser.open(url)
                return
            time.sleep(1)

    rc = run_logged([str(desk_exe()), "serve", "--host", "127.0.0.1", "--port", str(port)], env, log,
                    on_start=open_when_ready)
    if rc in STOPPED:
        say("Сервер остановлен.")
        return 0
    raise Fail(f"Сервер остановился с ошибкой (код {rc}); подробности выше и в {log}")


def cmd_stop(_args: list[str]) -> int:
    env = desk_environ()
    desk, _ = bundle_processes()
    if desk:
        say(f"Останавливаю процессы Desk Monitor из {ROOT}: {len(desk)}")
        _kill(desk)
        time.sleep(1)
    stop_store(env)
    say("Остановлено.")
    return 0


def cmd_cycle(args: list[str]) -> int:
    env = desk_environ()
    home = home_dir(env)
    ensure_env(env)
    rc = run_logged([str(desk_exe()), "cycle", *args], env, home / "logs" / "cycle.log")
    if rc:
        raise Fail(f"desk cycle завершился с ошибкой (код {rc}); журнал {home / 'logs' / 'cycle.log'}")
    return 0


def cmd_desk(args: list[str]) -> int:
    env = desk_environ()
    home_dir(env)
    ensure_env(env)
    return subprocess.run([str(desk_exe()), *args], env=env, check=False).returncode


def main(argv: list[str]) -> int:
    cmds = {"start": cmd_start, "stop": cmd_stop, "cycle": cmd_cycle, "desk": cmd_desk,
            "reinstall": lambda a: ensure_env(desk_environ(), force=True) or 0}
    if not argv or argv[0] not in cmds:
        say(__doc__.split("\n\n")[1])
        return 2
    try:
        return cmds[argv[0]](argv[1:])
    except Fail as e:
        say()
        say(f"ОШИБКА: {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
