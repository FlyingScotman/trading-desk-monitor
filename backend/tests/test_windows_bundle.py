"""The Windows bundle: the build helpers (backend/tools/build_windows_bundle.py) and the launcher the .bat files
call (backend/windows/deskctl.py). Neither is part of the package, so they are loaded from their files."""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
