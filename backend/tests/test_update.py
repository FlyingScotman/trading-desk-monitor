"""Hot update of the Windows bundle: update zips as backend/tools/build_windows_bundle.py writes them, checked and
staged by desk.update, the routes behind the settings page, and the launcher (backend/windows/deskctl.py) applying,
confirming and rolling back an update; end to end with the real launcher and the real API on a fake bundle."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import update
from desk.api import create_app
from desk.settings import load

REPO = Path(__file__).resolve().parents[2]
OLD, NEW, BAD = "0.1.0-20261019-aaaaaaa", "0.1.0-20261020-bbbbbbb", "0.1.0-20261021-ccccccc"
STAMP = (2026, 10, 20, 0, 0, 0)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load("build_windows_bundle_u", REPO / "backend/tools/build_windows_bundle.py")
deskctl = _load("deskctl_u", REPO / "backend/windows/deskctl.py")


def bundle(version: str, changes: dict[str, bytes | None] | None = None) -> dict[str, bytes]:
    """A small bundle: the real launcher, a page, a wheel, a runtime file; ``changes`` by path (None drops one)."""
    files = {"VERSION": f"{version}\n".encode(), "start.bat": b"@echo off\r\n", "INSTALL.txt": b"install",
             "app/deskctl.py": (REPO / "backend/windows/deskctl.py").read_bytes(),
             "app/prototypes/settings.html": b"<p>1</p>", "app/prototypes/old.js": b"old",
             "wheelhouse/requirements.txt": b"desk-backend==0.1.0\n",
             "wheelhouse/desk_backend-0.1.0-py3-none-any.whl": b"wheel 1", "python/python.exe": b"py"}
    for rel, data in (changes or {}).items():
        if data is None:
            files.pop(rel, None)
        else:
            files[rel] = data
    return files


def stage_dir(path: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(data)
    build.write_manifest(path, f"# DeskMonitor {files['VERSION'].decode().strip()}\n")
    return path


def full_zip(stage: Path, out: Path) -> Path:
    build.write_zip(out, ((p.relative_to(stage).as_posix(), p.read_bytes())
                          for p in sorted(stage.rglob("*")) if p.is_file()), STAMP)
    return out


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.relative_to(root).parts[0] != "env"}


def raw_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


@pytest.fixture
def old_new(tmp_path: Path):
    """(the installed old bundle, the new bundle's stage, the old bundle's full zip): the new one changes a page,
    adds one, drops old.js."""
    old = stage_dir(tmp_path / "old", bundle(OLD))
    new = stage_dir(tmp_path / "new", bundle(NEW, {"app/prototypes/settings.html": b"<p>2</p>",
                                                   "app/prototypes/new.js": b"new", "app/prototypes/old.js": None}))
    root = tmp_path / "DeskMonitor"
    shutil.copytree(old, root)
    return root, new, full_zip(old, tmp_path / f"DeskMonitor-{OLD}.zip")


# ---- desk.update: zips -----------------------------------------------------------------------------------------


def test_version_order():
    assert update.compare(NEW, OLD) == "newer" and update.compare(OLD, NEW) == "older"
    assert update.compare(OLD, OLD) == "same" and update.compare("0.1.0-20261019-bbbbbbb", OLD) == "other"
    assert update.compare(OLD + "-dirty202610200705", OLD) == "newer" and update.compare("x", OLD) == "other"
    assert update.compare("0.2.0-20261001-aaaaaaa", OLD) == "newer"


def test_full_zip_stages_only_what_changed(old_new, tmp_path):
    root, new, _ = old_new
    z = full_zip(new, tmp_path / f"DeskMonitor-{NEW}.zip")
    item = update.describe(z, root, OLD)
    assert item["version"] == NEW and item["kind"] == "full" and item["compare"] == "newer" and item["error"] is None
    doc = update.stage(z, root, tmp_path / "home")
    assert sorted(doc["files"]) == ["MANIFEST.sha256", "VERSION", "app/prototypes/new.js", "app/prototypes/settings.html"]
    assert doc["delete"] == ["app/prototypes/old.js"] and doc["from_version"] == OLD and doc["kind"] == "full"
    staged = Path(doc["staged"])
    assert staged == tmp_path / "home" / "updates" / "staged" / NEW
    assert (staged / "files/app/prototypes/new.js").read_bytes() == b"new"
    assert json.loads((staged / "update.json").read_text())["files"] == doc["files"]
    assert tree(root) == tree(tmp_path / "old")  # the server never writes into the bundle


def test_patch_zip_of_the_builder_stages_and_checks_its_base(old_new, tmp_path):
    root, new, old_zip = old_new
    patch = build.write_patch(old_zip, new, NEW, tmp_path, STAMP)
    item = update.describe(patch, root, OLD)
    assert item["kind"] == "patch" and item["base_version"] == OLD and item["error"] is None
    doc = update.stage(patch, root, tmp_path / "home")
    assert sorted(doc["files"]) == ["MANIFEST.sha256", "VERSION", "app/prototypes/new.js", "app/prototypes/settings.html"]
    assert doc["delete"] == ["app/prototypes/old.js"]
    (root / "wheelhouse/desk_backend-0.1.0-py3-none-any.whl").write_bytes(b"other")  # another installed build
    (root / "MANIFEST.sha256").write_text(build.sha256(root / "wheelhouse/desk_backend-0.1.0-py3-none-any.whl")
                                          + "  wheelhouse/desk_backend-0.1.0-py3-none-any.whl\n")
    with pytest.raises(update.UpdateError, match=f"для версии {OLD}"):
        update.stage(patch, root, tmp_path / "home")


def test_python_changes_and_the_same_version_are_refused(old_new, tmp_path):
    root, _, old_zip = old_new
    py = stage_dir(tmp_path / "py", bundle(NEW, {"python/python.exe": b"py 2"}))
    assert "Python" in update.describe(full_zip(py, tmp_path / "py.zip"), root, OLD)["error"]
    patch = build.write_patch(old_zip, py, NEW, tmp_path, STAMP)
    assert "Python" in update.describe(patch, root, OLD)["error"]
    assert "уже установлена" in update.describe(old_zip, root, OLD)["error"]
    nopy = stage_dir(tmp_path / "nopy", bundle(NEW, {"python/python.exe": None}))  # --no-python: keeps python/
    assert update.describe(full_zip(nopy, tmp_path / "nopy.zip"), root, OLD)["error"] is None


def test_adapter_is_kept_unless_the_full_zip_brings_one(old_new, tmp_path):
    root, _, _ = old_new
    (root / "adapter/pkg").mkdir(parents=True)
    (root / "adapter/pkg/__init__.py").write_text("old")
    (root / "adapter/pkg/gone.py").write_text("old")
    plain = full_zip(stage_dir(tmp_path / "a", bundle(NEW)), tmp_path / "a.zip")
    assert not [r for r in update.stage(plain, root, tmp_path / "h")["delete"] if r.startswith("adapter/")]
    withad = full_zip(stage_dir(tmp_path / "b", bundle(NEW, {"adapter/pkg/__init__.py": b"new"})), tmp_path / "b.zip")
    doc = update.stage(withad, root, tmp_path / "h")
    assert doc["delete"] == ["adapter/pkg/gone.py"] and "adapter/pkg/__init__.py" in doc["files"]


GOOD = {"DeskMonitor/VERSION": b"1.0\n", "DeskMonitor/app/deskctl.py": b"", "DeskMonitor/start.bat": b"",
        "DeskMonitor/wheelhouse/requirements.txt": b""}


def manifest_of(files: dict[str, bytes]) -> bytes:
    import hashlib
    return "".join(f"{hashlib.sha256(d).hexdigest()}  {n.removeprefix('DeskMonitor/')}\n"
                   for n, d in files.items()).encode()


@pytest.mark.parametrize("extra, match", [
    ({"DeskMonitor/../evil.txt": b"x"}, "Недопустимый путь"),
    ({"/etc/evil": b"x"}, "Недопустимый путь"),
    ({"DeskMonitor\\app\\x.py": b"x"}, "Недопустимый путь"),
    ({"C:/evil.txt": b"x"}, "Недопустимый путь"),
    ({"DeskMonitor/app/con.txt": b"x"}, "Недопустимый путь"),
    ({"DeskMonitor/app/x. ": b"x"}, "Недопустимый путь"),
    ({"Other/VERSION": b"x"}, "не архив Desk Monitor"),
    ({"DeskMonitor/env/Scripts/desk.exe": b"x"}, "Лишний файл"),
    ({"DeskMonitor/evil.exe": b"x"}, "Лишний файл"),
    ({"DeskMonitor/app/unlisted.js": b"x"}, "нет в MANIFEST"),
])
def test_unsafe_or_foreign_zips_are_refused(tmp_path, extra, match):
    files = {**GOOD, "DeskMonitor/MANIFEST.sha256": manifest_of(GOOD), **extra}
    item = update.describe(raw_zip(tmp_path / "u.zip", files), None, OLD)
    assert match in item["error"]


def test_zips_missing_their_version_manifest_or_files_are_refused(tmp_path):
    names = iter(range(100))

    def err(files):
        return update.describe(raw_zip(tmp_path / f"z{next(names)}.zip", files), None, OLD)["error"]

    nover = {k: v for k, v in GOOD.items() if k != "DeskMonitor/VERSION"}
    assert "нет файла VERSION" in err({**nover, "DeskMonitor/MANIFEST.sha256": manifest_of(nover)})
    assert "нет MANIFEST.sha256" in err(GOOD)
    incomplete = {k: v for k, v in GOOD.items() if k != "DeskMonitor/start.bat"}
    assert "неполный" in err({**incomplete, "DeskMonitor/MANIFEST.sha256": manifest_of(GOOD)})
    noapp = {k: v for k, v in GOOD.items() if "deskctl" not in k}
    assert "нет app/deskctl.py" in err({**noapp, "DeskMonitor/MANIFEST.sha256": manifest_of(noapp)})
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    assert "не zip" in update.describe(tmp_path / "junk.zip", None, OLD)["error"]
    bad_delete = {**GOOD, "DeskMonitor/MANIFEST.sha256": manifest_of(GOOD), "DeskMonitor/PATCH_README.txt": b"",
                  "DeskMonitor/PATCH_DELETE.txt": b"app/../../evil\n"}
    assert "PATCH_DELETE" in err(bad_delete)
    link = tmp_path / "link.zip"
    with zipfile.ZipFile(link, "w") as z:
        for n, d in {**GOOD, "DeskMonitor/MANIFEST.sha256": manifest_of(GOOD)}.items():
            z.writestr(n, d)
        info = zipfile.ZipInfo("DeskMonitor/app/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    assert "Ссылка" in update.describe(link, None, OLD)["error"]


def test_size_limits(tmp_path, monkeypatch):
    z = raw_zip(tmp_path / "big.zip", {**GOOD, "DeskMonitor/MANIFEST.sha256": manifest_of(GOOD),
                                       "DeskMonitor/app/x.bin": bytes(10_000)})
    monkeypatch.setattr(update, "MAX_UNPACKED_BYTES", 5_000)
    assert "Распакованный архив больше" in update.describe(z, None, OLD)["error"]
    monkeypatch.setattr(update, "MAX_ZIP_BYTES", 100)
    assert "Архив больше" in update.describe(z, None, "other")["error"]
    monkeypatch.setattr(update, "MAX_FILES", 3)
    monkeypatch.setattr(update, "MAX_ZIP_BYTES", 1 << 30)
    assert "больше 3 файлов" in update.describe(z, None, "third")["error"]


def test_a_member_that_does_not_match_the_manifest_is_caught_while_staging(old_new, tmp_path):
    root, new, _ = old_new
    (new / "app/prototypes/settings.html").write_bytes(b"<p>tampered</p>")  # after the manifest was written
    z = full_zip(new, tmp_path / "t.zip")
    with pytest.raises(update.UpdateError, match="повреждён"):
        update.stage(z, root, tmp_path / "home")
    assert not (tmp_path / "home/updates/staged" / NEW).exists()


# ---- the launcher: apply, confirm, roll back --------------------------------------------------------------------


@pytest.fixture
def launcher(old_new, tmp_path, monkeypatch):
    """deskctl working on the installed old bundle; returns (env, root, the new stage)."""
    root, new, _ = old_new
    for name, value in (("ROOT", root), ("APP", root / "app"), ("WHEELHOUSE", root / "wheelhouse"),
                        ("ADAPTER", root / "adapter"), ("ENV", root / "env"), ("SCRIPTS", root / "env/bin"),
                        ("MARKER", root / "env/desk-bundle.json")):
        monkeypatch.setattr(deskctl, name, value)
    monkeypatch.setattr(deskctl, "say", lambda msg="": None)
    env = {"DESK_HOME": str(tmp_path / "home")}
    (tmp_path / "home/logs").mkdir(parents=True)
    return env, root, new


def test_apply_backs_up_and_rolls_back(launcher, tmp_path):
    env, root, new = launcher
    old_tree = tree(root)
    doc = update.stage(full_zip(new, tmp_path / "n.zip"), root, Path(env["DESK_HOME"]))
    deskctl.apply_update(env, Path(doc["staged"]))
    assert tree(root) == tree(new) and deskctl.version() == NEW
    upd = Path(env["DESK_HOME"]) / "updates"
    applying = json.loads((upd / "applying.json").read_text())
    assert applying["version"] == NEW and applying["from_version"] == OLD
    backup = json.loads((upd / "backup" / OLD / "backup.json").read_text())
    assert backup["created"] == ["app/prototypes/new.js"] and "app/prototypes/old.js" in backup["replaced"]
    assert deskctl.rollback_update(env)
    assert tree(root) == old_tree and not (upd / "applying.json").exists() and not (root / "app/prototypes/new.js").exists()
    assert not deskctl.rollback_update(env)  # nothing more to restore

    doc = update.stage(full_zip(new, tmp_path / "n.zip"), root, Path(env["DESK_HOME"]))
    deskctl.apply_update(env, Path(doc["staged"]))
    deskctl.confirm_update(env)
    last = json.loads((upd / "last.json").read_text())
    assert last["ok"] and last["version"] == NEW and last["from_version"] == OLD
    assert not (upd / "applying.json").exists() and not Path(doc["staged"]).exists() and tree(root) == tree(new)


def test_apply_refuses_unsafe_or_damaged_staging(launcher, tmp_path):
    env, root, _ = launcher
    old_tree = tree(root)
    staged = tmp_path / "staged"
    (staged / "files/python").mkdir(parents=True)
    (staged / "files/python/python.exe").write_bytes(b"x")
    for files, delete, match in (({"python/python.exe": build.sha256(staged / "files/python/python.exe")}, [], "пути"),
                                 ({}, ["../outside.txt"], "пути"),
                                 ({"app/x.js": "0" * 64}, [], "повреждён")):
        (staged / "update.json").write_text(json.dumps({"version": NEW, "from_version": OLD, "files": files,
                                                        "delete": delete}))
        with pytest.raises(deskctl.Fail, match=match):
            deskctl.apply_update(env, staged)
    (staged / "update.json").write_text(json.dumps({"version": NEW, "from_version": "0.0.1", "files": {}, "delete": []}))
    with pytest.raises(deskctl.Fail, match="для версии 0.0.1"):
        deskctl.apply_update(env, staged)
    assert tree(root) == old_tree and not (Path(env["DESK_HOME"]) / "updates/applying.json").exists()
    assert deskctl.safe_rel("app/x.js") and not deskctl.safe_rel("env/x") and not deskctl.safe_rel("a\\b")


def test_hot_update_rolls_back_when_the_new_version_does_not_confirm(launcher, tmp_path, monkeypatch):
    env, root, new = launcher
    old_tree = tree(root)
    home = Path(env["DESK_HOME"])
    update.request(update.stage(full_zip(new, tmp_path / "n.zip"), root, home), home)
    monkeypatch.setattr(deskctl, "bundle_processes", lambda: ([], []))
    calls = []

    def child(cmd, child_env):  # the new launcher: installs, but its server fails
        calls.append(cmd)
        assert deskctl.version() == NEW and "--verify-update" in cmd and child_env["DESK_LAUNCHER_PIDS"]
        deskctl.record_update(env, False, NEW, "", "сервер остановился с ошибкой, код 1")
        return 1

    monkeypatch.setattr(deskctl, "run_child", child)
    assert deskctl.hot_update(env, ["--port", "8123"]) is None
    assert calls and calls[0][-2:] == ["--port", "8123"]
    assert tree(root) == old_tree and not (home / "updates/pending.json").exists()
    last = json.loads((home / "updates/last.json").read_text())
    assert not last["ok"] and last["version"] == NEW and "код 1" in last["message"] and OLD in last["message"]
    assert deskctl.hot_update(env, []) is None  # a plain restart: nothing pending


def test_changed_wheels_and_the_quick_venv_update(launcher, monkeypatch):
    env, root, _ = launcher
    deskctl.SCRIPTS.mkdir(parents=True)
    deskctl.desk_exe().write_text("")
    deskctl.MARKER.write_text(json.dumps(deskctl.env_marker()))
    have = deskctl.env_marker()
    assert deskctl.changed_wheels(have, have) == []
    assert deskctl.changed_wheels({"version": OLD, "root": str(root)}, have) is None  # a marker of an older launcher
    assert deskctl.changed_wheels({**have, "requirements": "x"}, have) is None
    (root / "VERSION").write_text(NEW + "\n")
    (root / "wheelhouse/desk_backend-0.1.0-py3-none-any.whl").write_bytes(b"wheel 2")
    calls = []
    monkeypatch.setattr(deskctl.subprocess, "run", lambda cmd, **k: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(deskctl, "bundle_processes", lambda: ([], []))
    monkeypatch.setattr(deskctl, "stop_store", lambda env: None)
    deskctl.ensure_env(env)
    pip = [c for c in calls if "pip" in c]
    assert len(pip) == 1 and "--no-deps" in pip[0] and pip[0][-1].endswith("desk_backend-0.1.0-py3-none-any.whl")
    assert calls[-1][-1] == "db-migrate" and json.loads(deskctl.MARKER.read_text())["version"] == NEW
    calls.clear()
    deskctl.ensure_env(env)
    assert calls == []  # current now
    (root / "VERSION").write_text(BAD + "\n")
    (root / "wheelhouse/requirements.txt").write_text("desk-backend==0.1.0\nnew-dep==1\n")
    with pytest.raises(deskctl.Fail, match="окружение Python"):  # made anew: python -m venv (stubbed, so no venv)
        deskctl.ensure_env(env)
    assert any("venv" in c for c in calls)


# ---- routes ------------------------------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    return h


def client_for(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def test_routes_in_a_checkout_list_and_check_but_do_not_install(home, old_new, tmp_path):
    _, new, _ = old_new
    c = client_for(create_app("postgresql://unused"))
    j = c.get("/api/v1/update").json()
    assert not j["can_install"] and "разработки" in j["unavailable"] and j["items"] == [] and j["last"] is None
    z = full_zip(new, tmp_path / f"DeskMonitor-{NEW}.zip").read_bytes()
    r = c.post("/api/v1/update/upload", params={"name": f"C:\\Загрузки\\DeskMonitor-{NEW}.zip"}, content=z,
               headers={"Content-Type": "application/zip"})
    assert r.status_code == 200 and r.json()["version"] == NEW and r.json()["name"] == f"DeskMonitor-{NEW}.zip"
    assert (home / "updates/uploads" / f"DeskMonitor-{NEW}.zip").is_file()
    body = {"source": "upload", "name": f"DeskMonitor-{NEW}.zip"}
    assert c.post("/api/v1/update/check", json=body).json()["error"] is None
    r = c.post("/api/v1/update/install", json=body)
    assert r.status_code == 409 and "разработки" in r.json()["message"]


def test_routes_upload_check_and_install_under_the_launcher(home, old_new, tmp_path, monkeypatch):
    root, new, old_zip = old_new
    monkeypatch.setenv("DESK_BUNDLE_ROOT", str(root))
    monkeypatch.setenv("DESK_BUNDLE_VERSION", OLD)
    app = create_app("postgresql://unused")
    c = client_for(app)
    assert "start.bat" in c.get("/api/v1/update").json()["unavailable"]  # no restart code: not under start.bat
    monkeypatch.setenv("DESK_RESTART_CODE", "75")
    asked = []
    app.state.request_restart = lambda: asked.append(True)
    z = full_zip(new, tmp_path / "z.zip").read_bytes()
    assert c.post("/api/v1/update/upload", params={"name": "a.zip"}, content=z,
                  headers={"Content-Type": "application/json"}).status_code == 415
    assert c.post("/api/v1/update/upload", params={"name": "a.zip"}, content=z, headers={"Content-Type": "application/zip",
                  "Origin": "http://evil.example"}).status_code == 403
    r = c.post("/api/v1/update/upload", params={"name": "junk.zip"}, content=b"PK not really",
               headers={"Content-Type": "application/zip"})
    assert r.status_code == 422 and not (home / "updates/uploads/junk.zip").exists()
    assert c.post("/api/v1/update/upload", params={"name": "notes.txt"}, content=z,
                  headers={"Content-Type": "application/zip"}).status_code == 422
    shared = tmp_path / "share"
    shared.mkdir()
    shutil.copy(old_zip, shared / old_zip.name)
    (shared / f"DeskMonitor-{NEW}.zip").write_bytes(z)
    assert c.put("/api/v1/settings", json={"update_dir": str(shared)}).status_code == 200
    assert load().update_dir == shared
    j = c.get("/api/v1/update").json()
    assert j["can_install"] and j["current_version"] == OLD and j["update_dir"] == str(shared)
    items = {i["name"]: i for i in j["items"]}
    assert items[f"DeskMonitor-{NEW}.zip"]["compare"] == "newer" and items[f"DeskMonitor-{NEW}.zip"]["error"] is None
    assert items[old_zip.name]["compare"] == "same" and items[old_zip.name]["source"] == "dir"
    assert c.post("/api/v1/update/install", json={"source": "dir", "name": "../x.zip"}).status_code == 422
    assert c.post("/api/v1/update/install", json={"source": "dir", "name": old_zip.name}).status_code == 422
    r = c.post("/api/v1/update/install", json={"source": "dir", "name": f"DeskMonitor-{NEW}.zip"})
    assert r.status_code == 202 and r.json()["version"] == NEW and r.json()["delete"] == 1 and asked == [True]
    pending = c.get("/api/v1/update").json()["pending"]
    assert pending["version"] == NEW and pending["from_version"] == OLD and Path(pending["staged"]).is_dir()
    r = c.post("/api/v1/update/install", json={"source": "dir", "name": f"DeskMonitor-{NEW}.zip"})
    assert r.status_code == 409 and asked == [True]
    (shared / "broken.zip").write_bytes(b"x")
    assert {i["name"]: i for i in c.get("/api/v1/update").json()["items"]}["broken.zip"]["error"]
    assert c.put("/api/v1/settings", json={"update_dir": str(tmp_path / "missing")}).status_code == 200
    assert "нет" in c.get("/api/v1/update").json()["update_dir_error"]


# ---- end to end: the real launcher and API on a fake bundle -------------------------------------------------------

STUB = """#!{python}
import os, sys
from pathlib import Path
if sys.argv[1] == "db-migrate":
    sys.exit(0)
if (Path(os.environ["DESK_BUNDLE_ROOT"]) / "app" / "FAIL").exists():
    sys.exit("this build is broken")
from desk.api.app import serve
sys.exit(serve("postgresql://unused", port=int(sys.argv[sys.argv.index("--port") + 1])))
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(os.name == "nt", reason="the launcher's process checks are tested on Linux")
def test_hot_update_end_to_end(tmp_path):
    """start.bat's launcher on a fake bundle whose venv runs the real API without a store: a patch uploaded and
    installed from the routes is applied and the new version answers; a broken full build is rolled back."""
    old = stage_dir(tmp_path / "old", bundle(OLD))
    new = stage_dir(tmp_path / "new", bundle(NEW, {"app/prototypes/settings.html": b"<p>2</p>",
                                                   "app/prototypes/old.js": None}))
    bad = stage_dir(tmp_path / "bad", bundle(BAD, {"app/FAIL": b"", "app/prototypes/old.js": None}))
    patch = build.write_patch(full_zip(old, tmp_path / "old.zip"), new, NEW, tmp_path, STAMP)
    broken = full_zip(bad, tmp_path / f"DeskMonitor-{BAD}.zip")
    root = tmp_path / "DeskMonitor"
    shutil.copytree(old, root)
    scripts = root / "env" / "bin"
    scripts.mkdir(parents=True)
    (scripts / "desk").write_text(STUB.format(python=sys.executable))
    (scripts / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    for f in scripts.iterdir():
        f.chmod(0o755)
    wh = root / "wheelhouse"
    (root / "env/desk-bundle.json").write_text(json.dumps({
        "version": OLD, "root": str(root.resolve()), "python": f"{sys.executable} {sys.version}",
        "requirements": build.sha256(wh / "requirements.txt"),
        "wheels": {p.name: build.sha256(p) for p in sorted(wh.glob("*.whl"))}}))
    home, port = tmp_path / "home", _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("DESK_")}
    env.update(DESK_HOME=str(home), DESK_ENV_FILE=str(tmp_path / "none.env"), DESK_PORT=str(port), BROWSER="true")
    with open(tmp_path / "launcher.log", "wb") as log:  # the child keeps its own handle
        proc = subprocess.Popen([sys.executable, "-I", str(root / "app/deskctl.py"), "start"], env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"

    def call(method, path, data=None, ctype="application/json"):
        req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": ctype})
        with opener.open(req, timeout=30) as r:
            return r.status, json.loads(r.read())

    def until(cond, what, timeout=90.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                j = call("GET", "/api/v1/update")[1]
                if cond(j):
                    return j
            except (OSError, ValueError):
                pass
            assert proc.poll() is None, (tmp_path / "launcher.log").read_text(errors="replace")
            time.sleep(0.5)
        raise AssertionError(f"{what}:\n" + (tmp_path / "launcher.log").read_text(errors="replace"))

    try:
        j = until(lambda j: j["current_version"] == OLD, "the old version did not start")
        assert j["can_install"] and j["bundle_root"] == str(root.resolve())
        status, item = call("POST", f"/api/v1/update/upload?name={patch.name}", patch.read_bytes(), "application/zip")
        assert status == 200 and item["kind"] == "patch" and item["error"] is None, item
        status, r = call("POST", "/api/v1/update/install", json.dumps({"source": "upload", "name": patch.name}).encode())
        assert status == 202 and r["version"] == NEW
        j = until(lambda j: j["current_version"] == NEW and not j["applying"] and not j["pending"], "no new version")
        assert j["last"]["ok"] and j["last"]["version"] == NEW and j["last"]["from_version"] == OLD
        assert tree(root) == tree(new)
        assert (home / "updates/backup" / OLD / "files/app/prototypes/old.js").read_bytes() == b"old"
        assert json.loads((root / "env/desk-bundle.json").read_text())["version"] == NEW

        status, item = call("POST", f"/api/v1/update/upload?name={broken.name}", broken.read_bytes(), "application/zip")
        assert item["kind"] == "full" and item["error"] is None, item
        status, r = call("POST", "/api/v1/update/install", json.dumps({"source": "upload", "name": broken.name}).encode())
        assert status == 202 and r["version"] == BAD
        j = until(lambda j: j["last"] and j["last"]["version"] == BAD and not j["applying"] and not j["pending"]
                  and j["current_version"] == NEW, "the broken version was not rolled back")
        assert not j["last"]["ok"] and "код" in j["last"]["message"] and NEW in j["last"]["message"]
        assert tree(root) == tree(new)
        assert "Обновление Desk Monitor" in (home / "logs/update.log").read_text()
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)


def test_a_patch_without_the_adapter_keeps_the_installed_one(old_new, tmp_path):
    """The owner's bundle has the private adapter; a patch built from a bundle without it must not delete it."""
    root, new, old_zip = old_new
    (root / "adapter/pkg").mkdir(parents=True)
    (root / "adapter/pkg/__init__.py").write_text("private")
    with (root / "MANIFEST.sha256").open("a") as f:  # the installed manifest lists the adapter (a -with-adapter build)
        f.write(build.sha256(root / "adapter/pkg/__init__.py") + "  adapter/pkg/__init__.py\n")
    patch = build.write_patch(old_zip, new, NEW, tmp_path, STAMP)
    doc = update.stage(patch, root, tmp_path / "home")
    assert doc["delete"] == ["app/prototypes/old.js"]


# ---- a patch and another build of its base version (the 0.2.0 incident) -----------------------------------------


def test_build_id_is_the_builders():
    m = {"VERSION": "a" * 64, "wheelhouse/x.whl": "b" * 64, "adapter/pkg/__init__.py": "c" * 64,
         "MANIFEST.sha256": "d" * 64}
    assert update.build_id(m) == build.build_id(m) and len(update.build_id(m)) == 12
    assert update.build_id(m) == update.build_id({k: v for k, v in m.items() if k.startswith(("V", "w"))})


def test_a_patch_for_another_build_of_the_same_version_says_what_differs(old_new, tmp_path):
    """The patch was made from a rebuild of the installed version: same VERSION, another core wheel. The message
    names both builds, the file and both hashes, not the same version twice."""
    root, _, _ = old_new
    whl = "wheelhouse/desk_backend-0.1.0-py3-none-any.whl"
    rebuilt = stage_dir(tmp_path / "rebuilt", bundle(OLD, {whl: b"wheel 1, rebuilt"}))
    new_same_wheel = stage_dir(tmp_path / "new2", bundle(NEW, {whl: b"wheel 1, rebuilt",
                                                               "app/prototypes/settings.html": b"<p>2</p>"}))
    patch = build.write_patch(full_zip(rebuilt, tmp_path / "rebuilt.zip"), new_same_wheel, NEW, tmp_path, STAMP)
    with update.open_zip(patch) as z:
        c = update.read_contents(z)
    assert c.base == OLD and c.base_builds == (build.base_from_zip(tmp_path / "rebuilt.zip").build,)
    with pytest.raises(update.PatchBaseError) as e:
        update.stage(patch, root, tmp_path / "home")
    msg = str(e.value)
    installed = update.build_id(update.installed_manifest(root))
    assert f"для версии {OLD} (сборка {c.base_builds[0]})" in msg and f"установлена версия {OLD} (сборка {installed})" in msg
    assert "Версия та же, но сборка другая" in msg and whl in msg
    assert build.sha256(new_same_wheel / whl)[:12] in msg and build.sha256(root / whl)[:12] in msg
    assert f"DeskMonitor-{NEW}.zip" in msg and msg.count(OLD) == 2
    assert update.describe(patch, root, OLD)["error"] == msg


def test_a_patch_only_needs_the_files_it_does_not_ship(old_new, tmp_path):
    """Files the patch replaces may be anything installed (another build of them); the others must match."""
    root, new, old_zip = old_new
    patch = build.write_patch(old_zip, new, NEW, tmp_path, STAMP)
    (root / "app/prototypes/settings.html").write_bytes(b"<p>another build</p>")  # the patch ships this one
    have = update.installed_manifest(root)
    have["app/prototypes/settings.html"] = build.sha256(root / "app/prototypes/settings.html")
    (root / "MANIFEST.sha256").write_text("".join(f"{d}  {r}\n" for r, d in sorted(have.items())))
    doc = update.stage(patch, root, tmp_path / "home")
    assert "app/prototypes/settings.html" in doc["files"]


def test_patches_leave_the_installed_adapter_alone(old_new, tmp_path):
    """A patch of a with-adapter build carries no adapter files and no adapter lines; an older with-adapter patch
    that lists adapter files without shipping them no longer demands the installed adapter to be that one."""
    root, new, old_zip = old_new
    (root / "adapter/pkg").mkdir(parents=True)
    (root / "adapter/pkg/__init__.py").write_text("installed adapter, version 1")
    (root / "adapter/pkg/old.py").write_text("x")
    (new / "adapter/pkg").mkdir(parents=True)
    (new / "adapter/pkg/__init__.py").write_text("adapter, version 2")
    build.write_manifest(new, f"# DeskMonitor {NEW}\n")
    patch = build.write_patch(old_zip, new, NEW, tmp_path, STAMP)
    with zipfile.ZipFile(patch) as z:
        assert not [n for n in z.namelist() if "/adapter/" in n]
        assert b"adapter/" not in z.read("DeskMonitor/MANIFEST.sha256")
    doc = update.stage(patch, root, tmp_path / "home")
    assert not [r for r in [*doc["files"], *doc["delete"]] if r.startswith("adapter/")]
    # the old form: the with-adapter manifest in the patch, the adapter not shipped (it did not change there)
    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(patch) as src, zipfile.ZipFile(legacy, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, (new / "MANIFEST.sha256").read_bytes() if n.endswith("MANIFEST.sha256") else src.read(n))
    doc = update.stage(legacy, root, tmp_path / "home")
    assert not [r for r in [*doc["files"], *doc["delete"]] if r.startswith("adapter/")]
