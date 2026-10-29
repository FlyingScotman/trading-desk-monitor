"""What the settings page finds out by itself on this computer (#449): ``GET /api/v1/settings/detect``.

Only local reads, no network and no side effect: the process environment, the Windows registry (``winreg``, the
standard library), files and folders on local disks and the store. Nothing here is written to settings.json; the
page shows each finding as «определено автоматически» (a setting left empty already uses it) or as a suggestion
the user takes with a click.

* ``proxy``    the proxy the hub requests take when ``https_proxy`` is empty: the real environment's
               ``HTTPS_PROXY``, else the Windows Internet Options (``ProxyEnable`` / ``ProxyServer``), and whether
               Windows uses an automatic configuration script (``AutoConfigURL``, a PAC file urllib cannot read)
* ``trust``    whose certificates the Python transport trusts (``desk.marketdata.hub.tls_context``)
* ``curl``     the curl the transport ``curl`` / ``auto`` would run (``find_curl``)
* ``python``   Python interpreters found on this computer for ``acquisition_python``: offered, never chosen
               (08 §2: the acquisition runtime is explicit); none of them is run
* ``adapter``  the bundle's adapter and the default config folder; ``defaults``: the folders an empty
               ``snapshot_cache``, ``inbox_archive`` and ``private_adapter_config`` give
* ``inbox``    folders to offer as report folders: absolute folder paths named in the adapter's config files (the
               adapter's own folder settings feed nothing to the inbox, #449) and folders whose files the inbox
               accepted before, both only when no report folder covers them yet

A folder on a network share (``\\\\server\\share``) is never touched: its existence is not checked.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path, PureWindowsPath
from typing import Any

from .settings import ADAPTER_CONFIG_DIR, PROXY_KEYS, Settings, mask_proxy

MAX_CONFIG_FILES = 20
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_SUGGESTIONS = 12
INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/]|/[^/])")
_PY_DIR = re.compile(r"python(\d)(\d{1,2})", re.IGNORECASE)
_NOT_INPUT = re.compile(r"output|archive|cache|backup|python|venv|(?<![a-z])(?:out|logs?|te?mp)(?![a-z])",
                        re.IGNORECASE)
"""Config keys of folders that are not report folders (where the adapter writes, its runtime, …)."""


# ---- proxy and trust ----------------------------------------------------------------------------------------------


def internet_settings() -> dict[str, Any]:
    """``ProxyEnable``, ``ProxyServer``, ``ProxyOverride`` and ``AutoConfigURL`` of the current user's Internet
    Options (what the browser and urllib read); {} outside Windows or when the key is missing."""
    if os.name != "nt":
        return {}
    try:
        import winreg
    except ImportError:
        return {}
    out: dict[str, Any] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS) as k:
            for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
                try:
                    out[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    pass
    except OSError:
        return {}
    return out


def registry_proxy(reg: dict[str, Any]) -> str | None:
    """The https proxy of ``ProxyServer`` when ``ProxyEnable`` is on (``host:port`` or ``https=host:port;http=…``,
    as ``urllib.request.getproxies_registry`` reads it)."""
    server = str(reg.get("ProxyServer") or "").strip()
    if not reg.get("ProxyEnable") or not server:
        return None
    if "=" in server:
        parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
        server = (parts.get("https") or parts.get("http") or "").strip()
        if not server:
            return None
    return server if "://" in server else "http://" + server


def proxy(environ: dict[str, str], stored: dict[str, Any], exported: dict[str, str],
          reg: dict[str, Any]) -> dict[str, Any]:
    """Where the hub's proxy comes from. ``stored``: settings.json; ``exported``: the variables ``load()`` set itself
    from it (``desk.settings._exported``), not the system's."""
    def real(var: str) -> str | None:
        for v in (var, var.lower()):
            value = environ.get(v)
            if value and exported.get(var) != value:
                return value
        return None

    windows_proxy = registry_proxy(reg)
    out: dict[str, Any] = {"settings": mask_proxy(stored.get("https_proxy")) if isinstance(stored.get("https_proxy"), str)
                           and stored["https_proxy"].strip() else None,
                           "environment": mask_proxy(real(PROXY_KEYS["https_proxy"])),
                           "no_proxy_environment": real(PROXY_KEYS["no_proxy"]),
                           "windows": mask_proxy(windows_proxy), "windows_bypass": reg.get("ProxyOverride") or None,
                           "pac": bool(reg.get("AutoConfigURL"))}
    # the order of desk.settings._export_proxy and urllib: the real environment, settings.json, Internet Options
    if out["environment"]:
        out["system"], out["source"] = out["environment"], "environment"
    elif windows_proxy:
        out["system"], out["source"] = out["windows"], "windows"
    else:
        out["system"], out["source"] = None, None
    out["effective_source"] = "environment" if out["environment"] else "settings" if out["settings"] else out["source"]
    return out


def trust(st: Settings) -> dict[str, Any]:
    """``bundle`` (``hub_ca_bundle``), ``system`` (the OS store through ``truststore``) or ``python`` (the ``ssl``
    default: ``certifi`` or OpenSSL's paths), as ``desk.marketdata.hub.tls_context`` chooses."""
    if st.hub_ca_bundle:
        return {"kind": "bundle", "path": str(st.hub_ca_bundle), "exists": st.hub_ca_bundle.is_file()}
    try:
        import truststore  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed, or an OS it does not support
        import ssl

        return {"kind": "python", "path": ssl.get_default_verify_paths().cafile, "exists": None}
    return {"kind": "system", "path": None, "exists": None}


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


def pythons(environ: dict[str, str], *, registry: Callable[[], list[tuple[str, str]]] = registry_pythons,
            own: str | None = None) -> list[dict[str, Any]]:
    """Interpreters on local disks, by where they were found (``path``: on PATH, ``registry``, ``folder``; this
    program's own last, ``own``); the Microsoft Store's stub (``WindowsApps``) is not one. None is run."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(path: str | None, source: str, tag: str | None = None) -> None:
        if not path or "windowsapps" in path.lower() or _network(path):
            return
        key = os.path.normcase(os.path.realpath(path))  # local paths only (see _network)
        if key in seen or not os.path.isfile(path):
            return
        seen.add(key)
        found.append({"path": path, "source": source, "version": _version(path, tag), "own": False})

    search = environ.get("PATH")
    for name in ("python", "python3"):
        add(shutil.which(name, path=search), "path")
    for exe, tag in registry():
        add(exe, "registry", tag)
    for pat in python_globs(environ):
        for p in sorted(glob.glob(pat), reverse=True):
            add(p, "folder")
    own = own if own is not None else sys.executable
    own_key = os.path.normcase(os.path.realpath(own)) if own else None
    found = [f for f in found if os.path.normcase(os.path.realpath(f["path"])) != own_key]
    if own and os.path.isfile(own):
        found.append({"path": own, "source": "own", "version": _version(own) or f"{sys.version_info[0]}.{sys.version_info[1]}",
                      "own": True})
    return found[:MAX_SUGGESTIONS]


# ---- report folders -----------------------------------------------------------------------------------------------


def _network(path: str) -> bool:
    return path.startswith(("\\\\", "//"))


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


def config_folders(config_dir: Path | None, dirs: list[tuple[str, bool]]) -> list[dict[str, Any]]:
    """Absolute folder paths in the adapter's ``*.json`` config files that no report folder covers. A local folder
    must exist; one on a network share is listed unchecked (``exists`` null)."""
    if config_dir is None or not config_dir.is_dir():
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
            exists = None if _network(path) else Path(path).is_dir()
            if exists is False:
                continue
            seen.add(_norm(path))
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


def accepted_folders(rows: list[dict[str, Any]], st: Settings, dirs: list[tuple[str, bool]]) -> list[dict[str, Any]]:
    """Folders of files the inbox accepted (``ACCEPTED_FOLDERS``) that no report folder covers now, without the
    adapter's output folder and the archive (the inbox reads those itself)."""
    own = [(str(st.adapter_output_dir), True), (str(st.inbox_archive), True)]
    out = []
    for r in rows:
        folder = r.get("folder") or ""
        if not folder or covered(folder, dirs) or covered(folder, own):
            continue
        last = r.get("last_report")
        out.append({"path": folder, "files": int(r.get("files") or 0),
                    "last_report": last.isoformat() if hasattr(last, "isoformat") else last,
                    "exists": None if _network(folder) else Path(folder).is_dir()})
    return out[:MAX_SUGGESTIONS]


def report_dirs(stored: dict[str, Any]) -> list[tuple[str, bool]]:
    """``(path, recursive)`` of settings.json's ``inbox_dirs``, as the page lists them."""
    out = []
    for d in stored.get("inbox_dirs") or []:
        if isinstance(d, dict) and isinstance(d.get("path"), str) and d["path"].strip():
            out.append((d["path"].strip(), bool(d.get("recursive"))))
    return out


# ---- all ----------------------------------------------------------------------------------------------------------


def detect(st: Settings, stored: dict[str, Any], *, environ: dict[str, str] | None = None,
           exported: dict[str, str] | None = None, reg: dict[str, Any] | None = None,
           registry: Callable[[], list[tuple[str, str]]] = registry_pythons, own_python: str | None = None,
           accepted: list[dict[str, Any]] | None = None, accepted_error: str | None = None,
           curl: Callable[[], str | None] | None = None) -> dict[str, Any]:
    """The findings (see the module doc). The arguments after ``stored`` stand for the computer in tests."""
    from .marketdata.hub import find_curl

    env = dict(os.environ if environ is None else environ)
    if exported is None:
        from . import settings as _settings

        exported = dict(_settings._exported)
    dirs = report_dirs(stored)
    if not dirs and not isinstance(stored.get("inbox_dirs"), list):  # DESK_INBOX_DIRS or the env file
        dirs = [(str(d), False) for d in st.inbox_dirs]
    default_cfg = st.home / ADAPTER_CONFIG_DIR
    base = Settings(home=st.home)  # what an empty field gives
    return {
        "proxy": proxy(env, stored, exported, internet_settings() if reg is None else reg),
        "trust": trust(st),
        "curl": (curl or find_curl)(),
        "python": pythons(env, registry=registry, own=own_python),
        "acquisition_python": {"path": str(st.acquisition_python) if st.acquisition_python else None,
                               "exists": st.acquisition_python.is_file() if st.acquisition_python else None},
        "defaults": {"snapshot_cache": str(base.snapshot_cache), "inbox_archive": str(base.inbox_archive),
                     "private_adapter_config": str(default_cfg)},
        "adapter": {"default": st.private_adapter if st.private_adapter_default else None,
                    "config_default": str(default_cfg), "config_default_exists": default_cfg.is_dir(),
                    "config_files": len(list(default_cfg.glob("*"))) if default_cfg.is_dir() else 0},
        "inbox": {"from_adapter_config": config_folders(st.private_adapter_config, dirs),
                  "from_accepted": accepted_folders(accepted or [], st, dirs), "accepted_error": accepted_error},
    }
