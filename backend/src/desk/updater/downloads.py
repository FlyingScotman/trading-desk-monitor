"""The browser's downloads folder: find the release zip the browser saved there and prove it is the one published.

The browser at work downloads from GitHub when the server cannot. The page starts the download, the server waits
for the file in the downloads folder (``downloads_dir``: the settings' ``update_downloads_dir``, else the Windows
known folder «Загрузки» from the registry, else ``~/Downloads``) and takes it only when its size and SHA-256 are
the release's. A browser that finds the name taken saves ``name (1).zip`` (Chrome, Edge) or ``name(1).zip``
(Firefox); while it downloads the file is ``name.zip.crdownload`` / ``name.zip.part`` (``partial``).

``scan_folder`` serves the page's «Найти в Загрузках» when even the GitHub API is out of reach: the owner saved the
zip and ``SHA256SUMS`` (or ``release.json``) from the release page by hand; a zip counts only when one of those
files lists its exact checksum.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .release import classify_name, parse_sums

KNOWN_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"  # FOLDERID_Downloads
PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp")
SCAN_LIMIT = 12  # the newest DeskMonitor zips scan_folder looks at


def windows_downloads() -> Path | None:
    """The user's Downloads known folder (redirected ones too: OneDrive, a network share), from the registry."""
    if os.name != "nt":
        return None
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, _ = winreg.QueryValueEx(k, KNOWN_DOWNLOADS)
        return Path(os.path.expandvars(str(value)))
    except OSError:
        return None


def downloads_dir(configured: Path | None) -> Path:
    if configured is not None:
        return configured
    return windows_downloads() or Path.home() / "Downloads"


def _copies(name: str) -> re.Pattern[str]:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    tail = r"\." + re.escape(ext) if ext else ""
    return re.compile(re.escape(stem) + r"(?: ?\(\d{1,3}\))?" + tail, re.IGNORECASE)


def copies(folder: Path, name: str) -> list[Path]:
    """``name`` and the browser's numbered copies of it in ``folder``, newest first."""
    pat = _copies(name)
    try:
        found = [p for p in folder.iterdir() if pat.fullmatch(p.name) and p.is_file()]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def partial(folder: Path, name: str) -> int | None:
    """Bytes a browser has written so far of ``name`` (its ``.crdownload`` / ``.part`` file), None when none."""
    best = None
    try:
        for p in folder.iterdir():
            low = p.name.lower()
            if low.startswith(name.lower()) and low.endswith(PARTIAL_SUFFIXES) and p.is_file():
                best = max(best or 0, p.stat().st_size)
    except OSError:
        return None
    return best


_hashes: dict[tuple[str, int, int], str] = {}


def sha256_of(p: Path) -> str:
    """SHA-256 of ``p``, remembered by (path, size, mtime): a large zip is read once."""
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    if key not in _hashes:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _hashes[key] = h.hexdigest()
    return _hashes[key]


@dataclass
class Found:
    state: str  # "waiting" (nothing yet), "downloading", "found", "mismatch" (a file of that name, other bytes)
    path: Path | None = None
    size: int | None = None
    message: str | None = None

    def public(self) -> dict[str, Any]:
        return {"state": self.state, "path": str(self.path) if self.path else None, "size": self.size,
                "message": self.message}


def find_expected(folder: Path, name: str, size: int | None, sha256: str) -> Found:
    """The copy of ``name`` in ``folder`` with exactly this size and SHA-256 (``found``); else what is there."""
    if not folder.is_dir():
        return Found("waiting", message=f"Папки {folder} нет: укажите папку загрузок браузера.")
    mismatch = None
    for p in copies(folder, name):
        try:
            got = p.stat().st_size
            if size is not None and got != size:
                mismatch = mismatch or Found("mismatch", p, got, f"{p.name}: размер {got} байт, в релизе {size} — "
                                                                 "скачан не до конца или другой файл.")
                continue
            if sha256_of(p) == sha256:
                return Found("found", p, got)
            mismatch = mismatch or Found("mismatch", p, got, f"{p.name}: контрольная сумма не совпадает с релизом "
                                                             "(файл повреждён или подменён); он не используется.")
        except OSError as e:
            mismatch = mismatch or Found("mismatch", p, None, f"{p.name} не читается: {e}")
    part = partial(folder, name)
    if part is not None:
        return Found("downloading", size=part)
    return mismatch or Found("waiting")


def _sums_files(folder: Path) -> dict[str, str]:
    """Checksums of every ``SHA256SUMS*`` and ``release*.json`` saved in ``folder``: asset name -> sha256."""
    out: dict[str, str] = {}
    try:
        files = sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime)
    except OSError:
        return out
    for p in files:
        low = p.name.lower()
        try:
            if low.startswith("sha256sums") and p.stat().st_size < 1 << 20:
                out.update(parse_sums(p.read_text(encoding="utf-8-sig", errors="replace")))
            elif low.startswith("release") and low.endswith(".json") and p.stat().st_size < 1 << 20:
                doc = json.loads(p.read_text(encoding="utf-8-sig"))
                for a in doc.get("assets", []) if isinstance(doc, dict) else []:
                    if isinstance(a, dict) and isinstance(a.get("name"), str) and isinstance(a.get("sha256"), str):
                        out[a["name"]] = a["sha256"].lower()
        except (OSError, ValueError):
            continue
    return out


def _original(name: str) -> str:
    """``DeskMonitor-x (1).zip`` -> ``DeskMonitor-x.zip``."""
    return re.sub(r" ?\(\d{1,3}\)(?=\.zip$)", "", name, flags=re.IGNORECASE)


def scan_folder(folder: Path, extra: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The newest DeskMonitor zips of ``folder``, each with ``verified`` (its SHA-256 is the one a saved
    ``SHA256SUMS`` / ``release.json`` or ``extra`` lists for its name) and the reason when not."""
    try:
        zips = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".zip"
                and classify_name(_original(p.name)) is not None]
    except OSError:
        return []
    zips.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    sums = {**_sums_files(folder), **(extra or {})}
    out = []
    for p in zips[:SCAN_LIMIT]:
        name = _original(p.name)
        kind = classify_name(name)
        assert kind is not None
        st = p.stat()
        item: dict[str, Any] = {"file": p.name, "name": name, "path": str(p), "size": st.st_size,
                                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                                "kind": kind[0], "version": kind[1], "base_version": kind[2], "verified": False,
                                "sha256": None, "reason": None}
        want = sums.get(name)
        if want is None:
            item["reason"] = "нет контрольной суммы: сохраните рядом SHA256SUMS из того же релиза"
        else:
            try:
                got = sha256_of(p)
            except OSError as e:
                item["reason"] = f"не читается: {e}"
            else:
                item["sha256"] = got
                item["verified"] = got == want
                if not item["verified"]:
                    item["reason"] = "контрольная сумма не совпадает с SHA256SUMS: файл повреждён или из другого релиза"
        out.append(item)
    return out
