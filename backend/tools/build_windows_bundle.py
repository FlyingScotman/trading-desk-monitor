"""Build the offline Windows bundle of Desk Monitor (dev only, runs at home): one zip, unzip, start.bat.

    python3.11 backend/tools/build_windows_bundle.py [--out dist] [--adapter-zip PATH] [--allow-dirty]
                                                     [--patch-base OLD_BUNDLE.zip]

Needs uv, git, network to PyPI and GitHub, and a Python with pip (``python3.11 -m pip``). Writes
``dist/DeskMonitor-<version>[-with-adapter].zip``, version ``<backend version>-<commit date>-<short sha>``
(``-dirty<UTC build time>`` appended when shipped files have uncommitted changes). Inside, ``DeskMonitor/``:

* ``python/``     CPython 3.11 for Windows x64 from python-build-standalone (``install_only``), its download
                  checked against the release's ``SHA256SUMS`` and cached (``--cache``); debug symbols and
                  Tk dropped. ``--no-python`` leaves it out: the .bat files then need ``py -3.11``
* ``wheelhouse/`` win_amd64 / cp311 wheels with the exact pins of ``backend/uv.lock`` plus pgserver (the
                  embedded store), wheels of desk-backend and rub-fixed-income-core (the
                  commit pinned in ``[tool.uv.sources]``), ``requirements.txt``; proved complete for Windows by
                  an offline ``pip install --dry-run --platform win_amd64`` before zipping
* ``app/``        ``deskctl.py`` (the launcher the .bat files call), ``prototypes/``, ``frontend/dist/``,
                  ``examples/test_book.csv``, ``INSTALL_BACKEND_WINDOWS.md``
* ``adapter/``    the private adapter's package from ``--adapter-zip`` (never in this repository)
* ``START_HERE.txt`` (the owner's short guide), ``start.bat``, ``stop.bat``, ``update_daily.bat``, ``desk.bat``,
  ``INSTALL.txt``, ``VERSION``, ``MANIFEST.sha256``

start.bat makes the venv ``env/`` from ``python/`` and the wheelhouse, offline, on the first start and whenever
the version or the folder changes; data and settings live in DESK_HOME (see backend/windows/deskctl.py).
``--adapter-zip`` writes the public ``DeskMonitor-<version>.zip`` too, from the same files: the two zips hold
byte-identical files outside ``adapter/`` (checked after writing; ``MANIFEST.sha256`` differs only by the adapter's
lines), so every public patch applies on top of a with-adapter install. For a release the CI already published,
``--from-bundle DeskMonitor-<version>.zip --adapter-zip A.zip`` makes the with-adapter zip from the published one
instead of rebuilding (no network).

Reproducible: the same commit gives the same files, byte for byte, here or on CI. Third-party wheels come from
PyPI as published; ours are built by ``uv build`` with the build backends pinned
(``bundle-build-constraints.txt``), no wheel cache, ``SOURCE_DATE_EPOCH`` the time of the commit that last changed
the package's sources (core: its pinned commit), then rewritten in a fixed form (sorted entries, that time on every
entry, 0644/0755, stored, not deflated: no zlib version in the bytes); each is built twice and compared
(``--no-repro-check`` skips it). The zips have fixed timestamps (the HEAD commit's), order and permissions.

``--patch-base`` (repeatable) also writes ``DeskMonitor-patch-<old version>-to-<new version>.zip`` next to the full
zip. The base is a full bundle zip (the previous release's published asset: the release workflow downloads it), a
``MANIFEST.sha256``, or a version with records in ``backend/tools/delivered/<version>/`` (``--patch-from-delivered``:
the newest one): what was actually delivered, so a patch is never made against a rebuild that only looks like it.
Several bases of one version (two builds of it) give one patch that fits each. A patch holds the files whose bytes
differ from the base (from any base of its version) or are new (``VERSION`` and ``MANIFEST.sha256`` always), under the
same ``DeskMonitor/`` folder, plus ``PATCH_README.txt``, ``PATCH_BASE.txt`` (``<old version> (сборка <build id>)``:
the base build, ``build_id``) and, when files were dropped, ``PATCH_DELETE.txt`` (the new deskctl.py deletes those on
start). It never touches ``adapter/`` (nor lists it in its manifest): the installed adapter stays as it is. Unpacked
over the old folder it gives the new bundle's files exactly.

``--record-delivered ZIP...`` (no build): after handing a zip to the owner, write its record
``backend/tools/delivered/<version>/<build id>.sha256`` (its ``MANIFEST.sha256`` without ``adapter/``, every file
checked) and commit it.

Both zips install from the settings page too («Обновление программы», ``desk.update``): it checks the layout, the
VERSION, every file against ``MANIFEST.sha256`` and, for a patch, that the installed files it does not replace are
the ones the patch was made for (else the GitHub updater takes the full zip). A build whose ``python/`` differs from
the installed one is refused there (the manual way only).

Built from the files git tracks or sees (``git ls-files -co --exclude-standard``); a dirty tree is refused
unless ``--allow-dirty``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# what the bundle needs from the repository; the rest (tests, docs, tools) stays at home
INCLUDE = ("backend/pyproject.toml", "backend/uv.lock", "backend/src/",
           "backend/examples/test_book.csv", "backend/windows/", "prototypes/", "frontend/dist/",
           "docs/INSTALL_BACKEND_WINDOWS.md")
BATS = ("start.bat", "stop.bat", "update_daily.bat", "desk.bat")
TOP = "DeskMonitor"
WIN = ["--platform", "win_amd64", "--python-version", "3.11", "--implementation", "cp", "--abi", "cp311"]
# python-build-standalone: a release tag and the CPython it carries (latest-release.json names the newest)
PBS_RELEASE, PBS_PYTHON = "20261013", "3.11.17"
PBS_BASE = "https://github.com/astral-sh/python-build-standalone/releases/download"
# not needed to run the backend: debug symbols (80 MB), Tk/IDLE, turtle demos
PY_DROP_DIRS = ("python/tcl/", "python/Lib/tkinter/", "python/Lib/idlelib/", "python/Lib/turtledemo/")
PY_DROP_FILES = ("python/DLLs/_tkinter.pyd", "python/DLLs/tcl86t.dll", "python/DLLs/tk86t.dll")
# the build backends of our wheels, pinned: their version is in the wheel (WHEEL's Generator, METADATA's form)
CONSTRAINTS = REPO / "backend/tools/bundle-build-constraints.txt"
# what was delivered: <version>/<build id>.sha256, the bundle's MANIFEST.sha256 without adapter/
DELIVERED = REPO / "backend/tools/delivered"
ADAPTER_DIR = "adapter/"
ZIP_MIN_EPOCH = 315532800  # zip dates start in 1980
# our pure-Python wheels: the paths whose last commit gives SOURCE_DATE_EPOCH (the wheel stays byte-identical
# from release to release while they do not change)
OURS = {"desk_backend": ("backend/pyproject.toml", "backend/README.md", "backend/src/")}
_VERSION_KEY = re.compile(r"(\d+(?:\.\d+)*)-(\d{8})-[0-9a-f]{7,40}(?:-dirty(\d{12}))?")


def run(*cmd: str, cwd: Path | None = None, env: dict | None = None) -> str:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE, text=True).stdout


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def pins(text: str) -> list[str]:
    """``name==version[ ; marker]`` lines of a uv export; local (-e) and git requirements dropped."""
    return [ln for ln in text.splitlines() if ln and not ln.startswith(("#", " ", "-e")) and "==" in ln]


def bundle_version(base: str, commit_epoch: int, sha: str, dirty: bool, now: datetime | None = None) -> str:
    """``0.1.0-20261019-e7410dc``; a dirty build adds its UTC build time, so start.bat reinstalls it."""
    v = f"{base}-{datetime.fromtimestamp(commit_epoch, UTC):%Y%m%d}-{sha[:7]}"
    return v + (f"-dirty{(now or datetime.now(UTC)):%Y%m%d%H%M}" if dirty else "")


def fetch(url: str, dest: Path) -> Path:
    """Download ``url`` to ``dest`` unless it is there (urllib honours HTTPS_PROXY)."""
    if not dest.is_file():
        print(f"+ download {url}", flush=True)
        tmp = dest.with_name(dest.name + ".part")
        with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(dest)
    return dest


def python_runtime(cache: Path, release: str, version: str) -> tuple[Path, str]:
    """The cached ``install_only`` archive of CPython ``version`` for Windows x64, checked against SHA256SUMS."""
    cache.mkdir(parents=True, exist_ok=True)
    asset = f"cpython-{version}+{release}-x86_64-pc-windows-msvc-install_only.tar.gz"
    sums = fetch(f"{PBS_BASE}/{release}/SHA256SUMS", cache / f"SHA256SUMS-{release}").read_text()
    want = next((ln.split()[0] for ln in sums.splitlines() if ln.split()[1:] == [asset]), None)
    if want is None:
        sys.exit(f"{asset} is not in the SHA256SUMS of python-build-standalone {release}")
    archive = fetch(f"{PBS_BASE}/{release}/{asset.replace('+', '%2B')}", cache / asset)
    got = sha256(archive)
    if got != want:
        archive.unlink()
        sys.exit(f"{asset}: sha256 {got}, the release says {want}; the download was removed, run again")
    return archive, want


def unpack_python(archive: Path, stage: Path) -> None:
    """``python/`` of the archive into ``stage/python`` without debug symbols and Tk."""
    with tarfile.open(archive) as t:
        keep = [m for m in t.getmembers()
                if m.name.startswith("python/") and not m.name.endswith(".pdb")
                and not m.name.startswith(PY_DROP_DIRS) and m.name not in PY_DROP_FILES]
        t.extractall(stage, members=keep, filter="data")
    py = stage / "python"
    for need in ("python.exe", "python311.dll", "Lib/venv/scripts/nt/python.exe", "Lib/encodings/__init__.py"):
        assert (py / need).is_file(), f"the runtime has no {need}"
    assert list((py / "Lib/ensurepip/_bundled").glob("pip-*.whl")), "the runtime has no bundled pip (ensurepip)"


def adapter_files(zip_bytes: bytes) -> dict[str, bytes]:
    """The private adapter zip as ``{bundle path under adapter/: bytes}``: its one top-level package (a folder
    with ``__init__.py`` whose parent has none) as ``<package>/...``, plus README/LICENSE files at its root."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        for n in names:
            norm = posixpath.normpath(n)
            if n.startswith(("/", "\\")) or norm.startswith("..") or ":" in n or "\\" in n:
                raise ValueError(f"unsafe path in the adapter zip: {n!r}")
        inits = {posixpath.dirname(n) for n in names if posixpath.basename(n) == "__init__.py"}
        tops = sorted(d for d in inits if posixpath.dirname(d) not in inits)
        if len(tops) != 1:
            raise ValueError(f"the adapter zip must hold exactly one top-level package, found {tops or 'none'}")
        top = tops[0]
        base = posixpath.dirname(top)
        out: dict[str, bytes] = {}
        for n in names:
            if "__pycache__/" in n or n.endswith((".pyc", ".pyo")):
                continue
            if n.startswith(top + "/"):
                out[posixpath.basename(top) + n[len(top):]] = z.read(n)
            elif posixpath.dirname(n) in ("", base) and posixpath.basename(n).upper().startswith(("README", "LICENSE")):
                out[posixpath.basename(n)] = z.read(n)
        return out


def crlf(data: bytes, bom: bool = False) -> bytes:
    return (b"\xef\xbb\xbf" if bom else b"") + data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


PATCH_README = """\
Desk Monitor: обновление {old} -> {new}

Это небольшой архив-заплатка: только изменённые файлы. Старую папку программы не удаляйте.
Она подходит только к сборке {old} с кодом {builds} (код установленной сборки виден
в сообщении, если заплатка не подошла). Не подошла - поставьте полный архив
DeskMonitor-{new}.zip (обновление с GitHub возьмёт его само). Папку adapter заплатка не трогает.

Проще всего (если установленная версия уже умеет обновляться сама): Настройки ->
«Обновление программы» -> «Загрузить zip с компьютера» (или положите zip в папку
обновлений) -> «Установить». Страница сама перезагрузится с новой версией. Иначе - вручную:

1) Остановите программу: двойной щелчок по stop.bat (или закройте окно start.bat).

2) Распакуйте архив в C:\\DeskMonitor с заменой файлов. Проще всего так:
   правой кнопкой по zip -> Свойства -> галочка «Разблокировать» -> ОК; затем откройте zip
   двойным щелчком, зайдите в папку DeskMonitor внутри, выделите всё (Ctrl+A), скопируйте
   и вставьте в папку, где у вас лежит start.bat (например C:\\DeskMonitor). На вопрос
   Windows выберите «Заменить файлы в папке назначения».
   Проверка: в файле VERSION в папке программы теперь {new}.

3) Запустите start.bat. Он пересоберёт окружение (без интернета, ~1-2 минуты);
   настройки, токен и данные сохранятся.

4) Откройте Настройки (http://127.0.0.1:8100/settings.html), раздел «Автообновление»,
   поле «Брать отчёты не старше, дней»: по умолчанию 10 (пустое поле - тоже 10).
   Большую папку с отчётами за годы можно оставить как есть: файлы старше этого срока
   не читаются (первый цикл после обновления только отметит их), а уже прочитанные
   старые отчёты ничему не мешают - пересчитываются только последние дни.

5) Главная (http://127.0.0.1:8100/): значок автопилота в шапке
   показывает ход цикла, например «Скан отчётов: 37 из 412», «Загрузка снимков: 2 из 5»,
   «EOD 16.10: 1 из 3». Пока нет ни одного посчитанного дня, вместо таблицы прогонов
   написано «В базе пока нет прогонов»; как только посчитается первый день, данные появятся
   сами и в мониторах (http://127.0.0.1:8100/concepts/index.html).
"""


def zip_stamp(epoch: int) -> tuple:
    return time.gmtime(max(epoch, ZIP_MIN_EPOCH))[:6]


def stage_files(stage: Path) -> list[tuple[str, Path]]:
    """(path under the bundle, file) of ``stage``, sorted by the path: the one order of manifests and zips."""
    return sorted(((p.relative_to(stage).as_posix(), p) for p in stage.rglob("*") if p.is_file()), key=lambda t: t[0])


def manifest_lines(entries: dict[str, str]) -> str:
    return "".join(f"{entries[rel]}  {rel}\n" for rel in sorted(entries))


def write_manifest(stage: Path, header: str) -> None:
    """``MANIFEST.sha256``: ``header`` (``#`` lines, nothing about the adapter: the public and the with-adapter
    bundle share it), then the sha256 of every file of ``stage``."""
    entries = {rel: sha256(p) for rel, p in stage_files(stage) if rel != "MANIFEST.sha256"}
    (stage / "MANIFEST.sha256").write_text(header + manifest_lines(entries))


def parse_manifest(text: str) -> tuple[list[str], dict[str, str]]:
    """(the ``#`` lines, {path: sha256}) of a ``MANIFEST.sha256`` or a delivered record."""
    header, entries = [], {}
    for line in text.splitlines():
        if line.startswith("#"):
            header.append(line)
        elif line.strip():
            digest, sep, rel = line.partition("  ")
            if not sep or not re.fullmatch(r"[0-9a-f]{64}", digest) or not rel:
                raise ValueError(f"not a manifest line: {line[:120]!r}")
            entries[rel] = digest
    return header, entries


def without_adapter(manifest: bytes) -> bytes:
    """A ``MANIFEST.sha256`` without its ``adapter/`` lines: the public bundle's."""
    return b"".join(ln for ln in manifest.splitlines(keepends=True)
                    if not ln.partition(b"  ")[2].startswith(ADAPTER_DIR.encode()))


def build_id(entries: dict[str, str]) -> str:
    """The build of a bundle in 12 hex: the sha256 of its manifest lines outside ``adapter/`` (``MANIFEST.sha256``
    itself left out). Two builds of one version that differ in any byte have different ids. ``desk.update.build_id``
    is the same function (the installed side)."""
    keep = {rel: d for rel, d in entries.items() if not rel.startswith(ADAPTER_DIR) and rel != "MANIFEST.sha256"}
    return hashlib.sha256(manifest_lines(keep).encode()).hexdigest()[:12]


def version_key(v: str) -> tuple:
    m = _VERSION_KEY.fullmatch(v)
    return (tuple(int(x) for x in m[1].split(".")), m[2], m[3] or "") if m else ((), "", v)


def write_zip(out: Path, files, stamp: tuple) -> None:
    """``files``: (path under ``DeskMonitor/``, bytes) pairs, deflated, in the given order, every entry with the
    build's timestamp, 0644 and the same "made by": the same files give the same zip."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, data in files:
            info = zipfile.ZipInfo(f"{TOP}/{rel}", stamp)
            info.compress_type, info.create_system = zipfile.ZIP_DEFLATED, 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(info, data)


def bundle_entries(stage: Path, adapter: bool):
    """(path, bytes) of the bundle zip from ``stage``; without ``adapter``: the public bundle (no ``adapter/``, its
    lines dropped from ``MANIFEST.sha256``)."""
    for rel, p in stage_files(stage):
        if rel.startswith(ADAPTER_DIR) and not adapter:
            continue
        data = p.read_bytes()
        yield rel, (without_adapter(data) if rel == "MANIFEST.sha256" and not adapter else data)


def normalize_wheel(src: Path, dest: Path, epoch: int) -> None:
    """Rewrite the wheel ``src`` as ``dest`` in one fixed form, whatever the build backend and this machine did:
    entries sorted (``.dist-info/`` last, ``RECORD`` the very last), each with the time ``epoch``, mode 0644 (0755
    when it was executable), made on Unix, stored (deflate output depends on the zlib build; the bundle zip
    compresses the wheelhouse anyway). The contents, and so ``RECORD``, stay as they are."""
    with zipfile.ZipFile(src) as z:
        entries = [(i.filename, bool((i.external_attr >> 16) & 0o111), z.read(i)) for i in z.infolist()
                   if not i.is_dir()]

    def order(name: str) -> tuple:
        return name.split("/")[0].endswith(".dist-info"), name.endswith(".dist-info/RECORD"), name

    stamp = zip_stamp(epoch)
    tmp = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as out:
        for name, executable, data in sorted(entries, key=lambda e: order(e[0])):
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type, info.create_system = zipfile.ZIP_STORED, 3
            info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            out.writestr(info, data)
    tmp.replace(dest)


def build_wheel(src: Path, out: Path, epoch: int, env: dict) -> Path:
    """The pure-Python wheel of the project in ``src`` into ``out``, reproducibly: ``uv build`` in a fresh
    isolated environment with the pinned backends (``CONSTRAINTS``), ``SOURCE_DATE_EPOCH`` ``epoch``, no
    ``[tool.uv.sources]`` and no uv config, then ``normalize_wheel``."""
    with tempfile.TemporaryDirectory(prefix="desk-wheel-") as t:
        run("uv", "build", "--wheel", "--no-config", "--no-sources", "--build-constraint", str(CONSTRAINTS),
            "--python", sys.executable, "--out-dir", t, str(src), env={**env, "SOURCE_DATE_EPOCH": str(epoch)})
        built = sorted(Path(t).glob("*.whl"))
        assert len(built) == 1, f"uv build of {src} left {[p.name for p in built]}"
        dest = out / built[0].name
        normalize_wheel(built[0], dest, epoch)
    return dest


def git_source(url: str, rev: str, dest: Path) -> int:
    """Check out ``url`` at the commit ``rev`` into ``dest``; that commit's time (its SOURCE_DATE_EPOCH)."""
    run("git", "init", "-q", str(dest))
    run("git", "-C", str(dest), "fetch", "-q", "--depth", "1", url, rev)
    run("git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD")
    head = run("git", "-C", str(dest), "rev-parse", "HEAD").strip()
    if len(rev) == 40 and head != rev:
        sys.exit(f"{url}@{rev}: checked out {head}")
    return int(run("git", "-C", str(dest), "log", "-1", "--format=%ct"))


def check_reproducible(builds: list[tuple[Path, int, Path]], env: dict) -> None:
    """Build every (source, epoch, wheel) again from a fresh copy of the source; SystemExit when a byte differs."""
    with tempfile.TemporaryDirectory(prefix="desk-repro-") as t:
        for i, (src, epoch, wheel) in enumerate(builds):
            copy = Path(t) / f"src{i}"
            shutil.copytree(src, copy, ignore=shutil.ignore_patterns("build", "*.egg-info", "dist"))
            (Path(t) / f"out{i}").mkdir()
            again = build_wheel(copy, Path(t) / f"out{i}", epoch, env)
            if again.name != wheel.name or again.read_bytes() != wheel.read_bytes():
                sys.exit(f"{wheel.name} is not reproducible: a second build gives {again.name} sha256 {sha256(again)},"
                         f" the first {sha256(wheel)} (a build backend that is not pinned in {CONSTRAINTS.name}?)")
    print(f"reproducible: {', '.join(w.name for _, _, w in builds)}", flush=True)


# ---- patch bases: what was delivered ----------------------------------------------------------------------------


class Base:
    """A bundle a patch is made from: its version and the sha256 of each of its files (``adapter/`` and
    ``MANIFEST.sha256``, which a patch always ships, left out)."""

    def __init__(self, version: str, files: dict[str, str], source: str):
        self.version = version
        self.files = {rel: d for rel, d in files.items()
                      if not rel.startswith(ADAPTER_DIR) and rel != "MANIFEST.sha256"}
        self.source = source

    @property
    def build(self) -> str:
        return build_id(self.files)


def base_from_zip(path: Path) -> Base:
    """A full bundle zip as a base: the sha256 of each file's own bytes (not of what its manifest says)."""
    prefix = TOP + "/"
    with zipfile.ZipFile(path) as z:
        files = {n[len(prefix):]: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()
                 if n.startswith(prefix) and not n.endswith("/")}
        version = z.read(prefix + "VERSION").decode().strip() if prefix + "VERSION" in z.namelist() else "unknown"
    if any(rel.startswith("PATCH_") for rel in files):
        sys.exit(f"{path}: a patch, not a full bundle: a patch base must be a full bundle (or its record)")
    return Base(version, files, str(path))


def base_from_record(path: Path) -> Base:
    """A delivered record (``--record-delivered``) or a bundle's ``MANIFEST.sha256`` as a base."""
    header, files = parse_manifest(path.read_text(encoding="utf-8-sig"))
    version = None
    for line in header:
        if m := re.match(r"# delivered (\S+?):? ", line) or re.match(rf"# {TOP} (\S+?),", line):
            version = m[1]
            break
    if version is None:
        sys.exit(f"{path}: no '# delivered <version>' (or '# {TOP} <version>,') line: not a bundle manifest")
    return Base(version, files, str(path))


def delivered(version: str, folder: Path | None = None) -> list[Path]:
    d = (folder or DELIVERED) / version
    return sorted(d.glob("*.sha256")) if d.is_dir() else []


def newest_delivered(folder: Path | None = None) -> str | None:
    root = folder or DELIVERED
    versions = [d.name for d in root.iterdir() if d.is_dir() and delivered(d.name, root)] if root.is_dir() else []
    return max(versions, key=version_key, default=None)


def load_bases(args: list[str], folder: Path | None = None) -> list[Base]:
    """``--patch-base`` values as bases: a zip, a manifest / record file, or a delivered version (all its records).
    A zip of a version with delivered records is taken together with them: when the zip is another build of that
    version (a rebuild, or the release's while the owner got another), the patch is made to fit each."""
    out: list[Base] = []
    for arg in args:
        p = Path(arg)
        if p.is_file():
            b = base_from_zip(p) if zipfile.is_zipfile(p) else base_from_record(p)
            records = [base_from_record(r) for r in delivered(b.version, folder)]
            if records and b.build not in {r.build for r in records}:
                print(f"warning: {p} is build {b.build} of {b.version}, delivered was "
                      f"{', '.join(r.build for r in records)}: the patch is made to fit each", flush=True)
            out += [b, *records]
        elif records := delivered(arg, folder):
            out += [base_from_record(r) for r in records]
        else:
            sys.exit(f"--patch-base {arg}: no such file, and no record in {folder or DELIVERED}/{arg}/")
    unique: dict[tuple[str, str], Base] = {}
    for b in out:
        unique.setdefault((b.version, b.build), b)
    return list(unique.values())


def record_delivered(zip_path: Path, folder: Path | None = None) -> Path:
    """Write the record of a delivered full zip: ``<folder>/<version>/<build id>.sha256``, its ``MANIFEST.sha256``
    without ``adapter/`` under a ``# delivered`` line; every file of the zip is checked against the manifest first."""
    prefix = TOP + "/"
    with zipfile.ZipFile(zip_path) as z:
        names = {n[len(prefix):]: n for n in z.namelist() if n.startswith(prefix) and not n.endswith("/")}
        if "MANIFEST.sha256" not in names or "VERSION" not in names or any(r.startswith("PATCH_") for r in names):
            sys.exit(f"{zip_path}: not a full bundle built by this script")
        header, entries = parse_manifest(z.read(names["MANIFEST.sha256"]).decode("utf-8-sig"))
        version = z.read(names["VERSION"]).decode().strip()
        files = {rel: n for rel, n in names.items() if rel != "MANIFEST.sha256"}
        if set(files) != set(entries):
            sys.exit(f"{zip_path}: its files and MANIFEST.sha256 do not match")
        bad = [rel for rel, n in files.items() if hashlib.sha256(z.read(n)).hexdigest() != entries[rel]]
        if bad:
            sys.exit(f"{zip_path}: {bad[0]} does not match MANIFEST.sha256")
    base = Base(version, entries, str(zip_path))
    dest = (folder or DELIVERED) / version / f"{base.build}.sha256"
    dest.parent.mkdir(parents=True, exist_ok=True)
    zips = [f"# delivered {version}: {zip_path.name} sha256 {sha256(zip_path)}"]
    if dest.is_file():  # the same build delivered as another zip (the public one and the with-adapter one)
        zips = [*(ln for ln in parse_manifest(dest.read_text(encoding="utf-8"))[0] if ln.startswith("# delivered ")
                  and ln.split(": ", 1)[1].split()[0] != zip_path.name), *zips]
    dest.write_text("".join(f"{ln}\n" for ln in zips) +
                    f"# build {base.build}: the bundle's MANIFEST.sha256 without adapter/ "
                    "(build_windows_bundle.py --record-delivered); patches from this version are made against it\n"
                    + "".join(f"{ln}\n" for ln in header) + manifest_lines(base.files), encoding="utf-8")
    print(f"delivered {version} build {base.build}: {dest}")
    return dest


def write_patch(bases, stage: Path, version: str, out_dir: Path, stamp: tuple) -> Path:
    """``DeskMonitor-patch-<old>-to-<new>.zip`` from ``bases`` (``Base`` items of one version, or a full zip's
    path): the files of ``stage`` outside ``adapter/`` that are new or differ from a base, VERSION and
    MANIFEST.sha256 (without ``adapter/``) always, PATCH_README.txt, PATCH_BASE.txt, and PATCH_DELETE.txt when files
    were dropped."""
    if isinstance(bases, (str, Path)):
        bases = [base_from_zip(Path(bases))]
    old_version = bases[0].version
    if {b.version for b in bases} != {old_version}:
        raise ValueError(f"one patch per base version, got {sorted({b.version for b in bases})}")
    new = {rel: p for rel, p in stage_files(stage) if not rel.startswith(ADAPTER_DIR)}
    digest = {rel: sha256(p) for rel, p in new.items()}
    changed = [rel for rel in new if rel in ("VERSION", "MANIFEST.sha256")
               or any(b.files.get(rel) != digest[rel] for b in bases)]
    dropped = sorted({rel for b in bases for rel in b.files} - set(new))
    builds = ", ".join(sorted({b.build for b in bases}))
    out = out_dir / f"{TOP}-patch-{old_version}-to-{version}.zip"
    extra = {"PATCH_README.txt": crlf(PATCH_README.format(old=old_version, new=version, builds=builds).encode(),
                                      bom=True),
             "PATCH_BASE.txt": f"{old_version} (сборка {builds})\n".encode()}
    if dropped:
        extra["PATCH_DELETE.txt"] = ("".join(f"{rel}\n" for rel in dropped)).encode()

    def data(rel: str) -> bytes:
        b = new[rel].read_bytes()
        return without_adapter(b) if rel == "MANIFEST.sha256" else b

    write_zip(out, [*((rel, data(rel)) for rel in changed), *extra.items()], stamp)
    size = sum(new[rel].stat().st_size for rel in changed)
    print(f"\npatch {out}  {out.stat().st_size / 1e6:.2f} MB ({size / 1e6:.2f} MB unpacked), sha256 {sha256(out)}\n"
          f"  {old_version} (build {builds}) -> {version}: {len(changed)} files changed or new, "
          f"{len(dropped)} dropped; bases: {', '.join(b.source for b in bases)}")
    for rel in changed:
        print(f"    {rel}")
    for rel in dropped:
        print(f"    deleted: {rel}")
    return out


def write_patches(bases: list[Base], stage: Path, version: str, out_dir: Path, stamp: tuple) -> list[Path]:
    """One patch per base version (never from the version being built)."""
    by_version: dict[str, list[Base]] = {}
    for b in bases:
        if b.version == version:
            print(f"warning: {b.source} is this very version {version}: no patch from it", flush=True)
            continue
        by_version.setdefault(b.version, []).append(b)
    return [write_patch(group, stage, version, out_dir, stamp) for group in by_version.values()]


def check_public_private(public: Path, private: Path) -> None:
    """The with-adapter zip must hold the public zip's files byte for byte outside ``adapter/`` (and its manifest
    without the adapter's lines must be the public one): SystemExit otherwise."""
    def files(p: Path) -> dict[str, bytes]:
        with zipfile.ZipFile(p) as z:
            return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}

    ad, man = f"{TOP}/{ADAPTER_DIR}", f"{TOP}/MANIFEST.sha256"
    a, b = files(public), files(private)
    if any(n.startswith(ad) for n in a):
        sys.exit(f"{public.name} holds adapter/")
    b_pub = {n: (without_adapter(d) if n == man else d) for n, d in b.items() if not n.startswith(ad)}
    diff = sorted(n for n in set(a) | set(b_pub) if a.get(n) != b_pub.get(n))
    if diff:
        sys.exit(f"{private.name} and {public.name} differ outside adapter/: {', '.join(diff[:5])}")
    print(f"{private.name}: the files of {public.name} byte for byte, plus adapter/", flush=True)


def write_bundles(stage: Path, name: str, adapter: bool, out_dir: Path, stamp: tuple) -> list[Path]:
    """The full zip(s) of ``stage``: the public one always, the with-adapter one too when ``adapter``, checked."""
    out_dir.mkdir(parents=True, exist_ok=True)
    public = out_dir / f"{name}.zip"
    write_zip(public, bundle_entries(stage, adapter=False), stamp)
    if not adapter:
        return [public]
    private = out_dir / f"{name}-with-adapter.zip"
    write_zip(private, bundle_entries(stage, adapter=True), stamp)
    check_public_private(public, private)
    return [public, private]


def stage_from_bundle(bundle: Path, stage: Path) -> tuple[str, str, tuple]:
    """Unpack a published full zip into ``stage``, every file checked against its manifest; (version, the
    manifest's ``#`` lines, the zip's timestamp)."""
    prefix = TOP + "/"
    with zipfile.ZipFile(bundle) as z:
        names = [n for n in z.namelist() if n.startswith(prefix) and not n.endswith("/")]
        if any(n[len(prefix):].startswith((ADAPTER_DIR, "PATCH_")) for n in names) or prefix + "VERSION" not in names:
            sys.exit(f"{bundle}: not a public full bundle (no VERSION, or adapter/, or a patch)")
        header, entries = parse_manifest(z.read(prefix + "MANIFEST.sha256").decode("utf-8-sig"))
        got = set()
        for n in names:
            rel, data = n[len(prefix):], z.read(n)
            if rel != "MANIFEST.sha256":
                if entries.get(rel) != hashlib.sha256(data).hexdigest():
                    sys.exit(f"{bundle}: {rel} does not match its MANIFEST.sha256")
                got.add(rel)
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            (stage / rel).write_bytes(data)
        if set(entries) != got:
            sys.exit(f"{bundle}: incomplete, its MANIFEST.sha256 lists files it does not hold")
        stamp = z.getinfo(prefix + "VERSION").date_time
    return (stage / "VERSION").read_text().strip(), "".join(f"{ln}\n" for ln in header), stamp


def check_wheelhouse(wh: Path, env: dict) -> None:
    """Prove the wheelhouse installs on Windows / CPython 3.11 offline, as start.bat does it: pip checks the
    wheel tags (it evaluates markers for this machine), uv the markers for Windows (sys_platform == 'win32')."""
    req = ["--no-index", "--find-links", str(wh), "-r", str(wh / "requirements.txt")]
    with tempfile.TemporaryDirectory(prefix="desk-dry-") as target:
        out = run(sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--only-binary=:all:",
                  *WIN, "--target", target, *req, env=env)
        print(out.strip().splitlines()[-1] if out.strip() else "", flush=True)
        run("uv", "pip", "install", "--dry-run", "--no-config", "--only-binary", ":all:", "--python-platform",
            "x86_64-pc-windows-msvc", "--python-version", "3.11", "--target", target, *req, env=env)




def last_change(paths: tuple[str, ...], fallback: int) -> int:
    """The time of the commit that last changed ``paths`` (the SOURCE_DATE_EPOCH of a wheel built from them)."""
    out = run("git", "log", "-1", "--format=%ct", "--", *paths, cwd=REPO).strip()
    return int(out) if out else fallback


def finish(stage: Path, name: str, version: str, adapter: bool, a: argparse.Namespace, stamp: tuple,
           bases: list[Base]) -> None:
    zips = write_bundles(stage, name, adapter, a.out, stamp)
    listing = stage_files(stage)
    longest = max((f"{TOP}/{rel}" for rel, _ in listing), key=len)
    for z in zips:
        print(f"\n{z}  {z.stat().st_size / 1e6:.1f} MB, sha256 {sha256(z)}")
    print(f"version {version}, build {build_id({rel: sha256(p) for rel, p in listing})}; "
          f"adapter: {sum(rel.startswith(ADAPTER_DIR) for rel, _ in listing)} files; "
          f"longest path in the zip ({len(longest)}): {longest}")
    write_patches(bases, stage, version, a.out, stamp)
    if adapter:
        print("\nafter handing a zip over: python3.11 backend/tools/build_windows_bundle.py --record-delivered "
              "<zip>, then commit backend/tools/delivered/")


def from_bundle(a: argparse.Namespace, adapter: dict[str, bytes], bases: list[Base]) -> int:
    """The with-adapter zip of a published full zip (``--from-bundle``): its files as they are, plus adapter/."""
    with tempfile.TemporaryDirectory(prefix="desk-bundle-") as tmp_:
        stage = Path(tmp_) / TOP
        version, header, stamp = stage_from_bundle(a.from_bundle, stage)
        for rel, data in adapter.items():
            (stage / "adapter" / rel).parent.mkdir(parents=True, exist_ok=True)
            (stage / "adapter" / rel).write_bytes(data)
        write_manifest(stage, header)
        a.out.mkdir(parents=True, exist_ok=True)
        public = a.out / f"{TOP}-{version}.zip"
        if public.resolve() != a.from_bundle.resolve():
            shutil.copyfile(a.from_bundle, public)
        private = a.out / f"{TOP}-{version}-with-adapter.zip"
        write_zip(private, bundle_entries(stage, adapter=True), stamp)
        check_public_private(public, private)
        print(f"\n{private}  {private.stat().st_size / 1e6:.1f} MB, sha256 {sha256(private)}")
        write_patches(bases, stage, version, a.out, stamp)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "dist")
    ap.add_argument("--allow-dirty", action="store_true", help="build from uncommitted changes too")
    ap.add_argument("--adapter-zip", type=Path, help="the private adapter (a zip holding its package) for adapter/; "
                                                     "the public zip is written too, from the same files")
    ap.add_argument("--cache", type=Path, default=REPO / "dist" / "cache", help="downloads of the Python runtime")
    ap.add_argument("--python-release", default=PBS_RELEASE, help="python-build-standalone release tag")
    ap.add_argument("--python-version", default=PBS_PYTHON, help="the CPython 3.11.x of that release")
    ap.add_argument("--no-python", action="store_true", help="no bundled runtime: the PC then needs py -3.11")
    ap.add_argument("--patch-base", action="append", default=[], metavar="ZIP|MANIFEST|VERSION",
                    help="also write a patch from this: an older full zip (the published release asset), a "
                         "MANIFEST.sha256 / delivered record, or a version recorded in backend/tools/delivered/")
    ap.add_argument("--patch-from-delivered", action="store_true",
                    help="also a patch from the newest version recorded in backend/tools/delivered/")
    ap.add_argument("--from-bundle", type=Path, metavar="ZIP",
                    help="with --adapter-zip: make the with-adapter zip from this published full zip (no rebuild)")
    ap.add_argument("--record-delivered", type=Path, nargs="+", metavar="ZIP",
                    help="no build: record these delivered full zips in backend/tools/delivered/")
    ap.add_argument("--no-repro-check", action="store_true", help="do not build our wheels a second time to compare")
    a = ap.parse_args()
    if a.record_delivered:
        for z in a.record_delivered:
            record_delivered(z)
        return 0
    newest = newest_delivered() if a.patch_from_delivered else None
    bases = load_bases([*a.patch_base, *([newest] if newest else [])])
    adapter = adapter_files(a.adapter_zip.read_bytes()) if a.adapter_zip else {}
    if a.from_bundle:
        if not adapter:
            sys.exit("--from-bundle needs --adapter-zip")
        return from_bundle(a, adapter, bases)
    if not a.python_version.startswith("3.11."):
        sys.exit("the wheelhouse is cp311: --python-version must be 3.11.x")
    if shutil.which("uv") is None or subprocess.run([sys.executable, "-m", "pip", "--version"],
                                                    check=False, capture_output=True).returncode:
        sys.exit("needs uv on PATH and pip in this Python (try: python3.11 backend/tools/build_windows_bundle.py)")
    dirty = run("git", "status", "--porcelain", "--", *INCLUDE, cwd=REPO).strip()  # only what is shipped
    if dirty and not a.allow_dirty:
        sys.exit(f"the working tree is not clean (commit first, or --allow-dirty):\n{dirty}")
    commit = run("git", "rev-parse", "HEAD", cwd=REPO).strip()
    epoch = int(run("git", "log", "-1", "--format=%ct", cwd=REPO))
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch), "PIP_DISABLE_PIP_VERSION_CHECK": "1"}

    project = tomllib.loads((REPO / "backend/pyproject.toml").read_text(encoding="utf-8"))
    version = bundle_version(project["project"]["version"], epoch, commit, bool(dirty))
    core_src = project["tool"]["uv"]["sources"]["rub-fixed-income-core"]
    core_pin = next(d for d in project["project"]["dependencies"] if d.startswith("rub-fixed-income-core=="))
    runtime = None if a.no_python else python_runtime(a.cache, a.python_release, a.python_version)

    with tempfile.TemporaryDirectory(prefix="desk-bundle-") as tmp_:
        tmp = Path(tmp_)
        src, stage = tmp / "src", tmp / TOP
        files = [f for f in run("git", "ls-files", "-co", "--exclude-standard", cwd=REPO).splitlines()
                 if f.startswith(INCLUDE) and (REPO / f).is_file()]
        for f in files:
            (src / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / f, src / f)  # bytes only: no mode or time of this checkout
        wh = stage / "wheelhouse"
        wh.mkdir(parents=True)

        # 1. third-party pins: the lock's runtime set plus pgserver, resolved for Windows (markers such as
        #    sys_platform == 'win32' are evaluated for Windows, which pip download --platform does not do)
        export = ["uv", "export", "--frozen", "--no-hashes", "--no-header", "--no-annotate", "--no-emit-project",
                  "--project", str(src / "backend")]
        (tmp / "in.txt").write_text("\n".join(pins(run(*export)) + ["pgserver"]) + "\n")
        (tmp / "lock.txt").write_text("\n".join(pins(run(*export, "--extra", "dev"))) + "\n")
        run("uv", "pip", "compile", str(tmp / "in.txt"), "-c", str(tmp / "lock.txt"), "-o", str(tmp / "win.txt"),
            "--python-platform", "x86_64-pc-windows-msvc", "--python-version", "3.11",
            "--no-header", "--no-annotate", "--no-config", "--quiet")
        third = pins((tmp / "win.txt").read_text())
        run(sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:", *WIN,
            "-r", str(tmp / "win.txt"), "-d", str(wh), env=env)

        # 2. our wheels, pure Python, reproducible: the backend, core at the pinned commit
        core_dir = tmp / "core"
        builds = [(src / "backend", last_change(OURS["desk_backend"], epoch)),
                  (core_dir, git_source(core_src["git"], core_src["rev"], core_dir))]
        built = [(d, e, build_wheel(d, wh, e, env)) for d, e in builds]
        if not a.no_repro_check:
            check_reproducible(built, env)
        ours = {p.name.split("-")[0]: p.name for p in wh.glob("*-py3-none-any.whl")}
        for dist in ("desk_backend", "rub_fixed_income_core"):
            assert dist in ours, f"{dist}: no pure-Python wheel built ({sorted(ours)})"
        core_whl = ours["rub_fixed_income_core"]
        assert core_whl.split("-")[1] == core_pin.split("==")[1], f"core wheel {core_whl} is not {core_pin}"
        local = [f"desk-backend=={project['project']['version']}", core_pin]
        (wh / "requirements.txt").write_text("\n".join(third + local) + "\n")
        n_req = len(third) + len(local)
        n_whl = len(list(wh.glob("*.whl")))
        assert n_whl == n_req, f"{n_whl} wheels for {n_req} pins"
        check_wheelhouse(wh, env)

        # 3. the runtime, the app (launcher, what desk serve shows, the test book, the doc), the adapter
        if runtime:
            unpack_python(runtime[0], stage)
        app = stage / "app"
        for d in ("prototypes", "frontend/dist"):
            shutil.copytree(src / d, app / d, copy_function=shutil.copyfile)
        (app / "examples").mkdir()
        shutil.copyfile(src / "backend/examples/test_book.csv", app / "examples/test_book.csv")
        shutil.copyfile(src / "backend/windows/deskctl.py", app / "deskctl.py")
        shutil.copyfile(src / "docs/INSTALL_BACKEND_WINDOWS.md", app / "INSTALL_BACKEND_WINDOWS.md")
        for rel, data in adapter.items():
            (stage / "adapter" / rel).parent.mkdir(parents=True, exist_ok=True)
            (stage / "adapter" / rel).write_bytes(data)
        for bat in BATS:
            data = (src / "backend/windows" / bat).read_bytes()
            assert data.isascii(), f"{bat}: keep .bat files ASCII (the console code page is not UTF-8)"
            (stage / bat).write_bytes(crlf(data))
        for txt in ("START_HERE.txt", "INSTALL.txt"):  # UTF-8 with a BOM and CRLF: Notepad shows the Russian right
            (stage / txt).write_bytes(crlf((src / "backend/windows" / txt).read_bytes(), bom=True))
        (stage / "VERSION").write_text(version + "\n")

        py_line = (f"python-build-standalone {a.python_release} cpython {a.python_version} sha256 {runtime[1]}"
                   if runtime else "no bundled Python (py -3.11)")
        write_manifest(stage, f"# {TOP} {version}, repository commit {commit}{' (dirty)' if dirty else ''}\n"
                              f"# {py_line}\n")
        print(f"wheelhouse {n_whl} wheels; {py_line}")
        finish(stage, f"{TOP}-{version}", version, bool(adapter), a, zip_stamp(epoch), bases)
    return 0


if __name__ == "__main__":
    sys.exit(main())
