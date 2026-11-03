"""Dependency installer of the "no packages" Windows bundle: stdlib + pip's vendored ``packaging`` (PEP 440), run by
the bundled Python. The third-party wheels come from the machine's package index (an internal mirror: whatever
pip is configured for, pip.ini, PIP_INDEX_URL, PIP_EXTRA_INDEX_URL, cert, proxy, trusted-host); this module never
touches the network itself, every request is a ``python -m pip`` subprocess run with the ``pip_env`` it is given.

    install(python=, wheelhouse=, log_dir=, settings=, pip_env=, cache=, say=) -> Result
    load_settings(*paths) -> Settings           first existing ``mirror.ini`` (section ``[mirror]``), else defaults
    InstallError                                a message for a person (Russian) with the path of the logs

Inputs: ``wheelhouse\\mirror.json`` (``schema`` 1: ``local`` our own wheels as ``name==version``, ``packages`` every
other pinned package with its lock version and who requires it under which PEP 440 specifier, ``imports`` the
modules to import afterwards), ``wheelhouse\\requirements.txt`` (must agree with it) and our wheels in the wheelhouse.
``python`` is the interpreter of a new, empty venv (it must have pip).

Algorithm (only wheels, ``--only-binary=:all:``, nothing is built):
1. All lock versions plus our wheels are checked by the pip resolver at once (``pip install --dry-run --report``).
   Clean (nothing outside the lock is pulled in) means every package is ``as_lock``: one pip call, no index queries.
2. Otherwise the versions each package has on the index for this interpreter are asked of pip
   (``pip index versions --only-binary=:all:``; chosen over ``pip download``/dry-run probing because it is one
   cheap call per package that honours the same configuration, wheel tags and yanking; its text output is parsed,
   it is the only format every pip since 23.1 prints). A package whose lock version is there stays; for each other
   one the candidates are the available versions that satisfy every ``requires`` specifier, with no pre-release
   unless the lock itself is one, in this order (``order_candidates``; deterministic):
   the lock version, then the same ``major.minor`` as the lock (below it, nearest first, then above it, nearest
   first), then every older version (nearest first), then every newer version (nearest first).
   Strict mode (``Settings.strict``) tries the lock version only and refuses on any difference.
3. The candidates of the replaced packages are tried in that order (the first replaced package varies slowest, at
   most ``MAX_ATTEMPTS`` resolver runs): a combination the resolver rejects, or that pulls in a package outside the
   lock, goes to the next. When none is clean, the first one that resolves is taken and what it pulled in is
   reported as ``added`` (a deviation); strict mode refuses that too.
4. Every chosen wheel is downloaded (``pip download --no-deps``) into ``cache``; the SHA-256 is computed here, kept
   in ``cache\\sha256.json`` and a later run reuses a cached wheel whose SHA-256 still matches (nothing is downloaded
   again after a failed run; a rerun with every wheel cached resolves offline and needs no index at all). The
   source URL comes from pip's report and is kept next to the SHA-256. Mind that the resolver runs of step 1/3 let
   pip itself fetch wheels (pip 24 does so for ``--report``), pip's own HTTP cache applies; the downloads of this
   module are the ones skipped by the cache. Then pip installs from the cache and the
   wheelhouse only (``--no-index``), ``pip check`` runs and the ``imports`` are imported in the new venv.
5. ``install-<YYYYMMDD-HHMMSS>.log`` (Russian, for a person) and ``.json`` (for programs) in ``log_dir``: Python,
   pip, index addresses, mode, and for each package ``required``, ``tried``, ``available``, ``installed``, ``url``,
   ``sha256``, ``status`` (``as_lock`` / ``differs`` / ``not_found`` / ``added``), the summary and ``ok`` / ``failed``.
   They are written on a refusal too. Index addresses and everything quoted from pip are masked: ``user:pass@``
   becomes ``***@`` and the values of secret-looking query parameters (token, password, key, secret, auth, ...)
   become ``***``. ``available`` of an ``as_lock`` package found by step 1 holds just the lock version (it is all
   that was asked of the index).
The final install is ``--require-hashes`` over a staging folder holding exactly the logged files (ours included), and
a downloaded file must match the file name and hash of pip's resolver report. An unreachable index or a refused
login (pip's network error words, or no version of any package at all) is reported as such, package status
``index_error``, not as missing versions.
Machine pip configuration is never disabled here; ``Settings.index_url`` / ``extra_index_url``, when set, are
passed explicitly (``--index-url`` / ``--extra-index-url``).
"""

from __future__ import annotations

import configparser
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

try:
    from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
    from pip._vendor.packaging.version import InvalidVersion, Version
except ImportError:  # a Python without pip (development checkout); the bundled one always has it
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.tags import sys_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename
    from packaging.version import InvalidVersion, Version

SCHEMA = 1
MAX_ATTEMPTS = 25
PIP_TIMEOUT_S = 1800
INDEX_QUERY_WORKERS = 6
TAIL_LINES = 25
SECRET_QUERY_WORDS = ("token", "password", "passwd", "pwd", "key", "secret", "auth", "sig", "credential",
                      "session", "bearer")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&;][^=&\s'\"]*(?:" + "|".join(SECRET_QUERY_WORDS) + r")[^=&\s'\"]*=)[^&\s'\"]*")
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s'\"]+@")
_NETWORK_MARKERS = (
    "could not fetch url", "max retries exceeded", "newconnectionerror", "connection refused", "connectionerror",
    "failed to establish a new connection", "sslerror", "proxyerror", "certificate_verify_failed",
    "name or service not known", "temporary failure in name resolution", "getaddrinfo failed",
    "connection timed out", "read timed out", "connect timeout", "connection reset", "remotedisconnected",
    "unauthorized", "forbidden", "401 error", "403 error", "proxy authentication", "401 client error", "403 client error",
    "407 client error", "server error")


class InstallError(Exception):
    """A failure with a message for a person (Russian); ``log_path`` / ``json_path`` name the journals."""

    def __init__(self, message: str, log_path: Path | None = None, json_path: Path | None = None):
        self.log_path = log_path
        self.json_path = json_path
        text = message if log_path is None else f"{message}\nЖурнал: {log_path}"
        super().__init__(text)


@dataclass(frozen=True)
class Settings:
    index_url: str = ""
    extra_index_url: str = ""
    strict: bool = False


@dataclass
class Result:
    installed: list[str] = field(default_factory=list)      # "name==version", ours included
    not_found: list[str] = field(default_factory=list)      # packages with no usable version on the index
    deviations: list[str] = field(default_factory=list)     # what differs from the lock, one line each
    log_path: Path | None = None
    json_path: Path | None = None
    ok: bool = False


def load_settings(*paths: Path) -> Settings:
    """The first existing file of ``paths`` (INI, section ``[mirror]``); no file means the defaults."""
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read_string(path.read_text(encoding="utf-8-sig"))
        except (configparser.Error, OSError, UnicodeDecodeError) as exc:
            raise InstallError(f"Не удалось прочитать настройки зеркала {path}: {exc}") from exc
        sec = cp["mirror"] if cp.has_section("mirror") else {}
        raw = str(sec.get("strict", "false")).strip().lower()
        if raw in ("1", "true", "yes", "on"):
            strict = True
        elif raw in ("", "0", "false", "no", "off"):
            strict = False
        else:
            raise InstallError(f"В {path} параметр strict должен быть true или false, а не «{raw}».")
        return Settings(index_url=str(sec.get("index_url", "")).strip(),
                        extra_index_url=str(sec.get("extra_index_url", "")).strip(), strict=strict)
    return Settings()


# --- masking -------------------------------------------------------------------------------------------------------


def mask_url(url: str) -> str:
    """``https://user:pw@host/p?token=x`` -> ``https://***@host/p?token=***``."""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return mask_text(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rpartition("@")[2]
    query = _QUERY_SECRET_RE.sub(r"\1***", "?" + parts.query)[1:] if parts.query else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def mask_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Masks credentials in free text (pip output): userinfo, secret query values and the literal ``secrets``."""
    for secret in sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True):
        text = text.replace(secret, "***")  # first: a password may itself hold "@" or "?"
    text = _USERINFO_RE.sub(r"\1***@", text)
    return _QUERY_SECRET_RE.sub(r"\1***", text)


def is_network_failure(output: str) -> bool:
    """True when pip's output says the index could not be reached or refused us (not just "no such version")."""
    lines = [ln for ln in output.lower().splitlines() if "404 client error" not in ln]
    text = "\n".join(lines)
    return any(m in text for m in _NETWORK_MARKERS)


def _same_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def _secret_parts(url: str) -> list[str]:
    """The literal secrets inside a URL (the password, or a lone user part that is a token, and secret query values),
    to scrub from free text. A user name next to a password is not scrubbed as a substring (it is masked inside URLs
    only), so that it cannot damage names and versions."""
    out: list[str] = []
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return out
    if "@" in parts.netloc:
        user, colon, password = parts.netloc.rpartition("@")[0].partition(":")
        out.append(password if colon else user)
    for pair in re.split(r"[&;]", parts.query):
        key, _, value = pair.partition("=")
        if value and any(w in key.lower() for w in SECRET_QUERY_WORDS):
            out.append(value)
    return out


# --- version choice ------------------------------------------------------------------------------------------------


def order_candidates(lock: str, available: Iterable[str], specs: Iterable[str] = ()) -> list[str]:
    """Versions to try instead of ``lock``, best first: the lock itself (when available), the same major.minor
    (below, nearest first, then above, nearest first), every older version (nearest first), every newer one.
    Only versions that satisfy every spec of ``specs``; pre-releases only when ``lock`` is one."""
    lock_v = Version(lock)
    spec_sets = []
    for spec in specs:
        try:
            spec_sets.append(SpecifierSet(spec or ""))
        except InvalidSpecifier:
            continue  # a spec we cannot read does not restrict (pip still checks the real metadata)
    pre = lock_v.is_prerelease
    seen: dict[Version, str] = {}
    for text in available:
        try:
            v = Version(text)
        except InvalidVersion:
            continue
        if v.is_prerelease and not pre and v != lock_v:
            continue
        if all(s.contains(v, prereleases=True) for s in spec_sets):
            seen.setdefault(v, text)

    def minor(v: Version) -> tuple[int, int]:
        return (v.major, v.minor)

    versions = sorted(seen)
    first = [v for v in versions if v == lock_v]
    same = [v for v in versions if v != lock_v and minor(v) == minor(lock_v)]
    same_below = sorted((v for v in same if v < lock_v), reverse=True)
    same_above = sorted(v for v in same if v > lock_v)
    rest = [v for v in versions if v != lock_v and minor(v) != minor(lock_v)]
    below = sorted((v for v in rest if v < lock_v), reverse=True)
    above = sorted(v for v in rest if v > lock_v)
    return [seen[v] for v in (*first, *same_below, *same_above, *below, *above)]


def parse_index_versions(output: str) -> list[str]:
    """The versions of ``pip index versions`` output (``Available versions: 2.0, 1.0``)."""
    for line in output.splitlines():
        if line.strip().lower().startswith("available versions:"):
            return [v.strip() for v in line.split(":", 1)[1].split(",") if v.strip()]
    return []


# --- journal -------------------------------------------------------------------------------------------------------


class _Journal:
    def __init__(self, log_dir: Path, strict: bool, secrets: list[str]):
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = log_dir / f"install-{stamp}"
        n = 1
        while base.with_suffix(".log").exists() or base.with_suffix(".json").exists():
            n += 1
            base = log_dir / f"install-{stamp}-{n}"
        self.log_path = base.with_suffix(".log")
        self.json_path = base.with_suffix(".json")
        self.secrets = secrets
        self.started = datetime.now()
        self.data: dict = {"schema": SCHEMA, "started": self.started.isoformat(timespec="seconds"),
                           "mode": "strict" if strict else "flexible", "python": "", "pip": "", "index": [],
                           "packages": {}, "summary": {}, "status": "failed", "error": "", "notes": []}

    def mask(self, text: str) -> str:
        return mask_text(text, self.secrets)

    def _masked_data(self) -> dict:
        """The data for the .json: URLs through ``mask_url``, free text (error, pip output) through ``mask``; names,
        versions and hashes are never touched."""
        d = json.loads(json.dumps(self.data))
        d["error"] = self.mask(d["error"])
        for item in d["index"]:
            item["url"] = mask_url(item["url"])
        for p in d["packages"].values():
            if p["url"]:
                p["url"] = mask_url(p["url"])
        for n in d["notes"]:
            n["title"] = self.mask(n["title"])
            n["tail"] = [self.mask(x) for x in n["tail"]]
        return d

    def note(self, title: str, body: str) -> None:
        lines = self.mask(body).strip().splitlines()[-TAIL_LINES:]
        self.data["notes"].append({"title": title, "tail": lines})

    def pkg(self, name: str) -> dict:
        return self.data["packages"].setdefault(name, {"required": None, "tried": [], "available": [],
                                                        "installed": None, "url": None, "sha256": None,
                                                        "status": None})

    def write(self) -> None:
        self.data["finished"] = datetime.now().isoformat(timespec="seconds")
        masked = self._masked_data()
        self.json_path.write_text(json.dumps(masked, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                  encoding="utf-8")
        self.log_path.write_text(self.mask("\n".join(self._lines())) + "\n", encoding="utf-8")

    def _lines(self) -> list[str]:
        d = self.data
        strict = d["mode"] == "strict"
        out = ["Установка зависимостей с внутреннего зеркала пакетов",
               f"Начало: {d['started']}   Окончание: {d.get('finished', '')}",
               f"Python: {d['python']}", f"pip: {d['pip']}",
               f"Режим: {'строгий (только версии из lock)' if strict else 'гибкий (допускается ближайшая версия)'}",
               "Индекс pip:"]
        out += [f"  {i['url']}  ({i['source']}{'; переопределён mirror.ini' if i.get('overridden') else ''})"
                for i in d["index"]] or ["  (не определён)"]
        out.append(f"Пакеты ({len(d['packages'])}):")
        for name, p in sorted(d["packages"].items()):
            line = (f"  {name}: по lock {p['required'] or '-'}, установлено {p['installed'] or '-'}, "
                    f"статус {p['status'] or '-'}")
            out.append(line)
            if p["status"] != "as_lock" or p["tried"]:
                out.append(f"      перебрано: {', '.join(p['tried']) or '-'}; "
                           f"на индексе: {', '.join(p['available']) or '-'}")
            if p["url"]:
                out.append(f"      {p['url']}  sha256 {p['sha256'] or '-'}")
        s = d["summary"]
        out += ["Итог:", f"  установлено: {len(s.get('installed', []))}",
                f"  не установлено: {', '.join(s.get('not_installed', [])) or 'нет'}"]
        if s.get("deviations"):
            out.append("  ОТКЛОНЕНИЯ ОТ LOCK:")
            out += [f"    {x}" for x in s["deviations"]]
        else:
            out.append("  отклонений от lock: нет")
        out.append(f"Статус: {d['status']}")
        if d["error"]:
            out.append(f"Ошибка: {d['error']}")
        for n in d["notes"]:
            out.append(f"--- {n['title']} (последние строки) ---")
            out += n["tail"]
        return out


# --- pip -----------------------------------------------------------------------------------------------------------


@dataclass
class _Ctx:
    python: Path
    wheelhouse: Path
    settings: Settings
    env: dict[str, str]
    work: Path
    journal: _Journal
    counter: itertools.count = field(default_factory=itertools.count)


def _run(cmd: list[str], env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=PIP_TIMEOUT_S, creationflags=flags, check=False)


def _index_args(settings: Settings) -> list[str]:
    args: list[str] = []
    if settings.index_url:
        args += ["--index-url", settings.index_url]
    for url in settings.extra_index_url.split():
        args += ["--extra-index-url", url]
    return args


def _pip(ctx: _Ctx, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = [str(ctx.python), "-m", "pip", "--disable-pip-version-check", "--no-input", *args]
    try:
        return _run(cmd, ctx.env, ctx.work)
    except subprocess.TimeoutExpired as exc:
        raise _fail(ctx, f"pip не ответил за {PIP_TIMEOUT_S // 60} мин ({args[0]}).") from exc
    except OSError as exc:
        raise _fail(ctx, f"Не удалось запустить Python venv ({ctx.python}): {exc}") from exc


def _fail(ctx: _Ctx, message: str) -> InstallError:
    ctx.journal.data["error"] = message
    return InstallError(message, ctx.journal.log_path, ctx.journal.json_path)


def _output(cp: subprocess.CompletedProcess[str]) -> str:
    return (cp.stdout or "") + (cp.stderr or "")


def _index_addresses(ctx: _Ctx) -> list[dict]:
    """The index addresses, masked: mirror.ini (passed to pip explicitly, so it overrides the pip index-url) and
    always the machine configuration: ``pip config list`` (which also lists the PIP_* environment) and the
    PIP_INDEX_URL / PIP_EXTRA_INDEX_URL of the environment. Every secret found is remembered for scrubbing."""
    found: list[tuple[str, str, bool]] = []
    if ctx.settings.index_url:
        found.append((ctx.settings.index_url, "mirror.ini: index_url", False))
    found += [(u, "mirror.ini: extra_index_url", False) for u in ctx.settings.extra_index_url.split()]
    cp = _pip(ctx, "config", "list")
    for line in _output(cp).splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key.endswith(("index-url", "index_url")):
            over = bool(ctx.settings.index_url) and not key.endswith("extra-index-url")
            found += [(u, f"pip config: {key}", over) for u in value.strip().strip("'\"").split()]
    for var in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL"):
        over = bool(ctx.settings.index_url) and var == "PIP_INDEX_URL"
        found += [(u, var, over) for u in ctx.env.get(var, "").split()]
    out: list[dict] = []
    seen: set[str] = set()
    for url, source, over in found:
        ctx.journal.secrets.extend(_secret_parts(url))
        masked = mask_url(url)
        if masked not in seen:
            seen.add(masked)
            entry: dict = {"url": masked, "source": source}
            if over:
                entry["overridden"] = True
            out.append(entry)
    return out or [{"url": "https://pypi.org/simple (по умолчанию pip)", "source": "default"}]


def _index_failure(ctx: _Ctx, output: str, title: str, message: str = "") -> InstallError:
    """The index is unreachable or refuses us: note pip's (masked) output, mark the packages, explain."""
    j = ctx.journal
    j.note(title, output)
    for p in j.data["packages"].values():
        if p["status"] is None:
            p["status"] = "index_error"
    where = ", ".join(i["url"] for i in j.data["index"]) or "адрес не определён"
    return _fail(ctx, message or f"Индекс пакетов недоступен или отказал в доступе ({where}). Проверьте адрес "
                              "индекса, логин и пароль, сертификат, прокси и сеть.")


@dataclass
class _Resolution:
    ok: bool
    items: dict[str, tuple[str, str, str]] = field(default_factory=dict)   # name -> (version, url, sha256)
    error: str = ""


def _read_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;\\]+)", line)
        if m:
            pins[str(canonicalize_name(m.group(1)))] = m.group(2)
    return pins


def _resolve(ctx: _Ctx, selection: dict[str, str], local: dict[str, str], offline: Path | None = None
             ) -> _Resolution:
    """One resolver run (dry run, with dependencies) of the selection plus our wheels; ``offline`` (the wheel cache)
    resolves from it and the wheelhouse only, without the index."""
    n = next(ctx.counter)
    pins = ctx.work / f"resolve-{n}.txt"
    report = ctx.work / f"resolve-{n}.json"
    pins.write_text("".join(f"{k}=={v}\n" for k, v in sorted({**selection, **local}.items())), encoding="utf-8")
    cp = _pip(ctx, "install", "--dry-run", "--ignore-installed", "--only-binary=:all:", "--quiet",
              "--report", str(report), "--find-links", str(ctx.wheelhouse),
              *(["--no-index", "--find-links", str(offline)] if offline else _index_args(ctx.settings)),
              "-r", str(pins))
    if cp.returncode != 0 or not report.is_file():
        if not offline and is_network_failure(_output(cp)):
            raise _index_failure(ctx, _output(cp), "pip install --dry-run")
        return _Resolution(False, error=_output(cp))
    items: dict[str, tuple[str, str, str]] = {}
    for item in json.loads(report.read_text(encoding="utf-8")).get("install", []):
        meta = item.get("metadata", {})
        info = item.get("download_info", {})
        items[str(canonicalize_name(meta.get("name", "")))] = (
            meta.get("version", ""), info.get("url", ""),
            (info.get("archive_info", {}).get("hashes", {}) or {}).get("sha256", ""))
    return _Resolution(True, items)


def _available(ctx: _Ctx, name: str, pre: bool) -> list[str]:
    args = ["index", "versions", name, "--only-binary=:all:", *_index_args(ctx.settings)]
    if pre:
        args.append("--pre")
    cp = _pip(ctx, *args)
    text = _output(cp)
    if cp.returncode != 0:
        if is_network_failure(text):
            raise _index_failure(ctx, text, f"pip index versions {name}")
        if "no matching distribution" in text.lower() or "could not find a version" in text.lower():
            return []
        ctx.journal.note(f"pip index versions {name}", text)
        raise _fail(ctx, f"Не удалось получить список версий {name} с индекса (код pip {cp.returncode}); "
                         "проверьте адрес индекса, сеть и сертификаты.")
    return parse_index_versions(text)


# --- cache ---------------------------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_hashes(cache: Path) -> dict[str, dict[str, str]]:
    """``cache\\sha256.json``: wheel file name -> {"sha256", "url" (masked)}."""
    try:
        data = json.loads((cache / "sha256.json").read_text(encoding="utf-8"))
        return {str(k): {"sha256": str(v.get("sha256", "")), "url": str(v.get("url") or "")}
                for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_hashes(cache: Path, hashes: dict[str, dict[str, str]]) -> None:
    tmp = cache / "sha256.json.tmp"
    tmp.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, cache / "sha256.json")


def _cached_wheel(cache: Path, hashes: dict[str, dict[str, str]], name: str, version: str, tags: set) -> Path | None:
    """A wheel of ``cache`` for name==version that fits this interpreter and whose SHA-256 is the recorded one."""
    want = Version(version)
    for path in sorted(cache.glob("*.whl")):
        try:
            wname, wver, _build, wtags = parse_wheel_filename(path.name)
        except ValueError:  # a stray file in the cache (InvalidWheelFilename/InvalidVersion)
            continue
        if canonicalize_name(wname) != name or wver != want or not (set(wtags) & tags):
            continue
        if hashes.get(path.name, {}).get("sha256") == _sha256(path):
            return path
    return None


def _download(ctx: _Ctx, cache: Path, hashes: dict[str, dict[str, str]], name: str, version: str, tags: set,
              url: str = "", report_sha: str = "") -> tuple[Path, bool]:
    """``url`` is pip's report URL (raw) of the file the resolver chose, ``report_sha`` its hash when pip gave one:
    the file ``pip download`` fetched must be the same."""
    hit = _cached_wheel(cache, hashes, name, version, tags)
    if hit is not None:
        return hit, True
    part = cache / f".part-{name}"
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    cp = _pip(ctx, "download", "--no-deps", "--only-binary=:all:", "--dest", str(part),
              *_index_args(ctx.settings), f"{name}=={version}")
    wheels = sorted(part.glob("*.whl"))
    if cp.returncode != 0 or len(wheels) != 1:
        shutil.rmtree(part, ignore_errors=True)
        if is_network_failure(_output(cp)):
            raise _index_failure(ctx, _output(cp), f"pip download {name}=={version}")
        ctx.journal.note(f"pip download {name}=={version}", _output(cp))
        raise _fail(ctx, f"Не удалось скачать {name}=={version} с индекса (код pip {cp.returncode}).")
    digest = _sha256(wheels[0])
    if url:
        reported = Path(unquote(urlsplit(url).path)).name
        if reported != wheels[0].name or (report_sha and report_sha != digest):
            ctx.journal.note(f"сверка {name}=={version}", f"отчёт pip: файл {reported} sha256 {report_sha or '-'}; "
                                                         f"скачан: {wheels[0].name} sha256 {digest}")
            shutil.rmtree(part, ignore_errors=True)
            raise _fail(ctx, f"Скачанный файл {name}=={version} не совпадает с тем, что выбрал pip при проверке "
                             "набора (имя или SHA-256). Установка отменена.")
    target = cache / wheels[0].name
    os.replace(wheels[0], target)
    shutil.rmtree(part, ignore_errors=True)
    hashes[target.name] = {"sha256": digest, "url": mask_url(url) if url else ""}
    _save_hashes(cache, hashes)
    return target, False


# --- install -------------------------------------------------------------------------------------------------------


def _load_manifest(wheelhouse: Path) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]], list[str]]:
    """(local pins, third-party lock pins, name -> requirer specs, imports) of ``mirror.json``."""
    path = wheelhouse / "mirror.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("schema") != SCHEMA:
            raise ValueError(f"schema {data.get('schema')!r}, ожидается {SCHEMA}")
        local = {}
        for pin in data.get("local", []):
            name, _, ver = str(pin).partition("==")
            local[str(canonicalize_name(name))] = ver.strip()
        lock, specs = {}, {}
        for name, info in data["packages"].items():
            key = str(canonicalize_name(name))
            lock[key] = str(info["lock"])
            specs[key] = [str(r.get("spec", "")) for r in info.get("requires", [])]
        return local, lock, specs, [str(m) for m in data.get("imports", [])]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InstallError(f"Не удалось прочитать {path}: {exc}. Распакуйте комплект заново.") from exc


def install(*, python: Path, wheelhouse: Path, log_dir: Path, settings: Settings, pip_env: dict[str, str],
            cache: Path, say: Callable[[str], None] = print) -> Result:
    """Installs the bundle's dependencies into the venv of ``python``; see the module docstring."""
    python, wheelhouse, cache = Path(python), Path(wheelhouse), Path(cache)
    journal = _Journal(Path(log_dir), settings.strict, [])
    work = Path(tempfile.mkdtemp(prefix="deskmirror-"))
    env = dict(pip_env)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    ctx = _Ctx(python, wheelhouse, settings, env, work, journal)
    result = Result(log_path=journal.log_path, json_path=journal.json_path)
    try:
        _install(ctx, cache, result, say)
        journal.data["status"] = "ok"
        result.ok = True
    except InstallError as exc:
        if exc.log_path is not None:
            raise
        journal.data["error"] = str(exc)
        raise InstallError(str(exc), journal.log_path, journal.json_path) from exc
    except Exception as exc:
        journal.data["error"] = f"Непредвиденная ошибка: {type(exc).__name__}: {exc}"
        raise InstallError(journal.data["error"], journal.log_path, journal.json_path) from exc
    finally:
        _summarize(journal, result)
        try:
            journal.write()
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _print_summary(journal, result, say)
    return result


def _summarize(journal: _Journal, result: Result) -> None:
    pk = journal.data["packages"]
    result.installed = sorted(f"{n}=={p['installed']}" for n, p in pk.items() if p["installed"])
    result.not_found = sorted(n for n, p in pk.items() if p["status"] == "not_found")
    result.deviations = [
        f"{n}: по lock {p['required']}, будет {p['installed'] or '-'}" if p["status"] == "differs"
        else f"{n}: нет на индексе ({', '.join(p['tried']) or 'ничего подходящего'}), lock {p['required']}"
        if p["status"] == "not_found" else f"{n}: добавлен {p['installed'] or '-'}, его нет в lock"
        for n, p in sorted(pk.items()) if p["status"] in ("differs", "not_found", "added")]
    journal.data["summary"] = {
        "installed": result.installed, "not_installed": sorted(n for n, p in pk.items() if not p["installed"]),
        "deviations": result.deviations}


def _print_summary(journal: _Journal, result: Result, say: Callable[[str], None]) -> None:
    ok = journal.data["status"] == "ok"
    say(f"Зависимости: {'установлено' if ok else 'НЕ установлено'} ({len(result.installed)} пакетов).")
    if result.deviations:
        say("ВНИМАНИЕ: версии отличаются от проверенных (lock):")
        for line in result.deviations:
            say(f"  !!! {line}")
    say(f"Журналы: {journal.log_path} и {journal.json_path}")


def _install(ctx: _Ctx, cache: Path, result: Result, say: Callable[[str], None]) -> None:
    j, st = ctx.journal, ctx.settings
    local, lock, specs, imports = _load_manifest(ctx.wheelhouse)
    req_file = ctx.wheelhouse / "requirements.txt"
    if req_file.is_file():
        reqs = _read_pins(req_file)
        want = {**lock, **local}
        for name in sorted(set(want) | set(reqs)):
            if name not in reqs or name not in want or not _same_version(reqs[name], want[name]):
                raise _fail(ctx, f"requirements.txt и mirror.json расходятся в пакете {name} "
                                 f"({reqs.get(name) or 'нет'} / {want.get(name) or 'нет'}). "
                                 "Распакуйте комплект заново.")
    for name, ver in lock.items():
        j.pkg(name)["required"] = ver

    py = _run_checked(ctx, [str(ctx.python), "-c", "import platform;print(platform.python_version())"])
    j.data["python"] = py.strip()
    cp = _pip(ctx, "--version")
    if cp.returncode != 0:
        j.note("pip --version", _output(cp))
        raise _fail(ctx, "В новом окружении нет рабочего pip. Удалите папку env и запустите установку заново.")
    j.data["pip"] = cp.stdout.strip().splitlines()[0] if cp.stdout.strip() else ""
    j.data["index"] = _index_addresses(ctx)
    say(f"Установка зависимостей с индекса pip ({'строгий' if st.strict else 'гибкий'} режим)...")

    cache.mkdir(parents=True, exist_ok=True)
    hashes = _load_hashes(cache)
    tags = set(sys_tags())

    # 1. everything as the lock says; when every lock wheel is in the cache (a rerun) the resolver runs offline
    first = None
    offline = all(_cached_wheel(cache, hashes, n, v, tags) for n, v in lock.items())
    if offline:
        first = _resolve(ctx, lock, local, offline=cache)
        if not first.ok or _extras(first, lock, local):
            first, offline = None, False
    if first is None:
        first = _resolve(ctx, lock, local)
    extras: dict[str, tuple[str, str, str]] = {}
    chosen = dict(lock)
    resolved = first
    if first.ok:
        extras = _extras(first, lock, local)
        for name in lock:
            j.pkg(name)["available"] = [lock[name]]
    if not first.ok:
        chosen, resolved, extras = _search(ctx, lock, local, specs, first.error)
    for name, ver in chosen.items():
        p = j.pkg(name)
        same = _same_version(ver, lock[name])
        p["status"] = "as_lock" if same else "differs"
        if not same and ver not in p["tried"]:
            p["tried"].append(ver)
    if extras:
        for name, (ver, _url, _sha) in sorted(extras.items()):
            p = j.pkg(name)
            p.update(required=None, status="added", tried=[ver], available=[ver])
        if st.strict:
            raise _fail(ctx, "Строгий режим: версии из lock тянут пакеты, которых в lock нет: "
                             + ", ".join(sorted(extras)) + ".")
    final = {**chosen, **{n: v for n, (v, _u, _s) in extras.items()}}
    urls: dict[str, str] = {}
    reports: dict[str, tuple[str, str]] = {}
    for name, (_ver, url, sha) in resolved.items.items():
        if name in final and url and not offline:
            urls[name] = mask_url(url)
            reports[name] = (url, sha)
            j.secrets.extend(_secret_parts(url))
    if any(p["status"] in ("differs", "added") for p in j.data["packages"].values()):
        say("ВНИМАНИЕ: часть версий отличается от проверенных (lock), подробности в журнале.")

    # 2. download into the cache, then install from the cache and the wheelhouse only
    staged: dict[str, Path] = {}
    for name, ver in sorted(final.items()):
        path, cached = _download(ctx, cache, hashes, name, ver, tags, *reports.get(name, ("", "")))
        staged[name] = path
        p = j.pkg(name)
        p["sha256"] = hashes[path.name]["sha256"]
        p["url"] = urls.get(name) or hashes[path.name]["url"] or None
        say(f"  {name}=={ver}" + (" (из кэша)" if cached else ""))
    stage = ctx.work / "stage"          # exactly the files logged, so --require-hashes can name them
    stage.mkdir()
    digests: dict[str, str] = {n: hashes[p.name]["sha256"] for n, p in staged.items()}
    for name, ver in local.items():
        wheel = _local_wheel(ctx.wheelhouse, name, ver)
        if wheel is None:
            raise _fail(ctx, f"В комплекте нет колеса {name}=={ver}. Распакуйте комплект заново.")
        staged[name] = wheel
        digests[name] = _sha256(wheel)
    for path in staged.values():
        shutil.copy2(path, stage / path.name)
    pins = j.log_path.with_suffix(".requirements.txt")
    pins.write_text("".join(f"{k}=={v} --hash=sha256:{digests[k]}\n" for k, v in sorted({**final, **local}.items())),
                    encoding="utf-8")
    cp = _pip(ctx, "install", "--no-index", "--only-binary=:all:", "--require-hashes", "--find-links", str(stage),
              "-r", str(pins))
    if cp.returncode != 0:
        j.note("pip install", _output(cp))
        raise _fail(ctx, f"pip не смог установить пакеты из кэша и комплекта (код {cp.returncode}).")
    for name, ver in final.items():
        j.pkg(name)["installed"] = ver
    for name, ver in local.items():
        j.pkg(name).update(required=ver, installed=ver, status="as_lock")

    # 3. checks
    cp = _pip(ctx, "check")
    if cp.returncode != 0:
        j.note("pip check", _output(cp))
        raise _fail(ctx, "pip check нашёл несовместимые зависимости после установки.")
    if imports:
        script = ("import importlib,json,sys\nbad={}\nfor m in sys.argv[1:]:\n try: importlib.import_module(m)\n"
                  " except Exception as e: bad[m]=type(e).__name__+': '+str(e)\nprint(json.dumps(bad))\n")
        cp = _run(([str(ctx.python), "-I", "-c", script, *imports]), ctx.env, ctx.work)
        try:
            bad = json.loads(cp.stdout.strip().splitlines()[-1]) if cp.returncode == 0 else {"?": cp.stderr}
        except (ValueError, IndexError):
            bad = {"?": _output(cp)}
        if bad:
            j.note("проверка импорта", json.dumps(bad, ensure_ascii=False))
            raise _fail(ctx, "Не импортируются модули: " + ", ".join(sorted(bad)) + ".")


def _local_wheel(wheelhouse: Path, name: str, version: str) -> Path | None:
    for path in sorted(wheelhouse.glob("*.whl")):
        try:
            wname, wver, _b, _t = parse_wheel_filename(path.name)
        except ValueError:
            continue
        if canonicalize_name(wname) == name and wver == Version(version):
            return path
    return None


def _run_checked(ctx: _Ctx, cmd: list[str]) -> str:
    try:
        cp = _run(cmd, ctx.env, ctx.work)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail(ctx, f"Не удалось запустить Python venv ({ctx.python}): {exc}") from exc
    if cp.returncode != 0:
        ctx.journal.note("python", _output(cp))
        raise _fail(ctx, f"Python venv ({ctx.python}) не запускается (код {cp.returncode}).")
    return cp.stdout


def _extras(res: _Resolution, lock: dict[str, str], local: dict[str, str]) -> dict[str, tuple[str, str, str]]:
    return {n: v for n, v in res.items.items() if n not in lock and n not in local}


def _search(ctx: _Ctx, lock: dict[str, str], local: dict[str, str], specs: dict[str, list[str]],
            first_error: str = "") -> tuple[dict[str, str], _Resolution, dict[str, tuple[str, str, str]]]:
    """The lock set did not resolve: ask the index what exists, then try replacement candidates in order."""
    j, st = ctx.journal, ctx.settings
    j.note("pip install --dry-run (набор из lock)", first_error)

    def ask(name: str) -> list[str]:
        return _available(ctx, name, Version(lock[name]).is_prerelease)

    names = sorted(lock)
    with ThreadPoolExecutor(max_workers=INDEX_QUERY_WORKERS) as pool:
        listed = dict(zip(names, pool.map(ask, names), strict=True))
    if lock and not any(listed.values()):
        # pip is silent about a refused connection or a 401 at its default verbosity: no version of any package
        # at all means the index is unreachable or closed to us, not that every version is gone
        raise _index_failure(ctx, "pip index versions не вернул ни одной версии ни для одного пакета.\n"
                                  + first_error, "индекс не вернул версий",
                             "Индекс не вернул ни одной версии ни для одного пакета: он недоступен, отказал в "
                             "доступе или на нём нет колёс для этого Python и платформы. Проверьте адрес индекса, "
                             "логин и пароль, сертификат, прокси и сеть, а также что зеркало содержит колёса "
                             "для Windows (win_amd64, Python 3.11).")
    missing: dict[str, list[str]] = {}
    for name in names:
        avail = listed[name]
        j.pkg(name)["available"] = avail
        if any(_same_version(v, lock[name]) for v in avail):
            continue
        j.pkg(name)["tried"].append(lock[name])
        missing[name] = [] if st.strict else [v for v in order_candidates(lock[name], avail, specs[name])
                                                if not _same_version(v, lock[name])]
    if not missing:
        raise _fail(ctx, "Все версии из lock есть на индексе, но pip не смог согласовать их зависимости.")
    dead = sorted(n for n, c in missing.items() if not c)
    if dead:
        for n in dead:
            j.pkg(n)["status"] = "not_found"
        what = ("Строгий режим: на индексе нет версии из lock для "
                if st.strict else "На индексе нет подходящей версии для ")
        raise _fail(ctx, what + ", ".join(f"{n} (lock {lock[n]})" for n in dead) + ".")
    fixed = {n: v for n, v in lock.items() if n not in missing}
    order = sorted(missing)
    fallback: tuple[dict[str, str], _Resolution, dict[str, tuple[str, str, str]]] | None = None
    last_error = ""
    for attempt, combo in enumerate(itertools.product(*(missing[n] for n in order))):
        if attempt >= MAX_ATTEMPTS:
            break
        selection = {**fixed, **dict(zip(order, combo, strict=True))}
        for n, v in zip(order, combo, strict=True):
            if v not in j.pkg(n)["tried"]:
                j.pkg(n)["tried"].append(v)
        res = _resolve(ctx, selection, local)
        if not res.ok:
            last_error = res.error
            continue
        extras = _extras(res, lock, local)
        if not extras:
            return selection, res, {}
        if fallback is None:
            fallback = (selection, res, extras)
    if fallback is not None:
        return fallback
    for n in order:
        j.pkg(n)["status"] = "not_found"
    j.note("pip install --dry-run (последний вариант)", last_error)
    raise _fail(ctx, "Ни одна из доступных версий не подходит: pip отверг все варианты замены для "
                     + ", ".join(f"{n} (lock {lock[n]})" for n in order) + ".")
