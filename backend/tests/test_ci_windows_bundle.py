"""The Windows CI driver (``backend/tools/ci_windows_bundle.py``, TDM#639): what runs on Linux of it. The parts that need
Windows (the bundle's python.exe, a process group, tasklist) are not run here; the layout check of a zip, the waiting,
the HTTP helper and the update and rollback phases against a stub of the update API are."""

from __future__ import annotations

import importlib.util
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "ci_windows_bundle.py"
spec = importlib.util.spec_from_file_location("ci_windows_bundle", TOOL)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)  # type: ignore[union-attr]

OLD, NEW = "0.2.25-20261111-abcdef0", "0.2.25-20261111-abcdef0-dirty202611111200"


def make_zip(path: Path, version: str, *, mirror: bool = True, wheels=("desk_backend-0.2.25-py3-none-any.whl",
                                                                       "rub_fixed_income_core-0.45.0-py3-none-any.whl")) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("DeskMonitor/VERSION", version + "\n")
        z.writestr("DeskMonitor/app/deskctl.py", "")
        if mirror:
            z.writestr("DeskMonitor/wheelhouse/mirror.json", "{}")
        for w in wheels:
            z.writestr(f"DeskMonitor/wheelhouse/{w}", "x")
    return path


# ---- the zips --------------------------------------------------------------------------------------------------------


def test_a_bundle_without_packages_passes_the_layout_check(tmp_path: Path) -> None:
    z = make_zip(tmp_path / f"DeskMonitorMirror-{OLD}.zip", OLD)
    assert ci.check_zip(z) == [] and ci.zip_version(z) == OLD


def test_the_layout_check_refuses_what_the_delivery_rule_forbids(tmp_path: Path) -> None:
    full = make_zip(tmp_path / f"DeskMonitor-{OLD}.zip", OLD, mirror=False)
    assert any("not a DeskMonitorMirror" in p for p in ci.check_zip(full))
    assert any("no wheelhouse/mirror.json" in p for p in ci.check_zip(full))
    third = make_zip(tmp_path / "DeskMonitorMirror-x.zip", OLD, wheels=("desk_backend-1-py3-none-any.whl",
                                                                       "rub_fixed_income_core-1-py3-none-any.whl",
                                                                       "numpy-2.0-cp311-cp311-win_amd64.whl"))
    problems = ci.check_zip(third)
    assert any("third-party wheels" in p and "numpy" in p for p in problems)
    one = make_zip(tmp_path / "DeskMonitorMirror-y.zip", OLD, wheels=("desk_backend-1-py3-none-any.whl",))
    assert any("exactly our two wheels" in p for p in ci.check_zip(one))


def test_unpack_makes_a_clean_folder(tmp_path: Path) -> None:
    z = make_zip(tmp_path / "DeskMonitorMirror-a.zip", OLD)
    dest = tmp_path / "install"
    (dest).mkdir()
    (dest / "stale.txt").write_text("x", encoding="utf-8")
    root = ci.unpack(z, dest)
    assert root == dest / "DeskMonitor" and (root / "VERSION").read_text(encoding="utf-8").strip() == OLD
    assert not (dest / "stale.txt").exists()


# ---- waiting and http ----------------------------------------------------------------------------------------------------


def test_wait_for_returns_when_ready_fails_at_the_timeout_and_at_once_when_the_launcher_is_gone() -> None:
    t = [0.0]
    clock, sleep = lambda: t[0], lambda s: t.__setitem__(0, t[0] + s)
    n = [0]

    def ready() -> bool:
        n[0] += 1
        return n[0] >= 4

    assert ci.wait_for("x", ready, 100, every=2, clock=clock, sleep=sleep) == 6
    with pytest.raises(ci.CheckFailed, match="not ready after 10 s"):
        ci.wait_for("x", lambda: False, 10, every=3, clock=clock, sleep=sleep)
    with pytest.raises(ci.CheckFailed, match="the launcher exited with code 1"):
        ci.wait_for("x", lambda: False, 10, alive=lambda: "the launcher exited with code 1", clock=clock, sleep=sleep)


class Stub(BaseHTTPRequestHandler):
    routes: ClassVar[dict] = {}
    seen: ClassVar[list] = []

    def log_message(self, *a) -> None:
        pass

    def _answer(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = self.path.split("?")[0]
        Stub.seen.append((self.command, self.path, body[:60]))
        h = Stub.routes.get((self.command, path))
        code, doc = h(body) if callable(h) else (404, {"message": "nope"})
        raw = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _answer


@pytest.fixture
def stub():
    Stub.routes, Stub.seen = {}, []
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_http_gives_status_and_json_or_zero_when_nothing_answers(stub: str) -> None:
    Stub.routes[("GET", "/a")] = lambda b: (200, {"k": 1})
    assert ci.http("GET", stub + "/a") == (200, {"k": 1})
    assert ci.http("GET", stub + "/missing")[0] == 404
    assert ci.http("GET", "http://127.0.0.1:1/x", timeout=1)[0] == 0


class FakeBundle(ci.Bundle):
    """A Bundle whose launcher never exists: the phases talk to the stub server only."""

    def launcher_gone(self) -> str | None:
        return None


def _bundle(tmp_path: Path, url: str, version_file: str) -> FakeBundle:
    root = tmp_path / "DeskMonitor"
    root.mkdir(exist_ok=True)
    (root / "VERSION").write_text(version_file + "\n", encoding="utf-8")
    b = FakeBundle(root, tmp_path / "home", 0, tmp_path)
    b.url = url
    return b


def _api(state: dict, *, last_ok: bool = True) -> None:
    """The update API of ``desk.api.update_routes`` as the phases use it: the installed version changes after install."""
    ok = lambda b: (200, {"status": "ok"})

    def get_update(_b):
        return 200, {"current_version": state["version"], "can_install": True, "unavailable": None,
                     "rollback": {"version": state["old"]} if state.get("applied") else None,
                     "last": {"ok": last_ok, "version": state["version"], "from_version": state["old"]}}

    def upload(_b):
        return 200, {"name": "n.zip", "version": state["new"], "compare": state.get("compare", "newer"), "error": None}

    def install(_b):
        state["version"], state["applied"] = state["new"], True
        state["version_file"].write_text(state["new"] + "\n", encoding="utf-8")
        return 202, {"restarting": True}

    def rollback(_b):
        state["version"] = state["old"]
        state["version_file"].write_text(state["old"] + "\n", encoding="utf-8")
        return 202, {"restarting": True}

    Stub.routes.update({("GET", "/health/live"): ok, ("GET", "/health/ready"): ok, ("GET", "/api/v1/update"): get_update,
                        ("POST", "/api/v1/update/upload"): upload, ("POST", "/api/v1/update/install"): install,
                        ("POST", "/api/v1/update/rollback"): rollback})


def test_update_then_rollback_phases_follow_the_api(stub: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    b = _bundle(tmp_path, stub, OLD)
    state = {"version": OLD, "old": OLD, "new": NEW, "version_file": b.root / "VERSION"}
    _api(state)
    new_zip = make_zip(tmp_path / f"DeskMonitorMirror-{NEW}.zip", NEW)
    ci.phase_update(b, new_zip, OLD, NEW)
    assert b.version() == NEW
    ci.phase_rollback(b, OLD)
    assert b.version() == OLD and (b.root / "VERSION").read_text(encoding="utf-8").strip() == OLD
    posted = [(m, p) for m, p, _ in Stub.seen if m == "POST"]
    assert posted == [("POST", f"/api/v1/update/upload?name={new_zip.name}"), ("POST", "/api/v1/update/install"),
                      ("POST", "/api/v1/update/rollback")]


def test_update_phase_fails_on_every_wrong_answer(stub: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    new_zip = make_zip(tmp_path / f"DeskMonitorMirror-{NEW}.zip", NEW)
    b = _bundle(tmp_path, stub, OLD)
    state = {"version": "something-else", "old": OLD, "new": NEW, "version_file": b.root / "VERSION"}
    _api(state)
    with pytest.raises(ci.CheckFailed, match="the server says version 'something-else'"):
        ci.phase_update(b, new_zip, OLD, NEW)
    state.update(version=OLD, compare="same")
    with pytest.raises(ci.CheckFailed, match="expected 'newer'"):
        ci.phase_update(b, new_zip, OLD, NEW)
    state.pop("compare")
    _api(state, last_ok=False)  # the launcher says the update was rolled back by itself
    with pytest.raises(ci.CheckFailed, match="not a confirmed update"):
        ci.phase_update(b, new_zip, OLD, NEW)


def test_rollback_phase_needs_the_point_of_the_old_version(stub: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    b = _bundle(tmp_path, stub, NEW)
    state = {"version": NEW, "old": OLD, "new": NEW, "version_file": b.root / "VERSION"}  # nothing applied: no point
    _api(state)
    with pytest.raises(ci.CheckFailed, match="rollback: the point is"):
        ci.phase_rollback(b, OLD)


def test_run_refuses_a_full_bundle_before_it_starts_anything(tmp_path: Path) -> None:
    old = make_zip(tmp_path / f"DeskMonitor-{OLD}.zip", OLD, mirror=False)
    new = make_zip(tmp_path / f"DeskMonitorMirror-{NEW}.zip", NEW)
    with pytest.raises(ci.CheckFailed, match="not a DeskMonitorMirror"):
        ci.run(old, new, tmp_path / "work", 18100)
    same = make_zip(tmp_path / f"DeskMonitorMirror-{OLD}.zip", OLD)
    with pytest.raises(ci.CheckFailed, match="both zips are version"):
        ci.run(same, make_zip(tmp_path / "DeskMonitorMirror-copy.zip", OLD), tmp_path / "work", 18100)
