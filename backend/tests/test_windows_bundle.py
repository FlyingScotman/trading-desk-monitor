"""The Windows bundle: the build helpers (backend/tools/build_windows_bundle.py) and the launcher the .bat files
call (backend/windows/deskctl.py). Neither is part of the package, so they are loaded from their files."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import types
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from desk import update as update_module

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load("build_windows_bundle", REPO / "backend/tools/build_windows_bundle.py")
deskctl = _load("deskctl", REPO / "backend/windows/deskctl.py")


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    return buf.getvalue()


def test_bundle_version_has_date_and_sha():
    epoch = int(datetime(2026, 10, 19, 12, tzinfo=UTC).timestamp())
    assert build.bundle_version("0.1.0", epoch, "e7410dc0ffee", False) == "0.1.0-20261019-e7410dc"
    now = datetime(2026, 10, 20, 7, 5, tzinfo=UTC)
    assert build.bundle_version("0.1.0", epoch, "e7410dc0ffee", True, now) == "0.1.0-20261019-e7410dc-dirty202610200705"


def test_adapter_files_takes_the_one_package_and_its_readme():
    out = build.adapter_files(_zip({"src/pkg/__init__.py": b"x", "src/pkg/sub/__init__.py": b"", "src/pkg/m.json": b"{}",
                                    "src/pkg/__pycache__/a.cpython-311.pyc": b"", "README.md": b"r", "tests/t.py": b""}))
    assert sorted(out) == ["README.md", "pkg/__init__.py", "pkg/m.json", "pkg/sub/__init__.py"]


def test_adapter_files_keeps_packages_under_a_plain_folder_of_the_package():
    # the adapter's acquisition helper: src/tda/_acquisition (no __init__.py) holds the packages system2, profiler
    out = build.adapter_files(_zip({"src/tda/__init__.py": b"x", "src/tda/_acquisition/system2/__init__.py": b"",
                                    "src/tda/_acquisition/system2/acquire.py": b"", "README_INSTALL.txt": b"r",
                                    "src/tda/_acquisition/profiler/__init__.py": b""}))
    assert sorted(out) == ["README_INSTALL.txt", "tda/__init__.py", "tda/_acquisition/profiler/__init__.py",
                           "tda/_acquisition/system2/__init__.py", "tda/_acquisition/system2/acquire.py"]


@pytest.mark.parametrize("files", [{"a/__init__.py": b"", "b/__init__.py": b""}, {"x.py": b""},
                                   {"../evil/__init__.py": b""}])
def test_adapter_files_refuses(files):
    with pytest.raises(ValueError):
        build.adapter_files(_zip(files))


def test_bats_are_ascii_crlf_and_quote_their_paths():
    for bat in build.BATS:
        data = (REPO / "backend/windows" / bat).read_bytes()
        assert data.isascii() and data.startswith(b"@echo off\r\n") and b"setlocal" in data
        assert data.count(b"\n") == data.count(b"\r\n")
        assert b'"%~dp0app\\deskctl.py"' in data


def test_owner_guides_are_russian_text_the_builder_ships():
    src = (REPO / "backend/tools/build_windows_bundle.py").read_text(encoding="utf-8")
    for name in ("START_HERE.txt", "INSTALL.txt"):
        text = (REPO / "backend/windows" / name).read_text(encoding="utf-8")
        assert name.isascii() and f'"{name}"' in src and "start.bat" in text and "Распакуйте" in text
    guide = (REPO / "backend/windows/START_HERE.txt").read_text(encoding="utf-8")
    for must in ("C:\\DeskMonitor", "Разблокировать", "Проверить связь", "snapshots", "Позиции", "books.json",
                 "patterns.json", "report_risk", "Обновить", "%LOCALAPPDATA%\\DeskMonitor", "adapter check --dir",
                 "hub-check", "stop.bat"):
        assert must in guide, must


def test_desk_environ_sets_defaults_only(monkeypatch, tmp_path):
    adapter = tmp_path / "adapter" / "pkg"
    adapter.mkdir(parents=True)
    (adapter / "__init__.py").write_text("")
    monkeypatch.setattr(deskctl, "ADAPTER", tmp_path / "adapter")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("DESK_HOME", raising=False)
    monkeypatch.delenv("DESK_DEFAULT_PRIVATE_ADAPTER", raising=False)
    monkeypatch.delenv("DESK_ENV_FILE", raising=False)
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    env = deskctl.desk_environ()
    assert env["DESK_HOME"] == str(tmp_path / "local" / "DeskMonitor")
    assert env["DESK_DEFAULT_PRIVATE_ADAPTER"] == str(adapter)
    assert env["DESK_STATIC_ROOT"] == str(deskctl.APP)
    assert env["DESK_BUNDLE_VERSION"] == deskctl.version()  # in each run's lineage (desk.logic_version)
    assert "PYTHONPATH" not in env and "DESK_ENV_FILE" not in env
    monkeypatch.setenv("DESK_HOME", str(tmp_path / "mine"))
    monkeypatch.setenv("DESK_DEFAULT_PRIVATE_ADAPTER", "other")
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "desk.env").write_text("")
    env = deskctl.desk_environ()
    assert env["DESK_HOME"] == str(tmp_path / "mine") and env["DESK_DEFAULT_PRIVATE_ADAPTER"] == "other"
    assert env["DESK_ENV_FILE"] == str(tmp_path / "mine" / "desk.env")


def test_ensure_env_keeps_a_venv_of_this_version_and_folder(monkeypatch, tmp_path):
    env_dir = tmp_path / "env"
    monkeypatch.setattr(deskctl, "ROOT", tmp_path)
    monkeypatch.setattr(deskctl, "ENV", env_dir)
    monkeypatch.setattr(deskctl, "SCRIPTS", env_dir / "Scripts")
    monkeypatch.setattr(deskctl, "MARKER", env_dir / "desk-bundle.json")
    (tmp_path / "VERSION").write_text("1.0-x\n")
    (env_dir / "Scripts").mkdir(parents=True)
    deskctl.desk_exe().write_text("")
    deskctl.MARKER.write_text(json.dumps({"version": "1.0-x", "root": str(tmp_path)}))
    calls = []
    monkeypatch.setattr(deskctl.subprocess, "run", lambda *a, **k: calls.append(a))
    deskctl.ensure_env({"DESK_HOME": str(tmp_path / "home")})
    assert calls == []  # nothing to do
    (tmp_path / "VERSION").write_text("1.1-y\n")
    monkeypatch.setattr(deskctl, "bundle_processes", lambda: ([4242], []))
    with pytest.raises(deskctl.Fail):  # a new version, but a desk process of this folder still runs
        deskctl.ensure_env({"DESK_HOME": str(tmp_path / "home")})
    assert env_dir.is_dir()


def test_db_migrate_is_retried_when_postgres_starts_slowly(monkeypatch, tmp_path):
    """pgserver gives pg_ctl 10 s; an antivirus on the first start can take longer: up to 3 tries, then Fail."""
    codes, calls, said = [1, 1, 0], [], []
    monkeypatch.setattr(deskctl.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or types.SimpleNamespace(returncode=codes.pop(0)))
    monkeypatch.setattr(deskctl.time, "sleep", lambda s: None)
    monkeypatch.setattr(deskctl, "say", said.append)
    env = {"DESK_HOME": str(tmp_path / "home")}
    deskctl.migrate_store(env)
    assert [c[-1] for c in calls] == ["db-migrate"] * 3
    assert sum("Повтор" in x for x in said) == 2 and "Повтор 3 из 3" in said[-1]
    codes[:] = [1, 1, 1]
    with pytest.raises(deskctl.Fail, match="3 попытки"):
        deskctl.migrate_store(env)


def test_desk_home_falls_back_to_ascii_and_odd_paths_are_refused(monkeypatch, tmp_path):
    # PostgreSQL writes its paths in the ANSI code page and pgserver reads them as UTF-8; its psql runs unquoted
    monkeypatch.setattr(deskctl, "WIN", True)
    env = {"LOCALAPPDATA": "C:\\Users\\Иванов\\AppData\\Local", "SystemDrive": "C:"}
    home = deskctl.default_home(env)  # C:\DeskMonitorData (a posix Path here)
    assert str(home).startswith("C:\\") and home.name == "DeskMonitorData"
    env = {"LOCALAPPDATA": "C:\\Users\\ivanov\\AppData\\Local"}
    assert deskctl.default_home(env) == Path("C:\\Users\\ivanov\\AppData\\Local") / "DeskMonitor"
    monkeypatch.setattr(deskctl, "ROOT", Path("/x/Desk Monitor"))
    with pytest.raises(deskctl.Fail):
        deskctl.check_paths({"DESK_HOME": "C:\\DeskMonitorData"})
    deskctl.check_paths({"DESK_HOME": "C:\\DeskMonitorData", "DESK_STORE_URL": "postgresql://h/desk"})
    monkeypatch.setattr(deskctl, "ROOT", Path("/x/DeskMonitor"))
    deskctl.check_paths({"DESK_HOME": "C:\\DeskMonitorData"})
    with pytest.raises(deskctl.Fail):
        deskctl.check_paths({"DESK_HOME": "C:\\Users\\Иванов\\DeskMonitor"})


def test_desk_environ_drops_this_machines_pip_settings(monkeypatch):
    monkeypatch.setenv("PIP_USER", "1")
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirror.example/simple")
    env = deskctl.desk_environ()
    assert "PIP_USER" not in env and "PIP_INDEX_URL" not in env and env["PIP_NO_INPUT"] == "1"


def test_patch_zip_holds_only_changed_files_and_overlays_exactly(monkeypatch, tmp_path):
    def tree(root: Path) -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    old = {"VERSION": b"1.0-a\n", "MANIFEST.sha256": b"m1", "python/python.exe": b"py", "app/deskctl.py": b"v1",
           "wheelhouse/desk_backend-0.1.0-py3-none-any.whl": b"w1", "app/gone.txt": b"x"}
    base = tmp_path / "old.zip"
    with zipfile.ZipFile(base, "w") as z:
        for rel, data in old.items():
            z.writestr(f"DeskMonitor/{rel}", data)
    stage = tmp_path / "stage"
    new = {**{k: v for k, v in old.items() if k != "app/gone.txt"}, "VERSION": b"1.1-b\n", "MANIFEST.sha256": b"m2",
           "wheelhouse/desk_backend-0.1.0-py3-none-any.whl": b"w2", "app/new.js": b"n"}
    for rel, data in new.items():
        (stage / rel).parent.mkdir(parents=True, exist_ok=True)
        (stage / rel).write_bytes(data)
    patch = build.write_patch(base, stage, "1.1-b", tmp_path, (2026, 10, 20, 0, 0, 0))
    assert patch.name == "DeskMonitor-patch-1.0-a-to-1.1-b.zip"
    with zipfile.ZipFile(patch) as z:
        names = sorted(n.removeprefix("DeskMonitor/") for n in z.namelist())
        assert names == ["MANIFEST.sha256", "PATCH_BASE.txt", "PATCH_DELETE.txt", "PATCH_README.txt", "VERSION",
                         "app/new.js", "wheelhouse/desk_backend-0.1.0-py3-none-any.whl"]
        assert "1.1-b" in z.read("DeskMonitor/PATCH_README.txt").decode("utf-8-sig")
        base_build = build.base_from_zip(base).build
        assert z.read("DeskMonitor/PATCH_BASE.txt") == f"1.0-a (сборка {base_build})\n".encode()
        installed = tmp_path / "C"
        zipfile.ZipFile(base).extractall(installed)
        z.extractall(installed)
    root = installed / "DeskMonitor"
    (root / "PATCH_README.txt").unlink()
    monkeypatch.setattr(deskctl, "ROOT", root)
    deskctl.apply_patch_delete()
    assert tree(root) == new


# ---- reproducible builds ----------------------------------------------------------------------------------------


def _wheel(path: Path, entries: list[tuple[str, bytes, int, tuple]], method=zipfile.ZIP_DEFLATED) -> Path:
    """A wheel as a build backend may write it: ``entries`` (name, data, mode, date) in this order."""
    with zipfile.ZipFile(path, "w", method) as z:
        for name, data, mode, date in entries:
            info = zipfile.ZipInfo(name, date)
            info.external_attr, info.compress_type = mode << 16, method
            z.writestr(info, data)
    return path


def test_normalize_wheel_gives_one_form_whatever_the_backend_wrote(tmp_path):
    """The 0.2.0 incident: the core wheel of two builds of one commit differed only in its entries' times (the pip
    cache kept a wheel built under another SOURCE_DATE_EPOCH) and modes (the checkout's umask)."""
    files = [("pkg/__init__.py", b"x = 1\n"), ("pkg/tool.sh", b"#!/bin/sh\n"), ("pkg-1.0.dist-info/METADATA", b"m"),
             ("pkg-1.0.dist-info/WHEEL", b"w"), ("pkg-1.0.dist-info/RECORD", b"r")]
    a = _wheel(tmp_path / "a.whl", [(n, d, 0o100755 if n.endswith(".sh") else 0o100644, (2026, 10, 22, 8, 17, 12))
                                    for n, d in files])
    b = _wheel(tmp_path / "b.whl", [(n, d, 0o100775 if n.endswith(".sh") else 0o100664, (2026, 10, 19, 1, 2, 4))
                                    for n, d in reversed(files)], zipfile.ZIP_STORED)
    epoch = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
    build.normalize_wheel(a, tmp_path / "a2.whl", epoch)
    build.normalize_wheel(b, tmp_path / "b2.whl", epoch)
    assert (tmp_path / "a2.whl").read_bytes() == (tmp_path / "b2.whl").read_bytes()
    with zipfile.ZipFile(tmp_path / "a2.whl") as z:
        infos = z.infolist()
        assert [i.filename for i in infos] == ["pkg/__init__.py", "pkg/tool.sh", "pkg-1.0.dist-info/METADATA",
                                               "pkg-1.0.dist-info/WHEEL", "pkg-1.0.dist-info/RECORD"]
        assert {i.date_time for i in infos} == {(2026, 10, 1, 0, 0, 0)}
        assert [i.external_attr >> 16 & 0o777 for i in infos] == [0o644, 0o755, 0o644, 0o644, 0o644]
        assert {n: z.read(n) for n in z.namelist()} == dict(files)  # contents (and so RECORD) untouched


def _stage(root: Path, files: dict[str, bytes], order: list[str]) -> Path:
    for i, rel in enumerate(order):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(files[rel])
        os.utime(root / rel, (1_700_000_000 + 1000 * i,) * 2)
        os.chmod(root / rel, 0o664 if i % 2 else 0o644)
    build.write_manifest(root, f"# DeskMonitor {files['VERSION'].decode().strip()}, repository commit abc\n")
    return root


BUNDLE = {"VERSION": b"1.1-b\n", "start.bat": b"@echo off\r\n", "app/deskctl.py": b"print()\n",
          "wheelhouse/requirements.txt": b"a==1\n", "wheelhouse/a-1-py3-none-any.whl": b"PK...",
          "python/python.exe": b"MZ"}


def test_same_files_give_byte_identical_bundles_and_manifests(tmp_path):
    """Two builds of one commit (files written in another order, other mtimes and modes) give the same zips."""
    stamp = build.zip_stamp(1_760_000_000)
    s1 = _stage(tmp_path / "s1" / "DeskMonitor", BUNDLE, list(BUNDLE))
    s2 = _stage(tmp_path / "s2" / "DeskMonitor", BUNDLE, list(reversed(BUNDLE)))
    assert (s1 / "MANIFEST.sha256").read_bytes() == (s2 / "MANIFEST.sha256").read_bytes()
    z1 = build.write_bundles(s1, "DeskMonitor-1.1-b", False, tmp_path / "o1", stamp)
    z2 = build.write_bundles(s2, "DeskMonitor-1.1-b", False, tmp_path / "o2", stamp)
    assert [p.read_bytes() for p in z1] == [p.read_bytes() for p in z2]
    with zipfile.ZipFile(z1[0]) as z:
        assert {i.external_attr >> 16 for i in z.infolist()} == {0o100644} and {i.create_system for i in z.infolist()} == {3}


def test_our_wheels_are_built_by_uv_with_pinned_backends_and_the_commit_time(monkeypatch, tmp_path):
    calls = []

    def fake_run(*cmd, cwd=None, env=None):
        calls.append((cmd, env))
        out = Path(cmd[cmd.index("--out-dir") + 1])
        _wheel(out / "pkg-1.0-py3-none-any.whl", [("pkg/__init__.py", b"", 0o100664, (2026, 1, 1, 0, 0, 0))])
        return ""

    monkeypatch.setattr(build, "run", fake_run)
    whl = build.build_wheel(tmp_path / "src", tmp_path, 1_760_000_000, {"PATH": "/bin"})
    (cmd, env), = calls
    assert cmd[:3] == ("uv", "build", "--wheel") and "--no-sources" in cmd and "--no-config" in cmd
    assert cmd[cmd.index("--build-constraint") + 1] == str(build.CONSTRAINTS)
    assert env["SOURCE_DATE_EPOCH"] == "1760000000" and env["PATH"] == "/bin"
    with zipfile.ZipFile(whl) as z:
        assert z.infolist()[0].date_time == build.zip_stamp(1_760_000_000)
    pins = dict(ln.split("==") for ln in build.CONSTRAINTS.read_text().splitlines() if ln and not ln.startswith("#"))
    assert "hatchling" in pins and "setuptools" not in pins  # the backend of desk-backend and core; no root package
    src = (REPO / "backend/tools/build_windows_bundle.py").read_text(encoding="utf-8")
    assert '"pip", "wheel"' not in src  # pip's wheel cache kept a core wheel of another build: never again


@pytest.mark.bundle
def test_a_real_wheel_build_is_reproducible(tmp_path):
    """``pytest -m bundle`` (needs uv and PyPI for the pinned backends): desk-backend built twice, from two
    copies with other mtimes and modes, gives the same bytes."""
    import subprocess

    epoch = int(subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=REPO, capture_output=True, text=True,
                               check=True).stdout)
    wheels = []
    for i in (1, 2):
        src = tmp_path / f"src{i}"
        shutil.copytree(REPO / "backend/src", src / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(REPO / "backend/pyproject.toml", src / "pyproject.toml")
        for p in src.rglob("*"):
            os.utime(p, (1_600_000_000 + i * 99_999,) * 2)
            if p.is_file():
                os.chmod(p, 0o664 if i == 2 else 0o644)
        (tmp_path / f"out{i}").mkdir()
        wheels.append(build.build_wheel(src, tmp_path / f"out{i}", epoch, dict(os.environ)))
    assert wheels[0].name == wheels[1].name and wheels[0].read_bytes() == wheels[1].read_bytes()


# ---- patch bases: delivered records -----------------------------------------------------------------------------


def _full(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    stage = _stage(tmp_path / name / "DeskMonitor", files, list(files))
    return build.write_bundles(stage, f"DeskMonitor-{name}", False, tmp_path / "dist", build.zip_stamp(1_760_000_000))[0]


def test_a_delivered_record_is_the_base_of_local_patches(tmp_path):
    old = {**BUNDLE, "VERSION": b"1.0-a\n"}
    delivered_zip = _full(tmp_path, "1.0-a", old)
    rebuilt = _full(tmp_path, "1.0-a-rebuilt", {**old, "wheelhouse/a-1-py3-none-any.whl": b"PK rebuilt"})
    folder = tmp_path / "delivered"
    rec = build.record_delivered(delivered_zip, folder)
    assert rec.parent == folder / "1.0-a" and rec.name == build.base_from_zip(delivered_zip).build + ".sha256"
    assert build.base_from_record(rec).files == build.base_from_zip(delivered_zip).files
    twin = tmp_path / "DeskMonitor-1.0-a-with-adapter.zip"  # the same build delivered as another zip: one record
    shutil.copyfile(delivered_zip, twin)
    assert build.record_delivered(twin, folder) == rec and len(list(rec.parent.iterdir())) == 1
    assert [ln.split()[3] for ln in rec.read_text().splitlines() if ln.startswith("# delivered")] == \
        [delivered_zip.name, twin.name]
    assert build.record_delivered(twin, folder) == rec and rec.read_text().count("# delivered") == 2
    assert build.newest_delivered(folder) == "1.0-a"
    for v in ("0.9.0-20261002-bbbbbbb", "0.10.0-20261001-aaaaaaa", "0.9.0-20261003-ccccccc"):
        (folder / v).mkdir()
        (folder / v / "x.sha256").write_text(rec.read_text().replace("1.0-a", v))
    assert build.newest_delivered(folder) == "0.10.0-20261001-aaaaaaa"  # by version, then by commit date
    for v in ("0.9.0-20261002-bbbbbbb", "0.10.0-20261001-aaaaaaa", "0.9.0-20261003-ccccccc"):
        shutil.rmtree(folder / v)

    # a patch from the version name uses the record; from a rebuild of that version, it fits both builds
    new = _stage(tmp_path / "new" / "DeskMonitor", {**BUNDLE, "app/deskctl.py": b"print(2)\n"}, list(BUNDLE))
    by_name = build.load_bases(["1.0-a"], folder)
    assert [b.source for b in by_name] == [str(rec)]
    both = build.load_bases([str(rebuilt)], folder)
    assert {b.build for b in both} == {build.base_from_zip(rebuilt).build, build.base_from_zip(delivered_zip).build}
    (patch,) = build.write_patches(both, new, "1.1-b", tmp_path, build.zip_stamp(1_760_000_000))
    with zipfile.ZipFile(patch) as z:
        names = sorted(n.removeprefix("DeskMonitor/") for n in z.namelist())
        base_txt = z.read("DeskMonitor/PATCH_BASE.txt").decode()
    # the wheel differs from one of the two builds of 1.0-a: shipped, so the patch fits either install
    assert "wheelhouse/a-1-py3-none-any.whl" in names and "app/deskctl.py" in names and "start.bat" not in names
    assert base_txt.startswith("1.0-a (сборка ") and all(b.build in base_txt for b in both)
    with pytest.raises(SystemExit):
        build.load_bases(["0.0.1-nothing"], folder)
    assert build.write_patches(build.load_bases([str(rebuilt)], folder), new, "1.0-a", tmp_path, (2026, 1, 1, 0, 0, 0)) == []


def test_the_with_adapter_zip_is_the_public_one_plus_adapter_and_patches_leave_it_alone(tmp_path):
    files = {**BUNDLE, "adapter/pkg/__init__.py": b"private", "adapter/README.md": b"r"}
    stage = _stage(tmp_path / "s" / "DeskMonitor", files, list(files))
    public, private = build.write_bundles(stage, "DeskMonitor-1.1-b", True, tmp_path / "dist",
                                          build.zip_stamp(1_760_000_000))
    assert private.name == "DeskMonitor-1.1-b-with-adapter.zip"
    with zipfile.ZipFile(public) as z:
        man = z.read("DeskMonitor/MANIFEST.sha256")
        assert not [n for n in z.namelist() if "/adapter/" in n] and b"adapter/" not in man
    # a public build of the same files without the adapter is byte-identical to the public zip written here
    plain = _stage(tmp_path / "p" / "DeskMonitor", BUNDLE, list(BUNDLE))
    (again,) = build.write_bundles(plain, "DeskMonitor-1.1-b", False, tmp_path / "dist2", build.zip_stamp(1_760_000_000))
    assert again.read_bytes() == public.read_bytes()
    # a with-adapter zip whose other files differ is refused
    other = _stage(tmp_path / "o" / "DeskMonitor", {**files, "app/deskctl.py": b"other"}, list(files))
    bad = tmp_path / "bad.zip"
    build.write_zip(bad, build.bundle_entries(other, adapter=True), build.zip_stamp(1_760_000_000))
    with pytest.raises(SystemExit, match="differ outside adapter"):
        build.check_public_private(public, bad)
    # the patch of a with-adapter build: no adapter files, its manifest without them (= the public patch)
    old = _full(tmp_path, "1.0-a", {**BUNDLE, "VERSION": b"1.0-a\n"})
    (tmp_path / "pp").mkdir()
    p_private = build.write_patch(old, stage, "1.1-b", tmp_path / "pp", (2026, 1, 1, 0, 0, 0))
    p_public = build.write_patch(old, plain, "1.1-b", tmp_path / "dist2", (2026, 1, 1, 0, 0, 0))
    assert p_private.read_bytes() == p_public.read_bytes()
    with zipfile.ZipFile(p_private) as z:
        assert not [n for n in z.namelist() if "adapter" in n]
        assert b"adapter/" not in z.read("DeskMonitor/MANIFEST.sha256")


def test_from_bundle_makes_the_with_adapter_zip_of_a_published_one(tmp_path):
    import argparse

    public = _full(tmp_path, "1.1-b", BUNDLE)
    a = argparse.Namespace(from_bundle=public, out=tmp_path / "out")
    assert build.from_bundle(a, {"pkg/__init__.py": b"private"}, []) == 0
    private = tmp_path / "out" / "DeskMonitor-1.1-b-with-adapter.zip"
    build.check_public_private(public, private)
    assert (tmp_path / "out" / "DeskMonitor-1.1-b.zip").read_bytes() == public.read_bytes()
    with zipfile.ZipFile(private) as z:
        assert z.read("DeskMonitor/adapter/pkg/__init__.py") == b"private"
        assert b"adapter/pkg/__init__.py" in z.read("DeskMonitor/MANIFEST.sha256")
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(public) as src, zipfile.ZipFile(broken, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, b"changed" if n.endswith("deskctl.py") else src.read(n))
    with pytest.raises(SystemExit, match="does not match"):
        build.from_bundle(argparse.Namespace(from_bundle=broken, out=tmp_path / "out2"), {"pkg/__init__.py": b""}, [])


def test_release_workflow_patches_from_the_published_release_and_the_delivered_record():
    wf = (REPO / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "gh release download" in wf and "SHA256SUMS" in wf and "sha256sum" in wf
    assert "--patch-from-delivered" in wf and "--patch-base" in wf


def test_a_tag_without_v_fails_its_own_workflow_and_v_tags_are_left_to_the_release(tmp_path):
    """Issue #75: a tag ``0.2.2`` (no ``v``) never ran release.yml and left an empty release; tag-format.yml fails on
    such a tag (pushed, or a release made by hand) with what to do; release.yml still runs on v-tags only."""
    wf = (REPO / ".github/workflows/tag-format.yml").read_text(encoding="utf-8")
    assert 'tags: ["*", "!v*"]' in wf and "types: [published]" in wf
    assert "!startsWith(github.event.release.tag_name, 'v')" in wf and "exit 1" in wf and "::error" in wf
    rel = (REPO / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'tags: ["v*"]' in rel and "check-tag" in rel
    # «Run workflow» on main without a tag makes v<backend version> itself (on the built commit), never twice
    assert "MAKE_TAG=1" in rel and '--target "$GITHUB_SHA"' in rel and "refs/heads/main" in rel
    assert "git ls-remote --exit-code --tags origin" in rel
    script = wf.split("run: |\n", 1)[1]
    script = "\n".join(ln[10:] for ln in script.splitlines())

    summary = tmp_path / "summary.md"
    r = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, check=False,
                       env={**os.environ, "TAG": "0.2.2", "GITHUB_STEP_SUMMARY": str(summary)})
    assert r.returncode == 1 and "::error title=Release tag without v::tag '0.2.2' has no 'v'" in r.stdout
    assert "do not create releases by hand" in summary.read_text(encoding="utf-8")


def test_a_patch_unpacked_by_hand_over_another_build_is_reported(monkeypatch, tmp_path):
    old = {**BUNDLE, "VERSION": b"1.0-a\n"}
    base = _full(tmp_path, "1.0-a", old)
    new = _stage(tmp_path / "new" / "DeskMonitor", {**BUNDLE, "app/deskctl.py": b"print(2)\n"}, list(BUNDLE))
    patch = build.write_patch(base, new, "1.1-b", tmp_path, (2026, 1, 1, 0, 0, 0))
    said: list[str] = []
    monkeypatch.setattr(deskctl, "say", said.append)
    for name, wheel in (("same", old["wheelhouse/a-1-py3-none-any.whl"]), ("other", b"PK another build")):
        root = tmp_path / name
        zipfile.ZipFile(base).extractall(root)
        (root / "DeskMonitor/wheelhouse/a-1-py3-none-any.whl").write_bytes(wheel)
        zipfile.ZipFile(patch).extractall(root)
        monkeypatch.setattr(deskctl, "ROOT", root / "DeskMonitor")
        deskctl.apply_patch_delete()
        assert not (root / "DeskMonitor/PATCH_BASE.txt").exists()
    assert len(said) == 1 and "wheelhouse/a-1-py3-none-any.whl" in said[0] and "1.0-a (сборка" in said[0]


# ---- the bundle without packages (--mirror): mirror.json, the mirror stage, names and patches ------------------------


def _whl(folder: Path, dist: str, version: str, requires: list[str], modules: tuple[str, ...] = ()) -> Path:
    """A wheel with METADATA (Name, Version, Requires-Dist) and empty modules, as the build reads them."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{dist.replace('-', '_')}-{version}-py3-none-any.whl"
    meta = f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n" + "".join(f"Requires-Dist: {r}\n" for r in requires)
    with zipfile.ZipFile(path, "w") as z:  # a fixed time: the same wheel is the same bytes, so the same sha256
        for m in modules:
            z.writestr(zipfile.ZipInfo(m, (2020, 1, 1, 0, 0, 0)), b"")
        z.writestr(zipfile.ZipInfo(f"{dist.replace('-', '_')}-{version}.dist-info/METADATA", (2020, 1, 1, 0, 0, 0)), meta)
    return path


def _lock_set(folder: Path, backend_requires: tuple[str, ...] = ("psycopg[binary]>=3.2",)) -> list[Path]:
    """The wheels of a small lock set: our two, psycopg with its binary extra, pgserver (nobody requires it)."""
    return [
        _whl(folder, "desk-backend", "0.2.14",
             ["rub-fixed-income-core==0.42.0", *backend_requires, "fastapi>=0.115",
              'colorama>=0.4; sys_platform == "win32"', 'uvloop>=0.19; sys_platform != "win32"',
              'tomli>=2; python_version < "3.11"', 'pytest>=8; extra == "dev"'], ("desk/api/__init__.py",)),
        _whl(folder, "rub_fixed_income_core", "0.42.0", ["numpy>=1.26"], ("rub_fixed_income/__init__.py",)),
        _whl(folder, "Psycopg", "3.2.1", ['psycopg-binary==3.2.1; extra == "binary"',
                                          'typing-extensions>=4.4; python_version < "3.13"',
                                          'tzdata; sys_platform == "win32"'], ("psycopg/__init__.py",)),
        _whl(folder, "psycopg_binary", "3.2.1", [], ("psycopg_binary/__init__.py",)),
        _whl(folder, "typing_extensions", "4.12.2", []),
        _whl(folder, "tzdata", "2024.1", []),
        _whl(folder, "fastapi", "0.115.0", ["starlette>=0.37,<0.39", 'colorama; sys_platform == "linux"'], ()),
        _whl(folder, "starlette", "0.38.2", ["anyio>=3.4,<5"]),
        _whl(folder, "anyio", "4.4.0", ['colorama; sys_platform == "win32" and extra == "trio"']),
        _whl(folder, "colorama", "0.4.6", []),
        _whl(folder, "numpy", "1.26.4", []),
        _whl(folder, "pgserver", "0.1.4", [], ("pgserver/__init__.py",)),
    ]


LOCAL = ["desk-backend==0.2.14", "rub-fixed-income-core==0.42.0"]


def test_mirror_json_is_built_from_the_wheels_metadata_for_windows(tmp_path):
    wheels = {w.name.split("-")[0]: w for w in _lock_set(tmp_path / "w")}
    doc = build.mirror_doc(list(wheels.values()), LOCAL)
    assert doc["schema"] == 1 and doc["python"] == "3.11" and doc["platform"] == "win_amd64"
    assert doc["local"] == LOCAL and doc["imports"] == ["desk.api", "rub_fixed_income", "pgserver", "psycopg"]
    pk = doc["packages"]
    # every third-party package, none of ours; names canonical (PEP 503)
    assert sorted(pk) == ["anyio", "colorama", "fastapi", "numpy", "pgserver", "psycopg", "psycopg-binary",
                          "starlette", "typing-extensions", "tzdata"]
    assert pk["psycopg"] == {"lock": "3.2.1", "requires": [{"by": "desk-backend", "spec": ">=3.2"}],
                             "file": "Psycopg-3.2.1-py3-none-any.whl", "sha256": build.sha256(wheels["Psycopg"])}
    # markers are evaluated for Windows / Python 3.11: colorama and tzdata are needed, uvloop, tomli not; the "linux"
    # colorama of fastapi and the "trio" extra of anyio do not count
    assert pk["colorama"]["requires"] == [{"by": "desk-backend", "spec": ">=0.4"}]
    assert pk["tzdata"]["requires"] == [{"by": "psycopg", "spec": ""}]
    # python_version < "3.13" holds for 3.11; the extra of psycopg is asked by desk-backend, so psycopg-binary counts
    assert pk["typing-extensions"]["requires"] == [{"by": "psycopg", "spec": ">=4.4"}]
    assert pk["psycopg-binary"]["requires"] == [{"by": "psycopg", "spec": "==3.2.1"}]
    assert pk["starlette"]["requires"] == [{"by": "fastapi", "spec": "<0.39,>=0.37"}]  # the specifier as packaging prints it
    assert pk["numpy"]["requires"] == [{"by": "rub-fixed-income-core", "spec": ">=1.26"}]
    # added by the build; the installer may not replace it (another PostgreSQL major version), so it is exact
    assert pk["pgserver"] == {"lock": "0.1.4", "requires": [], "exact": True, "file": "pgserver-0.1.4-py3-none-any.whl",
                              "sha256": build.sha256(wheels["pgserver"])}
    # every third-party package names the file of the lock set and its sha256; ours are in "local" only
    for name, info in pk.items():
        assert info["sha256"] == build.sha256(next(w for w in wheels.values() if w.name == info["file"])), name
    # psycopg-binary is pinned by psycopg's own "==", which the installer's candidate filter already honours
    assert "exact" not in pk["psycopg-binary"] and "exact" not in pk["psycopg"]
    # deterministic: the same wheels in any order give the same bytes
    again = build.mirror_doc(list(reversed(_lock_set(tmp_path / "w2"))), list(reversed(LOCAL)))
    assert build.mirror_json_text(doc) == build.mirror_json_text(again)
    text = build.mirror_json_text(doc)
    assert text.endswith("\n") and json.loads(text) == doc and list(json.loads(text)) == sorted(doc)


def test_mirror_json_follows_extras_and_checks_what_the_lock_holds(tmp_path):
    # without the extra nobody asks for psycopg-binary: it is a package of the lock that nothing requires
    plain = build.mirror_doc(_lock_set(tmp_path / "a", ("psycopg>=3.2",)), LOCAL)
    assert plain["packages"]["psycopg-binary"]["requires"] == []
    assert plain["packages"]["psycopg-binary"]["exact"] is True  # nobody pins it: the build does
    # a requirement the wheels do not hold
    needs = _lock_set(tmp_path / "b", ("psycopg>=3.2", "missing-pkg>=1"))
    with pytest.raises(SystemExit, match="missing-pkg"):
        build.mirror_doc(needs, LOCAL)
    # the lock's own version does not satisfy a specifier
    with pytest.raises(SystemExit, match=r"psycopg>=3.5.*lock has 3.2.1"):
        build.mirror_doc(_lock_set(tmp_path / "c", ("psycopg>=3.5",)), LOCAL)
    # a module of the imports that no wheel holds
    wheels = [w for w in _lock_set(tmp_path / "d") if not w.name.startswith("pgserver")]
    wheels.append(_whl(tmp_path / "d", "pgserver", "0.1.4", [], ("pgserver_other/__init__.py",)))
    with pytest.raises(SystemExit, match="module pgserver"):
        build.mirror_doc(wheels, LOCAL)
    # the real names: the core's module is rub_fixed_income, not its distribution's name
    assert "rub_fixed_income" in build.MIRROR_IMPORTS and "rub_fixed_income_core" not in build.MIRROR_IMPORTS


def test_the_build_copies_the_installer_unconditionally_into_the_one_app_of_both_kinds():
    """main() builds the full stage (the mirror stage is cut from it): the copy of app/mirror_install.py is not
    conditional, and its source exists in the repository."""
    assert (build.REPO / build.MIRROR_INSTALLER).is_file()
    lines = Path(build.__file__).read_text(encoding="utf-8").splitlines()
    (at,) = [i for i, ln in enumerate(lines) if "app / \"mirror_install.py\"" in ln and "copyfile" in ln]
    assert lines[at].startswith("        shutil.copyfile(") and "if " not in lines[at - 1].split("#")[0]


MIRROR_STAGE_FILES = {"VERSION": b"1.1-b\n", "start.bat": b"@echo off\r\n", "app/deskctl.py": b"print()\n",
                      "app/mirror_install.py": b"installer\n", "python/python.exe": b"MZ"}


def _full_and_mirror_stage(tmp_path: Path, files: dict[str, bytes] | None = None) -> tuple[Path, Path, dict]:
    """A full stage with the lock set's wheels and its mirror stage, as ``main`` makes them."""
    stage = tmp_path / "full" / "DeskMonitor"
    for rel, data in (files or MIRROR_STAGE_FILES).items():
        (stage / rel).parent.mkdir(parents=True, exist_ok=True)
        (stage / rel).write_bytes(data)
    wheels = _lock_set(stage / "wheelhouse")
    pins = ["psycopg==3.2.1", "psycopg-binary==3.2.1", "typing-extensions==4.12.2", "tzdata==2024.1", "fastapi==0.115.0",
            "starlette==0.38.2", "anyio==4.4.0", "colorama==0.4.6", "numpy==1.26.4", "pgserver==0.1.4"]
    (stage / "wheelhouse/requirements.txt").write_text("\n".join(pins + LOCAL) + "\n")
    header = "# DeskMonitor 1.1-b, repository commit abc\n# no bundled Python (py -3.11)\n"
    doc = build.mirror_doc(wheels, LOCAL)
    mirror = build.make_mirror_stage(stage, tmp_path / "mirror" / "DeskMonitor", doc, header)
    build.write_manifest(stage, header)
    return stage, mirror, doc


def test_the_mirror_stage_holds_only_our_wheels_requirements_and_mirror_json(tmp_path):
    stage, mirror, doc = _full_and_mirror_stage(tmp_path)
    names = {rel for rel, _ in build.stage_files(mirror)}
    assert {rel for rel in names if rel.startswith("wheelhouse/")} == {
        "wheelhouse/desk_backend-0.2.14-py3-none-any.whl", "wheelhouse/rub_fixed_income_core-0.42.0-py3-none-any.whl",
        "wheelhouse/requirements.txt", "wheelhouse/mirror.json"}
    # all but the wheelhouse is the full stage's; app/mirror_install.py is in both kinds
    full_names = {rel for rel, _ in build.stage_files(stage)}
    assert "app/mirror_install.py" in names and "app/mirror_install.py" in full_names
    assert names - {"wheelhouse/mirror.json"} <= full_names and not [n for n in full_names if "mirror.json" in n]
    assert full_names - names == {rel for rel in full_names if rel.startswith("wheelhouse/") and rel.endswith(".whl")
                                  and "desk_backend" not in rel and "rub_fixed_income_core" not in rel}
    for rel in ("wheelhouse/requirements.txt", "wheelhouse/desk_backend-0.2.14-py3-none-any.whl",
                "wheelhouse/rub_fixed_income_core-0.42.0-py3-none-any.whl", "app/deskctl.py", "VERSION"):
        assert (mirror / rel).read_bytes() == (stage / rel).read_bytes()  # byte for byte the full bundle's
    assert (mirror / "wheelhouse/mirror.json").read_text(encoding="utf-8") == build.mirror_json_text(doc)
    # its own manifest (it lists mirror.json, not the third-party wheels), same header
    header, entries = build.parse_manifest((mirror / "MANIFEST.sha256").read_text())
    assert "wheelhouse/mirror.json" in entries and "wheelhouse/psycopg-3.2.1-py3-none-any.whl" not in entries
    assert header == build.parse_manifest((stage / "MANIFEST.sha256").read_text())[0]
    assert (build.base_from_zip(_zip_of(mirror, tmp_path)).flavor, build.base_from_zip(_zip_of(stage, tmp_path)).flavor) \
        == ("mirror", "full")
    # the zips are named DeskMonitorMirror-..., with and without the adapter
    stamp = build.zip_stamp(1_760_000_000)
    (public,) = build.write_bundles(mirror, "DeskMonitorMirror-1.1-b", False, tmp_path / "dist", stamp)
    assert public.name == "DeskMonitorMirror-1.1-b.zip"
    with update_module.open_zip(public) as z:
        c = update_module.read_contents(z)
    assert c.flavor == "mirror" and c.kind == "full"
    (mirror / "adapter/pkg").mkdir(parents=True)
    (mirror / "adapter/pkg/__init__.py").write_bytes(b"private")
    public2, private = build.write_bundles(mirror, "DeskMonitorMirror-1.1-b", True, tmp_path / "dist2", stamp)
    assert private.name == "DeskMonitorMirror-1.1-b-with-adapter.zip"
    assert public2.read_bytes() == public.read_bytes()  # the public zip has no adapter/


def _zip_of(stage: Path, tmp: Path) -> Path:
    out = tmp / f"{stage.parent.name}.zip"
    build.write_zip(out, build.bundle_entries(stage, adapter=False), build.zip_stamp(1_760_000_000))
    return out


def test_check_mirror_refuses_a_wheelhouse_that_does_not_match_requirements(tmp_path):
    _, mirror, _ = _full_and_mirror_stage(tmp_path)
    wh = mirror / "wheelhouse"
    build.check_mirror(wh)  # as written: fine
    req = (wh / "requirements.txt").read_text()
    (wh / "requirements.txt").write_text(req + "extra-pkg==1\n")
    with pytest.raises(SystemExit, match="different packages"):
        build.check_mirror(wh)
    (wh / "requirements.txt").write_text(req.replace("numpy==1.26.4", "numpy==1.26.5"))
    with pytest.raises(SystemExit, match="numpy lock"):
        build.check_mirror(wh)
    (wh / "requirements.txt").write_text(req)
    _whl(wh, "colorama", "0.4.6", [])  # a third-party wheel left behind
    with pytest.raises(SystemExit, match="only our wheels"):
        build.check_mirror(wh)
    (wh / "colorama-0.4.6-py3-none-any.whl").unlink()
    doc = json.loads((wh / "mirror.json").read_text())
    doc["packages"]["numpy"]["requires"].append({"by": "x", "spec": ">=2"})
    (wh / "mirror.json").write_text(json.dumps(doc))
    with pytest.raises(SystemExit, match="numpy>=2"):
        build.check_mirror(wh)


def test_mirror_json_hashes_are_those_of_the_full_stage_wheels_and_are_checked(tmp_path):
    stage, mirror, _ = _full_and_mirror_stage(tmp_path)
    full_wh, wh = stage / "wheelhouse", mirror / "wheelhouse"
    pk = json.loads((wh / "mirror.json").read_text(encoding="utf-8"))["packages"]
    third = sorted(w.name for w in full_wh.glob("*.whl") if w.name.split("-")[0] not in build.OUR_DISTS)
    assert sorted(p["file"] for p in pk.values()) == third and len(pk) == 10
    for info in pk.values():
        assert info["sha256"] == build.sha256(full_wh / info["file"])
    build.check_mirror(wh, full_wh)  # as written: fine
    original = (wh / "mirror.json").read_text(encoding="utf-8")

    def change(mutate):
        d = json.loads(original)
        mutate(d["packages"])
        (wh / "mirror.json").write_text(json.dumps(d), encoding="utf-8")

    change(lambda p: p["numpy"].update(sha256="0" * 64))
    with pytest.raises(SystemExit, match=r"numpy-1.26.4.*sha256"):
        build.check_mirror(wh, full_wh)
    build.check_mirror(wh)  # the format alone is fine without the full wheelhouse
    change(lambda p: p["numpy"].update(file="numpy-1.26.5-py3-none-any.whl"))
    with pytest.raises(SystemExit, match="not the wheel of numpy 1.26.4"):
        build.check_mirror(wh, full_wh)
    change(lambda p: p["numpy"].update(file="colorama-0.4.6-py3-none-any.whl"))
    with pytest.raises(SystemExit, match="not the wheel of numpy"):
        build.check_mirror(wh, full_wh)
    change(lambda p: p["numpy"].pop("sha256"))
    with pytest.raises(SystemExit, match="numpy has no file / sha256"):
        build.check_mirror(wh)
    change(lambda p: p["numpy"].update(sha256="ABC"))
    with pytest.raises(SystemExit, match="numpy has no file / sha256"):
        build.check_mirror(wh)
    change(lambda p: p["numpy"].update(file="not-a-wheel.txt"))
    with pytest.raises(SystemExit, match="is not a wheel name"):
        build.check_mirror(wh)
    change(lambda p: p.pop("numpy"))  # a package of the lock without a record at all
    with pytest.raises(SystemExit, match="different packages"):
        build.check_mirror(wh, full_wh)
    (wh / "mirror.json").write_text(original, encoding="utf-8")
    _whl(full_wh, "extra-pkg", "1.0", [])  # a third-party wheel of the full stage that mirror.json does not list
    with pytest.raises(SystemExit, match="extra_pkg"):
        build.check_mirror(wh, full_wh)


def test_make_mirror_stage_refuses_a_document_whose_hashes_are_not_the_stage_wheels(tmp_path):
    stage = tmp_path / "full" / "DeskMonitor"
    wheels = _lock_set(stage / "wheelhouse")
    pins = ["psycopg==3.2.1", "psycopg-binary==3.2.1", "typing-extensions==4.12.2", "tzdata==2024.1", "fastapi==0.115.0",
            "starlette==0.38.2", "anyio==4.4.0", "colorama==0.4.6", "numpy==1.26.4", "pgserver==0.1.4"]
    (stage / "wheelhouse/requirements.txt").write_text("\n".join(pins + LOCAL) + "\n")
    doc = build.mirror_doc(wheels, LOCAL)
    (stage / "wheelhouse/numpy-1.26.4-py3-none-any.whl").write_bytes(b"another file")  # changed after the document
    with pytest.raises(SystemExit, match=r"numpy-1.26.4.*sha256"):
        build.make_mirror_stage(stage, tmp_path / "mirror" / "DeskMonitor", doc, "# h\n")


def test_patches_of_the_mirror_kind_come_from_the_mirror_stage(tmp_path):
    stamp = build.zip_stamp(1_760_000_000)
    old_files = {**MIRROR_STAGE_FILES, "VERSION": b"1.0-a\n"}
    old_stage, old_mirror, _ = _full_and_mirror_stage(tmp_path / "old", old_files)
    new_stage, new_mirror, _ = _full_and_mirror_stage(tmp_path / "new", {**MIRROR_STAGE_FILES,
                                                                         "app/deskctl.py": b"print(2)\n"})
    full_base, mirror_base = _zip_of(old_stage, tmp_path), _zip_of(old_mirror, tmp_path)
    bases = [build.base_from_zip(full_base), build.base_from_zip(mirror_base)]
    assert [b.flavor for b in bases] == ["full", "mirror"]
    for d in ("o", "o2", "o3"):
        (tmp_path / d).mkdir()
    both = build.write_patches(bases, {"full": new_stage, "mirror": new_mirror}, "1.1-b", tmp_path / "o", stamp)
    assert sorted(p.name for p in both) == ["DeskMonitor-patch-1.0-a-to-1.1-b.zip",
                                            "DeskMonitorMirror-patch-1.0-a-to-1.1-b.zip"]
    by_name = {p.name: p for p in both}
    with zipfile.ZipFile(by_name["DeskMonitorMirror-patch-1.0-a-to-1.1-b.zip"]) as z:
        names = {n.removeprefix("DeskMonitor/") for n in z.namelist()}
        manifest = z.read("DeskMonitor/MANIFEST.sha256").decode()
        readme = z.read("DeskMonitor/PATCH_README.txt").decode("utf-8-sig")
    assert "app/deskctl.py" in names and not [n for n in names if n.endswith(".whl")]  # same wheels as the base
    assert "wheelhouse/mirror.json" in manifest and "DeskMonitorMirror-1.1-b.zip" in readme and "зеркала" in readme
    with zipfile.ZipFile(by_name["DeskMonitor-patch-1.0-a-to-1.1-b.zip"]) as z:
        assert "mirror.json" not in z.read("DeskMonitor/MANIFEST.sha256").decode()
        assert "DeskMonitor-1.1-b.zip" in z.read("DeskMonitor/PATCH_README.txt").decode("utf-8-sig")
        assert "без интернета" in z.read("DeskMonitor/PATCH_README.txt").decode("utf-8-sig")
    # only the full kind is built (no --mirror): a mirror base gives no patch, a full one does
    only_full = build.write_patches(bases, new_stage, "1.1-b", tmp_path / "o2", stamp)
    assert [p.name for p in only_full] == ["DeskMonitor-patch-1.0-a-to-1.1-b.zip"]
    # a patch is of one kind: mixed bases, or a base of the other kind than the stage, are refused
    with pytest.raises(ValueError, match="both"):
        build.write_patch(bases, new_stage, "1.1-b", tmp_path / "o3", stamp)
    with pytest.raises(ValueError, match="other one"):
        build.write_patch([bases[1]], new_stage, "1.1-b", tmp_path / "o3", stamp)
    # delivered records are of a kind too, and a zip is taken with the records of its own kind only
    folder = tmp_path / "delivered"
    build.record_delivered(full_base, folder)
    rec_m = build.record_delivered(mirror_base, folder)
    assert build.base_from_record(rec_m).flavor == "mirror"
    assert [b.flavor for b in build.load_bases([str(mirror_base)], folder)] == ["mirror"]
    assert sorted(b.flavor for b in build.load_bases(["1.0-a"], folder)) == ["full", "mirror"]


def test_from_bundle_of_a_mirror_zip_makes_the_mirror_with_adapter_zip(tmp_path):
    import argparse

    _, mirror, _ = _full_and_mirror_stage(tmp_path)
    public = tmp_path / "DeskMonitorMirror-1.1-b.zip"
    build.write_zip(public, build.bundle_entries(mirror, adapter=False), build.zip_stamp(1_760_000_000))
    a = argparse.Namespace(from_bundle=public, out=tmp_path / "out")
    assert build.from_bundle(a, {"pkg/__init__.py": b"private"}, []) == 0
    private = tmp_path / "out" / "DeskMonitorMirror-1.1-b-with-adapter.zip"
    assert private.is_file() and not (tmp_path / "out" / "DeskMonitor-1.1-b-with-adapter.zip").exists()
    build.check_public_private(tmp_path / "out" / "DeskMonitorMirror-1.1-b.zip", private)
    with zipfile.ZipFile(private) as z:
        assert z.read("DeskMonitor/adapter/pkg/__init__.py") == b"private"
        assert b"wheelhouse/mirror.json" in z.read("DeskMonitor/MANIFEST.sha256")


def test_release_workflow_builds_and_publishes_the_mirror_bundle_only():
    wf = (REPO / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "--mirror-only" in wf and "DeskMonitorMirror-patch-*.zip" in wf
    assert "dist/DeskMonitorMirror-*.zip" in wf  # the glob that uploads the mirror zip and its patches
    # the full bundle and its patches are neither downloaded as bases nor uploaded
    assert "dist/DeskMonitor-*.zip" not in wf and "prev/DeskMonitor-*.zip" not in wf and "'DeskMonitor-*.zip'" not in wf
    # the tag logic of #539 is untouched
    assert "MAKE_TAG" in wf and "--target" in wf and "--verify-tag" in wf and "Release tag" in wf


def test_mirror_only_takes_the_newest_delivered_version_of_the_mirror_kind(tmp_path):
    old_files = {**MIRROR_STAGE_FILES, "VERSION": b"1.0-a\n"}
    old_stage, old_mirror, _ = _full_and_mirror_stage(tmp_path / "old", old_files)
    full_base, mirror_base = _zip_of(old_stage, tmp_path), _zip_of(old_mirror, tmp_path)
    folder = tmp_path / "delivered"
    build.record_delivered(full_base, folder)
    assert build.newest_delivered(folder) == "1.0-a" and build.newest_delivered(folder, "mirror") is None
    build.record_delivered(mirror_base, folder)
    assert build.newest_delivered(folder, "mirror") == "1.0-a"


def test_a_broken_delivered_record_is_skipped_with_a_warning_not_a_release_failure(tmp_path, capsys):
    _, old_mirror, _ = _full_and_mirror_stage(tmp_path / "old", {**MIRROR_STAGE_FILES, "VERSION": b"1.0-a\n"})
    folder = tmp_path / "delivered"
    build.record_delivered(_zip_of(old_mirror, tmp_path), folder)
    (folder / "2.0-b").mkdir()
    (folder / "2.0-b" / "broken.sha256").write_text("not a manifest\n", encoding="utf-8")  # newer, but unreadable
    assert build.newest_delivered(folder, "mirror") == "1.0-a"  # no SystemExit; the readable record wins
    err = capsys.readouterr().err
    assert "warning: delivered record" in err and "broken.sha256" in err


def test_load_bases_skips_a_broken_record_beside_a_good_one(tmp_path, capsys):
    _, old_mirror, _ = _full_and_mirror_stage(tmp_path / "old", {**MIRROR_STAGE_FILES, "VERSION": b"1.0-a\n"})
    folder = tmp_path / "delivered"
    good = build.record_delivered(_zip_of(old_mirror, tmp_path), folder)
    (good.parent / "aaa-broken.sha256").write_text("not a manifest\n", encoding="utf-8")  # same version folder
    bases = build.load_bases(["1.0-a"], folder)
    assert [b.version for b in bases] == ["1.0-a"] and bases[0].build == build.base_from_record(good).build
    assert "warning: delivered record" in capsys.readouterr().err
