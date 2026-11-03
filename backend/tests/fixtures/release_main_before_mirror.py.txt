"""A GitHub Release of the monitor as the updater reads it, and the zip it picks for this installation.

The release workflow (``.github/workflows/release.yml``) publishes, for a tag ``v<backend version>``:

* ``DeskMonitor-<version>.zip``                    the full Windows bundle, never with the private adapter
* ``DeskMonitor-patch-<old>-to-<version>.zip``     a patch from the previous release's bundle (when there is one)
* ``SHA256SUMS``                                   ``<sha256>  <asset name>`` of every zip (``sha256sum`` format)
* ``release.json``                                 the same, machine-readable (``release_doc``), plus the version,
                                                   the commit and the core pin

Three ways in, one result (``Release``): ``release.json`` fetched by the server from the stable address
``https://github.com/<repo>/releases/latest/download/release.json`` (no API, no rate limit), the API's
``/repos/<repo>/releases/latest`` fetched by the server, or the same API document fetched by the browser (CORS
allows it) and posted to the server. An asset's checksum comes from ``release.json``, ``SHA256SUMS`` (also copied
into the release notes) and the API's own ``digest`` (GitHub computes it at upload); every one known must agree,
and an asset without any checksum is never installed (``Asset.sha256`` None).

A latest release without update files (made by hand on GitHub, or a tag without ``v`` the workflow ignores:
``EmptyReleaseError``) is skipped: ``first_usable`` takes the newest earlier release that has them and keeps why the
newer ones were skipped (``Release.skipped``). Nothing else is skipped: a checksum that disagrees stops the check.

Asset addresses are only ever ``https://github.com/<repo>/releases/download/<tag>/<name>``: a posted document cannot
make the server fetch anything else.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import update

RELEASE_JSON = "release.json"
SUMS_NAME = "SHA256SUMS"
SCHEMA = 1
_SHA = re.compile(r"[0-9a-f]{64}")
_SUM_LINE = re.compile(r"^([0-9a-fA-F]{64}) [ *]?(\S+)\s*$")
_FULL = re.compile(r"DeskMonitor-(?!patch-)(?P<v>[0-9A-Za-z][0-9A-Za-z._+-]*?)(?:-with-adapter)?\.zip")
_PATCH = re.compile(r"DeskMonitor-patch-(?P<base>[0-9A-Za-z][0-9A-Za-z._+-]*?)-to-(?P<v>[0-9A-Za-z][0-9A-Za-z._+-]*)"
                    r"\.zip")
_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")
LIST_LIMIT = 10  # the releases the fallback looks through (newest first)


class ReleaseError(ValueError):
    """The release cannot be used, in the owner's words."""


class EmptyReleaseError(ReleaseError):
    """The release has no update files (no full ``DeskMonitor-<version>.zip``): an earlier one may be used."""

    def __init__(self, tag: str, message: str) -> None:
        super().__init__(message)
        self.tag = tag


def empty_reason(tag: str) -> str:
    """Why the release ``tag`` has no update files, and what to do, in the owner's words."""
    why = (f"тег «{tag}» без «v»: файлы публикует только выпуск по тегу v<версия>, релиз, созданный вручную на GitHub, "
           "остаётся пустым" if not tag.lower().startswith("v")
           else "выпуск по тегу ещё идёт или не удался (GitHub → Actions → release)")
    return f"Релиз {tag} без файлов обновления (нет release.json и DeskMonitor-<версия>.zip): {why}."


@dataclass
class Asset:
    name: str
    kind: str  # "full" or "patch"
    version: str
    base: str | None  # a patch: the version it applies to
    size: int | None
    sha256: str | None  # None: no checksum published, never installed
    url: str

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Release:
    repo: str
    tag: str
    version: str
    published_at: str | None
    html_url: str
    assets: list[Asset]
    source: str  # "release.json", "api" (server) or "browser"
    notes: str = ""
    adapter_included: bool = False
    core: str | None = None
    commit: str | None = None
    skipped: list[dict[str, str]] = field(default_factory=list)  # newer releases without files: {tag, reason}
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        return d


@dataclass
class Choice:
    """What to install from a release: ``asset`` None when nothing fits (``reason`` says why)."""

    compare: str  # desk.update.compare(release version, installed)
    asset: Asset | None
    reason: str | None
    needs_confirm: bool  # older or unordered: only when the owner asks explicitly

    def public(self) -> dict[str, Any]:
        return {"compare": self.compare, "asset": self.asset.public() if self.asset else None, "reason": self.reason,
                "needs_confirm": self.needs_confirm}


# ---- checksums ----------------------------------------------------------------------------------------------


def parse_sums(text: str) -> dict[str, str]:
    """``sha256sum`` output (``<hex>  <name>`` or ``<hex> *<name>``) as name -> lowercase hex; other lines are
    ignored, so the checksum block of a release's notes reads too."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _SUM_LINE.match(line.strip().strip("`"))
        if m and _ASSET_NAME.fullmatch(m[2]):
            out[m[2]] = m[1].lower()
    return out


def agree(name: str, *values: str | None) -> str | None:
    """The one checksum the sources give for ``name`` (None when none does); ReleaseError when two differ."""
    known = {v.lower() for v in values if v}
    bad = [v for v in known if not _SHA.fullmatch(v)]
    if bad:
        raise ReleaseError(f"{name}: непонятная контрольная сумма {bad[0][:20]!r}")
    if len(known) > 1:
        raise ReleaseError(f"{name}: контрольные суммы в релизе не совпадают ({', '.join(sorted(known))}): "
                           "релиз повреждён или подменён, не устанавливаю.")
    return next(iter(known), None)


def _digest(value: object) -> str | None:
    """The API's asset ``digest``: ``sha256:<hex>``."""
    if isinstance(value, str) and value.startswith("sha256:"):
        return value.split(":", 1)[1].lower()
    return None


# ---- names and addresses ------------------------------------------------------------------------------------


def classify_name(name: str) -> tuple[str, str, str | None] | None:
    """(kind, version, base) of an update zip's name, None for any other asset."""
    if m := _PATCH.fullmatch(name):
        return "patch", m["v"], m["base"]
    if m := _FULL.fullmatch(name):
        return "full", m["v"], None
    return None


def download_url(repo: str, tag: str, name: str) -> str:
    q = urllib.parse.quote
    return f"https://github.com/{repo}/releases/download/{q(tag, safe='')}/{q(name, safe='')}"


def latest_url(repo: str, name: str) -> str:
    """The stable address of an asset of the latest release (github.com redirects it, no API)."""
    return f"https://github.com/{repo}/releases/latest/download/{urllib.parse.quote(name, safe='')}"


def api_latest_url(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}/releases/latest"


def api_list_url(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}/releases?per_page={LIST_LIMIT}"


def releases_page(repo: str) -> str:
    return f"https://github.com/{repo}/releases/latest"


def check_asset_url(repo: str, url: object) -> str:
    """``url`` when it is a release download of ``repo`` on github.com; ReleaseError otherwise."""
    if not isinstance(url, str):
        raise ReleaseError("адрес файла релиза не указан")
    u = urllib.parse.urlsplit(url)
    prefix = f"/{repo}/releases/download/".lower()
    if u.scheme != "https" or u.hostname != "github.com" or u.port not in (None, 443) or u.username or u.query \
            or not u.path.lower().startswith(prefix) or ".." in u.path:
        raise ReleaseError(f"адрес {url[:120]!r} не похож на файл релиза {repo} на github.com")
    return url


# ---- reading ------------------------------------------------------------------------------------------------


def _asset(repo: str, tag: str, name: object, size: object, sha: str | None, url: object = None) -> Asset | None:
    if not isinstance(name, str) or not _ASSET_NAME.fullmatch(name):
        return None
    kind = classify_name(name)
    if kind is None:
        return None
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
        raise ReleaseError(f"{name}: непонятный размер {size!r}")
    if size is not None and size > update.MAX_ZIP_BYTES:
        raise ReleaseError(f"{name}: больше {update.MAX_ZIP_BYTES >> 20} МБ")
    want = download_url(repo, tag, name)
    if url is not None:
        check_asset_url(repo, url)
        if urllib.parse.unquote(str(url)).lower() != urllib.parse.unquote(want).lower():
            raise ReleaseError(f"{name}: адрес {url!r} не соответствует тегу {tag}")
    return Asset(name, kind[0], kind[1], kind[2], size, sha, want)


def _tag(doc: dict[str, Any]) -> str:
    tag = doc.get("tag") if "tag" in doc else doc.get("tag_name")
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise ReleaseError(f"непонятный тег релиза {tag!r}")
    return tag


def _full_version(assets: list[Asset]) -> str:
    fulls = sorted({a.version for a in assets if a.kind == "full"})
    if len(fulls) != 1:
        raise ReleaseError("в релизе нет архива DeskMonitor-<версия>.zip" if not fulls
                           else f"в релизе несколько полных архивов разных версий: {', '.join(fulls)}")
    return fulls[0]


def from_release_json(doc: object, repo: str) -> Release:
    """``release.json`` of the workflow (``release_doc``)."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ReleaseError("release.json другой версии или повреждён: обновите программу вручную один раз")
    tag = _tag(doc)
    raw = doc.get("assets")
    if not isinstance(raw, list):
        raise ReleaseError("release.json: нет списка assets")
    assets = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        sha = agree(str(a.get("name")), a.get("sha256") if isinstance(a.get("sha256"), str) else None)
        item = _asset(repo, tag, a.get("name"), a.get("size"), sha)
        if item:
            assets.append(item)
    version = doc.get("version")
    if not isinstance(version, str) or not update.VERSION_RE.fullmatch(version):
        raise ReleaseError(f"release.json: непонятная версия {version!r}")
    if _full_version(assets) != version:
        raise ReleaseError(f"release.json: версия {version} не совпадает с полным архивом")
    return Release(repo=repo, tag=tag, version=version, published_at=_s(doc.get("published_at")),
                   html_url=f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(tag, safe='')}",
                   assets=assets, source="release.json", notes=_s(doc.get("notes")) or "",
                   adapter_included=bool(doc.get("adapter_included")), core=_s(doc.get("core")),
                   commit=_s(doc.get("commit")))


def from_api(doc: object, repo: str, source: str = "api", sums_text: str | None = None,
             release_json: dict[str, Any] | None = None) -> Release:
    """The API's release document (``GET /repos/<repo>/releases/latest``), the checksums of its ``digest``
    fields, of the notes and of ``sums_text`` (a fetched ``SHA256SUMS``) and of ``release_json`` if fetched."""
    if not isinstance(doc, dict):
        raise ReleaseError("ответ GitHub API не похож на релиз")
    if doc.get("draft") or doc.get("prerelease"):
        raise ReleaseError("последний релиз — черновик или предварительный: не устанавливается")
    tag = _tag(doc)
    body = doc.get("body") if isinstance(doc.get("body"), str) else ""
    from_notes = parse_sums(body)
    from_file = parse_sums(sums_text or "")
    from_json = {a.get("name"): a.get("sha256") for a in (release_json or {}).get("assets", [])
                 if isinstance(a, dict) and isinstance(a.get("sha256"), str)}
    assets = []
    for a in doc.get("assets") or []:
        if not isinstance(a, dict) or not isinstance(a.get("name"), str):
            continue
        name = a["name"]
        if classify_name(name) is None:
            continue
        sha = agree(name, _digest(a.get("digest")), from_notes.get(name), from_file.get(name), from_json.get(name))
        item = _asset(repo, tag, name, a.get("size"), sha, a.get("browser_download_url"))
        if item:
            assets.append(item)
    if not any(a.kind == "full" for a in assets):
        raise EmptyReleaseError(tag, empty_reason(tag))
    version = _full_version(assets)
    sums_asset = next((a for a in doc.get("assets") or [] if isinstance(a, dict) and a.get("name") == SUMS_NAME),
                      None)
    return Release(repo=repo, tag=tag, version=version, published_at=_s(doc.get("published_at")),
                   html_url=f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(tag, safe='')}",
                   assets=assets, source=source, notes=body[:4000],
                   adapter_included=any(a.name.endswith("-with-adapter.zip") for a in assets),
                   extra={"sums_url": download_url(repo, tag, SUMS_NAME) if sums_asset else None})


def first_usable(docs: object, repo: str, source: str = "api",
                 sums_for: Callable[[dict[str, Any]], str | None] | None = None,
                 ) -> tuple[Release | None, list[dict[str, str]]]:
    """The newest published release of ``docs`` (the API's release list, newest first) that has update files, its
    ``skipped`` the newer ones without; (None, skipped) when none has. Drafts and pre-releases are passed over;
    any other ReleaseError is raised (a checksum that disagrees is never skipped). ``sums_for(doc)``: the text of
    that release's ``SHA256SUMS`` (fetched only for a release that is tried)."""
    if not isinstance(docs, list):
        raise ReleaseError("ответ GitHub API не похож на список релизов")
    skipped: list[dict[str, str]] = []
    for doc in docs[:LIST_LIMIT]:
        if not isinstance(doc, dict) or doc.get("draft") or doc.get("prerelease"):
            continue
        try:
            rel = from_api(doc, repo, source, sums_text=sums_for(doc) if sums_for else None)
        except EmptyReleaseError as e:
            skipped.append({"tag": e.tag, "reason": str(e)})
            continue
        rel.skipped = skipped
        return rel, skipped
    return None, skipped


def _s(v: object) -> str | None:
    return v if isinstance(v, str) else None


def from_public(doc: object) -> Release | None:
    """A ``Release.public()`` dict read back (the updater's state file); None when it does not read."""
    if not isinstance(doc, dict):
        return None
    try:
        assets = [Asset(**a) for a in doc.get("assets") or []]
        return Release(**{**{k: v for k, v in doc.items() if k != "assets"}, "assets": assets})
    except TypeError:
        return None


# ---- choosing ---------------------------------------------------------------------------------------------------


def choose(rel: Release, current: str, allow_older: bool = False) -> Choice:
    """The zip to install over ``current``: a patch made from exactly ``current`` when the release has one, else the
    full zip. A release that is not newer is offered only with ``allow_older`` (and then the full zip)."""
    cmp = update.compare(rel.version, current)
    if cmp == "same":
        return Choice(cmp, None, f"Установлена последняя версия ({current}).", False)
    needs = cmp in ("older", "other")
    if needs and not allow_older:
        why = ("Версия на GitHub старее установленной" if cmp == "older"
               else "Какая из версий новее, неизвестно (другая сборка)")
        return Choice(cmp, None, f"{why}: {rel.version}, установлена {current}. Установить можно только явно.", True)
    if rel.adapter_included:
        return Choice(cmp, None, "В публичном релизе оказался приватный адаптер: такой релиз не устанавливается.",
                      needs)
    patch = next((a for a in rel.assets if a.kind == "patch" and a.base == current and a.version == rel.version),
                 None)
    full = next((a for a in rel.assets if a.kind == "full" and a.version == rel.version), None)
    asset = patch if patch is not None and not needs else full
    if asset is None:
        return Choice(cmp, None, "В релизе нет подходящего архива.", needs)
    if asset.sha256 is None:
        return Choice(cmp, None, f"У {asset.name} нет опубликованной контрольной суммы (SHA256SUMS): не устанавливаю.",
                      needs)
    return Choice(cmp, asset, None, needs)


def full_of(rel: Release) -> Asset | None:
    return next((a for a in rel.assets if a.kind == "full" and a.version == rel.version), None)


# ---- writing (the release workflow) ------------------------------------------------------------------------------


def release_doc(tag: str, version: str, files: list[tuple[str, int, str]], *, commit: str | None = None,
                core: str | None = None, published_at: str | None = None, notes: str = "") -> dict[str, Any]:
    """``release.json``: ``files`` (name, size, sha256) of the zips."""
    assets = []
    for name, size, sha in files:
        kind = classify_name(name)
        if kind is None:
            raise ReleaseError(f"{name}: не архив DeskMonitor")
        if kind[0] == "full" and name.endswith("-with-adapter.zip"):
            raise ReleaseError(f"{name}: архив с приватным адаптером не публикуется")
        assets.append({"name": name, "kind": kind[0], "version": kind[1], "base": kind[2], "size": size,
                       "sha256": sha})
    return {"schema": SCHEMA, "tag": tag, "version": version, "commit": commit, "core": core,
            "published_at": published_at, "adapter_included": False, "notes": notes, "assets": assets}


def sums_text(files: list[tuple[str, int, str]]) -> str:
    return "".join(f"{sha}  {name}\n" for name, _, sha in files)
