"""The Windows check of the "no packages" bundle (TDM#639), run by CI on ``windows-latest`` with CPython 3.11 (stdlib only):

    python backend/tools/ci_windows_bundle.py --old DeskMonitorMirror-<A>.zip --new DeskMonitorMirror-<B>.zip --work DIR

``--old`` is the bundle the release workflow builds (``build_windows_bundle.py --mirror-only``), ``--new`` the "next" build
(the same tree with one shipped file changed, a ``-dirty<time>`` version: newer than the first). Phases, each of them
what an owner does with ``start.bat`` and the settings page, on a clean folder with a clean DESK_HOME:

1. the layout: a bundle of the mirror kind holds only our own wheels (the rule of delivery, DEC15: no third-party wheel);
2. ``deskctl start --strict-mirror`` of the old bundle: ``mirror_install.py`` installs the third-party packages from the
   pip index of the machine (CI: the ordinary PyPI) into ``env\\`` with the lock's hashes, the embedded PostgreSQL makes
   its cluster, ``desk serve`` answers ``/health/live`` and ``/health/ready``;
3. the update: the new zip is uploaded and installed through the settings page's API, the launcher applies it and starts
   the new version, which must answer and be the one installed (``updates\\last.json``);
4. the rollback to the old version (``POST /api/v1/update/rollback``): the old version answers again;
5. the interruption: CTRL_BREAK to the launcher's process group (what Ctrl+C or a closed window of ``start.bat`` does);
   whether the embedded PostgreSQL survives it is reported (a warning, not a failure: TDM#518 isolates it, see
   ``desk.store.embedded``), then the server is killed hard with its database, as by a power cut;
6. the start after the crash reaches a working server by itself (the recovery of the database, TDM#518);
7. ``deskctl stop`` leaves no process of the bundle.

Everything is written to ``--work``: ``launcher-*.log`` (the console of each start), ``home\\`` (DESK_HOME: logs, journals
of the installer, PostgreSQL's log), ``summary.json``. On a failure the tails of the logs are printed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

OWN_WHEELS = ("desk_backend-", "rub_fixed_income_core-")
TOP = "DeskMonitor"
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FIRST_START_S = 1800  # the first start installs the packages from the index and makes the cluster
START_S = 600
UPDATE_S = 900


class CheckFailed(Exception):
    """A phase failed: the message is for the CI log."""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def tail(path: Path, lines: int = 60) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no file)"


# ---- the zip ---------------------------------------------------------------------------------------------------------


def zip_version(path: Path) -> str:
    """The ``VERSION`` file of a bundle zip (``DeskMonitor/VERSION``)."""
    with zipfile.ZipFile(path) as z:
        return z.read(f"{TOP}/VERSION").decode("utf-8").strip()


def check_zip(path: Path) -> list[str]:
    """What is wrong with a zip as the bundle without packages (empty: nothing): its name, ``wheelhouse/mirror.json``,
    and no wheel that is not ours (DEC15, AGENTS.md: the third-party packages come from the pip index of the PC)."""
    problems = []
    if not path.name.startswith("DeskMonitorMirror-"):
        problems.append(f"{path.name}: not a DeskMonitorMirror-<version>.zip (the full bundle is not built or released)")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    if f"{TOP}/wheelhouse/mirror.json" not in names:
        problems.append(f"{path.name}: no wheelhouse/mirror.json: not a bundle of the mirror kind")
    wheels = [n.rsplit("/", 1)[-1] for n in names if n.startswith(f"{TOP}/wheelhouse/") and n.endswith(".whl")]
    foreign = [w for w in wheels if not w.lower().startswith(OWN_WHEELS)]
    if foreign:
        problems.append(f"{path.name}: third-party wheels in the bundle: {', '.join(sorted(foreign)[:5])}")
    if sorted(w.split("-")[0].lower() for w in wheels) != sorted(o.rstrip("-") for o in OWN_WHEELS):
        problems.append(f"{path.name}: expected exactly our two wheels, found {sorted(wheels)}")
    return problems


def unpack(path: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(dest)
    return dest / TOP


# ---- http ------------------------------------------------------------------------------------------------------------

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a runner proxy


def http(method: str, url: str, body: bytes | None = None, ctype: str | None = None, timeout: float = 30) -> tuple[int, Any]:
    """(status, JSON body or text); (0, error text) when nothing answers."""
    req = urllib.request.Request(url, data=body, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    except (OSError, ValueError) as e:  # URLError, timeouts, resets, a server that is not up yet
        return 0, str(e)
    try:
        return code, json.loads(raw.decode("utf-8"))
    except ValueError:
        return code, raw.decode("utf-8", "replace")


def wait_for(what: str, ready: Callable[[], bool], timeout: float, *, every: float = 3.0,
             alive: Callable[[], str | None] | None = None, clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep) -> float:
    """Wait until ``ready()``; seconds it took. ``alive()`` returns a reason when the thing waited for can no longer
    happen (the launcher exited): raised at once, not after the timeout."""
    t0 = clock()
    last = t0
    while True:
        if ready():
            return clock() - t0
        why = alive() if alive else None
        if why:
            raise CheckFailed(f"{what}: {why}")
        now = clock()
        if now - t0 > timeout:
            raise CheckFailed(f"{what}: not ready after {timeout:.0f} s")
        if now - last >= 60:
            log(f"... still waiting for {what} ({now - t0:.0f} s)")
            last = now
        sleep(every)


# ---- the bundle's processes ------------------------------------------------------------------------------------------


class Bundle:
    def __init__(self, root: Path, home: Path, port: int, work: Path) -> None:
        self.root, self.home, self.port, self.work = root, home, port, work
        self.py = root / "python" / "python.exe"
        self.url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self.n = 0

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(DESK_HOME=str(self.home), DESK_PORT=str(self.port), PYTHONUTF8="1")
        env.pop("DESK_STORE_URL", None)
        env["BROWSER"] = "no-such-browser"  # the launcher opens a page: webbrowser finds nothing to start, returns False
        return env

    def cmd(self, *args: str) -> list[str]:
        return [str(self.py), "-I", "-X", "utf8", str(self.root / "app" / "deskctl.py"), *args]

    @property
    def launcher_log(self) -> Path:
        return self.work / f"launcher-{self.n}.log"

    def start(self) -> subprocess.Popen:
        """``deskctl start --strict-mirror`` in a process group of its own (CTRL_BREAK reaches it only)."""
        self.n += 1
        out = open(self.launcher_log, "wb")  # noqa: SIM115 - the child owns it until it ends
        self.proc = subprocess.Popen(self.cmd("start", "--port", str(self.port), "--strict-mirror"), cwd=self.root,
                                     env=self.env(), stdout=out, stderr=subprocess.STDOUT,
                                     creationflags=NEW_GROUP | NO_WINDOW)
        log(f"start #{self.n}: launcher pid {self.proc.pid}, console in {self.launcher_log.name}")
        return self.proc

    def launcher_gone(self) -> str | None:
        rc = self.proc.poll() if self.proc else None
        return None if rc is None else f"the launcher exited with code {rc}"

    def live(self) -> bool:
        return http("GET", f"{self.url}/health/live", timeout=5)[0] == 200

    def ready(self) -> bool:
        return http("GET", f"{self.url}/health/ready", timeout=15)[0] == 200

    def update_state(self) -> dict[str, Any]:
        code, doc = http("GET", f"{self.url}/api/v1/update")
        return doc if code == 200 and isinstance(doc, dict) else {}

    def version(self) -> str | None:
        return self.update_state().get("current_version")

    def stop(self) -> None:
        """``deskctl stop`` (what stop.bat runs), then the launcher must end."""
        r = subprocess.run(self.cmd("stop"), cwd=self.root, env=self.env(), capture_output=True, text=True,
                           timeout=600, check=False)
        log("stop: " + (r.stdout.strip().splitlines() or [""])[-1])
        if self.proc is not None:
            try:
                self.proc.wait(120)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                raise CheckFailed("the launcher did not end after stop") from None

    def pg_pid(self) -> int | None:
        try:
            return int((self.home / "pg" / "postmaster.pid").read_text(encoding="utf-8").splitlines()[0])
        except (OSError, ValueError, IndexError):
            return None


def process_alive(pid: int) -> bool:
    """Windows: tasklist (``os.kill(pid, 0)`` there would terminate the process)."""
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False)
    return str(pid) in r.stdout


def processes_of(root: Path) -> list[str]:
    """Names of postgres.exe / python.exe processes whose image is under ``root``: ``wmic`` is gone from new images,
    so ``Get-Process`` through PowerShell (the CI image has it; the bundle itself never needs it)."""
    ps = (f"Get-Process | Where-Object {{ $_.Path -and $_.Path.StartsWith('{root}', 'OrdinalIgnoreCase') }} | "
          "ForEach-Object { $_.Id.ToString() + ' ' + $_.ProcessName }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=False)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


# ---- the phases ------------------------------------------------------------------------------------------------------


def healthy(b: Bundle, what: str, timeout: float) -> float:
    secs = wait_for(f"{what}: /health/live", b.live, timeout, alive=b.launcher_gone)
    wait_for(f"{what}: /health/ready", b.ready, 120, alive=b.launcher_gone)
    log(f"{what}: answers after {secs:.0f} s")
    return secs


def phase_pool(b: Bundle) -> dict[str, Any]:
    """The processes of the history (``desk pool-check``), started by ``spawn`` from the program's own launcher: the way the
    owner's machine starts them, where the pool had never run (TDM#659). Two processes, each answers once."""
    r = subprocess.run(b.cmd("desk", "pool-check", "--workers", "2"), cwd=b.root, env=b.env(), capture_output=True,
                       text=True, errors="replace", check=False, timeout=600)
    last = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("{")), "")
    try:
        doc = json.loads(last)
    except ValueError:
        doc = {}
    if r.returncode or not doc.get("ok"):
        raise CheckFailed(f"the pool does not start: exit {r.returncode}, answer {last or r.stdout[-400:]!r}, "
                          f"{r.stderr[-1500:]}")
    log(f"the pool of the history: {doc}")
    return doc


def phase_update(b: Bundle, new_zip: Path, old: str, new: str) -> None:
    state = b.update_state()
    if state.get("current_version") != old or not state.get("can_install"):
        raise CheckFailed(f"update: the server says version {state.get('current_version')!r}, can_install "
                          f"{state.get('can_install')!r}, unavailable {state.get('unavailable')!r}")
    code, doc = http("POST", f"{b.url}/api/v1/update/upload?name={new_zip.name}", new_zip.read_bytes(),
                     "application/zip", timeout=600)
    if code != 200 or doc.get("error"):
        raise CheckFailed(f"upload of {new_zip.name}: HTTP {code}: {doc}")
    if doc.get("compare") != "newer":
        raise CheckFailed(f"the new zip is {doc.get('compare')!r} against the installed {old}, expected 'newer'")
    code, doc = http("POST", f"{b.url}/api/v1/update/install", json.dumps({"source": "upload", "name": new_zip.name})
                     .encode(), "application/json", timeout=120)
    if code != 202:
        raise CheckFailed(f"install: HTTP {code}: {doc}")
    log(f"update to {new} asked, the server restarts")
    wait_for("the new version", lambda: b.version() == new, UPDATE_S, alive=None)
    healthy(b, "after the update", START_S)
    # the launcher confirms the update from a thread that polls /health/live once a second: the answer of the server comes
    # first, last.json a moment later
    wait_for("the launcher's confirmation of the update", lambda: (b.update_state().get("last") or {}).get("version") == new,
             120, every=1.0)
    last = b.update_state().get("last") or {}
    if last.get("ok") is not True or last.get("version") != new:
        raise CheckFailed(f"the launcher's outcome is not a confirmed update to {new}: {last}")
    if (b.root / "VERSION").read_text(encoding="utf-8").strip() != new:
        raise CheckFailed("VERSION of the folder is not the new one")


def phase_rollback(b: Bundle, old: str) -> None:
    point = b.update_state().get("rollback") or {}
    if point.get("version") != old:
        raise CheckFailed(f"rollback: the point is {point}, expected version {old}")
    code, doc = http("POST", f"{b.url}/api/v1/update/rollback", b"{}", "application/json", timeout=120)
    if code != 202:
        raise CheckFailed(f"rollback: HTTP {code}: {doc}")
    wait_for("the old version again", lambda: b.version() == old, UPDATE_S)
    healthy(b, "after the rollback", START_S)
    if (b.root / "VERSION").read_text(encoding="utf-8").strip() != old:
        raise CheckFailed("VERSION of the folder is not the old one after the rollback")


def phase_interrupt(b: Bundle) -> dict[str, Any]:
    """CTRL_BREAK to the launcher's group; then the server and the database are killed hard (a power cut)."""
    out: dict[str, Any] = {}
    pg = b.pg_pid()
    assert b.proc is not None
    b.proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
    try:
        b.proc.wait(60)
    except subprocess.TimeoutExpired:
        b.proc.kill()
    time.sleep(3)
    out["postgres_survived_ctrl_break"] = bool(pg and process_alive(pg))
    log("PostgreSQL after CTRL_BREAK to the launcher: "
        + ("alive (isolated from the console)" if out["postgres_survived_ctrl_break"] else "gone"))
    if not out["postgres_survived_ctrl_break"]:
        print("::warning::the embedded PostgreSQL did not survive CTRL_BREAK to the launcher's process group: "
              "the isolation of TDM#518 does not work on this Windows; the launcher keeps saying «stop.bat»")
    for _ in range(3):  # the server and every postgres of the bundle, hard (a power cut leaves no checkpoint)
        left = [p for p in processes_of(b.root) if "postgres" in p.lower() or "python" in p.lower()]
        if not left:
            break
        for line in left:
            subprocess.run(["taskkill", "/F", "/T", "/PID", line.split()[0]], capture_output=True, check=False)
        time.sleep(2)
    if not (b.home / "pg" / "postmaster.pid").is_file():
        log("note: no postmaster.pid left; the crash start below is an ordinary start")
    return out


def phase_after_crash(b: Bundle) -> None:
    b.start()
    secs = healthy(b, "after the crash", START_S)
    text = tail(b.launcher_log, 400)
    out = {"seconds": round(secs), "said_recovering": "восстанавливается после аварийной остановки" in text,
           "removed_stale_pid": "postmaster.pid" in text and "убрал" in text}
    log(f"after the crash: {out}")


def run(old_zip: Path, new_zip: Path, work: Path, port: int) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    problems = [*check_zip(old_zip), *check_zip(new_zip)]
    if problems:
        raise CheckFailed("; ".join(problems))
    old, new = zip_version(old_zip), zip_version(new_zip)
    if old == new:
        raise CheckFailed(f"both zips are version {old}")
    summary.update(old=old, new=new)
    log(f"old {old}, new {new}")
    root = unpack(old_zip, work / "install")
    b = Bundle(root, work / "home", port, work)
    b.home.mkdir(parents=True, exist_ok=True)
    try:
        b.start()
        summary["first_start_s"] = round(healthy(b, "first start", FIRST_START_S))
        if b.version() != old:
            raise CheckFailed(f"the server says version {b.version()!r}, the bundle is {old}")
        summary["pool"] = phase_pool(b)
        phase_update(b, new_zip, old, new)
        summary["update"] = "ok"
        phase_rollback(b, old)
        summary["rollback"] = "ok"
        summary.update(phase_interrupt(b))
        phase_after_crash(b)
        summary["after_crash"] = "ok"
        b.stop()
        left = processes_of(b.root)
        if left:
            raise CheckFailed(f"processes of the bundle after stop: {left}")
        summary["stop"] = "ok"
    finally:
        (work / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        if b.proc is not None and b.proc.poll() is None:  # a failed run must not leave the server to the next step
            subprocess.run(b.cmd("stop"), cwd=b.root, env=b.env(), capture_output=True, check=False, timeout=600)
    return summary


def diagnose_database(b: Bundle) -> None:
    """After a failed start: what the data folder and ``initdb`` look like from here (printed into the CI log; the artifacts
    of a failed run cannot always be opened). Best effort, never raises."""
    pg = b.home / "pg"

    def sh(*cmd: str, lines: int = 22) -> None:
        try:
            r = subprocess.run(list(cmd), capture_output=True, text=True, errors="replace", check=False, timeout=120)
            print(f"$ {' '.join(cmd)}  -> {r.returncode}")
            print("\n".join((r.stdout + r.stderr).splitlines()[-lines:]))
        except (OSError, subprocess.SubprocessError) as e:
            print(f"$ {' '.join(cmd)}  -> {type(e).__name__}: {e}")

    print("--- diagnostics of the database folder ---")
    sh("icacls", str(b.home), lines=10)
    sh("icacls", str(pg), lines=10)
    sh("attrib", str(pg))
    sh("cmd", "/c", "dir", "/a", str(pg), lines=12)
    sh(str(b.py), "-I", "-c", "import os,sys;print(os.stat(sys.argv[1]));os.chmod(sys.argv[1], 0o700);print('chmod ok')", str(pg))
    initdb = b.root / "env" / "Lib" / "site-packages" / "pgserver" / "pginstall" / "bin" / "initdb.exe"
    if not initdb.is_file():  # the bundle removes its own environment after a failed first start: nothing to try then
        print(f"initdb not found ({initdb}): not tried")
        return
    for where in (b.work / "diag_work", b.home / "diag_home"):
        where.mkdir(exist_ok=True)
        sh(str(initdb), "-D", str(where), "--auth=trust", "--auth-local=trust", "--encoding=utf8", "-U", "postgres",
           lines=6)
    sh(str(initdb), "-D", str(pg), "--auth=trust", "--auth-local=trust", "--encoding=utf8", "-U", "postgres", lines=6)


def utf8_console() -> None:
    """The console of a runner is cp1252: the logs of the launcher are Russian, and printing them raised
    UnicodeEncodeError, which hid the real cause of a failed phase. Never raise on output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--old", type=Path, required=True, help="DeskMonitorMirror-<version>.zip, installed first")
    ap.add_argument("--new", type=Path, required=True, help="the next build, installed over it, then rolled back")
    ap.add_argument("--work", type=Path, required=True, help="a folder for the install, DESK_HOME and the logs")
    ap.add_argument("--port", type=int, default=18100)
    a = ap.parse_args(argv)
    a.work.mkdir(parents=True, exist_ok=True)
    try:
        summary = run(a.old, a.new, a.work, a.port)
    except CheckFailed as e:
        print(f"::error::{e}")
        for f in sorted(a.work.glob("launcher-*.log")):
            print(f"--- {f.name} (tail) ---\n{tail(f, 150)}")
        logs = a.work / "home" / "logs"
        if "initdb" in "".join(tail(f, 400) for f in a.work.glob("launcher-*.log")):
            diagnose_database(Bundle(a.work / "install" / TOP, a.work / "home", a.port, a.work))
        for f in sorted(logs.glob("*")) if logs.is_dir() else []:
            if f.is_file():
                print(f"--- {f.name} (tail) ---\n{tail(f, 12)}")
        print(f"FAILED: {e}", flush=True)  # again at the very end: the first line is far above the long tails
        return 1
    log("OK " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
