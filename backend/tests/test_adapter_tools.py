"""The settings page's adapter tools (TDM#421, ``desk.adapter_jobs``, ``/api/v1/adapter/check|status|actions``): the
conformance kit as a background job, the optional SPI 1 ``status(ctx)`` and ``describe()["actions"]`` / ``run_action``
(08 §3, §4), the action's files in ``<home>/adapter-output/<id>/`` and the inbox reading them like any report. The
adapter is a synthetic package built here (no firm names or values)."""

from __future__ import annotations

import dataclasses
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from desk import adapter_jobs
from desk.api.app import create_app
from desk.inbox import conformance, runner, scan, tools
from desk.inbox.loader import load_adapter
from desk.settings import load

ADAPTER = '''import json, os, time
from desk.inbox.reference import match, read, reference, describe as _describe

SPI_VERSION = 1
_HERE = os.path.dirname(__file__)


def describe():
    d = _describe()
    d["name"] = "tools_adapter"
    d["actions"] = [
        {"id": "export_rows", "title": "Выгрузка", "description": "Пишет файл для входящих", "output_dir": True,
         "params": {"type": "object", "required": ["day"], "properties": {
             "day": {"type": "string", "format": "date", "title": "Дата"},
             "books": {"type": "array", "items": {"type": "string", "minLength": 1}, "title": "Книги"}},
             "additionalProperties": False}},
        {"id": "sleepy", "title": "Долгое", "description": "", "params": {"type": "object", "properties": {}}},
        {"id": "broken", "title": "Сломанное"},
        {"id": "Bad-Id", "title": "x"},
    ]
    return d


def status(ctx):
    _flag = os.path.join(os.path.dirname(_HERE), "status_mode.txt")
    mode = open(_flag, encoding="utf-8").read() if os.path.isfile(_flag) else "ok"
    if mode == "raise":
        raise RuntimeError("cell value 123.45 must never be shown")
    if mode == "sleep":
        time.sleep(30)
    return {"panels": [
        {"id": "reports", "title": "Отчёты", "level": "ok", "lines": ["папок: %d" % len(ctx.inbox_dirs),
                                                                       "вывод: %s" % ctx.adapter_output_dir],
         "table": {"columns": ["отчёт", "файлов"], "rows": [["a", 3], ["b", None]]}},
        {"id": "bad", "title": "Плохая", "level": "fatal", "lines": []},
        {"id": "cfg", "title": "Настройки", "level": "warning", "lines": ["нет файла"]}],
        "issues": [{"severity": "warning", "code": "no_books", "message": "books file missing"}]}


def run_action(action_id, params, ctx):
    if action_id == "sleepy":
        time.sleep(30)
    if action_id == "broken":
        return {"ok": True, "files": [{"name": "../x.csv", "text": "a"}]}
    day = params["day"].replace("-", "")
    rows = "entity;book_id;line_id;pnl_total_rub\\n" + "".join("E1;%s;L1;1\\n" % b for b in params.get("books") or ["B1"])
    return {"ok": True, "summary": ["строк: %d" % len(params.get("books") or ["B1"]),
                                    "папок: %d" % len(ctx.inbox_dirs), "вывод: %s" % ctx.adapter_output_dir],
            "table": {"columns": ["книга"], "rows": [[b] for b in params.get("books") or ["B1"]]},
            "files": [{"name": "pnl_official_%s.csv" % day, "text": rows}]}
'''


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    pkg = tmp_path / "tools_adapter_t"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(ADAPTER, encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "pnl_official_20261020.csv").write_text("entity;book_id;line_id;pnl_total_rub\nE1;B1;L1;1\n",
                                                     encoding="utf-8")
    h.mkdir()
    (h / "settings.json").write_text(json.dumps({
        "private_adapter": str(pkg), "inbox_dirs": [{"path": str(inbox)}], "inbox_lookback_days": 0,
        "adapter_status_timeout_s": 5, "adapter_action_timeout_s": 5}), encoding="utf-8")
    scan.forget_adapters()
    adapter_jobs.reset()
    yield h
    adapter_jobs.reset()


def _client(url: str = "postgresql://unused@127.0.0.1:1/none") -> TestClient:
    return TestClient(create_app(url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def _wait(client: TestClient, url: str, timeout: float = 60.0) -> dict:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = client.get(url).json()
        if not r["running"]:
            return r
        time.sleep(0.2)
    raise AssertionError(f"{url}: still running")


# ---- describe()["actions"] and the optional functions --------------------------------------------------------------


def test_actions_are_checked_and_the_optional_functions_found(home: Path) -> None:
    st = load()
    h = load_adapter(st.private_adapter, None, None)
    assert h.ok and set(h.optional) == {"status", "run_action"}
    assert [a["id"] for a in h.actions] == ["export_rows", "sleepy", "broken"]
    assert h.actions[0]["output_dir"] and not h.actions[1]["output_dir"]
    assert h.actions[2]["params"] == {"type": "object", "properties": {}}
    assert any("actions[3]" in w for w in h.warnings)
    a = _client().get("/api/v1/adapter").json()
    assert a["optional"] == ["status", "run_action"] and a["actions"] == ["export_rows", "sleepy", "broken"]
    assert any(w.startswith("actions[3]") for w in a["warnings"])
    assert tools.check_actions({"actions": [{"id": "x"}]}, has_run_action=False) == (
        [], ["actions: the adapter has no run_action(), ignored"])


def test_an_adapter_without_them_loads_as_before(home: Path) -> None:
    h = load_adapter(None, None, None)  # the reference adapter
    assert h.ok and h.optional == () and h.actions == []
    with pytest.raises(Exception) as e:
        runner.call_status(h, None, 10)
    assert getattr(e.value, "type", None) == "missing_function"


# ---- status(ctx) ---------------------------------------------------------------------------------------------------


def test_status_panels_checked_bad_ones_dropped_failures_without_messages(home: Path, tmp_path: Path) -> None:
    c = _client()
    r = c.get("/api/v1/adapter/status").json()
    assert r["available"] and r["error"] is None
    assert [p["id"] for p in r["panels"]] == ["reports", "cfg"]
    # ctx.inbox_dirs: the inbox folders only; ctx.adapter_output_dir: the output folder, absolute
    assert r["panels"][0]["lines"] == ["папок: 1", f"вывод: {home / 'adapter-output'}"]
    assert r["panels"][0]["table"]["rows"] == [["a", 3], ["b", None]]
    assert r["problems"] == ["panels[1].level: one of ok, info, warning, error"]
    assert r["issues"][0]["code"] == "no_books"
    (tmp_path / "status_mode.txt").write_text("raise", encoding="utf-8")
    r = c.get("/api/v1/adapter/status").json()
    assert r["error_code"] == "failed" and "RuntimeError" in r["error"] and "123" not in json.dumps(r)
    (tmp_path / "status_mode.txt").write_text("sleep", encoding="utf-8")
    (home / "settings.json").write_text(json.dumps({**json.loads((home / "settings.json").read_text()),
                                                    "adapter_status_timeout_s": 1}), encoding="utf-8")
    t0 = time.monotonic()
    r = c.get("/api/v1/adapter/status").json()
    assert r["error_code"] == "timeout" and time.monotonic() - t0 < 15


def test_adapter_output_dir_only_for_status_and_actions(monkeypatch) -> None:
    """08 §7: ``ctx.adapter_output_dir`` reaches ``status`` and ``run_action``; ``match``, ``read`` and ``reference``
    get None even when a request carries it, and it never joins ``ctx.inbox_dirs``."""
    seen: dict[str, tuple] = {}

    def keep(op: str, ctx, result):
        seen[op] = (ctx.adapter_output_dir, list(ctx.inbox_dirs))
        return result

    fake = SimpleNamespace(match=lambda p, ctx: keep("match", ctx, None),
                           read=lambda p, hit, ctx: keep("read", ctx, {"rows": []}),
                           reference=lambda ctx: keep("reference", ctx, {"books": []}),
                           status=lambda ctx: keep("status", ctx, {"panels": []}),
                           run_action=lambda aid, params, ctx: keep("action", ctx, {"ok": True, "files": []}))
    monkeypatch.setitem(runner._modules, "fake_tools_adapter", fake)
    base = {"adapter": "fake_tools_adapter", "config_dir": None, "inbox_dirs": ["/in"], "instruments_index": None,
            "adapter_output_dir": "/home/adapter-output"}
    for op, extra in (("match", {"paths": ["a.csv"]}), ("read", {"path": "a.csv", "hit": {}}), ("reference", {}),
                      ("status", {}), ("action", {"action_id": "x", "params": {}})):
        assert runner._handle({**base, "op": op, **extra})["ok"], op
    assert seen == {"match": (None, ["/in"]), "read": (None, ["/in"]), "reference": (None, ["/in"]),
                    "status": ("/home/adapter-output", ["/in"]), "action": ("/home/adapter-output", ["/in"])}
    assert runner.Ctx(1, None, None).adapter_output_dir is None  # optional: absent is None
    assert runner._out_dir("rel") == os.path.abspath("rel") and runner._out_dir(None) is None


def test_status_shapes() -> None:
    with pytest.raises(tools.BadResult):
        tools.check_status([])
    got = tools.check_status({"panels": [{"id": "a", "level": "info", "table": {"columns": ["x"], "rows": [[1, 2]]}},
                                         {"id": "a", "level": "ok"}, {"id": "b", "level": "ok", "lines": "x"}],
                              "issues": [{"severity": "bad", "code": "x"}]})
    assert [p["level"] for p in got["panels"]] == ["ok"] and len(got["problems"]) == 3  # a bad panel frees its id


# ---- actions -------------------------------------------------------------------------------------------------------


def test_action_params_are_checked_against_its_schema(home: Path) -> None:
    c = _client()
    r = c.post("/api/v1/adapter/actions/export_rows/run", json={"params": {"day": "2026-13-01", "x": 1}})
    assert r.status_code == 422
    assert {i["code"] for i in r.json()["details"]["issues"]} >= {"bad_date"}
    # additionalProperties: false: an unknown parameter is an error, the action does not run
    r = c.post("/api/v1/adapter/actions/export_rows/run", json={"params": {"day": "2026-10-21", "x": 1}})
    assert r.status_code == 422, r.text
    assert [(i["code"], i["severity"]) for i in r.json()["details"]["issues"]] == [("unknown_config_key", "error")]
    assert adapter_jobs.job(home, "action:export_rows") is None
    assert c.post("/api/v1/adapter/actions/nope/run", json={}).status_code == 404
    assert c.post("/api/v1/adapter/actions/export_rows/run", content=b"{}").status_code == 415


def test_action_writes_its_files_and_the_inbox_reads_them(home: Path, store_url: str) -> None:
    c = _client(store_url)
    r = c.post("/api/v1/adapter/actions/export_rows/run", json={"params": {"day": "2026-10-21", "books": ["B1", "B2"]}})
    assert r.status_code == 202, r.text
    j = _wait(c, "/api/v1/adapter/actions/export_rows")["job"]
    assert j["state"] == "done", j
    assert j["result"]["ok"] and j["result"]["summary"] == ["строк: 2", "папок: 1", f"вывод: {home / 'adapter-output'}"]
    out = home / "adapter-output" / "export_rows"
    assert [f["name"] for f in j["files"]] == ["pnl_official_20261021.csv"] and (out / "pnl_official_20261021.csv").is_file()
    assert j["scan"]["ran"], j["scan"]
    assert j["files"][0]["inbox"]["source"] == "pnl_official_csv", j["files"][0]
    # a second run replaces the action's files; the folder is not an input of the adapter (ctx.inbox_dirs)
    c.post("/api/v1/adapter/actions/export_rows/run", json={"params": {"day": "2026-10-22"}})
    j = _wait(c, "/api/v1/adapter/actions/export_rows")["job"]
    assert sorted(p.name for p in out.iterdir()) == ["pnl_official_20261022.csv"]
    assert not list((home / ".adapter-output-staging").iterdir())
    assert load().scan_dirs[-1] == home / "adapter-output" / "**"
    # the last result is kept across a restart of the server
    adapter_jobs.reset()
    assert c.get("/api/v1/adapter/actions").json()["actions"][0]["job"]["request"]["params"] == {"day": "2026-10-22"}


def test_action_failures_one_at_a_time_and_nothing_written(home: Path) -> None:
    c = _client()
    assert c.post("/api/v1/adapter/actions/broken/run", json={}).status_code == 202
    j = _wait(c, "/api/v1/adapter/actions/broken")["job"]
    assert j["state"] == "failed" and j["error_code"] == "bad_result" and "files[0].name" in j["error"]
    assert not (home / "adapter-output").exists()
    assert c.post("/api/v1/adapter/actions/sleepy/run", json={}).status_code == 202
    assert c.post("/api/v1/adapter/actions/export_rows/run", json={"params": {"day": "2026-10-21"}}).status_code == 409
    j = _wait(c, "/api/v1/adapter/actions/sleepy", timeout=30)["job"]
    assert j["state"] == "timeout" and j["error_code"] == "timeout"


def test_output_file_names() -> None:
    for bad in ("", "a/b.csv", "..\\x", ".hidden", "~$lock.xlsx", "x.tmp", "CON.txt", "a" * 256, "a:b", "x\n"):
        assert tools.check_file_name(bad), bad
    assert tools.check_file_name("Отчёт 2026-10-21.csv") is None
    with pytest.raises(tools.BadResult):
        tools.check_action_result({"ok": True, "files": [{"name": "a.csv", "text": "x"}, {"name": "A.CSV", "text": ""}]},
                                  {"output_dir": True})
    got = tools.check_action_result({"ok": True, "files": [{"name": "a.csv", "text": "x"}]}, {"output_dir": False})
    assert got["warnings"] == [] and got["files"] == [{"name": "a.csv", "text": "x"}]


# ---- the kit -------------------------------------------------------------------------------------------------------


def test_check_runs_the_kit_in_the_background_and_keeps_the_result(home: Path, tmp_path: Path) -> None:
    c = _client()
    r = c.get("/api/v1/adapter/check").json()
    assert r["job"] is None and not r["running"] and r["timeout_s"] == 1800
    assert [o["kind"] for o in r["options"]] == ["inbox", "adapter_output"]
    assert c.post("/api/v1/adapter/check", json={"dirs": [str(tmp_path / "nope")]}).status_code == 422
    r = c.post("/api/v1/adapter/check", json={})
    assert r.status_code == 202 and r.json()["running"]
    assert c.post("/api/v1/adapter/check", json={}).status_code == 409
    j = _wait(c, "/api/v1/adapter/check")["job"]
    assert j["state"] == "done", j
    assert j["request"]["dirs"] == [str(tmp_path / "inbox")]
    rep = j["report"]
    assert rep["name"] == "tools_adapter" and rep["spi"] == 1 and rep["files"]["matched"] == "<10"
    assert j["text"].startswith("desk adapter check: backend ") and "pnl_official_csv 2026-10-20" in j["text"]
    assert "E1" not in j["text"] and "B1" not in j["text"]  # codes and counts only
    adapter_jobs.reset()
    assert c.get("/api/v1/adapter/check").json()["job"]["text"] == j["text"]


def test_kit_argv_keeps_a_path_starting_with_a_dash(home: Path, tmp_path: Path, monkeypatch) -> None:
    """A folder named ``-x`` goes to the kit as ``--dir=-x``, never as a flag of its own."""
    st = dataclasses.replace(load(), inbox_dirs=(Path("-inbox"),))
    argv = adapter_jobs.kit_argv(st, ["-reports", str(tmp_path / "--json")], stub_ctx=True)
    assert "--dir=-reports" in argv and "--inbox-dir=-inbox" in argv
    got: dict = {}

    class _Rep:
        ok = True

        def to_json(self) -> dict:
            return {}

    def fake_check(adapter, dirs, **kw):
        got.update(adapter=adapter, dirs=dirs, **kw)
        return _Rep()

    monkeypatch.setattr(conformance, "check", fake_check)
    assert conformance.main(argv) == 0
    assert got["dirs"] == ["-reports", str(tmp_path / "--json")]
    assert got["ctx_dirs"] == ["-inbox"] and got["stub_ctx"] is True
    assert got["adapter"] == st.private_adapter and got["snapshot_dir"] == str(st.snapshot_cache)
    no_inbox = adapter_jobs.kit_argv(dataclasses.replace(st, inbox_dirs=()), ["-r"], stub_ctx=False)
    got.clear()
    assert conformance.main(no_inbox) == 0
    assert got["ctx_dirs"] == [] and got["stub_ctx"] is False


def test_check_timeout_kills_the_kit(home: Path) -> None:
    doc = json.loads((home / "settings.json").read_text())
    (home / "settings.json").write_text(json.dumps({**doc, "adapter_kit_timeout_s": 0.01}), encoding="utf-8")
    c = _client()
    assert c.post("/api/v1/adapter/check", json={}).status_code == 202
    j = _wait(c, "/api/v1/adapter/check")["job"]
    assert j["state"] == "timeout" and "adapter_kit_timeout_s" in j["error"]


def test_with_sign_in_the_tools_are_the_admins(home: Path, store_url: str) -> None:
    doc = json.loads((home / "settings.json").read_text())
    (home / "settings.json").write_text(json.dumps({**doc, "auth": True}), encoding="utf-8")
    from desk.api import auth

    app = create_app(store_url, auth=auth.AuthConfig.from_settings(load()))
    c = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    assert c.get("/api/v1/adapter/check").status_code == 401
    assert c.post("/api/v1/adapter/actions/sleepy/run", json={}).status_code in (401, 403)
