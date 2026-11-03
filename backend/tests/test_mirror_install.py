"""The dependency installer of the "no packages" Windows bundle (backend/windows/mirror_install.py), which is not
part of the package and is loaded from its file. No internet: a PEP 503 index on a local ``http.server`` serves tiny
pure-Python wheels generated here, pip runs in a venv made in tmp_path, and the environment given to pip points at
that index only (own PIP_CONFIG_FILE, no proxy, no pip cache)."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look their module up
    spec.loader.exec_module(mod)
    return mod


mi = _load("mirror_install", REPO / "backend/windows/mirror_install.py")


# --- unit: candidate order, masking, settings -----------------------------------------------------------------------


def test_order_candidates_same_minor_then_lower_then_higher():
    avail = ["2.3.1", "2.3.9", "2.3.5", "2.2.7", "2.2.0", "1.9.0", "2.4.0", "2.5.1", "3.0.0"]
    # lock 2.3.4: same minor below (nearest first), same minor above (nearest first), older (nearest first), newer
    assert mi.order_candidates("2.3.4", avail) == ["2.3.1", "2.3.5", "2.3.9", "2.2.7", "2.2.0", "1.9.0",
                                                    "2.4.0", "2.5.1", "3.0.0"]


def test_order_candidates_puts_lock_first_and_applies_every_spec():
    avail = ["1.0.0", "1.1.0", "1.2.0", "1.2.3", "1.3.0", "2.0.0"]
    assert mi.order_candidates("1.2.3", avail)[0] == "1.2.3"
    got = mi.order_candidates("1.2.3", avail, [">=1.1", "<2", ""])
    assert got == ["1.2.3", "1.2.0", "1.1.0", "1.3.0"]
    assert mi.order_candidates("1.2.3", avail, [">=5"]) == []


def test_order_candidates_skips_prereleases_unless_the_lock_is_one():
    avail = ["1.2.0", "1.2.5rc1", "1.3.0.dev1", "1.2.4"]
    assert mi.order_candidates("1.2.3", avail) == ["1.2.0", "1.2.4"]
    assert mi.order_candidates("1.2.3rc1", ["1.2.3rc2", "1.2.2"]) == ["1.2.2", "1.2.3rc2"]


def test_order_candidates_ignores_garbage_versions_and_specs():
    assert mi.order_candidates("1.0", ["not-a-version", "1.1"], ["???"]) == ["1.1"]


def test_parse_index_versions():
    out = "hm-beta (2.3.1)\nAvailable versions: 2.3.1, 2.2.0, 1.0\n  INSTALLED: 2.2.0\n"
    assert mi.parse_index_versions(out) == ["2.3.1", "2.2.0", "1.0"]
    assert mi.parse_index_versions("ERROR: nothing") == []


@pytest.mark.parametrize("raw, masked", [
    ("https://user:pw@mirror.example/simple/", "https://***@mirror.example/simple/"),
    ("https://tok@mirror.example:8443/simple", "https://***@mirror.example:8443/simple"),
    ("https://mirror.example/simple/?token=abc123&x=1", "https://mirror.example/simple/?token=***&x=1"),
    ("https://mirror.example/s?api_key=k&Password=p&auth=a&secret=s&keep=v",
     "https://mirror.example/s?api_key=***&Password=***&auth=***&secret=***&keep=v"),
    ("https://mirror.example/simple/", "https://mirror.example/simple/"),
])
def test_mask_url(raw, masked):
    assert mi.mask_url(raw) == masked


def test_mask_text_hides_userinfo_query_secrets_and_literals():
    text = ("Looking in indexes: https://bob:hunter22@host/simple/?token=T0K3N&a=b\n"
            "retry http://host/x?signature=ABCDEF and plain hunter22 again")
    out = mi.mask_text(text, ["hunter22"])
    for secret in ("bob", "hunter22", "T0K3N", "ABCDEF"):
        assert secret not in out
    assert "a=b" in out and "***@host" in out


def test_load_settings_first_existing_file_and_defaults(tmp_path):
    assert mi.load_settings(tmp_path / "none.ini") == mi.Settings()
    first = tmp_path / "a.ini"
    second = tmp_path / "b.ini"
    second.write_text("[mirror]\nindex_url = https://m.example/simple\nstrict = true\n", encoding="utf-8")
    first.write_text("[mirror]\nextra_index_url = https://x.example/simple\n", encoding="utf-8")
    assert mi.load_settings(tmp_path / "none.ini", first, second) == mi.Settings(
        extra_index_url="https://x.example/simple")
    assert mi.load_settings(second) == mi.Settings(index_url="https://m.example/simple", strict=True)


def test_load_settings_rejects_a_bad_boolean(tmp_path):
    bad = tmp_path / "mirror.ini"
    bad.write_text("[mirror]\nstrict = maybe\n", encoding="utf-8")
    with pytest.raises(mi.InstallError, match="strict"):
        mi.load_settings(bad)


# --- fixtures: wheels, index, venv ----------------------------------------------------------------------------------


def make_wheel(directory: Path, name: str, version: str, requires: tuple[str, ...] = ()) -> Path:
    """A tiny pure-Python wheel: module ``<name with _>`` with ``__version__``, METADATA, WHEEL, RECORD."""
    mod = name.replace("-", "_")
    info = f"{mod}-{version}.dist-info"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    meta += "".join(f"Requires-Dist: {r}\n" for r in requires)
    files = {f"{mod}/__init__.py": f"__version__ = {version!r}\n".encode(), f"{info}/METADATA": meta.encode(),
             f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    record = "".join(
        f"{n},sha256={base64.urlsafe_b64encode(hashlib.sha256(d).digest()).rstrip(b'=').decode()},{len(d)}\n"
        for n, d in files.items()) + f"{info}/RECORD,,\n"
    files[f"{info}/RECORD"] = record.encode()
    path = directory / f"{mod}-{version}-py3-none-any.whl"
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    return path


class FakeIndex:
    """PEP 503 index (with PEP 658 metadata files, so a resolver run fetches no wheel) on 127.0.0.1."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.requests: list[str] = []
        self.blocked: set[str] = set()      # wheel file names answered with 404
        self.status_all = 0                 # not 0: every request is answered with this status
        index = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                path = unquote(parts.path)
                index.requests.append(path)
                if index.status_all:
                    return self._send(b"denied", "text/plain", index.status_all)
                if path.startswith("/simple/") and parts.query:
                    return self._send(b"<html><body></body></html>", "text/html")
                if path in ("/simple", "/simple/"):
                    return self._send(b"<html><body></body></html>", "text/html")
                if path.startswith("/simple/"):
                    return self._send(index.project_page(path[len("/simple/"):].strip("/")), "text/html")
                if path.startswith("/files/"):
                    name = path[len("/files/"):]
                    if name in index.blocked:
                        return self._send(b"blocked", "text/plain", 404)
                    if name.endswith(".metadata"):
                        data = index.metadata(name[:-len(".metadata")])
                        return self._send(data, "text/plain") if data is not None else self._send(b"", "x", 404)
                    f = index.root / name
                    return self._send(f.read_bytes(), "application/octet-stream") if f.is_file() \
                        else self._send(b"", "x", 404)
                self._send(b"", "text/plain", 404)

            def _send(self, body: bytes, ctype: str, code: int = 200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/simple/"

    def add(self, name: str, version: str, requires: tuple[str, ...] = ()) -> None:
        make_wheel(self.root, name, version, requires)

    def metadata(self, wheel: str) -> bytes | None:
        f = self.root / wheel
        if not f.is_file():
            return None
        with zipfile.ZipFile(f) as z:
            return z.read(next(n for n in z.namelist() if n.endswith(".dist-info/METADATA")))

    def project_page(self, project: str) -> bytes:
        links = []
        for f in sorted(self.root.glob("*.whl")):
            if f.name.split("-")[0].replace("_", "-").lower() != project.replace("_", "-").lower():
                continue
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            meta = hashlib.sha256(self.metadata(f.name) or b"").hexdigest()
            links.append(f'<a href="/files/{f.name}#sha256={digest}" data-dist-info-metadata="sha256={meta}" '
                         f'data-core-metadata="sha256={meta}">{f.name}</a><br/>')
        return ("<html><body>" + "".join(links) + "</body></html>").encode()

    def wheel_gets(self, wheel: str) -> int:
        return self.requests.count(f"/files/{wheel}")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def index(tmp_path):
    idx = FakeIndex(tmp_path / "index-files")
    yield idx
    idx.close()


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.fixture(scope="module")
def venv_python(tmp_path_factory):
    venv = tmp_path_factory.mktemp("venv") / "env"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    py = _venv_python(venv)
    subprocess.run([str(py), "-m", "pip", "--version"], check=True, capture_output=True)
    return py


@pytest.fixture(autouse=True)
def _empty_venv_after(request):
    yield
    if "venv_python" not in request.fixturenames:
        return
    py = request.getfixturevalue("venv_python")
    frozen = subprocess.run([str(py), "-m", "pip", "freeze"], capture_output=True, text=True, check=False).stdout
    names = [line.split("==")[0] for line in frozen.splitlines() if "==" in line]
    if names:
        subprocess.run([str(py), "-m", "pip", "uninstall", "-y", *names], check=True, capture_output=True)


def pip_env(tmp_path: Path, url: str, **extra: str) -> dict[str, str]:
    conf = tmp_path / "pip.conf"
    conf.write_text(extra.pop("config", ""), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_") and "PROXY" not in k.upper()}
    env.update({"PIP_INDEX_URL": url, "PIP_CONFIG_FILE": str(conf), "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1", "PIP_NO_CACHE_DIR": "1", "NO_PROXY": "127.0.0.1,localhost"})
    env.update(extra)
    return env


LOCK_ALPHA, LOCK_BETA = "1.2.3", "2.3.4"


def make_bundle(tmp_path: Path) -> Path:
    """A wheelhouse as the build makes it: our wheel, requirements.txt and mirror.json."""
    wh = tmp_path / "wheelhouse"
    make_wheel(wh, "hm-app", "0.1.0", ("hm-alpha>=1", "hm-beta>=2.2,<3"))
    (wh / "requirements.txt").write_text(
        f"hm-alpha=={LOCK_ALPHA}\nhm-app==0.1.0\nhm-beta=={LOCK_BETA}\n", encoding="utf-8")
    manifest = {"schema": 1, "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                "platform": "any", "local": ["hm-app==0.1.0"],
                "packages": {"hm-alpha": {"lock": LOCK_ALPHA, "requires": [{"by": "hm-app", "spec": ">=1"}]},
                             "hm-beta": {"lock": LOCK_BETA, "requires": [{"by": "hm-app", "spec": ">=2.2,<3"}]}},
                "imports": ["hm_app", "hm_alpha", "hm_beta"]}
    (wh / "mirror.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return wh


def run_install(tmp_path, venv_python, index, *, settings=None, env=None, say=None):
    return mi.install(python=venv_python, wheelhouse=make_bundle(tmp_path) if not (tmp_path / "wheelhouse").exists()
                      else tmp_path / "wheelhouse", log_dir=tmp_path / "logs",
                      settings=settings or mi.Settings(), pip_env=env or pip_env(tmp_path, index.url),
                      cache=tmp_path / "cache", say=say or (lambda _m: None))


def last_json(tmp_path: Path) -> dict:
    files = sorted((tmp_path / "logs").glob("install-*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8"))


def venv_version(py: Path, module: str) -> str | None:
    cp = subprocess.run([str(py), "-c", f"import {module}; print({module}.__version__)"], capture_output=True,
                        text=True, check=False)
    return cp.stdout.strip() if cp.returncode == 0 else None


def serve_lock(index: FakeIndex) -> None:
    index.add("hm-alpha", LOCK_ALPHA)
    index.add("hm-beta", LOCK_BETA)


# --- integration: pip against the fake index ------------------------------------------------------------------------


def test_exact_versions_install_as_lock(tmp_path, venv_python, index):
    serve_lock(index)
    index.add("hm-beta", "2.3.1")  # a nearer-looking version is ignored while the lock one exists
    said: list[str] = []
    res = run_install(tmp_path, venv_python, index, say=said.append)
    assert res.ok and not res.deviations and not res.not_found
    assert res.installed == ["hm-alpha==1.2.3", "hm-app==0.1.0", "hm-beta==2.3.4"]
    assert venv_version(venv_python, "hm_beta") == "2.3.4" and venv_version(venv_python, "hm_app") == "0.1.0"
    data = last_json(tmp_path)
    assert data["status"] == "ok" and data["mode"] == "flexible"
    assert data["packages"]["hm-beta"]["status"] == "as_lock" and data["packages"]["hm-beta"]["installed"] == "2.3.4"
    assert len(data["packages"]["hm-alpha"]["sha256"]) == 64
    assert data["packages"]["hm-alpha"]["url"].startswith("http://127.0.0.1")
    assert "index-url" in data["index"][0]["source"] and data["python"] and "pip" in data["pip"]
    log = res.log_path.read_text(encoding="utf-8")
    assert "Режим: гибкий" in log and "hm-beta" in log and "отклонений от lock: нет" in log
    assert str(res.log_path) in "\n".join(said)


def test_missing_exact_version_takes_same_minor_below_first(tmp_path, venv_python, index):
    index.add("hm-alpha", LOCK_ALPHA)
    for v in ("2.3.9", "2.3.1", "2.2.7", "2.4.0"):
        index.add("hm-beta", v)
    said: list[str] = []
    res = run_install(tmp_path, venv_python, index, say=said.append)
    assert res.ok and "hm-beta==2.3.1" in res.installed
    assert venv_version(venv_python, "hm_beta") == "2.3.1"
    data = last_json(tmp_path)["packages"]["hm-beta"]
    assert data["status"] == "differs" and data["required"] == "2.3.4" and data["tried"] == ["2.3.4", "2.3.1"]
    assert data["available"] == ["2.4.0", "2.3.9", "2.3.1", "2.2.7"]
    assert res.deviations == ["hm-beta: по lock 2.3.4, будет 2.3.1"]
    assert any("!!!" in line and "hm-beta" in line for line in said)  # the deviation is loud on the screen
    assert "ОТКЛОНЕНИЯ ОТ LOCK" in res.log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("available, expected", [
    (["2.4.0", "2.2.5", "2.2.0"], "2.2.5"),    # no same minor: nearest below beats anything above
    (["2.5.0", "2.4.0"], "2.4.0"),             # nothing below: nearest above
    (["2.3.5", "2.2.5"], "2.3.5"),             # same minor above beats lower minor
])
def test_candidate_order_decides_the_installed_version(tmp_path, venv_python, index, available, expected):
    index.add("hm-alpha", LOCK_ALPHA)
    for v in available:
        index.add("hm-beta", v)
    res = run_install(tmp_path, venv_python, index)
    assert f"hm-beta=={expected}" in res.installed


def test_no_suitable_version_is_refused_with_journal(tmp_path, venv_python, index):
    index.add("hm-alpha", LOCK_ALPHA)
    index.add("hm-beta", "3.0.0")  # excluded by hm-app's "<3"
    with pytest.raises(mi.InstallError) as exc:
        run_install(tmp_path, venv_python, index)
    assert "hm-beta" in str(exc.value) and "нет подходящей версии" in str(exc.value)
    assert exc.value.log_path.is_file() and str(exc.value.log_path) in str(exc.value)
    data = last_json(tmp_path)
    assert data["status"] == "failed" and data["packages"]["hm-beta"]["status"] == "not_found"
    assert data["packages"]["hm-beta"]["available"] == ["3.0.0"] and data["error"]
    assert venv_version(venv_python, "hm_beta") is None  # nothing was installed
    assert "Статус: failed" in exc.value.log_path.read_text(encoding="utf-8")


def _mark_exact(tmp_path: Path, *names: str) -> None:
    wh = make_bundle(tmp_path) if not (tmp_path / "wheelhouse").exists() else tmp_path / "wheelhouse"
    doc = json.loads((wh / "mirror.json").read_text(encoding="utf-8"))
    for n in names:
        doc["packages"][n]["exact"] = True
    (wh / "mirror.json").write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")


def test_exact_package_is_never_replaced_even_in_flexible_mode(tmp_path, venv_python, index):
    index.add("hm-alpha", LOCK_ALPHA)
    index.add("hm-beta", "2.3.1")  # a fine replacement for a plain package, forbidden for an exact one
    _mark_exact(tmp_path, "hm-beta")
    with pytest.raises(mi.InstallError) as exc:
        run_install(tmp_path, venv_python, index)
    assert "hm-beta (lock 2.3.4)" in str(exc.value) and "ровно версии из lock" in str(exc.value)
    data = last_json(tmp_path)
    assert data["mode"] == "flexible" and data["status"] == "failed"
    assert data["packages"]["hm-beta"]["status"] == "not_found" and data["packages"]["hm-beta"]["tried"] == ["2.3.4"]
    assert venv_version(venv_python, "hm_beta") is None


def test_exact_package_installs_when_the_lock_version_is_there_and_others_may_differ(tmp_path, venv_python, index):
    index.add("hm-alpha", "1.5.0")  # alpha is replaceable, beta (exact) is on the index at the lock version
    index.add("hm-beta", LOCK_BETA)
    _mark_exact(tmp_path, "hm-beta")
    res = run_install(tmp_path, venv_python, index)
    assert res.ok and "hm-beta==2.3.4" in res.installed and "hm-alpha==1.5.0" in res.installed
    assert last_json(tmp_path)["packages"]["hm-alpha"]["status"] == "differs"


def test_mirror_json_without_the_exact_field_reads_as_before(tmp_path):
    wh = make_bundle(tmp_path)
    assert "exact" not in (wh / "mirror.json").read_text(encoding="utf-8")
    assert mi._load_manifest(wh)[4] == set()
    _mark_exact(tmp_path, "hm-alpha")
    assert mi._load_manifest(wh)[4] == {"hm-alpha"}


def test_strict_mode_refuses_any_difference(tmp_path, venv_python, index):
    index.add("hm-alpha", LOCK_ALPHA)
    index.add("hm-beta", "2.3.1")
    with pytest.raises(mi.InstallError, match="Строгий режим"):
        run_install(tmp_path, venv_python, index, settings=mi.Settings(strict=True))
    data = last_json(tmp_path)
    assert data["mode"] == "strict" and data["status"] == "failed"
    assert data["packages"]["hm-beta"]["status"] == "not_found" and data["packages"]["hm-beta"]["tried"] == ["2.3.4"]
    assert venv_version(venv_python, "hm_beta") is None


def test_strict_mode_installs_when_the_lock_is_there(tmp_path, venv_python, index):
    serve_lock(index)
    res = run_install(tmp_path, venv_python, index, settings=mi.Settings(strict=True))
    assert res.ok and last_json(tmp_path)["mode"] == "strict"


def test_package_outside_the_lock_pulled_by_the_only_replacement_is_added(tmp_path, venv_python, index):
    index.add("hm-alpha", LOCK_ALPHA)
    index.add("hm-beta", "2.3.1", ("hm-extra>=1",))
    index.add("hm-extra", "1.0")
    res = run_install(tmp_path, venv_python, index)
    assert res.ok and "hm-extra==1.0" in res.installed and "hm-beta==2.3.1" in res.installed
    data = last_json(tmp_path)["packages"]
    assert data["hm-extra"]["status"] == "added" and data["hm-extra"]["installed"] == "1.0"
    assert data["hm-beta"]["status"] == "differs"
    assert any("hm-extra" in d and "добавлен" in d for d in res.deviations)
    assert venv_version(venv_python, "hm_extra") == "1.0"


def test_a_replacement_pulling_outside_the_lock_loses_to_a_clean_one(tmp_path, venv_python, index):
    index.add("hm-alpha", LOCK_ALPHA)
    index.add("hm-beta", "2.3.1", ("hm-extra>=1",))
    index.add("hm-beta", "2.2.0")
    index.add("hm-extra", "1.0")
    res = run_install(tmp_path, venv_python, index)
    assert "hm-beta==2.2.0" in res.installed and not any(i.startswith("hm-extra") for i in res.installed)
    assert last_json(tmp_path)["packages"]["hm-beta"]["tried"] == ["2.3.4", "2.3.1", "2.2.0"]


def test_strict_mode_refuses_a_lock_version_that_pulls_outside_the_lock(tmp_path, venv_python, index):
    index.add("hm-alpha", LOCK_ALPHA)
    index.add("hm-beta", LOCK_BETA, ("hm-extra>=1",))
    index.add("hm-extra", "1.0")
    with pytest.raises(mi.InstallError, match="hm-extra"):
        run_install(tmp_path, venv_python, index, settings=mi.Settings(strict=True))
    assert venv_version(venv_python, "hm_beta") is None
    # the flexible mode takes it and says so
    res = run_install(tmp_path, venv_python, index)
    assert res.ok and "hm-extra==1.0" in res.installed
    assert last_json(tmp_path)["packages"]["hm-extra"]["status"] == "added"


class PipSpy:
    """Records the ``pip download`` calls of the installer; ``fail`` names packages whose download fails."""

    def __init__(self, monkeypatch):
        self.downloads: list[str] = []
        self.fail: set[str] = set()
        real = mi._run

        def spy(cmd, env, cwd=None):
            if "download" in cmd:
                spec = cmd[-1]
                self.downloads.append(spec)
                if spec.split("==")[0] in self.fail:
                    return subprocess.CompletedProcess(cmd, 1, "", "ERROR: simulated network failure")
            return real(cmd, env, cwd)

        monkeypatch.setattr(mi, "_run", spy)


@pytest.fixture
def spy(monkeypatch):
    return PipSpy(monkeypatch)


def test_rerun_after_a_failure_uses_the_cache_and_completes(tmp_path, venv_python, index, spy):
    serve_lock(index)
    alpha, beta = f"hm_alpha-{LOCK_ALPHA}-py3-none-any.whl", f"hm_beta-{LOCK_BETA}-py3-none-any.whl"
    spy.fail.add("hm-beta")
    with pytest.raises(mi.InstallError, match="hm-beta"):
        run_install(tmp_path, venv_python, index)
    first = last_json(tmp_path)
    assert first["status"] == "failed" and first["packages"]["hm-alpha"]["sha256"]
    assert (tmp_path / "cache" / alpha).is_file() and not (tmp_path / "cache" / beta).exists()
    assert spy.downloads == [f"hm-alpha=={LOCK_ALPHA}", f"hm-beta=={LOCK_BETA}"]
    spy.fail.clear()
    spy.downloads.clear()
    res = run_install(tmp_path, venv_python, index)
    assert res.ok and venv_version(venv_python, "hm_beta") == LOCK_BETA
    assert spy.downloads == [f"hm-beta=={LOCK_BETA}"]     # alpha is not downloaded again
    assert len(list((tmp_path / "logs").glob("install-*.json"))) == 2
    assert last_json(tmp_path)["packages"]["hm-alpha"]["sha256"] == first["packages"]["hm-alpha"]["sha256"]
    # everything is cached now: a third run needs neither downloads nor the index
    seen = len(index.requests)
    spy.downloads.clear()
    again = run_install(tmp_path, venv_python, index)
    assert again.ok and spy.downloads == [] and len(index.requests) == seen
    package = last_json(tmp_path)["packages"]["hm-beta"]
    assert package["status"] == "as_lock" and package["url"].startswith("http://127.0.0.1")


def test_a_cached_wheel_with_a_wrong_checksum_is_downloaded_again(tmp_path, venv_python, index, spy):
    serve_lock(index)
    alpha = f"hm_alpha-{LOCK_ALPHA}-py3-none-any.whl"
    run_install(tmp_path, venv_python, index)
    cached = tmp_path / "cache" / alpha
    recorded = json.loads((tmp_path / "cache" / "sha256.json").read_text(encoding="utf-8"))[alpha]["sha256"]
    assert hashlib.sha256(cached.read_bytes()).hexdigest() == recorded
    cached.write_bytes(b"corrupted")
    spy.downloads.clear()
    res = run_install(tmp_path, venv_python, index)
    assert res.ok and spy.downloads == [f"hm-alpha=={LOCK_ALPHA}"]
    assert hashlib.sha256(cached.read_bytes()).hexdigest() == recorded


def _all_logs(tmp_path: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in (tmp_path / "logs").iterdir())


def test_secrets_of_the_settings_urls_never_reach_the_journals(tmp_path, venv_python, index):
    serve_lock(index)
    secured = f"http://alice:s3cretPW@127.0.0.1:{index.port}/simple/"
    tokened = f"http://127.0.0.1:{index.port}/simple/?token=T0KEN-7788"
    settings = mi.Settings(index_url=secured, extra_index_url=tokened)
    res = run_install(tmp_path, venv_python, index, settings=settings)
    assert res.ok
    # a failing run writes pip's output too
    (index.root / f"hm_beta-{LOCK_BETA}-py3-none-any.whl").unlink()
    shutil.rmtree(tmp_path / "cache")
    with pytest.raises(mi.InstallError):
        run_install(tmp_path, venv_python, index, settings=settings)
    text = _all_logs(tmp_path)
    for secret in ("alice", "s3cretPW", "T0KEN-7788"):
        assert secret not in text
    data = last_json(tmp_path)
    assert {"url": f"http://***@127.0.0.1:{index.port}/simple/", "source": "mirror.ini: index_url"} in data["index"]
    assert {"url": f"http://127.0.0.1:{index.port}/simple/?token=***",
            "source": "mirror.ini: extra_index_url"} in data["index"]
    assert "***@127.0.0.1" in text and "token=***" in text


def test_secrets_of_the_pip_configuration_never_reach_the_journals(tmp_path, venv_python, index):
    serve_lock(index)
    base = f"127.0.0.1:{index.port}/simple/"
    env = pip_env(tmp_path, f"http://envuser:envPASS99@{base}", PIP_EXTRA_INDEX_URL=f"http://{base}?apikey=KEY-5544",
                  config=f"[global]\nindex-url = http://bob:cfgPASS42@{base}\n")
    res = run_install(tmp_path, venv_python, index, env=env)
    assert res.ok
    text = _all_logs(tmp_path)
    for secret in ("bob", "cfgPASS42", "envuser", "envPASS99", "KEY-5544"):
        assert secret not in text
    urls = [i["url"] for i in last_json(tmp_path)["index"]]
    assert f"http://***@{base}" in urls and f"http://{base}?apikey=***" in urls


def test_mismatching_requirements_txt_is_refused(tmp_path, venv_python, index):
    serve_lock(index)
    wh = make_bundle(tmp_path)
    (wh / "requirements.txt").write_text("hm-alpha==9.9.9\nhm-app==0.1.0\nhm-beta==2.3.4\n", encoding="utf-8")
    with pytest.raises(mi.InstallError, match="hm-alpha"):
        run_install(tmp_path, venv_python, index)
    assert last_json(tmp_path)["status"] == "failed"


def test_unreachable_index_is_reported_as_such_not_as_missing_versions(tmp_path, venv_python, index):
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://carol:cl0sedPW@127.0.0.1:{port}/simple/"
    env = pip_env(tmp_path, url, PIP_RETRIES="1", PIP_TIMEOUT="5")
    with pytest.raises(mi.InstallError) as exc:
        run_install(tmp_path, venv_python, index, env=env)
    assert "недоступен" in str(exc.value) and "нет подходящей версии" not in str(exc.value)
    data = last_json(tmp_path)
    assert data["status"] == "failed"
    assert {p["status"] for p in data["packages"].values()} == {"index_error"}
    assert data["notes"] and data["notes"][0]["tail"]
    assert "cl0sedPW" not in _all_logs(tmp_path) and "carol" not in _all_logs(tmp_path)


def test_index_refusing_access_is_reported_as_such(tmp_path, venv_python, index):
    serve_lock(index)
    index.status_all = 401
    env = pip_env(tmp_path, f"http://dave:d4vePW@127.0.0.1:{index.port}/simple/", PIP_RETRIES="0")
    with pytest.raises(mi.InstallError, match="отказал в доступе"):
        run_install(tmp_path, venv_python, index, env=env)
    data = last_json(tmp_path)
    assert {p["status"] for p in data["packages"].values()} == {"index_error"}
    assert "d4vePW" not in _all_logs(tmp_path)


def test_first_resolver_output_is_kept_in_the_journal(tmp_path, venv_python, index):
    index.add("hm-alpha", LOCK_ALPHA)
    index.add("hm-beta", "3.0.0")
    with pytest.raises(mi.InstallError):
        run_install(tmp_path, venv_python, index)
    titles = [n["title"] for n in last_json(tmp_path)["notes"]]
    assert "pip install --dry-run (набор из lock)" in titles
    first = last_json(tmp_path)["notes"][0]
    assert first["tail"]  # pip's own words about the failed lock set


def test_final_install_names_every_wheel_by_hash_ours_included(tmp_path, venv_python, index):
    serve_lock(index)
    res = run_install(tmp_path, venv_python, index)
    pins = res.log_path.with_suffix(".requirements.txt").read_text(encoding="utf-8").splitlines()
    assert len(pins) == 3 and all(" --hash=sha256:" in line for line in pins)
    ours = hashlib.sha256((tmp_path / "wheelhouse/hm_app-0.1.0-py3-none-any.whl").read_bytes()).hexdigest()
    assert f"hm-app==0.1.0 --hash=sha256:{ours}" in pins


def test_downloaded_file_differing_from_pips_report_is_refused(tmp_path, venv_python, index, monkeypatch):
    serve_lock(index)
    real = mi._resolve

    def lying(ctx, selection, local, offline=None):
        res = real(ctx, selection, local, offline)
        if res.ok and "hm-alpha" in res.items:
            ver, url, _sha = res.items["hm-alpha"]
            res.items["hm-alpha"] = (ver, url, "0" * 64)
        return res

    monkeypatch.setattr(mi, "_resolve", lying)
    with pytest.raises(mi.InstallError, match="не совпадает"):
        run_install(tmp_path, venv_python, index)
    assert venv_version(venv_python, "hm_alpha") is None


def test_requirements_txt_with_a_package_missing_from_mirror_json_is_refused(tmp_path, venv_python, index):
    serve_lock(index)
    wh = make_bundle(tmp_path)
    with (wh / "requirements.txt").open("a", encoding="utf-8") as f:
        f.write("hm-stray==1.0\n")
    with pytest.raises(mi.InstallError, match="hm-stray"):
        run_install(tmp_path, venv_python, index)


def test_several_extra_indexes_become_separate_flags():
    s = mi.Settings(index_url="https://a.example/simple", extra_index_url="https://b.example/s https://c.example/s")
    assert mi._index_args(s) == ["--index-url", "https://a.example/simple", "--extra-index-url", "https://b.example/s",
                                 "--extra-index-url", "https://c.example/s"]


def test_versions_compare_as_versions_not_strings():
    assert mi._same_version("1.2.3", "1.2.3.0") and not mi._same_version("1.2.3", "1.2.4")


def test_mask_text_scrubs_known_secrets_before_patterns_even_with_an_at_sign():
    url = "https://user:p@ss@host/simple/"
    text = mi.mask_text(f"Looking in {url}", mi._secret_parts(url))
    assert "p@ss" not in text and "ss@" not in text and "user" not in text


def test_network_failure_detection():
    assert mi.is_network_failure("WARNING: Retrying ... NewConnectionError('x')")
    assert mi.is_network_failure("401 Client Error: Unauthorized for url: http://h/")
    assert not mi.is_network_failure("WARNING: 404 Client Error: Not Found for url\nERROR: No matching distribution")


def test_index_returning_no_version_at_all_has_its_own_message(tmp_path, venv_python, index):
    serve_lock(index)
    index.status_all = 401
    env = pip_env(tmp_path, f"http://dave:d4vePW@127.0.0.1:{index.port}/simple/", PIP_RETRIES="0")
    with pytest.raises(mi.InstallError) as exc:
        run_install(tmp_path, venv_python, index, env=env)
    assert "не вернул ни одной версии" in str(exc.value) and "win_amd64" in str(exc.value)


def test_empty_lock_does_not_blame_the_index(tmp_path, venv_python, index):
    wh = tmp_path / "wheelhouse"
    make_wheel(wh, "hm-lonely", "0.1.0", ("hm-gone>=1",))   # needs a package nobody has
    (wh / "requirements.txt").write_text("hm-lonely==0.1.0\n", encoding="utf-8")
    manifest = {"schema": 1, "python": "3.11", "platform": "any", "local": ["hm-lonely==0.1.0"],
                "packages": {}, "imports": []}
    (wh / "mirror.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(mi.InstallError) as exc:
        run_install(tmp_path, venv_python, index)
    assert "не вернул ни одной версии" not in str(exc.value) and "недоступен" not in str(exc.value)
    assert last_json(tmp_path)["status"] == "failed"


def test_json_journal_keeps_names_and_urls_intact_when_the_login_is_a_name_part(tmp_path, venv_python, index):
    serve_lock(index)
    settings = mi.Settings(index_url=f"http://alpha:pw12345@127.0.0.1:{index.port}/simple/")
    res = run_install(tmp_path, venv_python, index, settings=settings)
    text = res.json_path.read_text(encoding="utf-8")
    assert "hm-***" not in text and "pw12345" not in text
    data = json.loads(text)
    assert data["packages"]["hm-alpha"]["installed"] == LOCK_ALPHA
    assert "/files/hm_alpha-1.2.3-py3-none-any.whl" in data["packages"]["hm-alpha"]["url"]
    assert data["index"][0]["url"] == f"http://***@127.0.0.1:{index.port}/simple/"
    assert "alpha:pw" not in res.log_path.read_text(encoding="utf-8")


def test_network_failure_of_a_download_is_noted_once(tmp_path, venv_python, index, monkeypatch):
    serve_lock(index)
    real = mi._run

    def flaky(cmd, env, cwd=None):
        if "download" in cmd and cmd[-1].startswith("hm-beta=="):
            return subprocess.CompletedProcess(cmd, 1, "", "WARNING: Max retries exceeded with url\nERROR: boom")
        return real(cmd, env, cwd)

    monkeypatch.setattr(mi, "_run", flaky)
    with pytest.raises(mi.InstallError, match="недоступен"):
        run_install(tmp_path, venv_python, index)
    titles = [n["title"] for n in last_json(tmp_path)["notes"]]
    assert titles.count(f"pip download hm-beta=={LOCK_BETA}") == 1
