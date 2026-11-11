"""«Индексы ставок» (TDM#651): every state in which the screen has no table from the adapter says why and what to press,
and the names already in the loaded trade terms are shown without a live call to the adapter. The adapter is a synthetic
package built here (its mode is a file in its config folder); every index name is invented."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import adapter_jobs, index_choices, store
from desk.api.app import create_app
from desk.inbox import scan, tools

URL = "/api/v1/adapter/choices/trade_index_codes"
CODES = ["RUONIA", "KEY_RATE", "RUSFAR", "RUSFARCNY", "SOFR", "ESTR", "OTHER"]

ADAPTER = r'''import json, os, time
from desk.inbox.reference import match, read, reference, describe as _describe

def _flag(name, default=""):
    p = os.path.join(os.path.dirname(__file__), name)  # flags of the test: files beside the package
    return open(p, encoding="utf-8").read().strip() if os.path.isfile(p) else default


SPI_VERSION = int(_flag("spi.txt", "1"))
CODES = ["RUONIA", "KEY_RATE", "RUSFAR", "RUSFARCNY", "SOFR", "ESTR", "OTHER"]
TITLES = {"KEY_RATE": "ключевая ставка"}


def describe():
    d = _describe()
    d["spi"] = SPI_VERSION
    d["name"] = "index_state_adapter"
    d["version"] = "0.1.0"
    d["config_files"] = [{"name": "index_codes.json", "title": "Индексы", "required": False, "template": {}}]
    d["settings_tabs"] = [{"id": "index_codes", "title": "Индексы ставок", "file": "index_codes.json", "schema": {
        "type": "object", "properties": {"index_codes": {
            "type": "object", "title": "Индексы",
            "additionalProperties": {"type": "string", "enum": CODES,
                                     "oneOf": [{"const": c, "title": TITLES.get(c, c)} for c in CODES]}}}}}]
    if _flag("no_action.txt") != "1":
        d["actions"] = [{"id": "index_names", "title": "Индексы", "choices": "trade_index_codes",
                         "params": {"type": "object", "properties": {}}}]
    return d


def _mode(ctx):
    p = os.path.join(ctx.config_dir or "", "mode.txt")
    return open(p, encoding="utf-8").read().strip() if os.path.isfile(p) else "ok"


def run_action(action_id, params, ctx):
    mode = _mode(ctx)
    if mode == "crash":
        raise RuntimeError("SECRET-CELL-VALUE")
    if mode == "sleep":
        time.sleep(30)
    if mode == "declined":
        return {"ok": False, "summary": ["Файлов условий сделок в папках отчётов нет"], "files": []}
    if mode == "declined_silent":
        return {"ok": False, "summary": [], "files": []}
    if mode == "bad_result":
        return {"nope": 1}
    if mode == "bad":
        return {"ok": True, "summary": [], "files": [], "choices": {"kind": "trade_index_codes", "rows": 5}}
    rows = [] if mode == "empty" else [
        {"key": "IDX_FAKE_ON", "label": "IDX_FAKE_ON", "value": None, "suggested": "RUSFAR", "future": 2,
         "cells": {"legs": 2}, "replaces": []}]
    return {"ok": True, "summary": ["названий: %d" % len(rows)], "files": [], "choices": {
        "kind": "trade_index_codes", "title": "Индексы", "save": {"tab": "index_codes", "key": "index_codes"},
        "options": [{"value": None, "title": "не выбрано"}] + [{"value": c, "title": c} for c in CODES],
        "columns": [{"id": "legs", "title": "Ног"}], "rows": rows}}
'''


def _settings(tmp_path: Path, pkg: Path | str, cfg: Path, **more: object) -> dict:
    return {"private_adapter": str(pkg) if pkg else "", "private_adapter_config": str(cfg),
            "inbox_dirs": [{"path": str(tmp_path / "in")}], "inbox_lookback_days": 0, "adapter_action_timeout_s": 30,
            **more}


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    pkg = tmp_path / "index_state_adapter_t"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(ADAPTER, encoding="utf-8")
    (tmp_path / "cfg").mkdir()
    (tmp_path / "in").mkdir()
    h.mkdir()
    write_settings(tmp_path, pkg)
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    scan.forget_adapters()
    adapter_jobs.reset()
    yield tmp_path
    adapter_jobs.reset()
    scan.forget_adapters()


def write_settings(tmp_path: Path, pkg: Path | str, **more: object) -> None:
    (tmp_path / "home" / "settings.json").write_text(
        json.dumps(_settings(tmp_path, pkg, tmp_path / "cfg", **more)), encoding="utf-8")
    scan.forget_adapters()


def _client(store_url: str | None) -> TestClient:
    return TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def _run(c: TestClient, timeout: float = 60.0) -> dict:
    assert c.post("/api/v1/adapter/actions/index_names/run", json={}).status_code == 202
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = c.get(URL).json()
        if not r["running"]:
            return r
        time.sleep(0.2)
    raise AssertionError("still running")


def _mode(env: Path, mode: str) -> None:
    (env / "cfg" / "mode.txt").write_text(mode, encoding="utf-8")


def _terms(con, rows: list[dict], *, book: str = "B1", day: date | None = None, source: str = "trade_terms_csv",
           sha: str = "a" * 64) -> int:
    day = day or date.today()
    con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, 'x', 'terms.csv', 1, now())"
                " ON CONFLICT DO NOTHING", (sha,))
    return store.store_dataset_set(
        con, dataset="trade_terms", scope={"book_id": book}, valid_date=day, rows=rows, source=source, priority=None,
        report_date=day, report_ts=None, seq=None, generated_at=None, file_mtime=datetime.now(UTC),
        file_name="terms.csv", sha256=sha, adapter_fingerprint="fp", unit_hash=sha[:16])


def _leg(trade: str, index: str | None, *, code: str = "OTHER", unmapped: bool = False, ccy: str = "RUB",
         maturity: str = "2099-01-01", kind: str = "irs") -> dict:
    r = {"trade_id": trade, "book_id": "B1", "ccy": ccy, "product_kind": kind, "maturity_date": maturity,
         "index_code": code, "_src": "terms.csv:1"}
    if index is not None:
        r["x_index"] = index
    if unmapped:
        r["x_index_unmapped"] = True
    return r


# ---- the states without a table -------------------------------------------------------------------------------------


def test_no_adapter_connected_says_so(env: Path) -> None:
    write_settings(env, "")
    r = _client(None).get(URL)
    st = r.json()["details"]["state"]
    assert r.status_code == 404 and st["code"] == "adapter_off" and st["level"] == "warn"
    assert "не подключён" in st["text"] and "Адаптер и источники" in st["text"]


def test_adapter_that_does_not_load_names_the_reason(env: Path) -> None:
    write_settings(env, env / "no_such_adapter")
    st = _client(None).get(URL).json()["details"]["state"]
    assert st["code"] == "adapter_error" and st["error_code"] == "not_found" and "путь к адаптеру" in st["text"]
    # a version mismatch: the SPI the monitor does not support
    write_settings(env, env / "index_state_adapter_t")
    (env / "index_state_adapter_t" / "spi.txt").write_text("99", encoding="utf-8")
    st = _client(None).get(URL).json()["details"]["state"]
    assert st["code"] == "adapter_error" and st["error_code"] == "unsupported_spi"
    assert "более новой версии интерфейса" in st["text"] and "обновите монитор" in st["text"]
    (env / "index_state_adapter_t" / "spi.txt").write_text("1", encoding="utf-8")
    write_settings(env, env / "index_state_adapter_t")
    # a config folder that is not there
    (env / "index_state_adapter_t" / "spi.txt").write_text("1", encoding="utf-8")
    write_settings(env, env / "index_state_adapter_t", private_adapter_config=str(env / "no_cfg"))
    st = _client(None).get(URL).json()["details"]["state"]
    assert st["error_code"] == "config_dir_missing" and "папка настроек адаптера" in st["text"]


def test_adapter_without_the_action_is_an_old_adapter(env: Path) -> None:
    (env / "index_state_adapter_t" / "no_action.txt").write_text("1", encoding="utf-8")
    r = _client(None).get(URL)
    st = r.json()["details"]["state"]
    assert r.status_code == 404 and st["code"] == "no_action" and "index_state_adapter 0.1.0" in st["text"]
    assert "«Обновления»" in st["text"]


def test_never_run_then_each_outcome_of_the_action(env: Path, store_url: str) -> None:
    c = _client(store_url)
    got = c.get(URL).json()
    assert got["state"]["code"] == "never_run" and got["choices"] is None and "«Обновить»" in got["state"]["text"]

    _mode(env, "ok")
    got = _run(c)
    assert got["state"]["code"] == "ok" and got["choices_source"] == "adapter" and got["state"]["text"] == ""

    _mode(env, "crash")  # the adapter fails: its exception text (it may hold a value) is nowhere
    got = _run(c)
    assert got["state"]["code"] == "job_failed" and got["state"]["level"] == "bad"
    assert "Проверку адаптера" in got["state"]["text"] and "SECRET-CELL-VALUE" not in json.dumps(got)
    assert got["choices"] is None  # no terms in the store: nothing to show instead

    _mode(env, "declined")  # the adapter's own reason is shown as it gave it
    got = _run(c)
    assert got["state"]["code"] == "adapter_declined"
    assert "Файлов условий сделок в папках отчётов нет" in got["state"]["text"]
    _mode(env, "declined_silent")
    got = _run(c)
    assert got["state"]["code"] == "adapter_declined" and "причину не назвал" in got["state"]["text"]
    assert "Индексы не получены" not in got["state"]["text"]

    _mode(env, "bad_result")  # an answer the monitor rejects whole (a version mismatch of the two sides)
    got = _run(c)
    assert got["state"]["code"] == "job_bad_result" and "версии адаптера и монитора" in got["state"]["text"]
    _mode(env, "bad")  # a table of a wrong shape is left out, the action itself stands
    got = _run(c)
    assert got["state"]["code"] == "adapter_bad_choices" and got["state"]["level"] == "bad"
    assert "обновите адаптер" in got["state"]["text"] and got["choices"] is None

    _mode(env, "empty")  # the adapter looked and found no floating legs
    got = _run(c)
    assert got["state"]["code"] == "adapter_empty" and got["choices"]["rows"] == []
    assert "плавающих ног" in got["state"]["text"]


def test_timeout_and_interrupted_job(env: Path, store_url: str) -> None:
    write_settings(env, env / "index_state_adapter_t", adapter_action_timeout_s=1)
    _mode(env, "sleep")
    c = _client(store_url)
    got = _run(c)
    assert got["state"]["code"] == "job_timeout" and "Одно действие адаптера" in got["state"]["text"]
    # a server that stopped during the run: the job file says running
    p = env / "home" / "adapter-jobs" / "action-index_names.json"
    j = json.loads(p.read_text(encoding="utf-8"))
    p.write_text(json.dumps({**j, "state": "running", "finished_at": None}), encoding="utf-8")
    adapter_jobs.reset()
    got = c.get(URL).json()
    assert got["state"]["code"] == "job_interrupted" and "«Обновить»" in got["state"]["text"]


def test_the_state_never_carries_a_name(env: Path, store_url: str) -> None:
    with store.connect(store_url) as con:
        _terms(con, [_leg("T1", "IDX_FAKE_SECRET", unmapped=True)])
    _mode(env, "declined")
    got = _run(_client(store_url))
    assert "IDX_FAKE_SECRET" in json.dumps(got["choices"]) and "IDX_FAKE_SECRET" not in json.dumps(got["state"])


# ---- the names the loaded terms already hold ---------------------------------------------------------------------------


def test_names_of_loaded_terms_are_shown_without_the_adapter(env: Path, store_url: str) -> None:
    (env / "cfg" / "index_codes.json").write_text(json.dumps({"index_codes": {"idx_fake_key ": "KEY_RATE"}}),
                                                  encoding="utf-8")
    with store.connect(store_url) as con:
        _terms(con, [
            _leg("T1", "IDX_FAKE_ON", unmapped=True, ccy="RUB"), _leg("T2", "IDX_FAKE_ON", unmapped=True, ccy="RUB"),
            _leg("T2", "IDX_FAKE_CNY", unmapped=True, ccy="CNY", kind="ccs"),
            _leg("T3", "IDX_FAKE_KEY", code="KEY_RATE"), _leg("T4", "IDX_FAKE_BUILTIN", code="SOFR", ccy="USD"),
            _leg("T5", "IDX_FAKE_ON", unmapped=True, maturity="2020-01-01"),  # matured: not a live leg
            _leg("T6", None, code="RUONIA"),  # a leg with no name: not a row
        ])
    c = _client(store_url)
    got = c.get(URL).json()  # the adapter never ran
    assert got["state"]["code"] == "never_run" and got["choices_source"] == "store"
    ch = got["choices"]
    rows = {r["key"]: r for r in ch["rows"]}
    assert set(rows) == {"IDX_FAKE_ON", "IDX_FAKE_CNY", "IDX_FAKE_KEY", "IDX_FAKE_BUILTIN"}
    assert ch["rows"][0]["key"] == "IDX_FAKE_ON"  # most legs first
    on = rows["IDX_FAKE_ON"]
    assert on["value"] is None and on["future"] == 2 and on["cells"]["legs"] == 3 and on["cells"]["trades"] == 3
    assert "без сопоставления" in on["hint"]
    assert rows["IDX_FAKE_CNY"]["cells"]["ccys"] == "CNY" and rows["IDX_FAKE_CNY"]["cells"]["kinds"] == "ccs"
    # the tab's own entry (another spelling: case and edge spaces do not matter) is the current code, the file's
    # spelling goes on a save; a code the terms carry from elsewhere is the current one too, with its origin said
    assert rows["IDX_FAKE_KEY"]["value"] == "KEY_RATE" and rows["IDX_FAKE_KEY"]["replaces"] == ["idx_fake_key "]
    assert rows["IDX_FAKE_BUILTIN"]["value"] == "SOFR" and "задан не на этой вкладке" in rows["IDX_FAKE_BUILTIN"]["hint"]
    assert [o["value"] for o in ch["options"]] == [None, *CODES]
    assert {o["value"]: o["title"] for o in ch["options"]}["KEY_RATE"] == "ключевая ставка"  # the tab's titles
    assert ch["source"] == "store" and ch["read_only"] is False and ch["file_sha256"]
    # the shape is the adapter's, the monitor's own check accepts it
    tools.check_choices({k: v for k, v in ch.items() if k in ("kind", "title", "save", "options", "columns", "rows",
                                                              "risky_unset", "replay", "notes")}, "trade_index_codes")
    assert ch["replay"] == {"source": "trade_terms_csv", "from": date.today().isoformat(),
                            "to": date.today().isoformat()}


def test_a_failed_action_keeps_the_stored_table_and_the_choice_is_saved(env: Path, store_url: str) -> None:
    path = env / "cfg" / "index_codes.json"
    path.write_text(json.dumps({"index_codes": {"IDX_FAKE_KEY": "KEY_RATE"}, "_note": "kept"}), encoding="utf-8")
    with store.connect(store_url) as con:
        _terms(con, [_leg("T1", "IDX_FAKE_ON", unmapped=True), _leg("T2", "IDX_FAKE_KEY", code="KEY_RATE")])
    c = _client(store_url)
    _mode(env, "crash")
    got = _run(c)
    assert got["state"]["code"] == "job_failed" and got["choices_source"] == "store"
    assert {r["key"] for r in got["choices"]["rows"]} == {"IDX_FAKE_ON", "IDX_FAKE_KEY"} and not got["stale"]
    # a value that is not an option of the screen: refused, nothing written
    before = path.read_text(encoding="utf-8")
    assert c.post(URL, json={"values": {"IDX_FAKE_ON": "LIBOR"}}).status_code == 422
    assert path.read_text(encoding="utf-8") == before
    r = c.post(URL, json={"values": {"IDX_FAKE_ON": "RUSFAR"}, "replay": True})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 1 and r.json()["saved"]["revision"]
    assert r.json()["replay"]["started"] in (True, False)  # the replay itself is checked in its own tests
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "index_codes": {"IDX_FAKE_KEY": "KEY_RATE", "IDX_FAKE_ON": "RUSFAR"}, "_note": "kept"}
    again = c.get(URL).json()  # the table follows the file: the chosen code is the current one now
    assert {r["key"]: r["value"] for r in again["choices"]["rows"]}["IDX_FAKE_ON"] == "RUSFAR"
    # the file changed under the table: 409, as with the adapter's table
    assert c.post(URL, json={"values": {"IDX_FAKE_ON": None}, "base_sha256": "0" * 64}).status_code == 409


def test_the_adapters_table_wins_over_the_stored_one(env: Path, store_url: str) -> None:
    with store.connect(store_url) as con:
        _terms(con, [_leg("T1", "IDX_FAKE_STORED", unmapped=True)])
    c = _client(store_url)
    _mode(env, "ok")
    got = _run(c)
    assert got["choices_source"] == "adapter" and [r["key"] for r in got["choices"]["rows"]] == ["IDX_FAKE_ON"]


def test_empty_adapter_answer_with_stored_names_shows_them(env: Path, store_url: str) -> None:
    with store.connect(store_url) as con:
        _terms(con, [_leg("T1", "IDX_FAKE_STORED", unmapped=True)])
    _mode(env, "empty")
    got = _run(_client(store_url))
    # an empty answer of the adapter has choices (no rows): its table stands, the state tells why it is empty
    assert got["choices_source"] == "adapter" and got["state"]["code"] == "adapter_empty"
    _mode(env, "declined")
    got = _run(_client(store_url))
    assert got["choices_source"] == "store" and got["choices"]["rows"][0]["key"] == "IDX_FAKE_STORED"
    assert got["state"]["code"] == "adapter_declined"


def test_stored_table_is_read_only_when_the_adapter_has_no_tab(env: Path, store_url: str) -> None:
    pkg = env / "index_state_adapter_t"
    src = (pkg / "__init__.py").read_text(encoding="utf-8").replace('"id": "index_codes"', '"id": "other_tab"')
    (pkg / "__init__.py").write_text(src, encoding="utf-8")
    scan.forget_adapters()
    with store.connect(store_url) as con:
        _terms(con, [_leg("T1", "IDX_FAKE_ON", unmapped=True)])
    c = _client(store_url)
    got = c.get(URL).json()
    assert got["choices_source"] == "store" and got["choices"]["read_only"] is True
    assert c.post(URL, json={"values": {"IDX_FAKE_ON": "RUSFAR"}}).status_code == 409


def test_no_store_or_no_names_means_no_stored_table(env: Path, store_url: str) -> None:
    assert _client(None).get(URL).json()["choices"] is None
    assert _client(store_url).get(URL).json()["choices"] is None  # a store with no trade terms
    with store.connect(store_url) as con:
        _terms(con, [_leg("T1", None, code="RUONIA")])  # terms with no index name at all
    assert _client(store_url).get(URL).json()["choices"] is None
    assert index_choices.stored_names(None) is None
    assert index_choices.stored_names("postgresql://127.0.0.1:1/none") is None


def test_only_the_current_set_of_a_unit_counts(env: Path, store_url: str) -> None:
    """A name that the next full reading of the unit dropped is not in the table; two units add up."""
    with store.connect(store_url) as con:
        _terms(con, [_leg("T1", "IDX_FAKE_OLD", unmapped=True)], sha="a" * 64)
        _terms(con, [_leg("T1", "IDX_FAKE_NEW", code="RUSFAR")], sha="b" * 64)  # revision 2 of the same unit
        _terms(con, [_leg("T9", "IDX_FAKE_NEW", code="RUSFAR")], book="B2", sha="c" * 64)
    names = index_choices.stored_names(store_url)
    assert [(n["label"], n["legs"]) for n in names] == [("IDX_FAKE_NEW", 2)]


def test_ocp_screen_is_untouched(env: Path, store_url: str) -> None:
    """The fallback is for the index kind only: another kind never gets a table built by the monitor."""
    got = _client(store_url).get("/api/v1/adapter/choices/ocp_derivative_types")
    assert got.status_code == 404  # this adapter declares no such action
    assert got.json()["details"]["state"]["code"] == "no_action"
    assert "ОВП" in got.json()["details"]["state"]["text"]
