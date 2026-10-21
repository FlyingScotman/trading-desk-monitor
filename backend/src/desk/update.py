"""Hot update of the Windows bundle from the settings page («Обновление программы»).

An update zip is what ``backend/tools/build_windows_bundle.py`` writes: a full bundle ``DeskMonitor-<version>.zip``
or a patch ``DeskMonitor-patch-<old>-to-<new>.zip`` (``--patch-base``), every file under ``DeskMonitor/``, with
``VERSION`` and ``MANIFEST.sha256`` (the sha256 of every file of the new bundle). The owner drops zips into the
settings' ``update_dir`` (a network share, say) or uploads one from the browser (``<home>/updates/uploads/``).

``stage`` checks a zip against the running bundle (``DESK_BUNDLE_ROOT``, set by the launcher) and extracts the files
that differ from the installed ones into ``<home>/updates/staged/<version>/files/``, each checked against the
manifest, with ``update.json`` (the files to write and to delete); ``request`` writes ``<home>/updates/pending.json``
and ``desk serve`` exits with ``DESK_RESTART_CODE``. The launcher (``app\\deskctl.py start``) applies it, starts the
new version and rolls back when it does not answer; ``<home>/updates/applying.json`` and ``last.json`` are its.
This process never writes into the bundle folder.

Checks: safe member paths only (relative, no ``..``, drive, backslash, link, reserved Windows name), the bundle's
layout (``app/``, ``wheelhouse/``, ``adapter/``, ``python/`` and the root files), size limits, a full zip complete
and a patch made for the installed files (the manifest's unchanged entries match the installed manifest).
``python/`` is never replaced while it runs the launcher: a zip that changes it is refused (the manual way).
A full zip without ``adapter/`` keeps the installed adapter, and so does a patch built from a bundle without one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .settings import atomic_write_text

TOP = "DeskMonitor"
RESTART_ENV = "DESK_RESTART_CODE"  # set by deskctl for the desk serve it starts again on that exit code
MAX_ZIP_BYTES = 1 << 30
MAX_UNPACKED_BYTES = 3 << 30
MAX_FILES = 20_000
MAX_TEXT_BYTES = 4 << 20  # VERSION, MANIFEST.sha256, PATCH_*.txt
KEEP_UPLOADS = 5
TOP_DIRS = ("app", "wheelhouse", "adapter", "python")
REPLACED_DIRS = ("app", "wheelhouse")  # a full zip replaces these whole; adapter/ only when it brings one
PATCH_NOTES = ("PATCH_README.txt", "PATCH_DELETE.txt", "PATCH_BASE.txt")  # describe a patch, never installed
NEEDED = ("app/deskctl.py", "wheelhouse/requirements.txt", "start.bat", "VERSION")
ROOT_FILE = re.compile(r"[A-Za-z0-9_-]+\.(?:bat|txt|md)|VERSION|MANIFEST\.sha256")
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,99}")
_RESERVED = re.compile(r"(?i)(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?")
_VERSION_KEY = re.compile(r"(\d+(?:\.\d+)*)-(\d{8})-[0-9a-f]{7,40}(?:-dirty(\d{12}))?")
_SHA = re.compile(r"[0-9a-f]{64}")
_UPLOAD_CHARS = re.compile(r"[^A-Za-z0-9._+-]")


class UpdateError(ValueError):
    """Why a zip cannot be installed, in the owner's words (the settings page shows it)."""


# ---- where and what runs ----------------------------------------------------------------------------------


def updates_dir(home: Path) -> Path:
    return home / "updates"


def uploads_dir(home: Path) -> Path:
    return updates_dir(home) / "uploads"


def bundle_root() -> Path | None:
    """The bundle folder this server runs from (``DESK_BUNDLE_ROOT``, set by deskctl); None in a checkout."""
    value = os.environ.get("DESK_BUNDLE_ROOT")
    root = Path(value) if value else None
    return root if root is not None and (root / "VERSION").is_file() else None


def restart_code() -> int | None:
    """The exit code the launcher restarts ``desk serve`` on; None when not started by ``start.bat``."""
    value = os.environ.get(RESTART_ENV, "").strip()
    return int(value) if value.isdigit() else None


def current_version() -> str:
    """The bundle's ``VERSION`` (``DESK_BUNDLE_VERSION``), else the package version of a checkout."""
    return os.environ.get("DESK_BUNDLE_VERSION", "").strip() or f"{__version__}-dev"


def unavailable() -> str | None:
    """Why this server cannot install an update itself; None when it can."""
    if bundle_root() is None:
        return ("Сервер запущен не из программы для Windows (среда разработки): установка из интерфейса недоступна, "
                "обновляйте код как обычно (git pull).")
    if restart_code() is None:
        return ("Сервер запущен не через start.bat: установка из интерфейса недоступна. "
                "Остановите его и запустите start.bat.")
    return None


def version_key(v: str) -> tuple | None:
    """``0.1.0-20261019-e7410dc[-dirty202610200705]`` as (version numbers, commit date, dirty build time)."""
    m = _VERSION_KEY.fullmatch(v)
    return (tuple(int(x) for x in m[1].split(".")), m[2], m[3] or "") if m else None


def compare(new: str, current: str) -> str:
    """``same``, ``newer``, ``older``, or ``other`` (no order: two builds of one day, an unknown format)."""
    if new == current:
        return "same"
    a, b = version_key(new), version_key(current)
    if a is None or b is None or a == b:
        return "other"
    return "newer" if a > b else "older"


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- reading a zip ----------------------------------------------------------------------------------------


def _bad_path(rel: str) -> bool:
    """Not a plain relative path inside the bundle (also what Windows would refuse or read otherwise)."""
    if not rel or "\\" in rel or ":" in rel or rel.startswith("/") or any(ord(c) < 32 for c in rel):
        return True
    return any(p in ("", ".", "..") or p != p.rstrip(". ") or _RESERVED.fullmatch(p) for p in rel.split("/"))


def parse_manifest(text: str) -> dict[str, str]:
    """``MANIFEST.sha256``: ``<sha256>  <path>`` lines, ``#`` comments."""
    out = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        digest, sep, rel = line.partition("  ")
        if not sep or not _SHA.fullmatch(digest) or _bad_path(rel):
            raise UpdateError(f"MANIFEST.sha256 повреждён: {line[:120]!r}")
        out[rel] = digest
    return out


@dataclass
class Contents:
    version: str
    kind: str  # "full" or "patch"
    base: str | None  # a patch: the version it was made from
    members: dict[str, zipfile.ZipInfo]  # bundle path -> member (the patch notes left out)
    manifest: dict[str, str]  # every file of the new bundle -> sha256
    delete: list[str]  # a patch: the files the new version no longer has

    @property
    def adapter(self) -> bool:
        return any(rel.startswith("adapter/") for rel in self.manifest)


def open_zip(path: Path) -> zipfile.ZipFile:
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise UpdateError(f"Архив больше {MAX_ZIP_BYTES >> 20} МБ.")
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, EOFError) as e:
        raise UpdateError(f"Это не zip-архив или он повреждён (докачался не до конца?): {e}") from None


def _text(z: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    if info.file_size > MAX_TEXT_BYTES:
        raise UpdateError(f"{info.filename}: слишком большой файл.")
    try:
        return z.read(info).decode("utf-8-sig")
    except (zipfile.BadZipFile, UnicodeDecodeError, OSError) as e:
        raise UpdateError(f"{info.filename} не читается: {e}") from None


def read_contents(z: zipfile.ZipFile) -> Contents:
    """The zip's layout, version and manifest, checked; UpdateError on anything an update zip does not hold."""
    infos = z.infolist()
    if len(infos) > MAX_FILES:
        raise UpdateError(f"В архиве больше {MAX_FILES} файлов: это не архив Desk Monitor.")
    members: dict[str, zipfile.ZipInfo] = {}
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if _bad_path(name.rstrip("/") if info.is_dir() else name):
            raise UpdateError(f"Недопустимый путь в архиве: {name!r}")
        if info.flag_bits & 1:
            raise UpdateError("Архив зашифрован.")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise UpdateError(f"Ссылка в архиве: {name}")
        if info.is_dir():
            continue
        if not name.startswith(TOP + "/"):
            raise UpdateError(f"Это не архив Desk Monitor: всё должно лежать в папке {TOP}/ (а здесь {name}).")
        rel = name[len(TOP) + 1:]
        if rel.casefold() in seen:
            raise UpdateError(f"Файл дважды в архиве: {name}")
        seen.add(rel.casefold())
        if ("/" in rel and rel.split("/")[0] not in TOP_DIRS) or ("/" not in rel and not ROOT_FILE.fullmatch(rel)):
            raise UpdateError(f"Лишний файл в архиве: {name} (в сборке Desk Monitor такого нет).")
        total += info.file_size
        members[rel] = info
    if total > MAX_UNPACKED_BYTES:
        raise UpdateError(f"Распакованный архив больше {MAX_UNPACKED_BYTES >> 30} ГБ.")
    if "VERSION" not in members:
        raise UpdateError("В архиве нет файла VERSION: это не архив Desk Monitor.")
    version = _text(z, members["VERSION"]).strip()
    if not VERSION_RE.fullmatch(version):
        raise UpdateError(f"Непонятная версия в VERSION: {version[:60]!r}")
    if "MANIFEST.sha256" not in members:
        raise UpdateError("В архиве нет MANIFEST.sha256: соберите его build_windows_bundle.py.")
    manifest = parse_manifest(_text(z, members["MANIFEST.sha256"]))
    patch = "PATCH_README.txt" in members or "PATCH_BASE.txt" in members
    base, delete = None, []
    if patch:
        if "PATCH_BASE.txt" in members:
            base = _text(z, members["PATCH_BASE.txt"]).strip() or None
        elif m := re.search(r"обновление (\S+) -> (\S+)", _text(z, members["PATCH_README.txt"])):
            base = m[1]
        if "PATCH_DELETE.txt" in members:
            delete = [ln.strip() for ln in _text(z, members["PATCH_DELETE.txt"]).splitlines() if ln.strip()]
        bad = [rel for rel in delete if _bad_path(rel) or rel.split("/")[0] == "env"]
        if bad:
            raise UpdateError(f"Недопустимый путь в PATCH_DELETE.txt: {bad[0]!r}")
    for note in PATCH_NOTES:
        members.pop(note, None)
    extra = [rel for rel in members if rel != "MANIFEST.sha256" and rel not in manifest]
    if extra:
        raise UpdateError(f"Файла {extra[0]} нет в MANIFEST.sha256 архива: архив собран не build_windows_bundle.py "
                          "или изменён после сборки.")
    missing = [rel for rel in NEEDED if rel not in manifest]
    if missing:
        raise UpdateError(f"Это не сборка Desk Monitor для Windows: в ней нет {missing[0]}.")
    if not patch:
        absent = sorted(set(manifest) - set(members))
        if absent:
            raise UpdateError(f"Архив неполный: нет {len(absent)} файлов из MANIFEST.sha256, например {absent[0]}.")
    return Contents(version, "patch" if patch else "full", base, members, manifest, delete)


# ---- fitting it to the installed bundle ----------------------------------------------------------------------


def installed_version(root: Path) -> str:
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def installed_manifest(root: Path) -> dict[str, str] | None:
    """The installed bundle's ``MANIFEST.sha256`` (a hot update keeps it current); None without a readable one."""
    try:
        return parse_manifest((root / "MANIFEST.sha256").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, UpdateError):
        return None


def _installed(root: Path, rel: str, manifest: dict[str, str] | None) -> str | None:
    """sha256 of the installed ``rel``: from the installed manifest, else from the file itself."""
    if manifest is not None:
        return manifest.get(rel)
    p = root / rel
    return file_sha256(p) if p.is_file() else None


def check_fits(c: Contents, root: Path) -> None:
    """UpdateError when ``c`` cannot be installed over the bundle in ``root`` by the launcher."""
    current = installed_version(root)
    if c.version == current:
        raise UpdateError(f"Версия {current} уже установлена.")
    have = installed_manifest(root)
    python = [rel for rel in [*c.members, *c.delete] if rel.startswith("python/")]
    if c.kind == "full":
        new_py = {rel: d for rel, d in c.manifest.items() if rel.startswith("python/")}
        python = [rel for rel, d in new_py.items() if _installed(root, rel, have) != d] if new_py else []
        if new_py and have is not None:
            python += [rel for rel in have if rel.startswith("python/") and rel not in new_py]
    if python:
        raise UpdateError("В этом архиве другая версия Python (папка python\\), а её нельзя заменить, пока программа "
                          "работает. Обновите вручную: stop.bat, распакуйте архив поверх папки программы, start.bat.")
    if c.kind == "patch":
        stale = [rel for rel, d in c.manifest.items() if rel not in c.members and _installed(root, rel, have) != d]
        if stale:
            raise UpdateError(f"Заплатка сделана для версии {c.base or '(неизвестно)'}, а установлена {current} "
                              f"(не совпадает, например, {stale[0]}). Нужна заплатка от {current} или полный архив.")


def plan(c: Contents, root: Path) -> tuple[list[str], list[str]]:
    """(the files to write: those that differ from the installed ones, MANIFEST.sha256 always; the files to delete:
    a full zip's replaced folders and root files it no longer has, a patch's PATCH_DELETE.txt and the installed
    manifest's files the new one no longer lists). ``python/`` never."""
    write = [rel for rel in c.members if not rel.startswith("python/") and
             (rel == "MANIFEST.sha256" or not (root / rel).is_file() or file_sha256(root / rel) != c.manifest[rel])]
    known = installed_manifest(root) or {}
    if c.kind == "patch":
        # a patch built from a bundle without the private adapter keeps the installed one, as a full zip does
        keeps_adapter = not any(rel.startswith("adapter/") for rel in c.manifest)
        gone = {*c.delete, *(rel for rel in known if rel not in c.manifest
                             and not (keeps_adapter and rel.startswith("adapter/")))}
        return write, sorted(rel for rel in gone if not rel.startswith("python/") and (root / rel).is_file())
    delete = []
    for d in (*REPLACED_DIRS, *(("adapter",) if c.adapter else ())):
        for p in sorted((root / d).rglob("*")) if (root / d).is_dir() else ():
            rel = p.relative_to(root).as_posix()
            if p.is_file() and "__pycache__" not in rel.split("/") and rel not in c.manifest:
                delete.append(rel)
    # root files of the old bundle only, never the owner's own
    delete += sorted(rel for rel in known if "/" not in rel and rel not in c.manifest and (root / rel).is_file())
    return write, delete


def _extract(z: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, want: str | None) -> str:
    """Extract one member to ``dest``; its sha256, checked against ``want`` (the manifest's) when given."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    try:
        with z.open(info) as src, open(dest, "wb") as out:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                h.update(chunk)
                out.write(chunk)
    except (zipfile.BadZipFile, EOFError) as e:
        raise UpdateError(f"Файл {info.filename} в архиве повреждён: {e}") from None
    if want is not None and h.hexdigest() != want:
        raise UpdateError(f"Файл {info.filename} в архиве повреждён: sha256 не совпадает с MANIFEST.sha256.")
    return h.hexdigest()


def stage(zip_path: Path, root: Path, home: Path) -> dict[str, Any]:
    """Check ``zip_path`` against the bundle in ``root`` and extract the files the update writes into
    ``<home>/updates/staged/<version>/files/``, with ``update.json`` for the launcher; returns that document plus
    ``staged`` (the folder). UpdateError when the zip cannot be installed."""
    with open_zip(zip_path) as z:
        c = read_contents(z)
        check_fits(c, root)
        write, delete = plan(c, root)
        dest = updates_dir(home) / "staged" / c.version
        if dest.exists():
            shutil.rmtree(dest)
        try:
            files = {rel: _extract(z, c.members[rel], dest / "files" / rel,
                                   None if rel == "MANIFEST.sha256" else c.manifest[rel]) for rel in write}
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
    doc = {"version": c.version, "from_version": installed_version(root), "kind": c.kind, "base_version": c.base,
           "source": zip_path.name, "files": files, "delete": delete,
           "created_at": datetime.now().isoformat(timespec="seconds")}
    atomic_write_text(dest / "update.json", json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return {**doc, "staged": str(dest)}


def request(staged: dict[str, Any], home: Path) -> None:
    """Ask the launcher to install ``staged`` (``stage``'s result) once desk serve exits."""
    keys = ("version", "from_version", "kind", "source", "staged")
    doc = {**{k: staged[k] for k in keys}, "requested_at": datetime.now().isoformat(timespec="seconds")}
    atomic_write_text(updates_dir(home) / "pending.json", json.dumps(doc, ensure_ascii=False, indent=1) + "\n")


def state(home: Path) -> dict[str, Any]:
    """``pending`` (asked, the launcher has not taken it), ``applying`` (applied, the new version not confirmed),
    ``last`` (the launcher's last outcome: ok, version, from_version, message, at); None each when absent."""
    out: dict[str, Any] = {}
    for key in ("pending", "applying", "last"):
        try:
            doc = json.loads((updates_dir(home) / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        out[key] = doc if isinstance(doc, dict) else None
    return out


# ---- the list on the settings page ------------------------------------------------------------------------------

_described: dict[tuple, dict[str, Any]] = {}  # (path, size, mtime, installed version, root) -> describe()


def describe(path: Path, root: Path | None, current: str) -> dict[str, Any]:
    """A zip for the list: name, size, modified, version, kind, base_version, adapter, compare, and ``error`` (None
    when it can be installed here; ``root`` None checks the zip alone)."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns, current, str(root))
    if key in _described:
        return dict(_described[key])
    item: dict[str, Any] = {"name": path.name, "size": st.st_size,
                            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                            "version": None, "kind": None, "base_version": None, "adapter": None, "compare": None,
                            "error": None}
    try:
        with open_zip(path) as z:
            c = read_contents(z)
            item.update(version=c.version, kind=c.kind, base_version=c.base, adapter=c.adapter,
                        compare=compare(c.version, current))
            if root is not None:
                check_fits(c, root)
    except UpdateError as e:
        item["error"] = str(e)
    except OSError as e:
        item["error"] = f"Не удалось прочитать архив: {e}"
    _described[key] = item
    return dict(item)


def list_zips(folder: Path, root: Path | None, current: str, limit: int = 50) -> list[dict[str, Any]]:
    """The ``*.zip`` of ``folder`` (not its subfolders), newest first; UpdateError when it cannot be read."""
    try:
        zips = [p for p in folder.iterdir() if p.suffix.lower() == ".zip" and p.is_file()]
    except FileNotFoundError:
        raise UpdateError(f"Папки {folder} нет (или нет доступа).") from None
    except OSError as e:
        raise UpdateError(f"Папка {folder} не читается: {e}") from None
    zips.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [describe(p, root, current) for p in zips[:limit]]


def find_zip(source: str, name: object, update_dir: Path | None, home: Path) -> Path:
    """The zip ``name`` of the update folder (``source`` "dir") or of the uploads ("upload"): a plain file name."""
    if source == "dir":
        if update_dir is None:
            raise UpdateError("Папка обновлений не задана в настройках.")
        folder = update_dir
    elif source == "upload":
        folder = uploads_dir(home)
    else:
        raise UpdateError("source: dir или upload")
    if not isinstance(name, str) or not name.lower().endswith(".zip") or Path(name).name != name or "\\" in name:
        raise UpdateError("name: имя zip-файла без папки")
    path = folder / name
    if not path.is_file():
        raise UpdateError(f"Файла {name} нет в {folder}.")
    return path


def upload_name(name: str) -> str:
    """A safe file name for an uploaded zip (its own name, other characters as ``_``)."""
    base = re.split(r"[\\/]", name or "")[-1].strip()
    if not base.lower().endswith(".zip"):
        raise UpdateError("Нужен zip-архив (файл *.zip).")
    safe = _UPLOAD_CHARS.sub("_", base)
    return ("_" + safe if safe.startswith(".") else safe)[-150:]


def prune_uploads(home: Path, keep: int = KEEP_UPLOADS) -> None:
    folder = uploads_dir(home)
    zips = sorted((p for p in folder.glob("*.zip") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in zips[keep:]:
        p.unlink(missing_ok=True)
