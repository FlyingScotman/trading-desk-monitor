"""What the settings page finds out by itself on this computer (#449): ``GET /api/v1/settings/detect``.

Only local reads, no network and no side effect: the process environment, the Windows registry (``winreg``, the
standard library), files and folders on local disks and the store. Nothing here is written to settings.json; the
page shows each finding as «определено автоматически» (a setting left empty already uses it) or as a suggestion
the user takes with a click.

* ``proxy``    the proxy hub requests take when ``https_proxy`` is empty, as ``desk.marketdata.hub.hub_proxy``
               decides (urllib: the environment without what ``load()`` exported from settings.json, else the
               Windows Internet Options with their exceptions), and whether Windows uses an automatic
               configuration script (``AutoConfigURL``, a PAC file urllib cannot read)
* ``trust``    whose certificates the Python transport trusts (``desk.marketdata.hub.tls_context``)
* ``curl``     the curl the transport ``curl`` / ``auto`` would run (``find_curl``)
* ``python``   Python interpreters found on this computer for ``acquisition_python``: offered, never chosen
               (08 §2: the acquisition runtime is explicit); none of them is run
* ``defaults`` what an empty ``snapshot_cache``, ``inbox_archive``, ``private_adapter_config`` and
               ``private_adapter`` (the bundle's adapter) give
* ``inbox``    folders to offer as report folders: absolute folder paths named in the adapter's config files (the
               adapter's own folder settings feed nothing to the inbox, #449) and folders whose files the inbox
               accepted before, both only when no report folder covers them yet

A path on a network share (UNC or a mapped network drive) is never touched: its existence is not checked; local
checks are limited per request and asked once each (``Disk``).
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path, PureWindowsPath
from typing import Any

from .settings import ADAPTER_CONFIG_DIR, Settings, mask_proxy

MAX_CONFIG_FILES = 20
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_SUGGESTIONS = 12
INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/]|/[^/])")
_PY_DIR = re.compile(r"python(\d)(\d{1,2})", re.IGNORECASE)
_NOT_INPUT = re.compile(r"output|archive|cache|backup|python|venv|(?<![a-z])(?:out|logs?|te?mp)(?![a-z])",
                        re.IGNORECASE)
"""Config keys of folders that are not report folders (where the adapter writes, its runtime, …)."""


# ---- local disks ---------------------------------------------------------------------------------------------------


_DRIVE_REMOTE = 4  # GetDriveTypeW: a mapped network drive


class Disk:
    """Existence checks on local disks: at most ``budget`` of them per request, each answer remembered (a missing
    folder is not asked twice); a path on a network share - UNC or a mapped network drive - is never touched (None).
    ``remote``: is this drive letter (``Z:``) a network drive (tests); by default ``GetDriveTypeW`` on Windows."""

    def __init__(self, budget: int = 60, remote: Callable[[str], bool] | None = None) -> None:
        self.budget, self._remote = budget, remote
        self._drives: dict[str, bool] = {}
        self._seen: dict[tuple[str, str], bool] = {}

    def network(self, path: str) -> bool:
        if path.startswith(("\\\\", "//")):
            return True
        m = re.match(r"^([A-Za-z]):", path)
        if not m:
            return False
        d = m.group(1).upper() + ":"
        if d not in self._drives:
            self._drives[d] = (self._remote or _windows_remote)(d)
        return self._drives[d]

    def _ask(self, path: str, kind: str) -> bool | None:
        if not path or self.network(path):
            return None
        key = (kind, os.path.normcase(path))
        if key not in self._seen:
            if self.budget <= 0:
                return None
            self.budget -= 1
            self._seen[key] = os.path.isdir(path) if kind == "dir" else os.path.isfile(path)
        return self._seen[key]

    def is_dir(self, path: str) -> bool | None:
        return self._ask(path, "dir")

    def is_file(self, path: str) -> bool | None:
        return self._ask(path, "file")


def _windows_remote(drive: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == _DRIVE_REMOTE  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no answer: treat it as a network drive, never touch it
        return True


# ---- proxy and trust ----------------------------------------------------------------------------------------------


def pac_script() -> bool:
    """Windows sets the proxy by an automatic configuration script (``AutoConfigURL``), which urllib cannot read."""
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS) as k:
            return bool(winreg.QueryValueEx(k, "AutoConfigURL")[0])
    except (ImportError, OSError):
        return False


def environment_proxies(environ: dict[str, str], exported: dict[str, str]) -> dict[str, str]:
    """``urllib.request.getproxies_environment`` over ``environ`` without the variables ``load()`` exported from
    settings.json itself (``desk.settings._exported``): the system's proxies, not the page's."""
    out: dict[str, str] = {}
    for name in sorted(environ, key=lambda n: n.islower()):  # the lowercase name wins, as in urllib
        value = environ[name]
        if value and name[-6:].lower() == "_proxy" and exported.get(name.upper()) != value:
            out[name[:-6].lower()] = value
    if "REQUEST_METHOD" in environ:
        out.pop("http", None)
    return out


def registry_proxies() -> tuple[dict[str, str], Callable[[str], bool]]:
    """The Internet Options' proxies and bypass check, as urllib reads them (``getproxies_registry``,
    ``proxy_bypass_registry``: ``ProxyServer`` per scheme, SOCKS, ``ProxyOverride`` with ``<local>``)."""
    if os.name != "nt":
        return {}, lambda host: False
    return urllib.request.getproxies_registry(), urllib.request.proxy_bypass_registry  # type: ignore[attr-defined]


def proxy(hub_url: str | None, environ: dict[str, str], exported: dict[str, str],
          registry: Callable[[], tuple[dict[str, str], Callable[[str], bool]]] = registry_proxies,
          pac: bool = False) -> dict[str, Any]:
    """The proxy hub requests take when ``https_proxy`` is empty - what ``desk.marketdata.hub.hub_proxy`` does
    (``getproxies()`` + ``proxy_bypass()``: the environment, else on Windows the Internet Options): ``system`` (the
    proxy URL, password masked; None: direct), ``source`` (``environment`` / ``windows``), ``bypassed`` (a proxy is
    set, but the hub's host is in its exceptions), ``socks`` (a SOCKS proxy, which the monitor cannot use), ``pac``."""
    u = urllib.parse.urlsplit(hub_url or "https://hub.invalid")
    host = u.hostname or ""
    env = environment_proxies(environ, exported)
    if env:
        proxies, source = env, "environment"
        bypass = lambda h: urllib.request.proxy_bypass_environment(h, env)
    else:
        proxies, bypass = registry()
        source = "windows" if proxies else None
    p = proxies.get(u.scheme or "https")
    bypassed = bool(p) and bool(hub_url) and bool(bypass(host))
    if p and "://" not in p:
        p = "http://" + p
    system = None if not p or bypassed else p
    return {"system": mask_proxy(system), "source": source if p else None, "bypassed": bypassed,
            "socks": bool(system and system.lower().startswith("socks")), "pac": pac}


def trust(st: Settings) -> dict[str, Any]:
    """``bundle`` (``hub_ca_bundle``), ``system`` (the OS store through ``truststore``) or ``python`` (the ``ssl``
    default), as ``desk.marketdata.hub.tls_context`` itself turns out: truststore installed but unusable is
    ``python``. Building the context reads nothing from the network."""
    from .marketdata.hub import tls_context

    if st.hub_ca_bundle:
        return {"kind": "bundle"}
    ctx = tls_context(None)
    return {"kind": "system" if type(ctx).__module__.split(".")[0] == "truststore" else "python"}


# ---- Python for the acquisition -------------------------------------------------------------------------------------


def _version(path: str, tag: str | None = None) -> str | None:
    m = re.fullmatch(r"(\d)\.(\d{1,2}).*", tag or "") or _PY_DIR.search(path)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def registry_pythons() -> list[tuple[str, str]]:
    """``(python.exe, tag)`` of the PEP 514 registry entries (``Software\\Python\\<company>\\<tag>\\InstallPath``),
    the current user's and the computer's, 64- and 32-bit views; [] outside Windows."""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    out: list[tuple[str, str]] = []

    def keys(k: Any) -> Iterator[str]:
        i = 0
        while True:
            try:
                yield winreg.EnumKey(k, i)
            except OSError:
                return
            i += 1

    for hive, view in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            root = winreg.OpenKey(hive, r"Software\Python", 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            for company in keys(root):
                if company == "PyLauncher":
                    continue
                try:
                    ck = winreg.OpenKey(root, company)
                except OSError:
                    continue
                with ck:
                    for tag in keys(ck):
                        try:
                            with winreg.OpenKey(ck, tag + r"\InstallPath") as ip:
                                try:
                                    exe = winreg.QueryValueEx(ip, "ExecutablePath")[0]
                                except OSError:
                                    exe = os.path.join(winreg.QueryValueEx(ip, "")[0], "python.exe")
                        except OSError:
                            continue
                        out.append((str(exe), tag))
    return out


def python_globs(environ: dict[str, str]) -> list[str]:
    """Usual install folders: python.org (per user and for all users), Anaconda and Miniconda."""
    if os.name != "nt":
        return []
    lad, pf = environ.get("LOCALAPPDATA"), environ.get("ProgramFiles")
    prof, pd = environ.get("USERPROFILE"), environ.get("ProgramData")
    pats = [os.path.join(lad, "Programs", "Python", "Python3*", "python.exe") if lad else None,
            os.path.join(pf, "Python3*", "python.exe") if pf else None]
    for base in (prof, pd):
        if base:
            pats += [os.path.join(base, d, "python.exe") for d in ("Anaconda3", "anaconda3", "Miniconda3", "miniconda3")]
    return [p for p in pats if p]


def pythons(environ: dict[str, str], disk: Disk, *, registry: Callable[[], list[tuple[str, str]]] = registry_pythons,
            own: str | None = None) -> list[dict[str, Any]]:
    """Interpreters on local disks, by where they were found (``path``: on PATH, ``registry``, ``folder``; this
    program's own last, ``own``); the Microsoft Store's stub (``WindowsApps``) is not one. None is run, PATH folders
    on network shares are not searched."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(path: str | None, source: str, tag: str | None = None) -> None:
        if not path or "windowsapps" in path.lower() or len(found) >= MAX_SUGGESTIONS:
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not disk.is_file(path):
            return
        seen.add(key)
        found.append({"path": path, "source": source, "version": _version(path, tag), "own": False})

    search = os.pathsep.join(d for d in (environ.get("PATH") or "").split(os.pathsep) if d and not disk.network(d))
    for name in ("python", "python3"):
        add(shutil.which(name, path=search) if search else None, "path")
    for exe, tag in registry():
        add(exe, "registry", tag)
    for pat in python_globs(environ):
        if not disk.network(pat):
            for p in sorted(glob.glob(pat), reverse=True):
                add(p, "folder")
    own = own if own is not None else sys.executable
    own_key = os.path.normcase(os.path.realpath(own)) if own else None
    found = [f for f in found if os.path.normcase(os.path.realpath(f["path"])) != own_key]
    if own and disk.is_file(own):
        found.append({"path": own, "source": "own", "version": _version(own) or f"{sys.version_info[0]}.{sys.version_info[1]}",
                      "own": True})
    return found


# ---- report folders -----------------------------------------------------------------------------------------------


def _norm(path: str) -> str:
    """A folder as a comparable key: no ``\\**`` marker, no trailing separator, the case of Windows paths folded."""
    p = re.sub(r"[\\/]\*\*$", "", path.strip()).rstrip("\\/")
    win = bool(re.match(r"^[A-Za-z]:", p)) or "\\" in p
    return str(PureWindowsPath(p)).lower() if win else p


def covered(folder: str, dirs: list[tuple[str, bool]]) -> bool:
    """``folder`` is a report folder, or inside a recursive one. ``dirs``: ``(path, recursive)``."""
    key = _norm(folder)
    for d, recursive in dirs:
        dk = _norm(d)
        if key == dk or (recursive and key.startswith(dk.rstrip("\\/") + ("\\" if "\\" in dk or ":" in dk else "/"))):
            return True
    return False


def _strings(doc: Any, at: str = "") -> Iterator[tuple[str, str]]:
    """``(key path, string)`` of every string in a JSON document (``report_dirs[0]``, ``sources.pos.folder``)."""
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield from _strings(v, f"{at}.{k}" if at else str(k))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from _strings(v, f"{at}[{i}]")
    elif isinstance(doc, str):
        yield at, doc


def config_folders(config_dir: Path | None, dirs: list[tuple[str, bool]], disk: Disk) -> list[dict[str, Any]]:
    """Absolute folder paths in the adapter's ``*.json`` config files that no report folder covers. A local folder
    must exist; one on a network share is listed unchecked (``exists`` null)."""
    if config_dir is None or disk.is_dir(str(config_dir)) is not True:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in sorted(config_dir.glob("*.json"))[:MAX_CONFIG_FILES]:
        try:
            if f.stat().st_size > MAX_CONFIG_BYTES:
                continue
            doc = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for key, value in _strings(doc):
            v = value.strip()
            if not _ABS.match(v) or "\n" in v or len(v) > 400 or _NOT_INPUT.search(key):
                continue
            recursive = bool(re.search(r"[\\/]\*\*$", v))
            path = re.sub(r"[\\/]\*\*$", "", v)
            if _norm(path) in seen or covered(path, dirs):
                continue
            seen.add(_norm(path))  # a missing folder is not asked again either
            exists = disk.is_dir(path)
            if exists is None and not disk.network(path):
                return out  # out of checks
            if exists is False:
                continue
            out.append({"path": path, "recursive": recursive, "file": f.name, "key": key, "exists": exists})
            if len(out) >= MAX_SUGGESTIONS:
                return out
    return out


ACCEPTED_FOLDERS = r"""
SELECT regexp_replace(f.path, '[\\/][^\\/]*$', '') AS folder, count(*) AS files, max(s.report_date) AS last_report
FROM inbox_file f
JOIN LATERAL (SELECT status, report_date FROM inbox_file_status x WHERE x.sha256 = f.sha256
              ORDER BY status_id DESC LIMIT 1) s ON true
WHERE s.status IN ('accepted', 'partially_accepted')
GROUP BY 1 ORDER BY 2 DESC LIMIT 50"""


def accepted_folders(rows: list[dict[str, Any]], st: Settings, dirs: list[tuple[str, bool]],
                     disk: Disk) -> list[dict[str, Any]]:
    """Folders of files the inbox accepted (``ACCEPTED_FOLDERS``) that no report folder covers now, without the
    adapter's output folder and the archive (the inbox reads those itself); ``exists`` null: not checked."""
    own = [(str(st.adapter_output_dir), True), (str(st.inbox_archive), True)]
    keep = [r for r in rows if r.get("folder") and not covered(r["folder"], dirs) and not covered(r["folder"], own)]
    out = []
    for r in keep[:MAX_SUGGESTIONS]:
        last = r.get("last_report")
        out.append({"path": r["folder"], "files": int(r.get("files") or 0),
                    "last_report": last.isoformat() if hasattr(last, "isoformat") else last,
                    "exists": disk.is_dir(r["folder"])})
    return out


def report_dirs(stored: dict[str, Any]) -> list[tuple[str, bool]]:
    """``(path, recursive)`` of settings.json's ``inbox_dirs``, as the page lists them."""
    out = []
    for d in stored.get("inbox_dirs") or []:
        if isinstance(d, dict) and isinstance(d.get("path"), str) and d["path"].strip():
            out.append((d["path"].strip(), bool(d.get("recursive"))))
    return out


# ---- all ----------------------------------------------------------------------------------------------------------


def detect(st: Settings, stored: dict[str, Any], *, environ: dict[str, str] | None = None,
           exported: dict[str, str] | None = None,
           registry_proxy: Callable[[], tuple[dict[str, str], Callable[[str], bool]]] = registry_proxies,
           registry: Callable[[], list[tuple[str, str]]] = registry_pythons, own_python: str | None = None,
           accepted: list[dict[str, Any]] | None = None, curl: Callable[[], str | None] | None = None,
           disk: Disk | None = None) -> dict[str, Any]:
    """The findings (see the module doc). The arguments after ``stored`` stand for the computer in tests."""
    from .marketdata.hub import find_curl

    env = dict(os.environ if environ is None else environ)
    if exported is None:
        from . import settings as _settings

        exported = dict(_settings._exported)
    disk = disk or Disk()
    dirs = report_dirs(stored)
    if not dirs and not isinstance(stored.get("inbox_dirs"), list):  # DESK_INBOX_DIRS or the env file
        dirs = [(str(d), False) for d in st.inbox_dirs]
    base = Settings(home=st.home)  # what an empty field gives
    acq = str(st.acquisition_python) if st.acquisition_python else None
    return {
        "proxy": proxy(st.hub_url, env, exported, registry_proxy, pac_script()),
        "trust": trust(st),
        "curl": (curl or find_curl)(),
        "python": pythons(env, disk, registry=registry, own=own_python),
        "acquisition_python": {"path": acq, "exists": disk.is_file(acq) if acq else None},
        "defaults": {"snapshot_cache": str(base.snapshot_cache), "inbox_archive": str(base.inbox_archive),
                     "private_adapter_config": str(st.home / ADAPTER_CONFIG_DIR),
                     "private_adapter": st.private_adapter if st.private_adapter_default else None},
        "inbox": {"from_adapter_config": config_folders(st.private_adapter_config, dirs, disk),
                  "from_accepted": accepted_folders(accepted or [], st, dirs, disk)},
    }
