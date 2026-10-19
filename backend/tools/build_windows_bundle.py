"""Build the offline Windows bundle of the desk backend (dev only, runs at home on the Mac).

    python3.11 backend/tools/build_windows_bundle.py [--allow-dirty] [--out dist]

Needs uv, git, network to PyPI and GitHub, and a Python with pip (``python3.11 -m pip``). Writes
``dist/desk-backend-windows-<version>.zip``: a win_amd64 / cp311 wheelhouse with the exact pins of
``backend/uv.lock`` plus pgserver (the embedded store), wheels of desk-backend, trading-desk-monitor and
rub-fixed-income-core (the commit pinned in ``[tool.uv.sources]``), the prototypes and the built frontend
(``desk serve`` finds them next to the venv, see INSTALL_BACKEND_WINDOWS.md), the synthetic test book,
``desk.env.example`` and the ``backend/windows/*.bat`` scripts. The install folder is the venv itself:
``desk.settings.REPO_ROOT`` of a wheel in ``<root>\\Lib\\site-packages`` is ``<root>``.

Built from the files git tracks or sees (``git ls-files -co --exclude-standard``); a dirty tree is refused
unless ``--allow-dirty``. Wheels and the zip take their timestamps from the HEAD commit (SOURCE_DATE_EPOCH).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# what the bundle needs from the repository; the rest (tests, docs, tools) stays at home
INCLUDE = ("pyproject.toml", "tdm/", "backend/pyproject.toml", "backend/uv.lock", "backend/src/",
           "backend/examples/test_book.csv", "backend/windows/", "prototypes/", "frontend/dist/",
           "docs/INSTALL_BACKEND_WINDOWS.md")
WIN = ["--platform", "win_amd64", "--python-version", "3.11", "--implementation", "cp", "--abi", "cp311"]


def run(*cmd: str, cwd: Path | None = None, env: dict | None = None) -> str:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE, text=True).stdout


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def pins(text: str) -> list[str]:
    """``name==version[ ; marker]`` lines of a uv export; local (-e) and git requirements dropped."""
    return [ln for ln in text.splitlines() if ln and not ln.startswith(("#", " ", "-e")) and "==" in ln]


def env_template(settings_py: str) -> str:
    keys = dict.fromkeys(k for k in re.findall(r"\bDESK_[A-Z0-9_]+\b", settings_py) if k != "DESK_ENV_FILE")
    return ("# Настройки desk-бэкенда: ключи без значений. Пустое значение = не задано (значение по умолчанию).\n"
            "# Описание каждого ключа: INSTALL_BACKEND_WINDOWS.md. Относительные пути считаются от папки установки.\n"
            "# Токен хаба храните в отдельном файле (DESK_HUB_TOKEN_FILE), не здесь.\n"
            "# HTTPS_PROXY здесь не читается: задайте его переменной окружения Windows.\n"
            + "".join(f"{k}=\n" for k in keys))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "dist")
    ap.add_argument("--allow-dirty", action="store_true", help="build from uncommitted changes too")
    a = ap.parse_args()
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
    version = project["project"]["version"]
    core_src = project["tool"]["uv"]["sources"]["rub-fixed-income-core"]
    core_pin = next(d for d in project["project"]["dependencies"] if d.startswith("rub-fixed-income-core=="))
    name = f"desk-backend-windows-{version}"

    with tempfile.TemporaryDirectory(prefix="desk-bundle-") as tmp_:
        tmp = Path(tmp_)
        src, stage = tmp / "src", tmp / name
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
        local = [f"desk-backend=={version}", f"trading-desk-monitor=={ours['trading_desk_monitor'].split('-')[1]}",
                 core_pin]
        (wh / "requirements.txt").write_text("\n".join(third + local) + "\n")
        n_req = len(third) + len(local)
        n_whl = len(list(wh.glob("*.whl")))
        assert n_whl == n_req, f"{n_whl} wheels for {n_req} pins"

        # 3. what desk serve finds next to the venv, the test book, the env template, the scripts, the doc
        for d in ("prototypes", "frontend/dist"):
            shutil.copytree(src / d, stage / d)
        (stage / "examples").mkdir()
        shutil.copy2(src / "backend/examples/test_book.csv", stage / "examples/test_book.csv")
        (stage / "desk.env.example").write_bytes(
            env_template((src / "backend/src/desk/settings.py").read_text(encoding="utf-8")).replace("\n", "\r\n")
            .encode("utf-8"))
        for bat in sorted((src / "backend/windows").glob("*.bat")):
            (stage / bat.name).write_bytes(bat.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        shutil.copy2(src / "docs/INSTALL_BACKEND_WINDOWS.md", stage / "INSTALL_BACKEND_WINDOWS.md")

        listing = sorted(p for p in stage.rglob("*") if p.is_file())
        manifest = "".join(f"{sha256(p)}  {p.relative_to(stage).as_posix()}\n" for p in listing)
        (stage / "MANIFEST.sha256").write_text(f"# {name}, repository commit {commit}{' (dirty)' if dirty else ''}\n"
                                               + manifest)
        a.out.mkdir(parents=True, exist_ok=True)
        out = a.out / f"{name}.zip"
        stamp = time.gmtime(max(epoch, 315532800))[:6]  # zip dates start in 1980
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(p for p in stage.rglob("*") if p.is_file()):
                info = zipfile.ZipInfo(f"{name}/{p.relative_to(stage).as_posix()}", stamp)
                info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
                z.writestr(info, p.read_bytes())
        print(f"\n{manifest}\n{out}  {out.stat().st_size / 1e6:.1f} MB, {len(listing) + 1} files, "
              f"sha256 {sha256(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
