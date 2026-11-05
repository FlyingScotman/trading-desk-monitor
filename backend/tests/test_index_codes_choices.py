"""The «Индексы ставок» screen's server side (TDM#565): an action that declares ``choices: "trade_index_codes"`` feeds
the screen, a save writes the chosen codes into the adapter's settings tab ``index_codes`` and reads the terms again,
and ``GET /api/v1/adapter/choices/trade_index_codes`` carries ``signal`` — the ``unmapped_index`` issue codes of the
newest reading of the trade terms files, codes and counts only. The adapter is a synthetic package built here; every
index name is invented."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import adapter_jobs, store
from desk.api.app import create_app
from desk.inbox import scan, tools

ADAPTER = r'''import json, os
from desk.inbox.reference import match, read, reference, describe as _describe

SPI_VERSION = 1
CODES = ["RUONIA", "KEY_RATE", "RUSFAR", "RUSFARCNY", "SOFR", "ESTR", "OTHER"]


def describe():
    d = _describe()
    d["name"] = "index_codes_adapter"
    d["config_files"] = [{"name": "index_codes.json", "title": "Индексы", "required": False, "template": {}}]
    d["settings_tabs"] = [{"id": "index_codes", "title": "Индексы ставок", "file": "index_codes.json", "schema": {
        "type": "object", "properties": {"index_codes": {
            "type": "object", "title": "Индексы",
            "additionalProperties": {"type": "string", "enum": CODES}}}}}]
    d["actions"] = [{"id": "index_names", "title": "Индексы ставок в условиях сделок", "choices": "trade_index_codes",
                     "params": {"type": "object", "properties": {}}},
                    {"id": "row_types", "title": "Типы", "choices": "ocp_derivative_types",
                     "params": {"type": "object", "properties": {}}}]
    return d


def run_action(action_id, params, ctx):
    p = os.path.join(ctx.config_dir or "", "index_codes.json")
    cur = (json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else {}).get("index_codes") or {}
    rows = [{"key": "IDX_FAKE_CNY_ON", "label": "IDX_FAKE_CNY_ON", "value": cur.get("IDX_FAKE_CNY_ON"),
             "suggested": "RUSFARCNY", "hint": "по названию и валюте ноги", "new": True, "future": 4,
             "cells": {"legs": 4, "deals": 2, "ccy": "CNY"}, "replaces": []},
            {"key": "IDX_FAKE_KEY", "label": "IDX_FAKE_KEY", "value": cur.get("IDX_FAKE_KEY"), "suggested": None,
             "future": 0, "cells": {"legs": 1, "deals": 1}, "replaces": []}]
    return {"ok": True, "summary": ["названий: 2"], "files": [], "choices": {
        "kind": "trade_index_codes", "title": "Индексы ставок в условиях сделок",
        "save": {"tab": "index_codes", "key": "index_codes"},
        "options": [{"value": None, "title": "не выбрано"}] + [{"value": c, "title": c} for c in CODES],
        "columns": [{"id": "legs", "title": "Ног"}, {"id": "deals", "title": "Сделок"}, {"id": "ccy", "title": "Валюты"}],
        "rows": rows, "replay": {"source": "tt_fake", "from": "2026-11-03", "to": "2026-11-03"}}}
'''


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    pkg = tmp_path / "index_codes_adapter_t"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(ADAPTER, encoding="utf-8")
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    (cfg / "index_codes.json").write_text(json.dumps({"index_codes": {"IDX_FAKE_KEY": "KEY_RATE"}, "_note": "kept"}),
                                          encoding="utf-8")
    h.mkdir()
    (h / "settings.json").write_text(json.dumps({
        "private_adapter": str(pkg), "private_adapter_config": str(cfg), "inbox_dirs": [{"path": str(inbox)}],
        "inbox_lookback_days": 0, "adapter_action_timeout_s": 30}), encoding="utf-8")
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    scan.forget_adapters()
    adapter_jobs.reset()
    yield h
    adapter_jobs.reset()
    scan.forget_adapters()


def _client(store_url: str | None) -> TestClient:
    return TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def _wait(c: TestClient, url: str, timeout: float = 60.0) -> dict:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = c.get(url).json()
        if not r["running"]:
            return r
        time.sleep(0.2)
    raise AssertionError(f"{url}: still running")


def _status(con, sha: str, *, status: str = "accepted", day: str, dataset: str | None = "trade_terms",
            issues: tuple[tuple[str, int], ...] = (), source: str = "tt_fake", seq: int | None = None) -> int:
    """One reading of a file: the file row (once), its status, a unit of ``dataset`` and its issues."""
    con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, 'x', 'x.csv', 1, now())"
                " ON CONFLICT DO NOTHING", (sha,))
    sid = con.execute("INSERT INTO inbox_file_status (sha256, status, adapter_fingerprint, path, source, report_date, seq)"
                      " VALUES (%s, %s, 'fp', 'x', %s, %s, %s) RETURNING status_id", (sha, status, source, day, seq)).fetchone()[0]
    uid = None
    if dataset:
        uid = con.execute("INSERT INTO inbox_unit (status_id, sha256, dataset, report_date, scope, status, row_count)"
                          " VALUES (%s, %s, %s, %s, '{}', 'accepted', 3) RETURNING unit_id",
                          (sid, sha, dataset, day)).fetchone()[0]
    for code, n in issues:
        con.execute("INSERT INTO inbox_issue (status_id, sha256, unit_id, severity, code, message, count, origin)"
                    " VALUES (%s, %s, %s, 'warning', %s, NULL, %s, 'adapter')", (sid, sha, uid, code, n))
    return sid


def _today(back: int = 0) -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=back)).isoformat()


def test_the_kind_is_a_known_screen() -> None:
    assert "trade_index_codes" in tools.CHOICE_KINDS and "ocp_derivative_types" in tools.CHOICE_KINDS
    ok = {"kind": "trade_index_codes", "save": {"tab": "index_codes", "key": "index_codes"},
          "options": [{"value": None, "title": "нет"}, {"value": "OTHER", "title": "другой"}],
          "rows": [{"key": "IDX_FAKE", "value": None, "suggested": "OTHER", "future": 2}]}
    res = tools.check_action_result({"ok": True, "files": [], "choices": ok}, {"choices": "trade_index_codes"})
    assert res["choices"]["rows"][0]["suggested"] == "OTHER" and res["warnings"] == []
    # an empty risky_unset is the norm for this kind
    assert res["choices"]["risky_unset"] == []
    acts, warn = tools.check_actions({"actions": [{"id": "a", "choices": "trade_index_codes"}]}, True)
    assert acts[0]["choices"] == "trade_index_codes" and not warn


def test_get_post_and_the_codes_written_into_the_tab(home: Path, store_url: str) -> None:
    c = _client(store_url)
    ch = c.get("/api/v1/adapter/choices/trade_index_codes").json()
    assert ch["choices"] is None and ch["action"]["id"] == "index_names" and ch["signal"] is None
    assert c.post("/api/v1/adapter/choices/trade_index_codes", json={"values": {}}).status_code == 409
    assert c.post("/api/v1/adapter/actions/index_names/run", json={}).status_code == 202
    ch = _wait(c, "/api/v1/adapter/choices/trade_index_codes")
    rows = {r["key"]: r for r in ch["choices"]["rows"]}
    assert set(rows) == {"IDX_FAKE_CNY_ON", "IDX_FAKE_KEY"} and not ch["stale"]
    assert rows["IDX_FAKE_KEY"]["value"] == "KEY_RATE" and rows["IDX_FAKE_CNY_ON"]["suggested"] == "RUSFARCNY"
    assert [x["id"] for x in ch["choices"]["columns"]] == ["legs", "deals", "ccy"]
    # a key or a value the choices do not have: 422, nothing written
    path = Path(load_cfg(home)) / "index_codes.json"
    before = path.read_text(encoding="utf-8")
    assert c.post("/api/v1/adapter/choices/trade_index_codes", json={"values": {"NOPE": "SOFR"}}).status_code == 422
    assert c.post("/api/v1/adapter/choices/trade_index_codes",
                  json={"values": {"IDX_FAKE_CNY_ON": "LIBOR"}}).status_code == 422
    assert path.read_text(encoding="utf-8") == before
    # the choice: written into the tab's object, the other keys of the file stay
    r = c.post("/api/v1/adapter/choices/trade_index_codes", json={"values": {"IDX_FAKE_CNY_ON": "RUSFARCNY"}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["changed"] == 1 and out["saved"]["revision"] and out["replay"] is None
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "index_codes": {"IDX_FAKE_KEY": "KEY_RATE", "IDX_FAKE_CNY_ON": "RUSFARCNY"}, "_note": "kept"}
    assert c.get("/api/v1/adapter/choices/trade_index_codes").json()["stale"]  # the file changed since the table
    # the table is older than the file: 409 (the page builds it again); a null removes the entry
    assert c.post("/api/v1/adapter/choices/trade_index_codes", json={"values": {"IDX_FAKE_KEY": None}}).status_code == 409
    r = c.post("/api/v1/adapter/choices/trade_index_codes", json={"values": {"IDX_FAKE_KEY": None}, "base_sha256": None})
    assert r.status_code == 200 and json.loads(path.read_text(encoding="utf-8"))["index_codes"] == {
        "IDX_FAKE_CNY_ON": "RUSFARCNY"}
    # with replay: the adapter's source and dates are checked as the replay page checks the owner's; the source is not
    # declared by this adapter, so nothing starts, and the saved file stays
    r = c.post("/api/v1/adapter/choices/trade_index_codes", json={"values": {"IDX_FAKE_KEY": "OTHER"}, "replay": True,
                                                                  "base_sha256": None})
    assert r.status_code == 200 and r.json()["replay"]["started"] is False and r.json()["replay"]["source"] == "tt_fake"
    assert json.loads(path.read_text(encoding="utf-8"))["index_codes"]["IDX_FAKE_KEY"] == "OTHER"


def load_cfg(home: Path) -> str:
    return json.loads((home / "settings.json").read_text(encoding="utf-8"))["private_adapter_config"]


def test_signal_counts_codes_of_the_latest_reading_of_terms_files_only(home: Path, store_url: str) -> None:
    c = _client(store_url)
    c.post("/api/v1/adapter/actions/index_names/run", json={})
    _wait(c, "/api/v1/adapter/choices/trade_index_codes")
    assert c.get("/api/v1/adapter/choices/trade_index_codes").json()["signal"] is None  # an empty store
    with store.connect(store_url) as con:
        # the newest file of the source: two readings, the first had the issue, the last (after the owner's choice) not
        _status(con, "a" * 64, day=_today(1), issues=(("unmapped_index", 5),))
        assert c.get("/api/v1/adapter/choices/trade_index_codes").json()["signal"] == {
            "code": "unmapped_index", "count": 5, "files": 1, "latest": _today(1)}
        _status(con, "a" * 64, day=_today(1), issues=(("unmapped_index", 2), ("other_code", 9)))
        sig = c.get("/api/v1/adapter/choices/trade_index_codes").json()["signal"]
        assert sig["count"] == 2 and sig["files"] == 1  # the old reading is not counted, other codes are not
        # a file of another source the same day adds up; an older day, an old file, a file of another dataset do not
        _status(con, "b" * 64, day=_today(1), issues=(("unmapped_index", 1),), source="tt_b")
        _status(con, "c" * 64, day=_today(3), issues=(("unmapped_index", 40),))
        _status(con, "d" * 64, day=_today(30), issues=(("unmapped_index", 40),), source="old_src")
        _status(con, "e" * 64, day=_today(1), dataset="positions", issues=(("unmapped_index", 40),), source="pos")
        sig = c.get("/api/v1/adapter/choices/trade_index_codes").json()["signal"]
        assert (sig["count"], sig["files"], sig["latest"]) == (3, 2, _today(1))
        # read again without the issue: no sign
        _status(con, "a" * 64, day=_today(1), source="tt_fake")
        _status(con, "b" * 64, day=_today(1), source="tt_b")
    assert c.get("/api/v1/adapter/choices/trade_index_codes").json()["signal"] is None


def test_two_files_of_one_source_and_day_count_the_last_only(home: Path, store_url: str) -> None:
    """The next file of the day replaces the earlier one (higher sequence): the earlier one's issues are not added."""
    c = _client(store_url)
    with store.connect(store_url) as con:
        _status(con, "a" * 64, day=_today(1), seq=1, issues=(("unmapped_index", 5),))
        _status(con, "b" * 64, day=_today(1), seq=2, issues=(("unmapped_index", 3),))
    sig = c.get("/api/v1/adapter/choices/trade_index_codes").json()["signal"]
    assert (sig["count"], sig["files"]) == (3, 1)
    with store.connect(store_url) as con:  # the replacing file read without the issue: nothing left, not the old 5
        _status(con, "b" * 64, day=_today(1), seq=2)
    assert c.get("/api/v1/adapter/choices/trade_index_codes").json()["signal"] is None


def test_without_a_store_the_signal_is_null_not_an_error(home: Path) -> None:
    c = _client(None)
    r = c.get("/api/v1/adapter/choices/trade_index_codes")
    assert r.status_code == 200 and r.json()["signal"] is None
    # an adapter with an action of another kind: the screen answers, with no signal (CHOICE_SIGNALS has no such kind)
    r = TestClient(create_app("postgresql://127.0.0.1:1/none"), base_url="http://127.0.0.1:8000",
                   client=("127.0.0.1", 50000)).get("/api/v1/adapter/choices/ocp_derivative_types")
    assert r.status_code == 200 and r.json()["action"]["id"] == "row_types" and r.json()["signal"] is None


def test_responses_carry_no_text_but_the_adapters(home: Path, store_url: str) -> None:
    c = _client(store_url)
    with store.connect(store_url) as con:
        _status(con, "a" * 64, day=_today(1), issues=(("unmapped_index", 5),))
    body = c.get("/api/v1/adapter/choices/trade_index_codes").json()
    assert set(body["signal"]) == {"code", "count", "files", "latest"}
    assert "IDX_FAKE" not in json.dumps(body["signal"])
