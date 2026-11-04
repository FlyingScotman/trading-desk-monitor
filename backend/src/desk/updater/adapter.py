"""The private adapter's updater: its GitHub Releases, read with a GitHub token, installed on the owner's click.

The adapter repository (``Settings.adapter_update_repo``) is private. Its release workflow publishes, for a tag
``v<version>``, exactly three assets of fixed names (the release contract, format 1): ``tda-adapter.zip`` (the
package under ``src/<name>/``, ``README*`` and ``ADAPTER_RELEASE.json``: the version, SPI, contract, commit and the
SHA-256 of every other member), ``adapter-release.json`` (the version, the tag and the zip's size and SHA-256) and
``SHA256SUMS`` of both.

**Check** (``check``): through the server's layers (``desk.updater.net``; ``update_transport`` as for the monitor),
with ``Authorization: Bearer <github_token>`` on ``https://api.github.com/`` only: the latest release
(``/repos/<repo>/releases/latest``), then ``adapter-release.json`` and ``SHA256SUMS`` by the API's asset address
(``/repos/<repo>/releases/assets/<id>``, ``Accept: application/octet-stream``: a 302 to GitHub's file storage,
which never gets the token). The zip's checksum must agree between ``adapter-release.json``, ``SHA256SUMS`` and
the API's ``digest``. State: ``<home>/updates/adapter-remote.json``.

**Install** (``start_install``: a job of this process, only on the owner's click, never automatic): the server
downloads the zip by its asset address with the token and checks size and SHA-256 against the release; without a
token, or after every layer failed, the page starts a browser download of the release's ``tda-adapter.zip`` on
github.com (the owner's browser is signed in to GitHub) and the job watches the downloads folder
(``downloads.downloads_dir``) for ``tda-adapter.zip`` / ``tda-adapter (N).zip`` written after the job started. A
zip also comes from the page (``install_zip`` of an upload) or from «Найти в «Загрузках»» (``scan`` /
``install_found``).

Every zip, whatever its way in, is checked by ``verify_zip`` against its inner ``ADAPTER_RELEASE.json``: every member
listed with its SHA-256, nothing more, nothing missing, safe paths (relative, no ``..``, backslash, drive or
reserved name), no links, no encryption, size limits, exactly one top-level package (a folder with ``__init__.py``
not inside another one). ``install_zip`` unpacks it into a new folder ``<home>/adapter-releases/<version>/`` (the
package at ``<version>/<name>/``, with ``ADAPTER_RELEASE.json`` and the README files), loads it with the monitor's own
loader (``desk.inbox.loader.load_adapter``: an adapter for a newer contract or SPI is refused there, before anything
switches), and only then writes ``current.json`` (``adapter_store``) and drops the loaded handles
(``desk.inbox.scan.forget_adapters``) and the settings forms' warm child: the settings resolve the new package on
their next read, every adapter call starts a child process of it, so no restart is needed. A pinned fingerprint
(``private_adapter_sha256``) refuses the install: the new files could never load under it.

On Windows a folder or file is often "in use" for a moment: an antivirus or the indexer reads the files just
written, an Explorer window is open in the folder (TDM#519). So the install never renames, replaces or writes into a
folder that exists: a folder of that version with the same files (checked by SHA-256) is used as it is, otherwise
the release goes into a new folder ``<version>+<hex>`` (``adapter_store``). Replacing ``current.json`` and removing
files are retried for up to ``BUSY_WAIT_S`` with growing pauses; when it still fails, the page says what is busy and
what to do, ``update.log`` gets the error's type and code, and the adapter in use stays. An inbox scan already
running finishes with the adapter it started with (its children are never killed: that would fail its files); the
next scan starts the new one. Its folder stays: ``prune`` never removes the current or the previous folder, nor one
that a child of this process uses (``desk.inbox.runner.specs_in_use``), nor one it cannot rename aside at once.

Only a newer version than the one in use is installed unless ``allow_older``. The newest ``KEEP`` version folders
stay (never the current or the previous one). ``rollback("previous")`` switches back to the previous release,
``rollback("bundle")`` removes ``current.json`` (the bundle's ``adapter\\`` again). Installs and rollbacks are
logged to ``<home>/logs/update.log``; the token never is.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import update
from ..settings import Settings, adapter_config_dir, atomic_write_text
from . import adapter_store, downloads, net, service
from .release import _SHA, agree, parse_sums

ZIP_NAME = "tda-adapter.zip"
RELEASE_DOC = "adapter-release.json"
SUMS_NAME = "SHA256SUMS"
MANIFEST = "ADAPTER_RELEASE.json"
FORMAT = 1
STATE_FILE = "adapter-remote.json"
UPLOADS = "adapter-uploads"
API_ACCEPT = "application/vnd.github+json"
ASSET_ACCEPT = "application/octet-stream"
MAX_ZIP_BYTES = 256 << 20
MAX_UNPACKED_BYTES = 1 << 30
MAX_FILES = 20_000
MAX_DOC_BYTES = 1 << 20
KEEP = 3
SCAN_LIMIT = 12
WATCH_TIMEOUT_S = service.WATCH_TIMEOUT_S
WATCH_POLL_S = service.WATCH_POLL_S
ROOT_KEEP = ("readme", "license")  # root files of the zip copied next to the package (and ADAPTER_RELEASE.json)
ACTIVE = ("starting", "downloading", "verifying", "waiting_browser", "installing")
BUSY_WAIT_S = 15.0  # how long a file operation is retried while Windows says the file or folder is in use
BUSY_WINERRORS = (5, 32, 33)  # access denied, sharing violation, lock violation
COMPILE_TIMEOUT_S = 120.0  # the precompile of a new release folder (``_precompile``)

_lock = threading.RLock()
_install_lock = threading.Lock()
_job: dict[str, Any] | None = None
_cancel = threading.Event()


class AdapterUpdateError(ValueError):
    """Why an adapter release cannot be installed, in the owner's words. ``needs_confirm``: an older (or unordered)
    version, installed only when the owner confirms (``allow_older``)."""

    def __init__(self, message: str, *, needs_confirm: bool = False, version: str | None = None,
                 compare: str | None = None) -> None:
        super().__init__(message)
        self.needs_confirm, self.version, self.compare = needs_confirm, version, compare


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_sleep = time.sleep  # tests: no real pauses


def busy(e: OSError) -> bool:
    """Windows' "the file or folder is in use" (or POSIX's EACCES / EBUSY): worth another try a moment later."""
    return (isinstance(e, PermissionError) or getattr(e, "winerror", None) in BUSY_WINERRORS
            or e.errno in (errno.EACCES, errno.EBUSY))


def retry(fn: Any, budget_s: float | None = None) -> Any:
    """``fn()``, again while it fails with ``busy``: pauses from 0.1 s doubling up to 2 s, ``budget_s``
    (``BUSY_WAIT_S``) in all; then the last error."""
    budget = BUSY_WAIT_S if budget_s is None else budget_s
    pause, waited = 0.1, 0.0
    while True:
        try:
            return fn()
        except OSError as e:
            if not busy(e) or waited >= budget:
                raise
            d = min(pause, budget - waited)
            _sleep(d)
            waited, pause = waited + d, min(pause * 2, 2.0)


def os_code(e: OSError) -> str:
    """The error's type and code for the log and the page, without its path: ``PermissionError WinError 32``."""
    w = getattr(e, "winerror", None)
    code = f"WinError {w}" if w else f"errno {e.errno}" if e.errno else ""
    return f"{type(e).__name__} {code}".strip()


def busy_message(e: OSError, what: str, folder: Path) -> str:
    """What the owner reads when ``what`` failed on a file or folder in use, and what to do."""
    if not busy(e):
        return f"Новый адаптер не установлен: не удалось {what} ({os_code(e)}: {e.strerror or e}). " \
               "Работает прежний."
    held = f" Занят: {e.filename}." if e.filename else ""
    return (f"Новый адаптер не установлен: не удалось {what} — Windows отвечает, что файл или папка заняты другой "
            f"программой ({os_code(e)}).{held} Закройте окна Проводника, открытые в папке {folder}, подождите "
            "минуту и установите ещё раз. Не помогло — перезапустите программу (stop.bat, затем start.bat) и "
            "установите снова. Папку adapter комплекта при запущенной программе вручную не перезаписывайте. "
            "Работает прежний адаптер.")


def log(st: Settings, msg: str) -> None:
    """A line of ``<home>/logs/update.log`` (the launcher's log of updates); never the token."""
    try:
        st.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(st.logs_dir / "update.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {net.scrub(msg, st.github_token)}\n")
    except OSError:
        pass


# ---- state ------------------------------------------------------------------------------------------------------


def state_path(home: Path) -> Path:
    return update.updates_dir(home) / STATE_FILE


def read_state(home: Path) -> dict[str, Any]:
    try:
        doc = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_state(home: Path, changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        doc = {**read_state(home), **changes}
        atomic_write_text(state_path(home), json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        return doc


def context(st: Settings, timeout: float | None = None) -> net.Context:
    ctx = service.context(st, timeout)
    ctx.token = st.github_token
    return ctx


# ---- addresses ----------------------------------------------------------------------------------------------------


def api_latest_url(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}/releases/latest"


def api_asset_url(repo: str, asset_id: int) -> str:
    return f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}"


def browser_url(repo: str, tag: str | None = None) -> str:
    """The zip on github.com for a signed-in browser: of ``tag``, else of the latest release."""
    if tag:
        return f"https://github.com/{repo}/releases/download/{urllib.parse.quote(tag, safe='')}/{ZIP_NAME}"
    return f"https://github.com/{repo}/releases/latest/download/{ZIP_NAME}"


def releases_page(repo: str) -> str:
    return f"https://github.com/{repo}/releases/latest"


# ---- the release ----------------------------------------------------------------------------------------------


def _asset_ids(api: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for a in api.get("assets") or []:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and type(a.get("id")) is int and a["id"] > 0:
            out[a["name"]] = a
    return out


def _digest(a: dict[str, Any] | None) -> str | None:
    v = (a or {}).get("digest")
    return v.split(":", 1)[1].lower() if isinstance(v, str) and v.startswith("sha256:") else None


def parse_release(api: object, doc_bytes: bytes, sums_text: str | None, repo: str) -> dict[str, Any]:
    """The release as the state keeps it, from the API document, ``adapter-release.json`` and ``SHA256SUMS``;
    ``AdapterUpdateError`` when it does not read or the checksums disagree."""
    if not isinstance(api, dict) or not isinstance(api.get("tag_name"), str):
        raise AdapterUpdateError("ответ GitHub API не похож на релиз")
    if api.get("draft") or api.get("prerelease"):
        raise AdapterUpdateError("последний релиз адаптера — черновик или предварительный")
    try:
        doc = json.loads(doc_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise AdapterUpdateError(f"{RELEASE_DOC} не читается (не JSON)") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise AdapterUpdateError(f"{RELEASE_DOC} другого формата (нужен format {FORMAT}): обновите программу")
    version, asset = doc.get("version"), doc.get("asset")
    if not adapter_store.valid_version(version):
        raise AdapterUpdateError(f"{RELEASE_DOC}: непонятная версия {str(version)[:40]!r}")
    if not isinstance(asset, dict) or asset.get("name") != ZIP_NAME:
        raise AdapterUpdateError(f"{RELEASE_DOC}: нет описания {ZIP_NAME}")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= MAX_ZIP_BYTES:
        raise AdapterUpdateError(f"{RELEASE_DOC}: непонятный размер архива {size!r}")
    tag = api["tag_name"]
    if doc.get("tag") not in (None, tag):
        raise AdapterUpdateError(f"{RELEASE_DOC} описывает тег {doc.get('tag')!r}, а релиз — {tag}")
    ids = _asset_ids(api)
    if ZIP_NAME not in ids:
        raise AdapterUpdateError(f"в релизе {tag} нет {ZIP_NAME}")
    api_size = ids[ZIP_NAME].get("size")
    if type(api_size) is int and api_size != size:
        raise AdapterUpdateError(f"{ZIP_NAME}: размер в {RELEASE_DOC} {size}, на GitHub {api_size}")
    sums = parse_sums(sums_text or "")
    try:
        sha = agree(ZIP_NAME, asset.get("sha256") if isinstance(asset.get("sha256"), str) else None,
                    sums.get(ZIP_NAME), _digest(ids[ZIP_NAME]))
        doc_sha = hashlib.sha256(doc_bytes).hexdigest()
        agree(RELEASE_DOC, doc_sha, sums.get(RELEASE_DOC), _digest(ids.get(RELEASE_DOC)))
    except ValueError as e:
        raise AdapterUpdateError(str(e)) from None
    if sha is None:
        raise AdapterUpdateError(f"{ZIP_NAME}: нет контрольной суммы в релизе")
    s = lambda v: v if isinstance(v, str) else None
    return {"repo": repo, "tag": tag, "version": version, "commit": s(doc.get("commit")), "spi": doc.get("spi")
            if type(doc.get("spi")) is int else None, "contract": s(doc.get("contract")),
            "notes": (s(doc.get("notes")) or "")[:2000], "published_at": s(api.get("published_at")),
            "html_url": f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(tag, safe='')}",
            "asset": {"name": ZIP_NAME, "size": size, "sha256": sha, "id": ids[ZIP_NAME]["id"],
                      "browser_url": browser_url(repo, tag)},
            "sums_checked": bool(sums)}


def _release_via(layer: str, ctx: net.Context, repo: str) -> dict[str, Any] | None:
    try:
        api = net.get_json(layer, ctx, api_latest_url(repo), accept=API_ACCEPT, auth=True)
    except net.NetError as e:
        if e.code == "no_access":
            return None
        raise
    ids = _asset_ids(api) if isinstance(api, dict) else {}
    if RELEASE_DOC not in ids:
        raise AdapterUpdateError(f"в последнем релизе {api.get('tag_name') if isinstance(api, dict) else ''} нет "
                                 f"{RELEASE_DOC}: релиз собран не выпуском (Actions → release)")

    def asset(name: str, limit: int) -> bytes:
        r = net.fetch(layer, ctx, api_asset_url(repo, ids[name]["id"]), limit, accept=ASSET_ACCEPT, auth=True)
        try:
            return r.read()
        finally:
            r.path.unlink(missing_ok=True)

    doc = asset(RELEASE_DOC, MAX_DOC_BYTES)
    sums = None
    if SUMS_NAME in ids:
        sums = asset(SUMS_NAME, MAX_DOC_BYTES).decode("utf-8-sig", "replace")
    return parse_release(api, doc, sums, repo)


def check(st: Settings) -> dict[str, Any]:
    """Ask GitHub for the adapter's latest release through the server's layers; the state's ``check`` part."""
    home, repo = st.home, st.adapter_update_repo
    attempts: list[dict[str, Any]] = []
    if read_state(home).get("repo") != repo:  # another repository now: what was known of the old one goes
        write_state(home, {"repo": repo, "release": None, "no_release": False, "check": None, "checked_at": None})
    if not st.github_token:
        chk = {"ok": False, "at": now(), "layer": None, "attempts": [],
               "error": {"code": "no_token", "what": net.CODES["no_token"], "it_hint": net.IT_HINTS["no_token"],
                         "message": "без токена сервер не видит приватный репозиторий"}}
        write_state(home, {"check": chk, "checked_at": now(), "repo": repo})
        return chk
    ctx = context(st)
    for layer in service.layers(st, home):
        t0 = time.monotonic()
        try:
            rel = _release_via(layer, ctx, repo)
        except net.NetError as e:
            attempts.append({"ok": False, "ms": int((time.monotonic() - t0) * 1000), **e.public()})
            if e.code == "token_rejected":
                break  # every layer would get the same answer
            continue
        except AdapterUpdateError as e:
            chk = {"ok": False, "at": now(), "layer": layer, "attempts": attempts,
                   "error": {"code": "release", "message": str(e), "what": "GitHub ответил, но релиз не подходит"}}
            write_state(home, {"check": chk, "checked_at": now(), "repo": repo, "release": None})
            return chk
        attempts.append({"ok": True, "layer": layer, "ms": int((time.monotonic() - t0) * 1000)})
        chk = {"ok": True, "at": now(), "layer": layer, "attempts": attempts, "error": None,
               "found": rel["tag"] if rel else None,
               "note": None if rel else (net.CODES["no_access"] + ". " + net.IT_HINTS["no_access"])}
        write_state(home, {"check": chk, "checked_at": now(), "repo": repo, "release": rel, "no_release": rel is None})
        return chk
    error = attempts[-1] if attempts else {"code": "unavailable", "message": "сервер не ходит в GitHub "
                                           "(способ связи «браузер»)" if st.update_transport == "browser"
                                           else "нет доступных способов"}
    chk = {"ok": False, "at": now(), "layer": None, "attempts": attempts, "error": error}
    write_state(home, {"check": chk, "checked_at": now(), "repo": repo})
    return chk


def known_release(st: Settings) -> dict[str, Any] | None:
    s = read_state(st.home)
    rel = s.get("release")
    return rel if isinstance(rel, dict) and s.get("repo") == st.adapter_update_repo else None


# ---- the zip ------------------------------------------------------------------------------------------------------


def _under(path: str, folder: str) -> bool:
    return path.startswith(folder + "/")


def verify_zip(path: Path) -> dict[str, Any]:
    """The checked contents of a ``tda-adapter.zip``: ``manifest`` (its ``ADAPTER_RELEASE.json``), ``package`` (the
    zip path of the package), ``name`` (its folder name), ``sha256`` of the zip, ``files``; AdapterUpdateError
    (in the owner's words) on anything the release contract does not allow."""
    label = path.name
    try:
        size = path.stat().st_size
    except OSError as e:
        raise AdapterUpdateError(f"{label} не читается: {e}") from None
    if size > MAX_ZIP_BYTES:
        raise AdapterUpdateError(f"{label}: больше {MAX_ZIP_BYTES >> 20} МБ — не архив адаптера")
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise AdapterUpdateError(f"{label}: не zip-архив (скачан не до конца или страница прокси?)") from None
    with z:
        infos = z.infolist()
        if len(infos) > MAX_FILES:
            raise AdapterUpdateError(f"{label}: больше {MAX_FILES} файлов")
        seen: set[str] = set()
        files: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for i in infos:
            name = i.filename
            if i.flag_bits & 0x1:
                raise AdapterUpdateError(f"{label}: зашифрованный файл {name[:120]}")
            if stat.S_ISLNK(i.external_attr >> 16):
                raise AdapterUpdateError(f"{label}: ссылка (symlink) {name[:120]}")
            is_dir = name.endswith("/")
            rel = name[:-1] if is_dir else name
            if update._bad_path(rel):
                raise AdapterUpdateError(f"{label}: небезопасный путь {name[:120]!r}")
            if rel.lower() in seen:
                raise AdapterUpdateError(f"{label}: путь {name[:120]} дважды")
            seen.add(rel.lower())
            if is_dir:
                if i.file_size:
                    raise AdapterUpdateError(f"{label}: папка {name[:120]} с данными")
                continue  # a folder entry carries no content
            total += i.file_size
            if total > MAX_UNPACKED_BYTES:
                raise AdapterUpdateError(f"{label}: распакованный больше {MAX_UNPACKED_BYTES >> 20} МБ")
            files[name] = i
        info = files.pop(MANIFEST, None)
        if info is None:
            raise AdapterUpdateError(f"{label}: нет {MANIFEST} — это не архив выпуска адаптера (нужен {ZIP_NAME} "
                                     "со страницы релиза)")
        if info.file_size > MAX_DOC_BYTES:
            raise AdapterUpdateError(f"{label}: {MANIFEST} больше {MAX_DOC_BYTES >> 20} МБ")
        try:
            man = json.loads(z.read(info).decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, zipfile.BadZipFile):
            raise AdapterUpdateError(f"{label}: {MANIFEST} не читается") from None
        if not isinstance(man, dict) or man.get("format") != FORMAT:
            raise AdapterUpdateError(f"{label}: {MANIFEST} другого формата (нужен format {FORMAT}): обновите "
                                     "программу")
        if not adapter_store.valid_version(man.get("version")):
            raise AdapterUpdateError(f"{label}: непонятная версия {str(man.get('version'))[:40]!r}")
        listed = man.get("files")
        if not isinstance(listed, dict) or not listed or not all(
                isinstance(k, str) and isinstance(v, str) and _SHA.fullmatch(v) for k, v in listed.items()):
            raise AdapterUpdateError(f"{label}: в {MANIFEST} нет списка файлов с SHA-256")
        extra = sorted(set(files) - set(listed))
        if extra:
            raise AdapterUpdateError(f"{label}: файл {extra[0][:120]} не указан в {MANIFEST}")
        missing = sorted(set(listed) - set(files))
        if missing:
            raise AdapterUpdateError(f"{label}: нет файла {missing[0][:120]}, указанного в {MANIFEST}")
        if "__init__.py" in files:
            raise AdapterUpdateError(f"{label}: __init__.py в корне архива — пакет должен лежать в своей папке")
        dirs = {n.rsplit("/", 1)[0] for n in files if n.endswith("/__init__.py")}
        tops = sorted(d for d in dirs if not any(_under(d, o) for o in dirs if o != d))
        if len(tops) != 1:
            raise AdapterUpdateError(f"{label}: в архиве должен быть ровно один пакет адаптера, найдено {len(tops)}"
                                     + (f" ({', '.join(t[:60] for t in tops[:3])})" if tops else ""))
        pkg = tops[0]
        if man.get("package") != pkg:
            raise AdapterUpdateError(f"{label}: пакет в архиве — {pkg}, в {MANIFEST} — {str(man.get('package'))[:60]}")
        name = pkg.rsplit("/", 1)[-1]
        if not name.isidentifier():
            raise AdapterUpdateError(f"{label}: имя пакета {name[:60]!r} не годится для Python")
        outside = [n for n in files if not _under(n, pkg) and "/" in n]
        if outside:
            raise AdapterUpdateError(f"{label}: файл {outside[0][:120]} вне пакета адаптера")
        for n, i in files.items():
            h = hashlib.sha256()
            try:
                with z.open(i) as f:
                    for chunk in iter(lambda f=f: f.read(1 << 20), b""):
                        h.update(chunk)
            except (zipfile.BadZipFile, OSError, RuntimeError) as e:
                raise AdapterUpdateError(f"{label}: {n[:120]} не читается ({e})") from None
            if h.hexdigest() != listed[n].lower():
                raise AdapterUpdateError(f"{label}: SHA-256 файла {n[:120]} не совпадает с {MANIFEST} — архив "
                                         "повреждён или подменён")
    return {"manifest": man, "package": pkg, "name": name, "sha256": downloads.sha256_of(path),
            "files": sorted(files), "size": size, "version": man["version"]}


def _targets(info: dict[str, Any], dest: Path) -> list[tuple[str, Path]]:
    """(zip name, path under ``dest``) of every file the install writes: the package and the README / LICENSE files."""
    pkg, name = info["package"], info["name"]
    out = []
    for n in info["files"]:
        if _under(n, pkg):
            out.append((n, dest / name / n[len(pkg) + 1:]))
        elif n.lower().startswith(ROOT_KEEP):
            out.append((n, dest / n))
    return out


def _unpack(path: Path, info: dict[str, Any], dest: Path) -> None:
    """The package and the README / LICENSE files of the checked zip into ``dest`` (created if needed), every
    file's SHA-256 checked again as it is written; ``ADAPTER_RELEASE.json`` last, so a folder with it holds every
    file."""
    listed = info["manifest"]["files"]
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        for n, target in _targets(info, dest):
            target.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            with z.open(n) as src, open(target, "wb") as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    h.update(chunk)
                    out.write(chunk)
            if h.hexdigest() != listed[n].lower():
                raise AdapterUpdateError(f"{n[:120]}: файл изменился при распаковке")
        (dest / MANIFEST).write_bytes(z.read(MANIFEST))


# ---- what is in use -----------------------------------------------------------------------------------------------


def bundle_package() -> Path | None:
    """The bundle's adapter package (``DESK_DEFAULT_PRIVATE_ADAPTER``, set by the launcher), if it exists."""
    p = Path(v) if (v := os.environ.get("DESK_DEFAULT_PRIVATE_ADAPTER")) else None
    return p if p is not None and p.is_dir() else None


def base_version(st: Settings) -> str | None:
    """The version a release is compared with: the adapter the updater would leave in use (the installed release
    or the bundle's, by the settings' rule), whatever an explicit ``private_adapter`` says."""
    got = adapter_store.installed(st.home)
    bundle = bundle_package()
    bver = adapter_store.package_version(bundle) if bundle is not None else None
    if got is not None and (bundle is None or adapter_store.compare(got[1], bver) != "older"):
        return got[1]
    return bver


def _loader_message(error: str) -> str:
    code = error.split(":", 1)[0]
    why = {
        "unsupported_contract": "адаптер написан для более нового контракта, чем знает эта версия программы: "
                                "сначала обновите программу («Обновление программы»)",
        "unsupported_spi": "адаптер для более новой версии интерфейса (SPI): сначала обновите программу",
        "bad_source": "адаптер объявляет набор данных, которого эта версия программы не знает: сначала обновите "
                      "программу («Обновление программы»)",
        "spi_mismatch": "адаптер объявляет разные версии SPI",
        "name_collision": "пакет с таким именем уже есть среди модулей программы",
        "import_error": "адаптер не импортируется (ошибка в коде или нет нужной библиотеки)",
        "config_dir_missing": "нет папки настроек адаптера (private_adapter_config)",
    }.get(code, "адаптер не загрузился")
    return f"Новый адаптер не установлен: {why} ({error[:200]}). Работает прежний."


PINNED = ("Задан отпечаток адаптера (private_adapter_sha256): адаптер с другими файлами не загрузится. Очистите "
          "поле «Отпечаток адаптера» в настройках адаптера и установите снова.")


def _check_loads(st: Settings, pkg: Path) -> Any:
    from ..inbox.loader import load_adapter

    cfg = adapter_config_dir(st)
    return load_adapter(str(pkg), str(cfg) if cfg else None, None, inbox_dirs=[str(d) for d in st.inbox_dirs])


def _forget() -> None:
    """After a switch: the loaded handles dropped (the next scan, check or page loads the new package) and the
    settings forms' warm child ended (unless a check runs on it now). A running scan keeps its children."""
    from .. import settings_forms
    from ..inbox import scan

    scan.forget_adapters()
    settings_forms.release_worker()


def _intact(folder: Path, info: dict[str, Any]) -> bool:
    """``folder`` holds exactly this release: its ``ADAPTER_RELEASE.json`` lists the same files and every file the
    install would write is there with its SHA-256 (``__pycache__`` or other extras do not matter)."""
    try:
        old = json.loads((folder / MANIFEST).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    man = info["manifest"]
    if not isinstance(old, dict) or old.get("files") != man.get("files") or old.get("package") != man.get("package"):
        return False
    for n, target in _targets(info, folder):
        try:
            if downloads.sha256_of(target) != man["files"][n].lower():
                return False
        except OSError:
            return False
    return True


def _target(rd: Path, info: dict[str, Any]) -> tuple[Path, bool]:
    """(folder, reused): the release's folder ``<version>`` when it is free or holds exactly this release; else
    ``<version>+<zip's SHA-256, 8 hex>`` the same way; else a fresh ``<version>+<16 hex>``. An existing folder
    with other files is never written into, renamed or removed here: it may be in use (TDM#519)."""
    version = info["version"]
    for name in (version, f"{version}+{info['sha256'][:8]}"):
        folder = rd / name
        if not folder.exists():
            return folder, False
        if _intact(folder, info) and (folder / info["name"] / "__init__.py").is_file():
            return folder, True
    return rd / f"{version}+{info['sha256'][:8]}{uuid.uuid4().hex[:8]}", False


def _discard(folder: Path) -> bool:
    """Remove ``folder`` when nothing holds it: renamed aside first (one try, an atomic step: a folder in use
    stays whole), then deleted; True when it is gone from its place. Never raises."""
    aside = folder.with_name(f".old-{folder.name}-{uuid.uuid4().hex[:8]}")
    try:
        folder.rename(aside)
    except OSError:
        return False
    shutil.rmtree(aside, ignore_errors=True)  # what an antivirus still reads goes with the next prune
    return True


def _write_current(home: Path, doc: dict[str, Any]) -> None:
    text = json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
    retry(lambda: atomic_write_text(adapter_store.current_path(home), text))


def _runner_specs() -> set[str]:
    from ..inbox import runner

    return runner.specs_in_use()


def _protected(home: Path, cur: dict[str, Any] | None) -> set[str]:
    """The folder names prune leaves alone: the current and the previous release's, and every folder a child of
    this process imports the adapter from now (an inbox scan still running on the previous one)."""
    out: set[str] = set()
    if cur:
        out |= {v for v in (cur.get("version"), cur.get("previous")) if isinstance(v, str)}
        for key in ("package", "previous_package"):
            if (pkg := adapter_store.safe_package(home, cur.get(key))) is not None:
                out.add(pkg.parent.name)
    try:
        base = os.path.normcase(str(adapter_store.releases_dir(home).resolve()))
    except OSError:
        return out
    for spec in _runner_specs():
        try:
            folder = Path(spec).resolve().parent
        except (OSError, ValueError):
            continue
        if os.path.normcase(str(folder.parent)) == base:
            out.add(folder.name)
    return out


def previous_package(home: Path, cur: dict[str, Any] | None) -> Path | None:
    """The previous release's package folder when it is still there: ``previous_package`` of ``current.json``, or
    (``current.json`` of an older version) the one package of the folder named ``previous``."""
    if not cur or not cur.get("previous"):
        return None
    if cur.get("previous_package"):
        pkg = adapter_store.safe_package(home, cur["previous_package"])
        return pkg if pkg is not None and (pkg / "__init__.py").is_file() else None
    prev = cur["previous"]
    folder = adapter_store.releases_dir(home) / str(prev)
    if not adapter_store.valid_version(prev) or not folder.is_dir():
        return None
    pkgs = sorted(p for p in folder.iterdir() if (p / "__init__.py").is_file())
    return pkgs[0] if len(pkgs) == 1 else None


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def prune(home: Path, keep: int = KEEP, protect: tuple[str, ...] = ()) -> list[str]:
    """Remove all but the newest ``keep`` release folders (never the current or the previous one, nor one in use:
    ``_protected``; one that cannot be renamed aside at once is left for the next time), and stale temporary
    folders; the folders removed. ``protect``: folder names kept whatever ``current.json`` reads now (the install
    passes the ones it has just made current and previous: a moment's failure to read it removes neither)."""
    rd = adapter_store.releases_dir(home)
    protect = (*protect, *_protected(home, adapter_store.read_current(home)))
    try:
        entries = [p for p in rd.iterdir() if p.is_dir()]
    except OSError:
        return []
    for p in entries:
        if p.name.startswith((".tmp-", ".old-")) and time.time() - _mtime(p) > 3600:
            shutil.rmtree(p, ignore_errors=True)
    folders = sorted((p for p in entries if adapter_store.valid_folder(p.name)), key=_mtime, reverse=True)
    removed = []
    for p in folders[keep:]:
        if p.name not in protect and _discard(p):
            removed.append(p.name)
    return removed


def _precompile(st: Settings, pkg: Path) -> None:
    """The bytecode of a new release folder, written once by the installer before anything uses the folder: the
    adapter's children run with ``-B`` (they never write into a folder in use) and read these ``__pycache__`` files
    instead of compiling the adapter on every start. ``compileall`` in a child of the same Python (``-I``, as the
    adapter's children; it compiles, it runs none of the adapter's code); a failure only costs that speed, so it is
    logged and the install goes on. ``__pycache__`` is outside the release check (``_intact``: the manifest's files
    only) and the fingerprint (``desk.inbox.loader``)."""
    from ..inbox import runner

    cmd = [sys.executable, "-I", "-m", "compileall", "-q", "-q", str(pkg)]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=runner.child_env(), timeout=COMPILE_TIMEOUT_S, check=False)
        why = f"код {r.returncode}" if r.returncode else None
    except (OSError, subprocess.SubprocessError) as e:
        why = type(e).__name__
    if why:
        log(st, f"Адаптер {pkg.parent.name}: предкомпиляция не удалась ({why}); адаптер работает, только медленнее "
                "стартует")


def install_zip(st: Settings, path: Path, *, source: str, allow_older: bool = False,
                expect_sha256: str | None = None) -> dict[str, Any]:
    """Check ``path`` (a ``tda-adapter.zip``), unpack it into a new folder of ``<home>/adapter-releases/``
    (``_target``), load it, and only then make it the current adapter; the result for the page.
    AdapterUpdateError when anything fails: the adapter in use stays."""
    home = st.home
    if not _install_lock.acquire(blocking=False):
        raise AdapterUpdateError("Адаптер уже устанавливается: дождитесь конца.")
    try:
        if st.private_adapter_sha256:
            raise AdapterUpdateError(PINNED)
        info = verify_zip(path)
        if expect_sha256 and info["sha256"] != expect_sha256.lower():
            raise AdapterUpdateError(f"{path.name}: контрольная сумма не та, что в релизе — архив подменён или "
                                     "из другого релиза")
        man, version, name = info["manifest"], info["version"], info["name"]
        base = base_version(st)
        cmp = adapter_store.compare(version, base) if base else "newer"
        if cmp != "newer" and not allow_older:
            why = {"same": "эта версия уже используется", "older": "эта версия старее используемой"}.get(
                cmp, "какая из версий новее, неизвестно")
            raise AdapterUpdateError(f"Адаптер {version}: {why} ({base}). Установить можно только явно.",
                                     needs_confirm=True, version=version, compare=cmp)
        cur = adapter_store.read_current(home)
        rd = adapter_store.releases_dir(home)
        try:
            retry(lambda: rd.mkdir(parents=True, exist_ok=True))
        except OSError as e:
            log(st, f"Адаптер {version} не установлен: папка {rd.name} не создаётся — {os_code(e)}")
            raise AdapterUpdateError(busy_message(e, "создать папку выпусков адаптера", rd)) from None
        dest, reused = _target(rd, info)
        if not reused:
            try:
                retry(dest.mkdir)  # a new folder: FileExistsError when another one appeared meanwhile
            except OSError as e:
                log(st, f"Адаптер {version} не установлен: папка {rd.name}/{dest.name} не создаётся — {os_code(e)}")
                raise AdapterUpdateError(busy_message(e, f"создать папку {dest}", rd)) from None
            try:
                _unpack(path, info, dest)
            except OSError as e:
                _discard(dest)
                log(st, f"Адаптер {version} не установлен: распаковка в {rd.name}/{dest.name} — {os_code(e)}")
                raise AdapterUpdateError(busy_message(e, f"распаковать адаптер в {dest}", rd)) from None
            except BaseException:
                _discard(dest)
                raise
            _precompile(st, dest / name)  # not in use yet: the only moment anything writes into it
        rel = adapter_store.package_rel(dest.name, name)
        h = _check_loads(st, dest / name)
        if not h.ok:
            if not reused:
                _discard(dest)
            log(st, f"Адаптер {version} не установлен: загрузчик ответил {h.error}")
            raise AdapterUpdateError(_loader_message(h.error or "load_failed"))
        if cur and cur.get("package") != rel:
            previous, previous_pkg = cur["version"], cur["package"]
        else:
            previous = cur.get("previous") if cur else None
            previous_pkg = cur.get("previous_package") if cur else None
        doc = {"version": version, "package": rel, "previous": previous, "previous_package": previous_pkg,
               "installed_at": now(), "source": source, "sha256": info["sha256"], "commit": man.get("commit"),
               "contract": man.get("contract"), "spi": man.get("spi"),
               "adapter": {"name": h.name, "version": h.version, "fingerprint": h.fingerprint}}
        try:
            _write_current(home, doc)
        except OSError as e:
            log(st, f"Адаптер {version} не установлен: {rd.name}/{adapter_store.CURRENT_FILE} не записан — "
                    f"{os_code(e)}")
            raise AdapterUpdateError(busy_message(e, f"записать {adapter_store.CURRENT_FILE}", rd)) from None
        _forget()
        prev_pkg = adapter_store.safe_package(home, previous_pkg)  # by name for a current.json of 0.2.13
        removed = prune(home, protect=(dest.name, prev_pkg.parent.name if prev_pkg else previous or ""))
        log(st, f"Адаптер {version} установлен ({source}, sha256 {info['sha256'][:12]}"
                + (f", папка {dest.name}" if dest.name != version else "") + "); прежний: "
                f"{previous or 'из комплекта'}" + (f"; удалены старые версии: {', '.join(removed)}" if removed else ""))
        return {"installed": True, "version": version, "previous": previous, "source": source,
                "adapter": doc["adapter"], "explicit": st.private_adapter_source in ("env", "settings"),
                "message": f"Адаптер {version} установлен и проверен загрузчиком; работает без перезапуска."}
    finally:
        _install_lock.release()


def rollback(st: Settings, to: str) -> dict[str, Any]:
    """``previous``: back to the previous installed release (its folder must still be there and load);
    ``bundle``: the bundle's adapter again (``current.json`` removed, the folders kept)."""
    home = st.home
    cur = adapter_store.read_current(home)
    with _install_lock:
        if to == "bundle":
            try:
                retry(lambda: adapter_store.current_path(home).unlink(missing_ok=True))
            except OSError as e:
                log(st, f"Адаптер: возврат к адаптеру из комплекта не сделан — {os_code(e)}")
                raise AdapterUpdateError(busy_message(e, f"удалить {adapter_store.CURRENT_FILE}",
                                                      adapter_store.releases_dir(home))
                                         .replace("Новый адаптер не установлен", "Адаптер не переключён")) from None
            _forget()
            log(st, f"Адаптер: возврат к адаптеру из комплекта (был {cur['version'] if cur else 'он же'})")
            return {"version": None, "message": "Используется адаптер из комплекта программы (или эталонный, если "
                                                "в комплекте его нет)."}
        if to != "previous":
            raise AdapterUpdateError("to: previous или bundle")
        prev = cur.get("previous") if cur else None
        if not prev:
            raise AdapterUpdateError("Предыдущей установленной версии нет: можно вернуться к адаптеру из комплекта.")
        pkg = previous_package(home, cur)
        if pkg is None:
            raise AdapterUpdateError(f"Папки версии {prev} больше нет: установите её заново из архива.")
        if st.private_adapter_sha256:
            raise AdapterUpdateError(PINNED)
        h = _check_loads(st, pkg)
        if not h.ok:
            raise AdapterUpdateError(_loader_message(h.error or "load_failed"))
        try:
            man = json.loads((pkg.parent / MANIFEST).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            man = {}
        assert cur is not None  # previous_package found one
        doc = {"version": prev, "package": adapter_store.package_rel(pkg.parent.name, pkg.name),
               "previous": cur["version"], "previous_package": cur["package"],
               "installed_at": now(), "source": "rollback", "sha256": None,
               "commit": man.get("commit") if isinstance(man, dict) else None,
               "adapter": {"name": h.name, "version": h.version, "fingerprint": h.fingerprint}}
        try:
            _write_current(home, doc)
        except OSError as e:
            log(st, f"Адаптер: возврат {cur['version']} -> {prev} не сделан — {os_code(e)}")
            raise AdapterUpdateError(busy_message(e, f"записать {adapter_store.CURRENT_FILE}",
                                                  adapter_store.releases_dir(home))
                                     .replace("Новый адаптер не установлен", "Адаптер не переключён")) from None
        _forget()
        log(st, f"Адаптер: возврат {cur['version']} -> {prev}")
        return {"version": prev, "message": f"Возвращён адаптер {prev}."}


# ---- the job -------------------------------------------------------------------------------------------------------


def _job_update(**changes: Any) -> None:
    with _lock:
        if _job is not None:
            _job.update(changes)


def job() -> dict[str, Any] | None:
    with _lock:
        return dict(_job) if _job is not None else None


def cancel() -> dict[str, Any] | None:
    _cancel.set()
    return job()


def _server_download(st: Settings, rel: dict[str, Any]) -> Path | None:
    asset = rel["asset"]
    ctx = context(st, 120.0)
    attempts: list[dict[str, Any]] = []
    for layer in service.layers(st, st.home):
        if _cancel.is_set():
            break
        _job_update(state="downloading", layer=layer, bytes=0, total=asset["size"],
                    message=f"Скачиваю {ZIP_NAME} {rel['version']} (способ {layer})…")
        t0 = time.monotonic()
        try:
            r = net.fetch(layer, ctx, api_asset_url(st.adapter_update_repo, asset["id"]), MAX_ZIP_BYTES,
                          accept=ASSET_ACCEPT, auth=True, progress=lambda n: _job_update(bytes=n))
        except net.NetError as e:
            attempts.append({"ok": False, "ms": int((time.monotonic() - t0) * 1000), **e.public()})
            _job_update(attempts=attempts)
            if e.code == "token_rejected":
                break
            continue
        if r.size != asset["size"] or downloads.sha256_of(r.path) != asset["sha256"]:
            _remove_file(r.path)
            attempts.append({"ok": False, "layer": layer, "code": "checksum", "what": "файл не совпал с релизом",
                             "message": "размер или SHA-256 не те, что в релизе (страница прокси?)"})
            _job_update(attempts=attempts)
            continue
        attempts.append({"ok": True, "layer": layer, "ms": int((time.monotonic() - t0) * 1000)})
        _job_update(attempts=attempts)
        named = r.path.with_name(f"tda-adapter-{rel['version']}.zip")  # messages name the file the owner knows
        try:
            retry(lambda src=r.path, dst=named: os.replace(src, dst))
        except OSError as e:  # an antivirus still reads the fresh download: installed under its own name
            log(st, f"Адаптер {rel['version']}: скачанный архив не переименован — {os_code(e)}")
            return r.path
        return named
    return None


def _remove_file(path: Path) -> None:
    """Best effort: a file still read by an antivirus stays, never failing what was done (TDM#519)."""
    try:
        retry(lambda: path.unlink(missing_ok=True), budget_s=3.0)
    except OSError:
        pass


def candidates(folder: Path, since: float | None = None) -> list[Path]:
    """``tda-adapter.zip`` and the browser's numbered copies in ``folder``, newest first; ``since``: written then
    or later (a two-second slack for the file system's clock)."""
    out = downloads.copies(folder, ZIP_NAME)
    if since is None:
        return out
    fresh = []
    for p in out:
        try:
            if p.stat().st_mtime >= since - 2:
                fresh.append(p)
        except OSError:
            continue
    return fresh


def _watch(st: Settings, folder: Path, rel: dict[str, Any] | None, since: float, deadline: float) -> Path | None:
    """Wait for the browser's ``tda-adapter.zip`` (written after ``since``) that verifies; None on cancel or timeout."""
    bad: dict[tuple[str, float], str] = {}
    want = rel["asset"]["sha256"] if rel else None
    while not _cancel.is_set() and time.monotonic() < deadline:
        found = None
        for p in candidates(folder, since):
            try:
                key = (p.name, p.stat().st_mtime)
            except OSError:
                continue
            if key in bad:
                continue
            try:
                if want and downloads.sha256_of(p) != want:
                    raise AdapterUpdateError(f"{p.name}: контрольная сумма не та, что в релизе {rel['tag']} — "
                                             "скачан другой релиз или файл повреждён")
                verify_zip(p)
            except (AdapterUpdateError, OSError) as e:
                bad[key] = str(e)
                _job_update(found={"state": "mismatch", "path": str(p), "message": str(e)})
                continue
            found = p
            break
        if found is not None:
            return found
        part = downloads.partial(folder, ZIP_NAME.rsplit(".", 1)[0])
        if part is not None:
            _job_update(found={"state": "downloading", "size": part}, bytes=part)
        _cancel.wait(WATCH_POLL_S)
    return None


def _run(st: Settings, rel: dict[str, Any] | None, browser_first: bool, allow_older: bool, since: float) -> None:
    home = st.home
    try:
        path, source = None, "server"
        if not browser_first and rel is not None:
            path = _server_download(st, rel)
        if path is None and not _cancel.is_set():
            folder = downloads.downloads_dir(st.update_downloads_dir)
            url = rel["asset"]["browser_url"] if rel else browser_url(st.adapter_update_repo)
            _job_update(state="waiting_browser", via="browser", layer="browser", browser_url=url,
                        downloads_dir=str(folder),
                        message=(f"Скачайте {ZIP_NAME} браузером, где вы вошли в GitHub (кнопка ниже, если загрузка "
                                 f"не началась сама): жду файл в папке {folder}."))
            found = _watch(st, folder, rel, since, time.monotonic() + WATCH_TIMEOUT_S)
            if found is None:
                if not _cancel.is_set():
                    _job_update(state="failed", finished_at=now(),
                                message=f"За {WATCH_TIMEOUT_S // 60} минут {ZIP_NAME} не появился в {folder}. "
                                        "Перетащите скачанный архив на страницу или нажмите «Найти в «Загрузках»».")
                return
            path, source = found, "browser"
        if path is None:
            if not _cancel.is_set():
                _job_update(state="failed", finished_at=now(), message="Не скачалось.")
            return
        if _cancel.is_set():
            return
        _job_update(state="installing", message=f"Проверяю и устанавливаю {path.name}…", file=path.name)
        try:
            out = install_zip(st, path, source=source, allow_older=allow_older,
                              expect_sha256=rel["asset"]["sha256"] if rel and source == "server" else None)
        except AdapterUpdateError as e:
            _job_update(state="failed", message=str(e), finished_at=now(), needs_confirm=e.needs_confirm,
                        found_file=path.name if source == "browser" else None)
            return
        finally:
            if source == "server":
                _remove_file(path)
        _job_update(state="done", message=out["message"], version=out["version"], finished_at=now())
        write_state(home, {"last_install": {"at": now(), "version": out["version"], "source": source}})
    except Exception as e:  # noqa: BLE001 - the job reports, the server lives on
        _job_update(state="failed", message=f"Ошибка: {net.scrub(str(e), st.github_token)}", finished_at=now())
    finally:
        if _cancel.is_set():
            _job_update(state="cancelled", message="Отменено.", finished_at=now())


def start_install(st: Settings, *, allow_older: bool = False, via: str | None = None) -> dict[str, Any]:
    """Install the latest known release: downloaded by the server with the token, else (``via`` ``browser``, no
    token, no check) by the browser; the job. AdapterUpdateError when there is nothing to install or a job runs."""
    global _job
    if st.private_adapter_sha256:
        raise AdapterUpdateError(PINNED)
    rel = known_release(st)
    if rel is not None and not allow_older:
        cmp = adapter_store.compare(rel["version"], base) if (base := base_version(st)) else "newer"
        if cmp != "newer":
            raise AdapterUpdateError(f"Адаптер {rel['version']} на GitHub не новее используемого ({base}). Установить "
                                     "можно только явно.", needs_confirm=True, version=rel["version"], compare=cmp)
    with _lock:
        if _job is not None and _job.get("state") in ACTIVE:
            raise AdapterUpdateError("Установка адаптера уже идёт.")
        chk = read_state(st.home).get("check") or {}
        browser_first = (via == "browser" or rel is None or not st.github_token or not chk.get("ok")
                         or not service.layers(st, st.home))
        folder = downloads.downloads_dir(st.update_downloads_dir)
        _cancel.clear()
        url = (rel["asset"]["browser_url"] if rel else browser_url(st.adapter_update_repo))
        _job = {"id": uuid.uuid4().hex[:8], "state": "starting", "via": "browser" if browser_first else "server",
                "version": rel["version"] if rel else None, "started_at": now(), "message": None,
                "browser_url": url if browser_first else None, "downloads_dir": str(folder), "bytes": None,
                "total": rel["asset"]["size"] if rel else None, "attempts": [], "found": None, "layer": None,
                "allow_older": allow_older}
        out = dict(_job)
    threading.Thread(target=_run, args=(st, rel, browser_first, allow_older, time.time()),
                     name="desk-adapter-install", daemon=True).start()
    return out


# ---- the downloads folder and uploads -----------------------------------------------------------------------------


def scan(st: Settings) -> dict[str, Any]:
    """The ``tda-adapter*.zip`` files of the downloads folder, newest first, each checked by its inner manifest
    (and against the known release's checksum)."""
    folder = downloads.downloads_dir(st.update_downloads_dir)
    rel = known_release(st)
    base = base_version(st)
    items = []
    for p in candidates(folder)[:SCAN_LIMIT]:
        try:
            s = p.stat()
        except OSError:
            continue
        item: dict[str, Any] = {"file": p.name, "path": str(p), "size": s.st_size,
                                "modified": datetime.fromtimestamp(s.st_mtime).isoformat(timespec="seconds"),
                                "ok": False, "version": None, "sha256": None, "reason": None, "compare": None,
                                "is_release": None}
        try:
            info = verify_zip(p)
        except AdapterUpdateError as e:
            item["reason"] = str(e)
        else:
            item.update(ok=True, version=info["version"], sha256=info["sha256"],
                        compare=adapter_store.compare(info["version"], base) if base else "newer",
                        is_release=(info["sha256"] == rel["asset"]["sha256"]) if rel else None)
        items.append(item)
    return {"downloads_dir": str(folder), "exists": folder.is_dir(), "items": items}


def install_found(st: Settings, name: str, sha256: str, allow_older: bool = False) -> dict[str, Any]:
    """Install ``name`` of the downloads folder that ``scan`` listed: its checksum must still be ``sha256``."""
    if Path(name).name != name or "\\" in name or not name.lower().endswith(".zip"):
        raise AdapterUpdateError("file: имя zip-файла в папке загрузок")
    path = downloads.downloads_dir(st.update_downloads_dir) / name
    if not path.is_file():
        raise AdapterUpdateError(f"Файла {name} нет в папке загрузок.")
    if downloads.sha256_of(path) != sha256:
        raise AdapterUpdateError(f"{name} изменился после проверки: найдите заново.")
    return install_zip(st, path, source="downloads", allow_older=allow_older, expect_sha256=sha256)


def uploads_dir(home: Path) -> Path:
    return update.updates_dir(home) / UPLOADS


# ---- what the page shows -------------------------------------------------------------------------------------------


def versions(home: Path) -> list[dict[str, Any]]:
    rd = adapter_store.releases_dir(home)
    try:
        dirs = [p for p in rd.iterdir() if p.is_dir() and adapter_store.valid_folder(p.name)]
    except OSError:
        return []
    return [{"version": adapter_store.folder_version(p.name), "folder": p.name,
             "modified": datetime.fromtimestamp(_mtime(p)).isoformat(timespec="seconds")}
            for p in sorted(dirs, key=_mtime, reverse=True)]


def status(st: Settings) -> dict[str, Any]:
    s = read_state(st.home)
    same = s.get("repo") == st.adapter_update_repo
    rel = known_release(st)
    cur = adapter_store.read_current(st.home)
    inst = adapter_store.installed(st.home)
    bundle = bundle_package()
    bver = adapter_store.package_version(bundle) if bundle is not None else None
    base = base_version(st)
    cmp = (adapter_store.compare(rel["version"], base) if base else "newer") if rel else None
    in_use_version = (inst[1] if st.private_adapter_source == "installed" and inst else
                      bver if st.private_adapter_source == "bundle" else
                      adapter_store.package_version(Path(st.private_adapter)) if st.private_adapter
                      and Path(st.private_adapter).is_dir() else None)
    folder = downloads.downloads_dir(st.update_downloads_dir)
    explicit = st.private_adapter_source in ("env", "settings")
    return {
        "repo": st.adapter_update_repo, "token_set": bool(st.github_token),
        "token_source": ("env" if st.github_token_source.startswith("DESK_GITHUB_TOKEN") else
                         "file" if st.github_token_source.startswith("file") else "not set"),
        "token_file": str(st.home / "github_token"),
        "source": st.private_adapter_source, "explicit": explicit,
        "explicit_note": ("В настройках задана папка адаптера (private_adapter): установленный выпуск не используется, "
                          "пока поле не очищено.") if explicit else None,
        "pinned": bool(st.private_adapter_sha256), "pinned_note": PINNED if st.private_adapter_sha256 else None,
        "in_use": {"path": st.private_adapter, "version": in_use_version, "source": st.private_adapter_source},
        "installed": ({k: cur.get(k) for k in ("version", "previous", "installed_at", "source", "sha256", "commit",
                                               "contract", "adapter")} | {"exists": inst is not None})
        if cur else None,
        "bundle": {"version": bver, "path": str(bundle)} if bundle is not None else None,
        "base_version": base, "versions": versions(st.home),
        "previous_available": previous_package(st.home, cur) is not None,
        "checked_at": s.get("checked_at") if same else None, "check": s.get("check") if same else None,
        "release": rel, "no_release": bool(s.get("no_release")) and same, "compare": cmp,
        "update_available": cmp == "newer", "job": job(),
        "downloads_dir": str(folder), "downloads_dir_exists": folder.is_dir(),
        "browser_url": rel["asset"]["browser_url"] if rel else browser_url(st.adapter_update_repo),
        "releases_page": releases_page(st.adapter_update_repo), "layers": {la: net.available(la) for la in net.LAYERS},
        "codes": net.CODES, "it_hints": net.IT_HINTS,
    }
