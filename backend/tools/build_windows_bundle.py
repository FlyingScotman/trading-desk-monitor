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
                  embedded store), wheels of desk-backend, trading-desk-monitor and rub-fixed-income-core (the
                  commit pinned in ``[tool.uv.sources]``), ``requirements.txt``; proved complete for Windows by
                  an offline ``pip install --dry-run --platform win_amd64`` before zipping
* ``app/``        ``deskctl.py`` (the launcher the .bat files call), ``prototypes/``, ``frontend/dist/``,
                  ``examples/test_book.csv``, ``INSTALL_BACKEND_WINDOWS.md``
* ``adapter/``    the private adapter's package from ``--adapter-zip`` (never in this repository)
* ``START_HERE.txt`` (the owner's short guide), ``start.bat``, ``stop.bat``, ``update_daily.bat``, ``desk.bat``,
  ``INSTALL.txt``, ``VERSION``, ``MANIFEST.sha256``

start.bat makes the venv ``env/`` from ``python/`` and the wheelhouse, offline, on the first start and whenever
the version or the folder changes; data and settings live in DESK_HOME (see backend/windows/deskctl.py).
``--patch-base`` also writes ``DeskMonitor-patch-<old version>-to-<new version>.zip`` next to the full zip: only
the files whose bytes differ from the old bundle or are new (``VERSION`` and ``MANIFEST.sha256`` always), under the
same ``DeskMonitor/`` folder, plus ``PATCH_README.txt`` and, when files were dropped, ``PATCH_DELETE.txt`` (the
new deskctl.py deletes those on start). Unpacked over the old folder it gives the new bundle's files exactly.

Built from the files git tracks or sees (``git ls-files -co --exclude-standard``); a dirty tree is refused
unless ``--allow-dirty``. Wheels and the zip take their timestamps from the HEAD commit (SOURCE_DATE_EPOCH).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import posixpath
import shutil
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
INCLUDE = ("pyproject.toml", "tdm/", "backend/pyproject.toml", "backend/uv.lock", "backend/src/",
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

1) Остановите программу: двойной щелчок по stop.bat (или закройте окно start.bat).

2) Распакуйте архив в C:\\DeskMonitor с заменой файлов. Проще всего так:
   правой кнопкой по zip -> Свойства -> галочка «Разблокировать» -> ОК; затем откройте zip
   двойным щелчком, зайдите в папку DeskMonitor внутри, выделите всё (Ctrl+A), скопируйте
   и вставьте в папку, где у вас лежит start.bat (например C:\\DeskMonitor). На вопрос
   Windows выберите «Заменить файлы в папке назначения».
   Проверка: в файле VERSION в папке программы теперь {new}.

3) Запустите start.bat. Он пересоберёт окружение (без интернета, ~1-2 минуты);
   настройки, токен и данные сохранятся.

4) В Настройках (http://127.0.0.1:8000/settings.html), раздел «Маркет-дата сервер»:
   - Способ подключения: «Авто» (или «curl Windows»);
   - Прокси: http://host:port БЕЗ логина и пароля - у вас вход по учётке Windows уже проверен;
   - «Логин прокси» и «Пароль прокси» оставьте пустыми;
   - «Сохранить», затем «Проверить связь».
   Если всё же не пускает: укажите логин прокси (ДОМЕН\\логин) и пароль прокси
   (кнопка «Сохранить пароль») и снова «Проверить связь».
"""


def write_patch(base_zip: Path, stage: Path, version: str, out_dir: Path, stamp: tuple) -> Path:
    """``DeskMonitor-patch-<old>-to-<new>.zip``: the files of ``stage`` that are new or differ from ``base_zip``,
    VERSION and MANIFEST.sha256 always, PATCH_README.txt, and PATCH_DELETE.txt when files were dropped."""
    prefix = TOP + "/"
    with zipfile.ZipFile(base_zip) as z:
        old = {n[len(prefix):]: z.read(n) for n in z.namelist() if n.startswith(prefix) and not n.endswith("/")}
    old_version = old.get("VERSION", b"unknown").decode().strip()
    new = {p.relative_to(stage).as_posix(): p for p in sorted(stage.rglob("*")) if p.is_file()}
    changed = [rel for rel, p in new.items()
               if rel in ("VERSION", "MANIFEST.sha256") or old.get(rel) != p.read_bytes()]
    dropped = sorted(set(old) - set(new))
    out = out_dir / f"{TOP}-patch-{old_version}-to-{version}.zip"
    extra = {"PATCH_README.txt": crlf(PATCH_README.format(old=old_version, new=version).encode(), bom=True)}
    if dropped:
        extra["PATCH_DELETE.txt"] = ("".join(f"{rel}\n" for rel in dropped)).encode()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in [*((rel, new[rel].read_bytes()) for rel in changed), *extra.items()]:
            info = zipfile.ZipInfo(prefix + rel, stamp)
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            z.writestr(info, data)
    size = sum(new[rel].stat().st_size for rel in changed)
    print(f"\npatch {out}  {out.stat().st_size / 1e6:.2f} MB ({size / 1e6:.2f} MB unpacked), sha256 {sha256(out)}\n"
          f"  {old_version} -> {version}: {len(changed)} files changed or new, {len(dropped)} dropped")
    for rel in changed:
        print(f"    {rel}")
    for rel in dropped:
        print(f"    deleted: {rel}")
    return out


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "dist")
    ap.add_argument("--allow-dirty", action="store_true", help="build from uncommitted changes too")
    ap.add_argument("--adapter-zip", type=Path, help="the private adapter (a zip holding its package) for adapter/")
    ap.add_argument("--cache", type=Path, default=REPO / "dist" / "cache", help="downloads of the Python runtime")
    ap.add_argument("--python-release", default=PBS_RELEASE, help="python-build-standalone release tag")
    ap.add_argument("--python-version", default=PBS_PYTHON, help="the CPython 3.11.x of that release")
    ap.add_argument("--no-python", action="store_true", help="no bundled runtime: the PC then needs py -3.11")
    ap.add_argument("--patch-base", type=Path, help="an older bundle zip: also write the patch zip from it")
    a = ap.parse_args()
    if not a.python_version.startswith("3.11."):
        sys.exit("the wheelhouse is cp311: --python-version must be 3.11.x")
    if shutil.which("uv") is None or subprocess.run([sys.executable, "-m", "pip", "--version"],
                                                    check=False, capture_output=True).returncode:
        sys.exit("needs uv on PATH and pip in this Python (try: python3.11 backend/tools/build_windows_bundle.py)")
    adapter = adapter_files(a.adapter_zip.read_bytes()) if a.adapter_zip else {}
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
    name = f"DeskMonitor-{version}" + ("-with-adapter" if adapter else "")
    runtime = None if a.no_python else python_runtime(a.cache, a.python_release, a.python_version)

    with tempfile.TemporaryDirectory(prefix="desk-bundle-") as tmp_:
        tmp = Path(tmp_)
        src, stage = tmp / "src", tmp / TOP
        files = [f for f in run("git", "ls-files", "-co", "--exclude-standard", cwd=REPO).splitlines()
                 if f.startswith(INCLUDE) and (REPO / f).is_file()]
        for f in files:
            (src / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / f, src / f)
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

        # 2. our wheels, pure Python: the root package, the backend, core at the pinned commit
        for req in (str(src), str(src / "backend"), f"rub-fixed-income-core @ git+{core_src['git']}@{core_src['rev']}"):
            run(sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wh), req, env=env)
        ours = {p.name.split("-")[0]: p.name for p in wh.glob("*-py3-none-any.whl")}
        for dist in ("desk_backend", "trading_desk_monitor", "rub_fixed_income_core"):
            assert dist in ours, f"{dist}: no pure-Python wheel built ({sorted(ours)})"
        core_whl = ours["rub_fixed_income_core"]
        assert core_whl.split("-")[1] == core_pin.split("==")[1], f"core wheel {core_whl} is not {core_pin}"
        local = [f"desk-backend=={project['project']['version']}",
                 f"trading-desk-monitor=={ours['trading_desk_monitor'].split('-')[1]}", core_pin]
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
            shutil.copytree(src / d, app / d)
        (app / "examples").mkdir()
        shutil.copy2(src / "backend/examples/test_book.csv", app / "examples/test_book.csv")
        shutil.copy2(src / "backend/windows/deskctl.py", app / "deskctl.py")
        shutil.copy2(src / "docs/INSTALL_BACKEND_WINDOWS.md", app / "INSTALL_BACKEND_WINDOWS.md")
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

        listing = sorted(p for p in stage.rglob("*") if p.is_file())
        manifest = "".join(f"{sha256(p)}  {p.relative_to(stage).as_posix()}\n" for p in listing)
        py_line = (f"python-build-standalone {a.python_release} cpython {a.python_version} sha256 {runtime[1]}"
                   if runtime else "no bundled Python (py -3.11)")
        (stage / "MANIFEST.sha256").write_text(f"# {TOP} {version}, repository commit {commit}"
                                               f"{' (dirty)' if dirty else ''}\n# {py_line}\n" + manifest)
        a.out.mkdir(parents=True, exist_ok=True)
        out = a.out / f"{name}.zip"
        stamp = time.gmtime(max(epoch, 315532800))[:6]  # zip dates start in 1980
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(p for p in stage.rglob("*") if p.is_file()):
                info = zipfile.ZipInfo(f"{TOP}/{p.relative_to(stage).as_posix()}", stamp)
                info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
                z.writestr(info, p.read_bytes())
        longest = max((f"{TOP}/{p.relative_to(stage).as_posix()}" for p in listing), key=len)
        print(f"\n{out}  {out.stat().st_size / 1e6:.1f} MB, {len(listing) + 1} files, sha256 {sha256(out)}\n"
              f"version {version}; {py_line}; adapter: {len(adapter)} files\n"
              f"wheelhouse {n_whl} wheels; longest path in the zip ({len(longest)}): {longest}")
        if a.patch_base:
            write_patch(a.patch_base, stage, version, a.out, stamp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
