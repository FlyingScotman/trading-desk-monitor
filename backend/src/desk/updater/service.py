"""The updater: notices a new release on GitHub and installs it with one button, through whatever path works here.

**Check.** The server asks GitHub through its layers (``net.LAYERS``, the last one that worked first; the
settings' ``update_transport`` names one): ``release.json`` of the latest release at its stable address on
github.com (no API, no rate limit), else the API. When no layer gets through, the page asks the API itself from the
browser (``api.github.com`` answers CORS requests) and posts the document here (``take_browser_release``):
``browser_check_due`` says when. A latest release without update files (made by hand, a tag without ``v``) is not
the end: the check says why and takes the newest earlier release that has them (``release.first_usable``); the
check record keeps when GitHub was asked, through which layer, and what it answered (``notes``). A page opened in the Windows bundle starts a check in the background when the
last one is older than ``CHECK_EVERY_S`` (``maybe_auto_check``); the shared navigation bar shows the new version.

**Install** (``start_install``): the zip ``release.choose`` picks (a patch from exactly the installed version, else
the full zip; never an older version unless asked), its size and SHA-256 checked against the release, put into the
uploads folder and installed by the existing pipeline (``desk.update.stage`` / ``request``, the launcher applies it
and rolls back a version that does not start). The server downloads it itself when a layer works; otherwise (or
after every layer failed) the job turns to the browser: the page starts an ordinary browser download of the asset's
github.com address, the server watches the downloads folder for that file (``downloads.find_expected``) and installs
it once its checksum is the release's. A patch that does not fit the installed files falls back to the full zip.

**Diagnostics** (``diagnose``): every layer against the API, github.com and the release file storage, each result
with its error class (``net.CODES``) and what to tell IT (``net.IT_HINTS``); the page adds what the browser reaches.

State: ``<home>/updates/remote.json`` (the last check, the release, the diagnostics, the layer that works); the job
lives in this process (the server restarts after an install anyway).
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import update
from ..settings import Settings, atomic_write_text
from . import downloads, net
from .release import (
    RELEASE_JSON,
    SUMS_NAME,
    Asset,
    EmptyReleaseError,
    Release,
    ReleaseError,
    api_latest_url,
    api_list_url,
    choose,
    download_url,
    first_usable,
    from_api,
    from_public,
    from_release_json,
    full_of,
    latest_url,
    releases_page,
)

CHECK_EVERY_S = 6 * 3600
RETRY_FAILED_S = 3600
BROWSER_CHECK_EVERY_S = 6 * 3600
WATCH_TIMEOUT_S = 45 * 60
WATCH_POLL_S = 2.0
API_ACCEPT = "application/vnd.github+json"
STATE_FILE = "remote.json"

_lock = threading.RLock()
_job: dict[str, Any] | None = None
_cancel = threading.Event()
_checking = threading.Event()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _age(ts: object) -> float:
    try:
        return time.time() - datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        return float("inf")


# ---- state ------------------------------------------------------------------------------------------------------


def state_path(home: Path) -> Path:
    return update.updates_dir(home) / STATE_FILE


def read_state(home: Path) -> dict[str, Any]:
    import json

    try:
        doc = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_state(home: Path, changes: dict[str, Any]) -> dict[str, Any]:
    import json

    with _lock:
        doc = {**read_state(home), **changes}
        atomic_write_text(state_path(home), json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        return doc


def context(st: Settings, timeout: float | None = None) -> net.Context:
    return net.Context(home=st.home, timeout_s=timeout or min(st.hub_timeout_s, 60.0), proxy_user=st.proxy_user,
                       proxy_password=st.proxy_password)


def layers(st: Settings, home: Path) -> list[str]:
    """The server's layers to try, in order: the one the settings name, else the last that worked first."""
    mode = st.update_transport
    if mode == "browser":
        return []
    if mode != "auto":
        return [mode]
    order = [la for la in net.LAYERS if net.available(la) is None]
    prefer = read_state(home).get("prefer")
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    return order


# ---- check ------------------------------------------------------------------------------------------------------


def _latest(reason: str) -> str:
    """«Релиз X …» of ``release.empty_reason`` as «Последний релиз X …»."""
    return "Последний " + reason[:1].lower() + reason[1:]


def _has(doc: object, name: str) -> bool:
    return isinstance(doc, dict) and any(isinstance(a, dict) and a.get("name") == name
                                         for a in doc.get("assets") or [])


def _release_via(layer: str, ctx: net.Context, repo: str, notes: list[str] | None = None) -> Release | None:
    """The latest release through ``layer``: release.json, else the API (with SHA256SUMS); None: no release. A
    latest release without update files: the newest earlier one that has them (``Release.skipped`` says why),
    ReleaseError when none has. ``notes`` gets what the owner should know about how the release was found."""
    notes = notes if notes is not None else []
    try:
        doc = net.get_json(layer, ctx, latest_url(repo, RELEASE_JSON))
        return from_release_json(doc, repo)
    except net.NetError as e:
        if e.code != "not_found":
            raise
    try:
        api = net.get_json(layer, ctx, api_latest_url(repo), accept=API_ACCEPT)
    except net.NetError as e:
        if e.code == "not_found":
            return None  # the repository has no release yet
        raise
    sums = None
    if _has(api, SUMS_NAME):
        try:
            sums = net.get_text(layer, ctx, latest_url(repo, SUMS_NAME))
        except net.NetError:
            sums = None  # the API's own digest still checks
    try:
        rel = from_api(api, repo, sums_text=sums)
    except EmptyReleaseError as e:
        empty = e
    else:
        notes.append(f"В последнем релизе {rel.tag} нет release.json: версия прочитана через GitHub API.")
        return rel

    def sums_of(doc: dict[str, Any]) -> str | None:
        if not _has(doc, SUMS_NAME) or not isinstance(doc.get("tag_name"), str):
            return None
        try:
            return net.get_text(layer, ctx, download_url(repo, doc["tag_name"], SUMS_NAME))
        except net.NetError:
            return None

    listing = net.get_json(layer, ctx, api_list_url(repo), accept=API_ACCEPT)
    rel, skipped = first_usable(listing, repo, sums_for=sums_of)
    if not any(x["tag"] == empty.tag for x in skipped):
        skipped.insert(0, {"tag": empty.tag, "reason": str(empty)})
    if rel is None:
        raise ReleaseError(f"{_latest(str(empty))} Более раннего релиза с файлами обновления на GitHub нет.")
    rel.skipped = skipped
    notes.append(f"{_latest(str(empty))} Предлагаю предыдущий релиз с файлами: {rel.tag}.")
    return rel


def check(st: Settings) -> dict[str, Any]:
    """Ask GitHub for the latest release through the server's layers; the state's ``check`` part."""
    home, repo = st.home, st.update_repo
    ctx = context(st)
    attempts: list[dict[str, Any]] = []
    order = layers(st, home)
    _checking.set()
    try:
        for layer in order:
            t0 = time.monotonic()
            notes: list[str] = []
            try:
                rel = _release_via(layer, ctx, repo, notes)
            except net.NetError as e:
                attempts.append({"ok": False, "ms": int((time.monotonic() - t0) * 1000), **e.public()})
                continue
            except ReleaseError as e:  # GitHub answered; the release itself is not usable
                chk = {"ok": False, "at": now(), "layer": layer, "attempts": attempts, "notes": notes,
                       "error": {"code": "release", "message": str(e), "what": "GitHub ответил, но релиз не подходит"}}
                write_state(home, {"check": chk, "checked_at": now(), "prefer": layer, "repo": repo})
                return chk
            attempts.append({"ok": True, "layer": layer, "ms": int((time.monotonic() - t0) * 1000)})
            chk = {"ok": True, "at": now(), "layer": layer, "attempts": attempts, "error": None, "notes": notes,
                   "found": rel.tag if rel else None}
            write_state(home, {"check": chk, "checked_at": now(), "prefer": layer, "repo": repo,
                               "release": rel.public() if rel else None, "no_release": rel is None})
            return chk
        error = (attempts[-1] if attempts else {"code": "unavailable", "message": "сервер не ходит в GitHub "
                 "(способ связи «браузер»)" if st.update_transport == "browser" else "нет доступных способов"})
        chk = {"ok": False, "at": now(), "layer": None, "attempts": attempts, "error": error}
        write_state(home, {"check": chk, "checked_at": now(), "repo": repo})
        return chk
    finally:
        _checking.clear()


def maybe_auto_check(st: Settings) -> bool:
    """Start a background check when due (the Windows bundle, ``update_auto_check`` on, the last check older than
    ``CHECK_EVERY_S``, a failed one older than ``RETRY_FAILED_S``); True when one was started."""
    if not st.update_auto_check or update.bundle_root() is None or _checking.is_set() or \
            st.update_transport == "browser":
        return False
    s = read_state(st.home)
    chk = s.get("check") or {}
    age = _age(s.get("checked_at")) if s.get("repo") == st.update_repo else float("inf")
    if age < (CHECK_EVERY_S if chk.get("ok") else RETRY_FAILED_S):
        return False
    _checking.set()
    threading.Thread(target=_quiet_check, args=(st,), name="desk-update-check", daemon=True).start()
    return True


def _quiet_check(st: Settings) -> None:
    try:
        check(st)
    except Exception:  # noqa: BLE001 - a background check must not take the server down
        _checking.clear()


def take_browser_release(st: Settings, body: dict[str, Any]) -> dict[str, Any]:
    """The browser's own API answer: ``{"releases": <the API's release list, newest first>}`` (the newest one with
    update files is taken, ``release.first_usable``), ``{"release": <one API document>}``, ``{"none": true}`` (404:
    no release yet) or ``{"error": "<why the browser could not ask>"}``. ReleaseError when the document does not
    read or no release has update files."""
    home, repo = st.home, st.update_repo
    rec: dict[str, Any] = {"checked_at": now(), "ok": False, "error": None}
    changes: dict[str, Any] = {}
    if body.get("none") is True:
        rec["ok"] = True
        changes = {"release": None, "no_release": True, "release_from": "browser"}
    elif "release" in body or "releases" in body:
        try:
            if "releases" in body:
                rel, skipped = first_usable(body["releases"], repo, source="browser")
                if rel is None and skipped:
                    raise ReleaseError(f"{_latest(skipped[0]['reason'])} Более раннего релиза с файлами "
                                       "обновления на GitHub нет.")
            else:
                rel = from_api(body["release"], repo, source="browser")
        except ReleaseError as e:
            rec["error"] = str(e)
            write_state(home, {"browser": rec})
            raise
        rec["ok"] = True
        changes = ({"release": rel.public(), "no_release": False, "repo": repo} if rel is not None
                   else {"release": None, "no_release": True, "release_from": "browser"})
    else:
        rec["error"] = str(body.get("error") or "браузер не смог спросить GitHub API")[:300]
    return write_state(home, {"browser": rec, **changes})


# ---- install ----------------------------------------------------------------------------------------------------


class VerifyError(Exception):
    """A downloaded file is not the published one (size or SHA-256)."""


def verify(path: Path, asset: Asset) -> None:
    """Mandatory: the asset's published SHA-256 (and size, when published) must be the file's."""
    if not asset.sha256:
        raise VerifyError(f"{asset.name}: нет опубликованной контрольной суммы, установка запрещена")
    size = path.stat().st_size
    if asset.size is not None and size != asset.size:
        raise VerifyError(f"{asset.name}: размер {size} байт, в релизе {asset.size}: файл скачан не полностью "
                          "или подменён (страница прокси?)")
    got = downloads.sha256_of(path)
    if got != asset.sha256:
        raise VerifyError(f"{asset.name}: SHA-256 {got[:12]}…, в релизе {asset.sha256[:12]}…: файл повреждён "
                          "или подменён, не устанавливаю")


def _into_uploads(src: Path, home: Path, name: str, move: bool) -> Path:
    folder = update.uploads_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / update.upload_name(name)
    part = folder / f".{dest.name}.part"
    if move:
        shutil.move(src, part)
    else:
        shutil.copyfile(src, part)
    os.replace(part, dest)
    return dest


def _job_update(**changes: Any) -> None:
    with _lock:
        if _job is not None:
            _job.update(changes)


def job() -> dict[str, Any] | None:
    with _lock:
        return dict(_job) if _job is not None else None


def _install(path: Path, home: Path, restart: Callable[[], None] | None, unavailable: str | None) -> dict[str, Any]:
    """Stage ``path`` (an uploaded zip) and ask the launcher to install it; the staged document, or ``ready``
    when this server cannot install (a checkout)."""
    if restart is None or unavailable:
        return {"ready": True, "message": unavailable or "Установка из интерфейса недоступна."}
    root = update.bundle_root()
    assert root is not None
    with update.INSTALL_LOCK:
        if update.state(home)["pending"]:
            raise update.UpdateError("Обновление уже запрошено: дождитесь перезапуска.")
        staged = update.stage(path, root, home)
        update.request(staged, home)
    restart()
    return staged


def _finish(path: Path, asset: Asset, rel: Release, home: Path, restart, unavailable, retry_full) -> bool:
    """Install the verified upload ``path``; a patch that does not fit: False when ``retry_full`` should run."""
    _job_update(state="staging", message=f"Проверяю и готовлю {asset.name}…", file=path.name)
    try:
        out = _install(path, home, restart, unavailable)
    except update.UpdateError as e:
        if asset.kind == "patch" and full_of(rel) is not None and retry_full:
            _job_update(message=f"Заплатка не подошла ({e}); беру полный архив…")
            return False
        _job_update(state="failed", message=str(e), finished_at=now())
        return True
    if out.get("ready"):
        _job_update(state="ready", message=f"Архив {asset.name} скачан и проверен (лежит в загруженных). "
                                           f"{out['message']}", finished_at=now())
    else:
        _job_update(state="restarting", message=f"Устанавливаю {rel.version}: сервер перезапустится (около минуты).",
                    target=rel.version, finished_at=now())
    return True


def _server_download(ctx: net.Context, order: list[str], asset: Asset, home: Path) -> tuple[Path | None, list]:
    attempts: list[dict[str, Any]] = []
    for layer in order:
        if _cancel.is_set():
            break
        _job_update(state="downloading", layer=layer, bytes=0, total=asset.size,
                    message=f"Скачиваю {asset.name} (способ {layer})…")
        t0 = time.monotonic()
        try:
            r = net.fetch(layer, ctx, asset.url, update.MAX_ZIP_BYTES, progress=lambda n: _job_update(bytes=n))
        except net.NetError as e:
            attempts.append({"ok": False, "ms": int((time.monotonic() - t0) * 1000), **e.public()})
            _job_update(attempts=attempts)
            continue
        _job_update(state="verifying", message=f"Проверяю контрольную сумму {asset.name}…")
        try:
            verify(r.path, asset)
        except VerifyError as e:
            r.path.unlink(missing_ok=True)
            attempts.append({"ok": False, "layer": layer, "code": "checksum", "message": str(e),
                             "what": "файл не совпал с релизом"})
            _job_update(attempts=attempts)
            continue
        attempts.append({"ok": True, "layer": layer, "ms": int((time.monotonic() - t0) * 1000)})
        _job_update(attempts=attempts)
        write_state(home, {"prefer": layer, "download": {"ok": True, "layer": layer, "at": now()}})
        return _into_uploads(r.path, home, asset.name, move=True), attempts
    if attempts:
        write_state(home, {"download": {"ok": False, "at": now(), "attempts": attempts}})
    return None, attempts


def _watch(folder: Path, asset: Asset, deadline: float) -> Path | None:
    """Wait for the browser's copy of ``asset`` in ``folder`` (or cancel / timeout); the verified file."""
    while not _cancel.is_set() and time.monotonic() < deadline:
        found = downloads.find_expected(folder, asset.name, asset.size, asset.sha256 or "")
        _job_update(found=found.public(), bytes=found.size if found.state == "downloading" else None)
        if found.state == "found" and found.path is not None:
            return found.path
        _cancel.wait(WATCH_POLL_S)
    return None


def _obtain(st: Settings, asset: Asset, browser_first: bool) -> Path | None:
    """The verified zip in the uploads folder: downloaded by the server, else (or ``browser_first``) by the
    browser into the downloads folder; None when cancelled or not found in time (the job says why)."""
    home = st.home
    order = [] if browser_first else layers(st, home)
    if order:
        path, _ = _server_download(context(st, 120.0), order, asset, home)
        if path is not None or _cancel.is_set():
            return path
    folder = downloads.downloads_dir(st.update_downloads_dir)
    _job_update(state="waiting_browser", via="browser", layer="browser", asset=asset.public(), browser_url=asset.url,
                downloads_dir=str(folder),
                message=("Скачайте архив браузером (кнопка ниже, если загрузка не началась сама): "
                         f"жду {asset.name} в папке {folder}."))
    found = _watch(folder, asset, time.monotonic() + WATCH_TIMEOUT_S)
    if found is None:
        if not _cancel.is_set():
            _job_update(state="failed", finished_at=now(),
                        message=f"За {WATCH_TIMEOUT_S // 60} минут {asset.name} не появился в {folder}. Проверьте "
                                "папку загрузок (ниже) или перетащите скачанный zip на страницу.")
        return None
    _job_update(state="verifying", message=f"Нашёл {found.name}, проверяю…")
    verify(found, asset)
    return _into_uploads(found, home, asset.name, move=False)


def _run(st: Settings, rel: Release, asset: Asset, browser_first: bool, restart, unavailable) -> None:
    full = full_of(rel)
    plan = [asset] + ([full] if asset.kind == "patch" and full is not None else [])
    try:
        for i, a in enumerate(plan):
            path = _obtain(st, a, browser_first)
            if path is None or _finish(path, a, rel, st.home, restart, unavailable, retry_full=i + 1 < len(plan)):
                break
        if _cancel.is_set():
            _job_update(state="cancelled", message="Отменено.", finished_at=now())
    except (VerifyError, update.UpdateError, OSError) as e:
        _job_update(state="failed", message=str(e), finished_at=now())
    except Exception as e:  # noqa: BLE001 - the job reports, the server lives on
        _job_update(state="failed", message=f"Ошибка: {e}", finished_at=now())


def start_install(st: Settings, *, allow_older: bool = False, via: str | None = None,
                  restart: Callable[[], None] | None = None, unavailable: str | None = None) -> dict[str, Any]:
    """Start installing the latest known release (``via`` ``browser`` skips the server's own download); the job.
    UpdateError when there is nothing to install or a job runs."""
    global _job
    s = read_state(st.home)
    rel = from_public(s.get("release"))
    if rel is None or s.get("repo", st.update_repo) != st.update_repo:
        raise update.UpdateError("Сначала проверьте наличие новой версии.")
    choice = choose(rel, update.current_version(), allow_older)
    if choice.asset is None:
        raise update.UpdateError(choice.reason or "Нечего устанавливать.")
    with _lock:
        if _job is not None and _job.get("state") in ("downloading", "verifying", "waiting_browser", "staging"):
            raise update.UpdateError("Обновление уже идёт.")
        # the server's own check reaches release.json on github.com, the same hosts as the download: without it
        # the browser downloads at once (while the click still counts as the owner's, for the browser)
        browser_first = via == "browser" or not layers(st, st.home) or not (s.get("check") or {}).get("ok")
        folder = downloads.downloads_dir(st.update_downloads_dir)
        _cancel.clear()
        _job = {"id": uuid.uuid4().hex[:8], "state": "starting", "via": "browser" if browser_first else "server",
                "version": rel.version, "asset": choice.asset.public(), "started_at": now(), "message": None,
                "browser_url": choice.asset.url if browser_first else None, "downloads_dir": str(folder),
                "bytes": None, "total": choice.asset.size, "attempts": [], "found": None, "layer": None}
        out = dict(_job)
    threading.Thread(target=_run, args=(st, rel, choice.asset, browser_first, restart, unavailable),
                     name="desk-update-install", daemon=True).start()
    return out


def cancel() -> dict[str, Any] | None:
    _cancel.set()
    return job()


def install_found(st: Settings, path: Path, sha256: str, restart, unavailable) -> dict[str, Any]:
    """Install a zip found in the downloads folder by ``scan``: its checksum must still be ``sha256``."""
    name = downloads._original(path.name)
    if downloads.sha256_of(path) != sha256:
        raise update.UpdateError(f"{path.name} изменился после проверки: найдите заново.")
    dest = _into_uploads(path, st.home, name, move=False)
    out = _install(dest, st.home, restart, unavailable)
    return {"name": dest.name, "restarting": not out.get("ready"), "version": out.get("version"),
            "message": out.get("message")}


# ---- diagnostics ------------------------------------------------------------------------------------------------


def diagnose(st: Settings) -> dict[str, Any]:
    """Every layer against every target (``net.TARGETS``), in parallel, a short timeout; stored and returned.
    The first layer that reaches github.com and the file storage becomes the preferred one."""
    ctx = context(st, 15.0)
    jobs: dict[tuple[str, str], Any] = {}
    out: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for layer in net.LAYERS:
            why = net.available(layer)
            out[layer] = {"unavailable": why} if why else {}
            if not why:
                for key, url, _ in net.TARGETS:
                    jobs[(layer, key)] = pool.submit(net.probe, layer, ctx, url)
        for (layer, key), fut in jobs.items():
            try:
                out[layer][key] = fut.result()
            except Exception as e:  # noqa: BLE001
                out[layer][key] = {"ok": False, "code": "failed", "message": str(e)}
    works = [la for la in net.LAYERS if not out[la].get("unavailable") and out[la].get("github", {}).get("ok")
             and (out[la].get("assets", {}).get("ok") or out[la].get("objects", {}).get("ok"))]
    doc = {"at": now(), "layers": out, "working": works,
           "targets": [{"key": k, "url": u, "label": label} for k, u, label in net.TARGETS],
           "proxy": {la: next((r.get("proxy") for r in out[la].values() if isinstance(r, dict) and r.get("proxy")),
                              None) for la in net.LAYERS}}
    changes: dict[str, Any] = {"diagnose": doc}
    if works and st.update_transport == "auto":
        changes["prefer"] = works[0]
    write_state(st.home, changes)
    return doc


def take_browser_diag(st: Settings, body: dict[str, Any]) -> dict[str, Any]:
    """What the page found from the browser: ``api`` (a CORS request to the API) and ``github`` / ``assets``
    (no-cors requests: they only tell the network path is open), each ``{ok, ms, error}``."""
    rec = {"at": now()}
    for key in ("api", "github", "assets"):
        v = body.get(key)
        if isinstance(v, dict):
            rec[key] = {"ok": bool(v.get("ok")), "ms": v.get("ms") if isinstance(v.get("ms"), int) else None,
                        "error": str(v.get("error"))[:200] if v.get("error") else None}
    s = read_state(st.home)
    return write_state(st.home, {"diagnose": {**(s.get("diagnose") or {}), "browser": rec}})


# ---- what the page shows ----------------------------------------------------------------------------------------


def status(st: Settings, can_install: bool, unavailable: str | None) -> dict[str, Any]:
    s = read_state(st.home)
    current = update.current_version()
    same_repo = s.get("repo", st.update_repo) == st.update_repo
    rel = from_public(s.get("release")) if same_repo else None
    choice = choose(rel, current) if rel else None
    chk = s.get("check") if same_repo else None
    server_ok = bool(chk and chk.get("ok"))
    browser = s.get("browser") or {}
    due = (not server_ok or st.update_transport == "browser") and \
        _age(browser.get("checked_at")) > BROWSER_CHECK_EVERY_S
    root = update.bundle_root()
    point = update.rollback_point(root, st.home) if root is not None else None
    folder = downloads.downloads_dir(st.update_downloads_dir)
    return {
        "repo": st.update_repo, "current_version": current, "can_install": can_install, "unavailable": unavailable,
        "transport": st.update_transport, "auto_check": st.update_auto_check and root is not None,
        "auto_check_setting": st.update_auto_check,
        "downloads_dir": str(folder), "downloads_dir_set": st.update_downloads_dir is not None,
        "downloads_dir_exists": folder.is_dir(),
        "checked_at": s.get("checked_at") if same_repo else None, "checking": _checking.is_set(), "check": chk,
        "browser": browser or None, "browser_check_due": due,
        "release": rel.public() if rel else None, "no_release": bool(s.get("no_release")) and same_repo,
        "choice": choice.public() if choice else None,
        "update_available": bool(choice and choice.asset is not None and choice.compare == "newer"),
        "job": job(), "rollback": {"version": point["version"]} if point else None,
        "diagnose": s.get("diagnose"), "prefer": s.get("prefer"), "download": s.get("download"),
        "layers": {la: net.available(la) for la in net.LAYERS},
        "releases_page": releases_page(st.update_repo), "api_url": api_latest_url(st.update_repo),
        "codes": net.CODES, "it_hints": net.IT_HINTS,
    }


def known_sums(st: Settings) -> dict[str, str]:
    """The checksums of the last release the check (server or browser) read: asset name -> sha256."""
    rel = from_public(read_state(st.home).get("release"))
    return {a.name: a.sha256 for a in rel.assets if a.sha256} if rel else {}


def scan(st: Settings) -> dict[str, Any]:
    """The downloads folder's DeskMonitor zips, verified against saved SHA256SUMS / release.json and the known
    release's checksums."""
    folder = downloads.downloads_dir(st.update_downloads_dir)
    current = update.current_version()
    items = downloads.scan_folder(folder, known_sums(st))
    for it in items:
        it["compare"] = update.compare(it["version"], current)
    return {"downloads_dir": str(folder), "exists": folder.is_dir(), "items": items}
