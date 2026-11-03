"""The launcher (backend/windows/deskctl.py) with the "no packages" bundle (``wheelhouse\\mirror.json``): the choice of
the install path, the atomic ``env`` / ``env.old`` swap, the pip environment of the installer, ``--strict-mirror``,
the marker and ``DESK_BUNDLE_KIND``. No network: the installer and the subprocesses are replaced."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


deskctl = _load("deskctl_mirror_t", REPO / "backend/windows/deskctl.py")
WHEEL = "desk_backend-0.1.0-py3-none-any.whl"


class Machine:
    """A bundle folder in tmp_path plus the stubs that stand for pip, venv and desk."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.root = tmp_path / "bundle"
        self.home = tmp_path / "home"
        self.env = {"DESK_HOME": str(self.home)}
        (self.root / "wheelhouse").mkdir(parents=True)
        (self.root / "VERSION").write_text("1.0-x\n")
        (self.root / "wheelhouse" / WHEEL).write_bytes(b"wheel 1")
        (self.root / "wheelhouse" / "requirements.txt").write_text("desk-backend==0.1.0\nfoo==1\n")
        (self.home / "logs").mkdir(parents=True)
        for name, value in (("ROOT", self.root), ("APP", self.root / "app"), ("WHEELHOUSE", self.root / "wheelhouse"),
                            ("ADAPTER", self.root / "adapter"), ("ENV", self.root / "env"),
                            ("SCRIPTS", self.root / "env/bin"), ("MARKER", self.root / "env/desk-bundle.json")):
            monkeypatch.setattr(deskctl, name, value)
        monkeypatch.setattr(deskctl, "STRICT_MIRROR", False)
        monkeypatch.setattr(deskctl, "MIGRATE_PAUSE_S", 0)
        self.said: list[str] = []
        monkeypatch.setattr(deskctl, "say", lambda msg="": self.said.append(msg))
        monkeypatch.setattr(deskctl, "bundle_processes", lambda: ([], []))
        monkeypatch.setattr(deskctl, "stop_store", lambda env: None)
        self.mi = deskctl.load_mirror()  # the real module of backend/windows (load_settings, InstallError)
        self.calls: list[list[str]] = []
        self.installs: list[dict] = []
        self.install_error: Exception | None = None
        self.migrate_rc = 0
        monkeypatch.setattr(self.mi, "install", self.fake_install)
        monkeypatch.setattr(deskctl.subprocess, "run", self.fake_run)

    def mirror_json(self, text: str = '{"schema": 1}') -> None:
        (self.root / "wheelhouse" / "mirror.json").write_text(text)

    def fake_install(self, **kw):
        self.installs.append(kw)
        if self.install_error is not None:
            raise self.install_error
        (deskctl.SCRIPTS / "desk").write_text("")
        return types.SimpleNamespace(ok=True)

    def fake_run(self, cmd, **kw):
        self.calls.append([str(c) for c in cmd])
        if "venv" in cmd:
            deskctl.SCRIPTS.mkdir(parents=True)
            (deskctl.SCRIPTS / "python").write_text("")
        elif "pip" in cmd and "--no-index" in cmd and not (deskctl.SCRIPTS / "desk").exists():
            (deskctl.SCRIPTS / "desk").write_text("")  # the full path's offline install
        rc = self.migrate_rc if cmd and cmd[-1] == "db-migrate" else 0
        return subprocess.CompletedProcess(cmd, rc, "", "")

    def old_env(self, complete: bool = True, name: str = "old-env") -> Path:
        """An installed env of an earlier version (a marker only when ``complete``)."""
        scripts = deskctl.ENV / "bin"
        scripts.mkdir(parents=True)
        (scripts / "desk").write_text("")
        (deskctl.ENV / "who").write_text(name)
        if complete:
            deskctl.MARKER.write_text(json.dumps({"version": "0.9-old", "root": str(self.root)}))
        return deskctl.ENV


@pytest.fixture
def m(tmp_path, monkeypatch):
    return Machine(tmp_path, monkeypatch)


def test_kind_follows_mirror_json(m):
    assert deskctl.bundle_kind() == "full"
    m.mirror_json()
    assert deskctl.bundle_kind() == "mirror"


def test_full_bundle_installs_as_before(m):
    deskctl.ensure_env(m.env)
    pip = [c for c in m.calls if "pip" in c]
    assert m.installs == [] and len(pip) == 1 and "--no-index" in pip[0]
    assert not deskctl.env_old().exists()
    assert json.loads(deskctl.MARKER.read_text())["kind"] == "full"


def test_mirror_bundle_installs_through_the_loader(m):
    m.mirror_json()
    deskctl.ensure_env(m.env)
    assert not [c for c in m.calls if "pip" in c]  # no direct pip run of the launcher
    kw = m.installs[0]
    assert kw["python"] == deskctl.SCRIPTS / "python" and kw["wheelhouse"] == deskctl.WHEELHOUSE
    assert kw["log_dir"] == m.home / "logs" and kw["cache"] == m.home / "cache" / "mirror-wheels"
    assert kw["cache"].is_dir() and kw["settings"].strict is False
    assert m.calls[-1][-1] == "db-migrate"
    marker = json.loads(deskctl.MARKER.read_text())
    assert marker["kind"] == "mirror" and marker["mirror"] == deskctl.sha256(m.root / "wheelhouse/mirror.json")
    assert any("зеркала pip этой машины" in s for s in m.said) and not any("без интернета" in s for s in m.said)


def test_success_removes_env_old(m):
    m.mirror_json()
    m.old_env()
    legacy = deskctl.ENV / ".devdata"
    legacy.mkdir()
    (legacy / "keep.txt").write_text("data")
    deskctl.ensure_env(m.env)
    assert not deskctl.env_old().exists() and not (deskctl.ENV / "who").exists()
    assert len(list(m.home.glob("legacy-devdata-*/keep.txt"))) == 1 and deskctl.env_complete()


def test_loader_failure_restores_env_old(m):
    m.mirror_json()
    m.old_env()
    m.install_error = m.mi.InstallError("Нет пакета foo.", m.home / "logs/install-1.log")
    with pytest.raises(deskctl.Fail) as e:
        deskctl.ensure_env(m.env)
    text = str(e.value)
    assert str(m.home / "logs") in text and "Нет пакета foo." in text and "Прежнее окружение возвращено" in text
    assert (deskctl.ENV / "who").read_text() == "old-env" and not deskctl.env_old().exists()
    assert json.loads(deskctl.MARKER.read_text())["version"] == "0.9-old"


def test_migrate_failure_without_an_old_env_leaves_nothing(m):
    m.mirror_json()
    m.migrate_rc = 1
    with pytest.raises(deskctl.Fail, match="db-migrate"):
        deskctl.ensure_env(m.env)
    assert not deskctl.ENV.exists() and not deskctl.env_old().exists()


def test_migrate_failure_restores_env_old(m):
    m.mirror_json()
    m.old_env()
    m.migrate_rc = 1
    with pytest.raises(deskctl.Fail, match=re.escape(str(m.home / "logs"))):
        deskctl.ensure_env(m.env)
    assert (deskctl.ENV / "who").read_text() == "old-env" and not deskctl.env_old().exists()


def test_failed_cleanup_of_env_old_is_a_warning(m, monkeypatch):
    m.mirror_json()
    m.old_env()
    real = deskctl._rmtree

    def rmtree(p):
        if p.name == "env.old":
            raise OSError("locked")
        real(p)

    monkeypatch.setattr(deskctl, "_rmtree", rmtree)
    deskctl.ensure_env(m.env)
    assert deskctl.env_complete() and deskctl.env_old().exists()
    assert any("Предупреждение" in s and "locked" in s for s in m.said)


def test_interrupted_install_restores_env_old_on_the_next_start(m):
    m.mirror_json()
    m.old_env(name="previous").rename(deskctl.env_old())  # the cut-off run: env.old is the good one ...
    (deskctl.ENV / "bin").mkdir(parents=True)  # ... and env\ is half made, without a marker
    (deskctl.ENV / "bin" / "partial").write_text("")
    deskctl.ensure_env(m.env)
    # restored, then (its marker is of 0.9-old) installed anew as usual
    assert not deskctl.env_old().exists() and not (deskctl.ENV / "bin" / "partial").exists()
    assert m.installs and json.loads(deskctl.MARKER.read_text())["version"] == "1.0-x"
    assert any("возвращаю прежнее окружение" in s for s in m.said)


def test_interrupted_install_without_env_at_all(m):
    m.old_env(name="previous").rename(deskctl.env_old())
    deskctl.settle_env_old(m.env)
    assert (deskctl.ENV / "who").read_text() == "previous" and not deskctl.env_old().exists()


def test_env_old_next_to_a_valid_env_is_just_deleted(m):
    m.old_env()
    deskctl.env_old().mkdir()
    (deskctl.env_old() / "x").write_text("")
    deskctl.settle_env_old(m.env)
    assert not deskctl.env_old().exists() and (deskctl.ENV / "who").read_text() == "old-env"


def test_env_old_is_not_taken_back_while_desk_runs(m, monkeypatch):
    m.old_env(complete=False).rename(deskctl.env_old())
    monkeypatch.setattr(deskctl, "bundle_processes", lambda: ([7], []))
    with pytest.raises(deskctl.Fail, match="ещё работает"):
        deskctl.settle_env_old(m.env)
    assert deskctl.env_old().exists()


def test_pip_environ_keeps_the_machine_index_and_drops_what_breaks_a_venv(m, monkeypatch):
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirror.example/simple")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://extra.example/simple")
    monkeypatch.setenv("PIP_CONFIG_FILE", "C:\\corp\\pip.ini")
    broken = ("PIP_USER", "PIP_TARGET", "PIP_PREFIX", "PIP_REQUIRE_VIRTUALENV", "PIP_NO_INDEX")
    for k in broken:
        monkeypatch.setenv(k, "1")
    base = deskctl.desk_environ()
    assert base["PIP_CONFIG_FILE"] == deskctl.os.devnull and "PIP_INDEX_URL" not in base  # the other processes
    env = deskctl.pip_environ(base)
    assert env["PIP_INDEX_URL"] == "https://mirror.example/simple"
    assert env["PIP_EXTRA_INDEX_URL"] == "https://extra.example/simple"
    assert env["PIP_CONFIG_FILE"] == "C:\\corp\\pip.ini"
    assert env["PIP_NO_INPUT"] == "1" and env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
    assert not set(broken) & set(env)
    monkeypatch.delenv("PIP_CONFIG_FILE")
    assert "PIP_CONFIG_FILE" not in deskctl.pip_environ(deskctl.desk_environ())  # not devnull any more


def test_the_loader_gets_the_pip_environment(m, monkeypatch):
    m.mirror_json()
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirror.example/simple")
    monkeypatch.setenv("PIP_USER", "1")
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    deskctl.ensure_env({**m.env, "PIP_CONFIG_FILE": deskctl.os.devnull})
    pip_env = m.installs[0]["pip_env"]
    assert pip_env["PIP_INDEX_URL"] == "https://mirror.example/simple"
    assert "PIP_USER" not in pip_env and "PIP_CONFIG_FILE" not in pip_env


def test_settings_from_files_and_strict_flag(m):
    m.mirror_json()
    assert deskctl.mirror_settings(m.env).strict is False
    (m.home / "mirror.ini").write_text("[mirror]\nindex_url = https://idx.example/simple\n")
    s = deskctl.mirror_settings(m.env)
    assert s.index_url == "https://idx.example/simple" and not s.strict
    (m.root / "mirror.ini").write_text("[mirror]\nstrict = true\n")  # the bundle folder is read first
    s = deskctl.mirror_settings(m.env)
    assert s.strict and s.index_url == ""
    (m.root / "mirror.ini").unlink()
    assert deskctl.take_strict(["--port", "9", "--strict-mirror"]) == ["--port", "9"]
    assert deskctl.mirror_settings(m.env).strict  # the flag on top of a file without it
    deskctl.ensure_env(m.env)
    assert m.installs[0]["settings"].strict and any("строгий режим" in s for s in m.said)


def test_strict_flag_is_taken_out_of_the_arguments(m, monkeypatch):
    seen = []
    monkeypatch.setattr(deskctl, "run_logged", lambda cmd, env, log, on_start=None: seen.append(cmd) or 0)
    monkeypatch.setattr(deskctl, "ensure_env", lambda env, force=False: seen.append(deskctl.STRICT_MIRROR))
    assert deskctl.cmd_cycle(["--strict-mirror", "--dry"]) == 0
    assert seen[0] is True and seen[1][1:] == ["cycle", "--dry"]
    monkeypatch.setattr(deskctl, "STRICT_MIRROR", False)
    seen.clear()
    monkeypatch.setattr(deskctl.subprocess, "run", lambda cmd, **k: seen.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0))
    deskctl.cmd_desk(["status", "--strict-mirror", "--json"])
    assert seen[-1][1:] == ["status", "--json"] and deskctl.STRICT_MIRROR


def test_marker_kind_and_mirror_decide_what_is_reinstalled(m):
    full = deskctl.env_marker()
    assert full["kind"] == "full" and full["mirror"] is None
    assert deskctl.changed_wheels(full, full) == []
    old = {k: v for k, v in full.items() if k not in ("kind", "mirror")}  # a marker of an older launcher
    assert deskctl.changed_wheels(old, full) == []  # no kind = full: no new env for the same full bundle
    m.mirror_json('{"a": 1}')
    mirror = deskctl.env_marker()
    assert mirror["kind"] == "mirror" and mirror["mirror"] == deskctl.sha256(m.root / "wheelhouse/mirror.json")
    assert deskctl.changed_wheels(old, mirror) is None and deskctl.changed_wheels(full, mirror) is None
    assert deskctl.changed_wheels(mirror, full) is None
    assert deskctl.changed_wheels(mirror, mirror) == []
    m.mirror_json('{"a": 2}')
    assert deskctl.changed_wheels(mirror, deskctl.env_marker()) is None  # mirror.json changed: a new env
    (m.root / "wheelhouse" / WHEEL).write_bytes(b"wheel 2")
    again = deskctl.env_marker()
    assert deskctl.changed_wheels({**again, "wheels": {WHEEL: "other"}}, again) == [WHEEL]  # only our wheel


def test_mirror_bundle_with_new_wheels_only_reinstalls_them(m):
    m.mirror_json()
    deskctl.ensure_env(m.env)
    m.installs.clear()
    m.calls.clear()
    (m.root / "VERSION").write_text("1.1-y\n")
    (m.root / "wheelhouse" / WHEEL).write_bytes(b"wheel 2")
    deskctl.ensure_env(m.env)
    pip = [c for c in m.calls if "pip" in c]
    assert m.installs == [] and len(pip) == 1 and "--no-index" in pip[0] and "--no-deps" in pip[0]
    assert not deskctl.env_old().exists() and json.loads(deskctl.MARKER.read_text())["version"] == "1.1-y"


def test_desk_environ_names_the_kind(m, monkeypatch):
    monkeypatch.delenv("DESK_BUNDLE_KIND", raising=False)
    assert deskctl.desk_environ()["DESK_BUNDLE_KIND"] == "full"
    m.mirror_json()
    env = deskctl.desk_environ()
    assert env["DESK_BUNDLE_KIND"] == "mirror" and env["PIP_CONFIG_FILE"] == deskctl.os.devnull
