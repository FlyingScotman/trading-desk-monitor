"""Windows: a file that another reader has open cannot be replaced for a moment (``PermissionError`` WinError 5/32).
The services' state, the history's state, the settings and the trades' history marks are written through a temporary
file and ``os.replace``; the replace is tried again (``desk.fsretry``), and a read that is refused is never taken for
an empty document that the next write would put over the real one. The failures are simulated: the same on every
platform. Synthetic data only."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from desk import adapter_services as svcs
from desk import backfill, fsretry, settings, trades_history
from desk.api import adapter_services_routes
from desk.api.adapter_services_routes import router
from desk.settings import Settings

LOOP = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}
FORM = {"Origin": "http://127.0.0.1:8100", "Content-Type": "application/x-www-form-urlencoded"}
FEED = {"id": "eodpos", "title": "Synthetic", "dataset": "positions", "enabled": False, "ready": True,
        "after_positions": False}


def win_busy(code: int = 5) -> PermissionError:
    """What Windows raises for a file a reader holds open."""
    e = PermissionError(13, "Access is denied", None)
    e.winerror = code  # type: ignore[attr-defined]
    return e


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(fsretry, "_sleep", slept.append)
    return slept


class Replace:
    """``os.replace`` that fails ``fail`` times (always when None) for the files named ``only``."""

    def __init__(self, monkeypatch, fail: int | None, only: str | None = None, code: int = 5) -> None:
        self.real, self.left, self.only, self.code, self.calls = os.replace, fail, only, code, 0
        monkeypatch.setattr(os, "replace", self)

    def __call__(self, src, dst, *a, **kw):
        if self.only is None or Path(dst).name == self.only:
            self.calls += 1
            if self.left is None or self.left > 0:
                self.left = self.left if self.left is None else self.left - 1
                raise win_busy(self.code)
        return self.real(src, dst, *a, **kw)


def read_busy(monkeypatch, name: str, fail: int | None) -> dict:
    """``Path.read_text`` of the file ``name`` fails ``fail`` times (always when None)."""
    real, seen = Path.read_text, {"n": 0}

    def read_text(self, *a, **kw):
        if self.name == name and (fail is None or seen["n"] < fail):
            seen["n"] += 1
            raise win_busy()
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)
    return seen


def left_over(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith((".tmp", ".part")))


def make(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


def raw(st: Settings) -> str:
    with open(svcs.state_dir(st) / svcs.STATE_FILE, encoding="utf-8") as f:  # not ``read_text``: tests patch it
        return f.read()


def with_feed(st: Settings) -> str:
    svcs.write_state(st, {**svcs._empty_state(), "feeds": [FEED], "runs": {"eodpos": {"last_ok": "2026-10-16"}}})
    return raw(st)


# ---- the retry -------------------------------------------------------------------------------------------------------

def test_replace_waits_while_the_target_is_in_use_and_gives_up_after_two_seconds(tmp_path, monkeypatch, pauses):
    src, dst = tmp_path / "a.tmp", tmp_path / "a"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    r = Replace(monkeypatch, 2)
    fsretry.replace(src, dst)
    assert dst.read_text(encoding="utf-8") == "new" and r.calls == 3 and pauses == [0.1, 0.2]
    src.write_text("newer", encoding="utf-8")
    pauses.clear()
    Replace(monkeypatch, None)
    with pytest.raises(PermissionError):
        fsretry.replace(src, dst)
    assert abs(sum(pauses) - fsretry.REPLACE_WAIT_S) < 1e-6 and max(pauses) <= 2.0
    assert dst.read_text(encoding="utf-8") == "new"


# ---- the services' state ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("code", [5, 32, 33])
def test_write_state_goes_through_while_a_reader_holds_the_file(tmp_path, monkeypatch, pauses, code):
    st = make(tmp_path)
    with_feed(st)
    r = Replace(monkeypatch, 2, svcs.STATE_FILE, code)
    svcs.write_state(st, {**svcs._empty_state(), "enabled": {"eodpos": True}})
    assert r.calls == 3 and pauses == [0.1, 0.2]
    assert svcs.read_state(st)["enabled"] == {"eodpos": True}
    assert left_over(svcs.state_dir(st)) == []


def test_write_state_that_stays_refused_keeps_the_old_file_and_leaves_no_temporary_file(tmp_path, monkeypatch, pauses):
    st = make(tmp_path)
    before = with_feed(st)
    Replace(monkeypatch, None, svcs.STATE_FILE)
    with pytest.raises(svcs.StateUnavailable) as e:
        svcs.write_state(st, {**svcs._empty_state(), "enabled": {"eodpos": True}})
    assert e.value.code == "state_unavailable" and isinstance(e.value, svcs.ServiceError)
    assert abs(sum(pauses) - fsretry.REPLACE_WAIT_S) < 1e-6
    assert raw(st) == before
    assert left_over(svcs.state_dir(st)) == []


def test_read_state_waits_while_busy_and_is_never_an_empty_document_then(tmp_path, monkeypatch, pauses):
    st = make(tmp_path)
    before = with_feed(st)
    seen = read_busy(monkeypatch, svcs.STATE_FILE, 2)
    assert [f["id"] for f in svcs.read_state(st)["feeds"]] == ["eodpos"] and seen["n"] == 2 and pauses == [0.1, 0.2]
    pauses.clear()
    read_busy(monkeypatch, svcs.STATE_FILE, None)
    with pytest.raises(svcs.StateUnavailable):
        svcs.read_state(st)
    assert abs(sum(pauses) - fsretry.REPLACE_WAIT_S) < 1e-6
    assert raw(st) == before


def test_a_missing_or_foreign_file_is_an_empty_document_as_before(tmp_path):
    st = make(tmp_path)
    assert svcs.read_state(st) == svcs._empty_state()  # no file yet
    d = svcs.state_dir(st)
    d.mkdir(parents=True)
    (d / svcs.STATE_FILE).write_text('{"version": 99, "feeds": [1]}', encoding="utf-8")
    assert svcs.read_state(st) == svcs._empty_state()  # another version
    (d / svcs.STATE_FILE).write_text("{not json", encoding="utf-8")
    assert svcs.read_state(st) == svcs._empty_state()  # damaged: the next write replaces it


def test_a_read_that_is_refused_does_not_wipe_the_feeds_the_switches_and_the_runs(tmp_path, monkeypatch, pauses):
    st = make(tmp_path)
    before = with_feed(st)
    s = svcs.AdapterServices(lambda: st, "", tick_s=3600)
    read_busy(monkeypatch, svcs.STATE_FILE, None)
    with pytest.raises(svcs.StateUnavailable):
        s._record(st, "eodpos", last_started="2026-10-16T09:00:00")  # reads, changes, writes back
    with pytest.raises(svcs.StateUnavailable):
        s.set_enabled("eodpos", True)
    assert raw(st) == before


def test_set_enabled_goes_through_a_busy_file_and_says_so_when_it_stays_busy(tmp_path, monkeypatch, pauses):
    st = make(tmp_path)
    before = with_feed(st)
    s = svcs.AdapterServices(lambda: st, "", tick_s=3600)
    monkeypatch.setattr(adapter_services_routes, "guard", lambda request: None)  # the sign-in has its own tests
    app = FastAPI()
    app.state.adapter_services = s
    app.include_router(router)
    c = TestClient(app, **LOOP)
    Replace(monkeypatch, 3, svcs.STATE_FILE)
    r = c.post("/api/v1/adapter-services/feeds/eodpos/enabled", content="enabled=true", headers=FORM)
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert svcs.read_state(st)["enabled"] == {"eodpos": True}
    Replace(monkeypatch, None, svcs.STATE_FILE)
    r = c.post("/api/v1/adapter-services/feeds/eodpos/enabled", content="enabled=false", headers=FORM)
    assert r.status_code == 409 and r.json() == {"state": "blocked", "code": "state_unavailable"}  # not a bare 500
    assert svcs.read_state(st)["enabled"] == {"eodpos": True} and left_over(svcs.state_dir(st)) == []
    assert before != raw(st)  # the first call's change stays
    read_busy(monkeypatch, svcs.STATE_FILE, None)
    for url in ("/api/v1/adapter-services", "/api/v1/adapter-services/history"):
        r = c.get(url)
        assert r.status_code == 409 and r.json()["code"] == "state_unavailable", url


def test_the_status_of_the_day_and_the_trade_sources_do_not_fail_on_a_busy_state(tmp_path, monkeypatch, pauses):
    st = make(tmp_path)
    with_feed(st)
    assert [f["id"] for f in svcs.day_feeds(st)] == ["eodpos"]
    read_busy(monkeypatch, svcs.STATE_FILE, None)
    from desk import daystatus
    assert daystatus._feeds(st) == []  # the status block is empty then, the day's page is not an error


def test_a_run_record_survives_a_reader_that_comes_and_goes(tmp_path, monkeypatch, pauses):
    """The case of the flaky Windows job: the writer of the run's outcome meets a reader of the same file."""
    st = make(tmp_path)
    with_feed(st)
    s = svcs.AdapterServices(lambda: st, "", tick_s=3600)
    Replace(monkeypatch, 2, svcs.STATE_FILE)
    out = []
    t = threading.Thread(target=lambda: out.append(s._record(st, "eodpos", state="complete")))
    t.start()
    t.join(10)
    assert out and out[0]["state"] == "complete" and out[0]["last_ok"] == "2026-10-16"
    assert svcs.read_state(st)["runs"]["eodpos"]["state"] == "complete"


# ---- the other writers of that class ---------------------------------------------------------------------------------

def test_atomic_write_text_retries_the_replace_and_removes_its_temporary_file(tmp_path, monkeypatch, pauses):
    target = tmp_path / "settings.json"
    target.write_text("old", encoding="utf-8")
    r = Replace(monkeypatch, 2, "settings.json")
    settings.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new" and r.calls == 3 and pauses == [0.1, 0.2]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]
    Replace(monkeypatch, None, "settings.json")
    with pytest.raises(PermissionError):
        settings.atomic_write_text(target, "newer")
    assert target.read_text(encoding="utf-8") == "new" and sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]
    pauses.clear()
    with pytest.raises(PermissionError):  # ``replace_wait_s=0``: once (the updater retries on its own)
        settings.atomic_write_text(target, "newer", replace_wait_s=0)
    assert pauses == []


def test_the_history_state_is_saved_through_a_busy_file(tmp_path, monkeypatch, pauses):
    st = make(tmp_path)
    st.home.mkdir(parents=True)
    backfill.save_state(st, {"stall": 1})
    r = Replace(monkeypatch, 2, backfill.STATE_FILE)
    backfill.save_state(st, {"stall": 2})
    assert backfill.state(st) == {"stall": 2} and r.calls == 3
    Replace(monkeypatch, None, backfill.STATE_FILE)
    backfill.save_state(st, {"stall": 3})  # logged, not raised (as before): the old file stays
    assert backfill.state(st) == {"stall": 2} and left_over(st.home) == []
    monkeypatch.undo()
    read_busy(monkeypatch, backfill.STATE_FILE, 2)
    assert backfill.state(st) == {"stall": 2}


def test_the_marks_of_the_trades_history_are_written_and_read_through_a_busy_file(tmp_path, monkeypatch, pauses):
    st = make(tmp_path)
    doc = {**trades_history.blank(), "failures": 1}
    trades_history.write(st, doc)
    Replace(monkeypatch, 2, trades_history.FILE)
    trades_history.write(st, {**doc, "failures": 2})
    assert trades_history.read(st)["failures"] == 2
    Replace(monkeypatch, None, trades_history.FILE)
    with pytest.raises(PermissionError):
        trades_history.write(st, {**doc, "failures": 3})
    assert trades_history.read(st)["failures"] == 2 and left_over(trades_history.path(st).parent) == []
    monkeypatch.undo()
    seen = read_busy(monkeypatch, trades_history.FILE, 2)
    assert trades_history.read(st)["failures"] == 2 and seen["n"] == 2


def test_the_updater_still_uses_its_own_pauses_and_budget(monkeypatch):
    from desk.updater import adapter

    slept: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", slept.append)
    with pytest.raises(PermissionError):
        adapter.retry(lambda: (_ for _ in ()).throw(win_busy(32)))
    assert abs(sum(slept) - adapter.BUSY_WAIT_S) < 1e-6 and adapter.BUSY_WAIT_S == 15.0
    assert adapter.busy(win_busy()) and not adapter.busy(OSError(2, "gone"))

