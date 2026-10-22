"""Desk Monitor launcher of the Windows bundle: stdlib only, run by the bundled Python (``-I``).

    python\\python.exe -I app\\deskctl.py start [--port N]  install if needed, desk serve, open the browser
    python\\python.exe -I app\\deskctl.py stop               stop this bundle's desk processes and the embedded store
    python\\python.exe -I app\\deskctl.py cycle [args]       install if needed, then desk cycle (hub, inbox, EOD)
    python\\python.exe -I app\\deskctl.py desk [args]        install if needed, then any desk command
    python\\python.exe -I app\\deskctl.py firewall add|remove|show [--port N]   the Windows firewall rule for colleagues
                                                          (also: desk.bat firewall add, as administrator)

The bundle folder (``DeskMonitor\\``, anywhere): ``python\\`` the runtime, ``wheelhouse\\`` every wheel and
``requirements.txt``, ``app\\`` this file, the prototypes and the built frontend, ``adapter\\`` the private adapter
when the build had one, ``VERSION``. The venv ``env\\`` is made from the runtime and the wheelhouse, offline,
whenever ``env\\desk-bundle.json`` does not name this version and this folder (a venv cannot move); when only some
wheels changed, just those are reinstalled. Data and settings live in DESK_HOME (default
``%LOCALAPPDATA%\\DeskMonitor``, or ``C:\\DeskMonitorData`` when that path is not plain ASCII, see ``default_home``),
outside the bundle, so they survive updates.
The .bat files only find a Python and call this; the logic is here, away from cmd.exe quoting.

Address (batch 1, item 9): ``desk serve`` listens where the settings say (``bind_host``, ``port`` of settings.json,
``DESK_PORT`` / ``--port`` here win for the port); a network address needs sign-in (``desk lan enable``), and the
server then also listens on 127.0.0.1, so the health check and the browser opened here stay on 127.0.0.1 (https
when ``tls_cert`` is set). ``firewall add`` adds the inbound rule colleagues need (``netsh advfirewall``, as
administrator); without administrator rights it prints the command to run.

Hot update (the settings page, ``desk.update``): ``desk serve`` checks an update zip, stages its changed files in
``<DESK_HOME>\\updates\\staged\\<version>\\``, writes ``updates\\pending.json`` and exits with ``RESTART``. ``start``
then waits for the other desk processes of this folder (``update_daily.bat``), stops the embedded PostgreSQL, backs
up every file it replaces or deletes into ``updates\\backup\\<old version>\\``, writes the new files (never
``python\\`` or ``env\\``) and runs the new launcher (``start --verify-update``) as a child process: it updates the
venv and starts the new server, and once that answers ``/health/live`` it confirms the update. When the child ends
without confirming (no answer within ``UPDATE_TIMEOUT_S``, a failed install), this launcher restores the backup and
starts the old version again. ``updates\\last.json`` keeps the outcome for the settings page.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import ssl
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
# desk serve's exit code asking for a restart, an update staged or not (desk.update reads DESK_RESTART_CODE)
RESTART = 75
UPDATE_TIMEOUT_S = 300  # the new version answers /health/live within this after its server starts, or is rolled back
IDLE_WAIT_S = 600  # how long an update waits for the other desk processes of this folder (a desk cycle)
KEEP_BACKUPS = 3
LOG_MAX_BYTES = 20_000_000
DEFAULT_PORT = 8100
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
    env["DESK_BUNDLE_ROOT"] = str(ROOT)  # desk.update checks update zips against this folder
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


def _launchers() -> set[int]:
    """This process and the launchers waiting on it (a hot update runs the new launcher as a child)."""
    return {os.getpid(), *(int(p) for p in os.environ.get("DESK_LAUNCHER_PIDS", "").split(",") if p.isdigit())}


def bundle_processes() -> tuple[list[int], list[int]]:
    """(desk/python pids, postgres pids) running from this bundle's runtime or venv, the launchers excluded."""
    desk, pg = [], []
    skip = _launchers()
    for pid, image in _processes().items():
        if pid in skip or not (_under(image, ENV) or _under(image, ROOT / "python")):
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
    (ROOT / "PATCH_BASE.txt").unlink(missing_ok=True)  # the version the patch applies to: for the hot update only
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


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def env_marker() -> dict:
    """What ``env\\`` is made of: the version and folder it belongs to, the runtime, requirements.txt, each wheel."""
    req = WHEELHOUSE / "requirements.txt"
    return {"version": version(), "root": str(ROOT), "python": f"{sys.executable} {sys.version}",
            "requirements": sha256(req) if req.is_file() else None,
            "wheels": {p.name: sha256(p) for p in sorted(WHEELHOUSE.glob("*.whl"))}}


def changed_wheels(have: object, want: dict) -> list[str] | None:
    """The wheels to reinstall (``--no-deps``) into the venv ``have`` describes when only their bytes changed: the
    same folder, runtime, requirements.txt and wheel names (requirements.txt pins every package, so the same pins
    and names mean the same set of packages); None when the venv must be made anew."""
    if not isinstance(have, dict) or not isinstance(have.get("wheels"), dict):
        return None
    if any(have.get(k) != want[k] for k in ("root", "python", "requirements")) or set(have["wheels"]) != set(want["wheels"]):
        return None
    return [name for name, digest in want["wheels"].items() if have["wheels"][name] != digest]


def ensure_env(env: dict[str, str], *, force: bool = False) -> None:
    """Create ``env\\`` from the bundled runtime and the wheelhouse unless it already belongs to this bundle; when only
    some wheels changed (a new desk build), reinstall just those."""
    apply_patch_delete()
    check_paths(env)
    want = {"version": version(), "root": str(ROOT)}
    try:
        have = json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        have = None
    if isinstance(have, dict) and {k: have.get(k) for k in want} == want and desk_exe().is_file() and not force:
        return
    if ENV.exists():
        desk, _ = bundle_processes()
        if desk:
            raise Fail("Desk Monitor из этой папки ещё работает. Закройте его окно (или запустите stop.bat) "
                       "и запустите снова.")
    marker = env_marker()
    changed = None if force or not desk_exe().is_file() else changed_wheels(have, marker)
    if changed is not None and update_wheels(env, changed):
        MARKER.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
        migrate_store(env)
        say("Окружение обновлено.")
        return
    say(f"Устанавливаю Desk Monitor {want['version']} в {ENV}")
    say("  (один раз после распаковки или обновления, без интернета, 1-3 минуты) ...")
    if ENV.exists():
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
    MARKER.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    migrate_store(env)
    say("Установка завершена.")


def update_wheels(env: dict[str, str], changed: list[str]) -> bool:
    """Reinstall the ``changed`` wheels into the existing venv; False when that did not work (make it anew)."""
    say(f"Обновляю окружение под Desk Monitor {version()}: " + (f"изменилось пакетов {len(changed)} (без интернета) ..."
                                                               if changed else "пакеты прежние ..."))
    py = SCRIPTS / f"python{EXE}"
    if changed:
        stop_store(env)  # its postgres.exe runs from the venv: a changed pgserver could not be replaced under it
        r = subprocess.run([str(py), "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall",
                            "--no-warn-script-location", *(str(WHEELHOUSE / w) for w in changed)], env=env, check=False)
        if r.returncode:
            say("  не получилось: ставлю окружение заново")
            return False
    r = subprocess.run([str(py), "-c", "import desk.api"], env=env, capture_output=True, text=True, check=False)
    if r.returncode:
        say(f"  пакет desk не импортируется, ставлю окружение заново:\n{r.stderr}")
        return False
    return True


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
    handlers: list = [urllib.request.ProxyHandler({})]  # never via the corporate proxy
    if url.startswith("https://127.0.0.1"):  # our own server, often a self-signed certificate: not checked
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
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


# ---- the address desk serve listens on, and the firewall (batch 1, item 9)

def serve_settings(env: dict[str, str], home: Path) -> dict:
    """``bind_host``, ``port``, ``tls`` and ``auth`` as desk serve will read them: the process environment, then
    ``<home>\\settings.json``, then the defaults (desk.settings; the env file is not read here)."""
    doc = _read_json(home / "settings.json") or {}
    doc = doc if isinstance(doc, dict) else {}

    def pick(var: str, key: str):
        return env.get(var) or doc.get(key)

    try:
        port = int(pick("DESK_PORT", "port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    auth = pick("DESK_AUTH", "auth")
    return {"host": str(pick("DESK_BIND_HOST", "bind_host") or "127.0.0.1").strip("[]"), "port": port,
            "tls": bool(pick("DESK_TLS_CERT", "tls_cert") and pick("DESK_TLS_KEY", "tls_key")),
            "auth": auth is True or str(auth).lower() in ("1", "true", "yes", "on")}


def _loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def lan_names(host: str) -> list[str]:
    """What colleagues type: this computer's name and addresses (0.0.0.0), or the address itself."""
    if host not in ("0.0.0.0", "::"):
        return [host]
    out = [socket.gethostname()]
    try:
        out += [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    return [n for i, n in enumerate(out) if n and n not in out[:i] and not n.startswith("127.")]


FIREWALL_RULE = "Desk Monitor (TCP {port})"


def firewall_commands(action: str, port: int, remoteip: str | None = None) -> list[list[str]]:
    """``netsh advfirewall firewall`` commands (INSTALL.txt explains each part): an inbound allow rule for the
    TCP port on the domain and private network profiles (not public: a café's Wi-Fi), from ``remoteip`` when
    given (e.g. ``localsubnet`` or ``10.1.0.0/16``)."""
    name = "name=" + FIREWALL_RULE.format(port=port)
    if action == "add":
        cmd = ["netsh", "advfirewall", "firewall", "add", "rule", name, "dir=in", "action=allow", "protocol=TCP",
               f"localport={port}", "profile=domain,private"]
        return [cmd + ([f"remoteip={remoteip}"] if remoteip else [])]
    if action == "remove":
        return [["netsh", "advfirewall", "firewall", "delete", "rule", name]]
    return [["netsh", "advfirewall", "firewall", "show", "rule", name]]


def _is_admin() -> bool:
    if not WIN:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def _shown(cmd: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in cmd)


def cmd_firewall(args: list[str]) -> int:
    action = args[0] if args and args[0] in ("add", "remove", "show") else None
    if action is None:
        say("firewall add | remove | show [--port N] [--remoteip ADDRESSES]: правило брандмауэра Windows для коллег")
        return 2
    env = desk_environ()
    opts = dict(zip(args[1::2], args[2::2]))
    port = int(opts.get("--port") or serve_settings(env, Path(env["DESK_HOME"]))["port"])
    cmds = firewall_commands(action, port, opts.get("--remoteip"))
    if action != "show" and not _is_admin():
        say("Нужны права администратора. Откройте «Командную строку» от имени администратора (Пуск -> cmd -> "
            "правой кнопкой -> «Запуск от имени администратора») и выполните:")
        for c in cmds:
            say("  " + _shown(c))
        say("Или там же, в папке Desk Monitor: desk.bat firewall " + action)
        return 1 if WIN else 0
    rc = 0
    for c in cmds:
        say("> " + _shown(c))
        rc = rc or subprocess.run(c, check=False).returncode
    if action == "add" and rc == 0:
        say(f"Готово: входящие подключения к порту {port} разрешены (сети домена и частные).")
    return rc


# ---- hot update (see the module doc; desk.update writes pending.json and the staged folder)

def updates_dir(env: dict[str, str]) -> Path:
    return Path(env["DESK_HOME"]) / "updates"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def update_log(env: dict[str, str], msg: str) -> None:
    """To the console and to ``logs\\update.log``."""
    say(msg)
    try:
        with open(Path(env["DESK_HOME"]) / "logs" / "update.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def record_update(env: dict[str, str], ok: bool, target: str, old: str, message: str) -> None:
    """The outcome the settings page shows (``updates\\last.json``)."""
    _write_json(updates_dir(env) / "last.json", {"ok": ok, "version": target, "from_version": old,
                                                 "message": message, "at": datetime.now().isoformat(timespec="seconds")})


def safe_rel(rel: object) -> bool:
    """A bundle path an update may write or delete: relative, ``/``-separated, no ``..``, never ``python/`` (the
    running runtime) or ``env/`` (the venv, made by ensure_env)."""
    if not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel or rel.startswith("/"):
        return False
    parts = rel.split("/")
    return all(p not in ("", ".", "..") for p in parts) and parts[0] not in ("python", "env")


def _copy(src: Path, dst: Path) -> None:
    """``src`` over ``dst`` through a temporary file and ``os.replace``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".desk-new")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _prune_dirs(path: Path) -> None:
    """Remove the empty folders from ``path`` up to (not including) the bundle folder."""
    while path != ROOT and _under(str(path), ROOT):
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def apply_update(env: dict[str, str], staged: Path) -> None:
    """Back up, then write the files of the staged update ``staged`` (``update.json``: ``files`` path -> sha256,
    ``delete``) into this folder; ``updates\\applying.json`` names the backup until the new version confirms.
    Fail when nothing could be applied; a failure while writing restores the backup first."""
    plan = _read_json(staged / "update.json")
    old = version()
    if not isinstance(plan, dict) or not isinstance(plan.get("files"), dict) or not isinstance(plan.get("delete"), list):
        raise Fail(f"Подготовленное обновление {staged} не читается: загрузите архив заново.")
    if plan.get("from_version") != old:
        raise Fail(f"Обновление подготовлено для версии {plan.get('from_version')}, а установлена {old}: "
                   "загрузите архив заново.")
    files, delete = plan["files"], plan["delete"]
    bad = [rel for rel in [*files, *delete] if not safe_rel(rel)]
    if bad:
        raise Fail(f"В подготовленном обновлении недопустимые пути: {', '.join(map(str, bad[:5]))}")
    for rel, digest in files.items():
        src = staged / "files" / rel
        if not src.is_file() or sha256(src) != digest:
            raise Fail(f"Подготовленный файл {rel} повреждён или пропал: загрузите архив заново.")
    backup = updates_dir(env) / "backup" / old
    if backup.exists():
        _rmtree(backup)
    replaced, created = [], []
    for rel in [*files, *delete]:
        target = ROOT / rel
        if target.is_file():
            _copy(target, backup / "files" / rel)
            replaced.append(rel)
        elif rel in files:
            created.append(rel)
    _write_json(backup / "backup.json", {"version": old, "to_version": plan.get("version"), "replaced": replaced,
                                         "created": created})
    _write_json(updates_dir(env) / "applying.json", {"version": plan.get("version"), "from_version": old,
                                                     "backup": str(backup), "staged": str(staged),
                                                     "at": datetime.now().isoformat(timespec="seconds")})
    try:
        for rel in files:
            _copy(staged / "files" / rel, ROOT / rel)
        for rel in delete:
            (ROOT / rel).unlink(missing_ok=True)
            _prune_dirs((ROOT / rel).parent)
    except OSError as e:
        rollback_update(env)
        raise Fail(f"Не удалось записать файлы обновления ({e}); прежняя версия восстановлена.") from e


def rollback_update(env: dict[str, str]) -> bool:
    """Restore the backup ``updates\\applying.json`` names; False when there is nothing to restore."""
    applying = updates_dir(env) / "applying.json"
    state = _read_json(applying)
    if not isinstance(state, dict):
        applying.unlink(missing_ok=True)
        return False
    backup = Path(str(state.get("backup") or ""))
    rec = _read_json(backup / "backup.json")
    if not isinstance(rec, dict):
        raise Fail(f"Нет резервной копии {backup} для отката обновления: распакуйте полный архив прежней версии "
                   "(stop.bat, распаковать поверх, start.bat).")
    for rel in rec.get("created", []):
        if safe_rel(rel):
            (ROOT / rel).unlink(missing_ok=True)
            _prune_dirs((ROOT / rel).parent)
    for rel in rec.get("replaced", []):
        if safe_rel(rel):
            _copy(backup / "files" / rel, ROOT / rel)
    applying.unlink(missing_ok=True)
    return True


def confirm_update(env: dict[str, str]) -> None:
    """The new version answers: the update stays; drop its staged files and all but the newest backups."""
    upd = updates_dir(env)
    state = _read_json(upd / "applying.json")
    if not isinstance(state, dict):
        return
    (upd / "applying.json").unlink(missing_ok=True)
    record_update(env, True, version(), str(state.get("from_version")), f"Установлена версия {version()}.")
    update_log(env, f"Обновление до {version()} завершено.")
    _drop_staged(state.get("staged"))
    if (upd / "backup").is_dir():
        backups = sorted((d for d in (upd / "backup").iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
        for d in backups[:-KEEP_BACKUPS]:
            _rmtree(d)


def _drop_staged(staged: object) -> None:
    if staged and Path(str(staged)).is_dir():
        try:
            _rmtree(Path(str(staged)))
        except OSError:
            pass


def wait_idle(env: dict[str, str], timeout: float) -> bool:
    """Wait until no other desk process of this folder runs (a desk cycle of update_daily.bat); False on timeout."""
    deadline = time.monotonic() + timeout
    told = False
    while bundle_processes()[0]:
        if time.monotonic() > deadline:
            return False
        if not told:
            update_log(env, "Жду, пока закончат работу другие процессы Desk Monitor из этой папки "
                            "(например, update_daily.bat) ...")
            told = True
        time.sleep(2)
    return True


def run_child(cmd: list[str], env: dict[str, str]) -> int:
    """Run ``cmd`` in this console and wait for it; Ctrl+C reaches it too, so wait for it to stop."""
    proc = subprocess.Popen(cmd, env=env)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            try:
                return proc.wait(30)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                proc.kill()
                return 130


def hot_update(env: dict[str, str], args: list[str]) -> int | None:
    """After desk serve asked for a restart: apply the staged update ``updates\\pending.json`` names and run the new
    launcher until it ends; returns its exit code. None: nothing new runs (a plain restart, or the update failed
    and the old files are back), the caller starts its own server again."""
    upd = updates_dir(env)
    pending = _read_json(upd / "pending.json")
    (upd / "pending.json").unlink(missing_ok=True)
    if not isinstance(pending, dict):
        say("Перезапуск сервера ...")
        return None
    old, target, staged = version(), str(pending.get("version")), Path(str(pending.get("staged") or ""))
    update_log(env, f"Обновление Desk Monitor {old} -> {target} ...")
    if not wait_idle(env, IDLE_WAIT_S):
        msg = ("Обновление не установлено: другие процессы Desk Monitor из этой папки (update_daily.bat?) "
               f"не закончили работу за {IDLE_WAIT_S // 60} минут. Попробуйте позже.")
        update_log(env, msg)
        record_update(env, False, target, old, msg)
        _drop_staged(staged)
        return None
    stop_store(env)
    try:
        apply_update(env, staged)
    except Fail as e:
        update_log(env, str(e))
        record_update(env, False, target, old, f"Обновление не установлено: {e}")
        _drop_staged(staged)
        return None
    update_log(env, f"Файлы обновлены; запускаю Desk Monitor {target} ...")
    child_env = {**os.environ, "DESK_LAUNCHER_PIDS": ",".join(map(str, sorted(_launchers())))}
    rc = run_child([sys.executable, "-I", "-X", "utf8", str(APP / "deskctl.py"), "start", "--verify-update", *args],
                   child_env)
    if not (upd / "applying.json").exists():
        return rc  # the new version answered and ran until it was stopped
    _kill(bundle_processes()[0])
    stop_store(env)
    last = _read_json(upd / "last.json")
    why = last.get("message") if isinstance(last, dict) and last.get("version") == target and not last.get("ok") else None
    rollback_update(env)
    msg = (f"Версия {target} не запустилась ({why or f'код {rc}'}); возвращена версия {version()}. "
           "Подробности: logs\\serve.log и logs\\update.log в папке данных.")
    update_log(env, msg)
    record_update(env, False, target, old, msg)
    _drop_staged(staged)
    return rc if rc in STOPPED else None  # stopped while it was starting: stay stopped


def cmd_start(args: list[str]) -> int:
    verify = "--verify-update" in args  # the new launcher of a hot update: confirm once the server answers
    args = [a for a in args if a != "--verify-update"]
    env = desk_environ()
    home = home_dir(env)
    addr = serve_settings(env, home)  # bind_host, port, tls of the settings (desk serve reads the same)
    port = addr["port"]
    if args[:1] == ["--port"] and len(args) > 1:
        port = int(args[1])
    if not verify and (updates_dir(env) / "applying.json").exists() and not bundle_processes()[0]:
        update_log(env, "Прошлое обновление не завершилось: возвращаю прежнюю версию из резервной копии ...")
        state = _read_json(updates_dir(env) / "applying.json") or {}
        if rollback_update(env):
            record_update(env, False, str(state.get("version")), version(),
                          f"Обновление не завершилось (окно закрыли или компьютер перезагрузили); "
                          f"возвращена версия {version()}.")
        env = desk_environ()
    say(f"Desk Monitor {version()}")
    say(f"  программа: {ROOT}")
    say(f"  данные и настройки (DESK_HOME): {home}")
    try:
        ensure_env(env)
    except Fail as e:
        if verify:
            record_update(env, False, version(), "", str(e))
        raise
    page = "concepts/index.html" if (home / "settings.json").is_file() else "settings.html"  # the monitors
    scheme = "https" if addr["tls"] else "http"
    local = f"{scheme}://127.0.0.1:{port}"  # desk serve listens here whatever bind_host says (desk.api.app)
    url = f"{local}/{page}"
    lan = not _loopback_host(addr["host"])
    if lan and not addr["auth"]:
        raise Fail(f"В настройках адрес {addr['host']} (доступ из сети), а вход по паролю выключен: сервер так не "
                   "запустится. Создайте администратора и включите вход: desk.bat user add ИМЯ --role admin, затем "
                   "desk.bat lan enable --self-signed. Или верните адрес 127.0.0.1: desk.bat lan disable.")
    if not verify and port_busy(port):  # verify: the old server just closed it (a busy port fails desk serve)
        ours = bool(bundle_processes()[0])
        if _no_proxy_get(f"{local}/health/live") and ours:
            say(f"Desk Monitor уже запущен: открываю {url}")
            webbrowser.open(url)
            return 0
        if _no_proxy_get(f"{local}/health/live"):
            raise Fail(f"Порт {port} занят другим экземпляром Desk Monitor (старая версия из другой папки?). "
                       "Закройте его окно или запустите stop.bat в его папке, затем снова start.bat.")
        raise Fail(f"Порт {port} занят другой программой. Кто занял: netstat -ano | findstr :{port} "
                   f"(последняя колонка - PID в Диспетчере задач); если там пусто, порт зарезервирован Windows: "
                   f"netsh interface ipv4 show excludedportrange protocol=tcp. Другой порт: в командной строке "
                   f"set DESK_PORT=8001, затем start.bat из того же окна.")
    log = home / "logs" / "serve.log"
    say(f"  журнал: {log}")
    say(f"  адрес: {url}")
    if lan:
        say("  для коллег: " + ", ".join(f"{scheme}://{n}:{port}/" for n in lan_names(addr["host"])[:3])
            + "  (вход по паролю; правило брандмауэра: desk.bat firewall add от имени администратора)")
    say("Остановить: закройте это окно (или Ctrl+C, или stop.bat).")
    say()

    browse = not verify  # after an update the settings page reloads itself

    def when_ready(proc: subprocess.Popen) -> None:
        # the first start of PostgreSQL may take a while; a new version that does not answer is rolled back
        deadline = time.monotonic() + (UPDATE_TIMEOUT_S if verify else 300)
        while time.monotonic() < deadline and proc.poll() is None:
            if _no_proxy_get(f"{local}/health/live"):
                if verify:
                    confirm_update(env)
                if browse:
                    webbrowser.open(url)
                return
            time.sleep(1)
        if verify and proc.poll() is None:
            timed_out.append(True)
            proc.kill()

    timed_out: list[bool] = []

    while True:
        # DESK_RESTART_CODE: desk serve may exit with it (desk.update), here it means "start me again"
        # the address is the settings' bind_host (desk serve reads it; it refuses a network one without sign-in)
        rc = run_logged([str(desk_exe()), "serve", "--port", str(port)],
                        {**env, "DESK_RESTART_CODE": str(RESTART)}, log, on_start=when_ready)
        if rc != RESTART:
            break
        if verify and (updates_dir(env) / "applying.json").exists():
            break  # restarted before it was confirmed: let the parent roll back
        verify = browse = False
        child = hot_update(env, args)
        if child is not None:
            return child
        env = desk_environ()  # the old version again, or the same one restarted
        ensure_env(env)
    if rc in STOPPED:
        say("Сервер остановлен.")
        return 0
    if verify and (updates_dir(env) / "applying.json").exists():
        record_update(env, False, version(), "", f"сервер не ответил за {UPDATE_TIMEOUT_S} с" if timed_out
                      else f"сервер остановился с ошибкой, код {rc}")
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
    if args[:1] == ["firewall"]:  # desk.bat firewall add: the launcher's, not a desk command
        return cmd_firewall(args[1:])
    env = desk_environ()
    home_dir(env)
    ensure_env(env)
    return subprocess.run([str(desk_exe()), *args], env=env, check=False).returncode


def main(argv: list[str]) -> int:
    cmds = {"start": cmd_start, "stop": cmd_stop, "cycle": cmd_cycle, "desk": cmd_desk, "firewall": cmd_firewall,
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
