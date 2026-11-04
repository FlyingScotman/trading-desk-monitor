"""The OCP «report base + day» end to end on synthetic data, the way the owner switches it on (TDM#554, TDM#553, TDM#8):
an adapter whose OCP report has a row type per line (as the private adapter's report does) and a mapping «row type ->
derivative kind» in its settings file. Without the mapping the report holds the forward at its nominal (no split, said
in the gaps). The owner runs the row types action, the screen gets the types with a suggested kind (``choices``), one
save writes the mapping into the settings tab's file and reads the report again (``/api/v1/inbox/replay``); the new
revision gives the forward apart, and the run's OCP is the report without it + its PV from the positions, never the
nominal and the PV both. A derivative kind given apart with no position valued keeps the report's nominal, marked.
The adapter is a synthetic package built here; every label and number is invented."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ocp_run import BOOKS, CASH, ENTITIES, POSITIONS, RULES, TERMS, _csv

from desk import adapter_jobs, store
from desk.api.app import create_app
from desk.inbox import scan, tools
from desk.settings import load

B = date(2026, 10, 15)
SNAPS = Path(__file__).parent / "fixtures" / "mdhub_v1_4"  # with the FX inputs: core values the forward (a PV)
ADAPTER = r'''import csv, io, json, os, re
from datetime import date
from desk.inbox import reference as R

SPI_VERSION = 1
KINDS = ["fx_forward", "fx_swap", "fx_future", "ndf", "fx_option", "swap", "other"]
TITLES = {None: "не дериватив (оставить в отчёте)", "fx_forward": "валютный форвард", "fx_swap": "FX-своп",
          "fx_future": "валютный фьючерс", "ndf": "NDF", "fx_option": "валютный опцион",
          "swap": "процентный / валютно-процентный своп", "other": "прочий дериватив"}
_TYPED = re.compile(r"^typed_ocp_([0-9]{8})\.csv$")


def describe():
    d = R.describe()
    d["name"] = "typed_ocp_adapter"
    d["sources"].append({"source": "typed_ocp", "dataset": "ocp", "time_basis": "end_of_day",
                         "completeness": "full_snapshot", "scope": ["entity", "book_id"], "covers": "listed"})
    d["config_files"] = [{"name": "ocp_cash.json", "title": "ОВП", "required": False, "template": {}}]
    d["settings_tabs"] = [{"id": "ocp_policy", "title": "ОВП: отчёт и состав", "file": "ocp_cash.json", "schema": {
        "type": "object", "properties": {"derivative_types": {
            "type": "object", "title": "Типы строк деривативов",
            "additionalProperties": {"type": ["string", "null"], "enum": KINDS + [None]}}}}}]
    d["actions"] = [{"id": "row_types", "title": "Типы строк отчёта ОВП", "description": "", "choices":
                     "ocp_derivative_types", "params": {"type": "object", "properties": {}}}]
    return d


def _mapping(ctx):
    p = os.path.join(ctx.config_dir or "", "ocp_cash.json")
    data = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else {}
    return {k.strip().lower(): v for k, v in (data.get("derivative_types") or {}).items() if v}, data


def _lines(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def match(path, ctx):
    m = _TYPED.match(os.path.basename(path))
    if not m:
        return R.match(path, ctx)
    d = m.group(1)
    return {"source": "typed_ocp", "report_date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "date_from": "name",
            "report_ts": None, "seq": None}


def read(path, hit, ctx):
    if hit["source"] != "typed_ocp":
        return R.read(path, hit, ctx)
    dmap, _ = _mapping(ctx)
    day = hit["report_date"]
    sums = {}
    for i, ln in enumerate(_lines(path)):
        key = (ln["book_id"], ln["ccy"])
        x = sums.setdefault(key, {"ocp": 0.0, "der": {k: 0.0 for k in KINDS if k in dmap.values()}, "src": i + 2})
        kind = dmap.get(ln["type"].strip().lower())
        if kind and ln["value_date"] > day:
            x["der"][kind] += float(ln["amount"])
        else:
            x["ocp"] += float(ln["amount"])
    rows = [{"entity": "ENT1", "book_id": b, "ccy": c, "ocp_ccy": f"{x['ocp']:.2f}", "_src": f"L{x['src']}",
             **({"derivatives_ccy": {k: f"{v:.2f}" for k, v in x["der"].items()}} if dmap else {})}
            for (b, c), x in sorted(sums.items())]
    scopes = [{"entity": "ENT1", "book_id": b} for b in sorted({b for b, _ in sums} | {"T1"})]
    return {"rows": rows, "scopes": scopes, "issues": [], "controls": {}, "meta": {}}


def reference(ctx):
    return R.reference(ctx)


def _suggest(label):
    t = label.lower()
    return "fx_forward" if "fwd" in t else "fx_future" if "fut" in t else None


def run_action(action_id, params, ctx):
    dmap, data = _mapping(ctx)
    raw = {k.strip().lower(): k for k in (data.get("derivative_types") or {})}
    found = {}
    for d in ctx.inbox_dirs:
        for name in sorted(os.listdir(d)):
            m = _TYPED.match(name)
            if m:
                day = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
                for ln in _lines(os.path.join(d, name)):
                    t = found.setdefault(ln["type"], {"lines": 0, "future": 0, "subs": {}, "last": day})
                    t["lines"] += 1
                    t["future"] += ln["value_date"] > day
                    t["subs"][ln["sub"]] = t["subs"].get(ln["sub"], 0) + 1
                    t["last"] = max(t["last"], day)
    last = max((t["last"] for t in found.values()), default=None)
    rows = [{"key": k, "label": k, "value": dmap.get(k.lower()), "suggested": _suggest(k), "future": t["future"],
             "new": False, "cells": {"lines": t["lines"], "future_share": round(t["future"] / t["lines"], 4)},
             "replaces": [raw[k.lower()]] if k.lower() in raw and raw[k.lower()] != k else [],
             "children": [{"label": s, "cells": {"lines": n}} for s, n in sorted(t["subs"].items())]}
            for k, t in sorted(found.items())]
    return {"ok": True, "summary": [f"типов: {len(rows)}"], "files": [], "choices": {
        "kind": "ocp_derivative_types", "save": {"tab": "ocp_policy", "key": "derivative_types"},
        "options": [{"value": v, "title": TITLES[v]} for v in [None] + KINDS],
        "columns": [{"id": "lines", "title": "Строк"}, {"id": "future_share", "title": "Будущих"}],
        "rows": rows, "risky_unset": ["fx_future", "ndf", "fx_option"],
        "replay": {"source": "typed_ocp", "from": last, "to": last} if last else None}}
'''
# the report of the 15th: D1's cash 150k USD, its forward's future leg 260k USD (type FAKE_FWD, value date after the
# report), a FAKE_FWD line already settled (cash, stays), T1 covered without lines
REPORT = """book_id;ccy;value_date;amount;type;sub
D1;USD;2026-10-15;150000;FAKE_CASH;S1
D1;USD;2026-12-17;260000;FAKE_FWD;S2
D1;USD;2026-10-14;5000;FAKE_FWD;S3
"""
# the forward of the positions with terms bound, so the core values it (its PV, not its notional)
FWD_TERMS = TERMS + ("FW-1;;1;D1;ENT1;D1|FWD;fx_forward;FWD;2026-10-01;2026-12-17;USD;RUB;buy;250000;20500000;82;0;"
                     "CP1\n")
POLICY = "policy_id;valid_from;published;report_max_age_bd\nfirm;;report_base_plus_day;1\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    pkg = tmp_path / "typed_ocp_adapter_t"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(ADAPTER, encoding="utf-8")
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    for name, text in (("books.csv", BOOKS), ("entities.csv", ENTITIES), ("funding_rules.csv", RULES),
                       ("ocp_policy.csv", POLICY)):
        _csv(cfg / name, text)
    (cfg / "ocp_cash.json").write_text(json.dumps({"derivative_types": {" fake_fwd ": None}, "_note": "kept"}),
                                       encoding="utf-8")
    for name, text in (("positions_20261016.csv", POSITIONS), ("trade_terms_20261015.csv", FWD_TERMS),
                       ("cash_balances_20261015.csv", CASH)):
        _csv(inbox / name, text)
    (inbox / "typed_ocp_20261015.csv").write_text(REPORT, encoding="utf-8")
    old = time.time() - 3600  # written an hour ago: stable for the first scan
    for f in inbox.iterdir():
        os.utime(f, (old, old))
    h.mkdir()
    (h / "settings.json").write_text(json.dumps({
        "private_adapter": str(pkg), "private_adapter_config": str(cfg), "inbox_dirs": [{"path": str(inbox)}],
        "inbox_lookback_days": 0, "adapter_action_timeout_s": 30,
        "snapshot_cache": str(SNAPS)}), encoding="utf-8")
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    scan.forget_adapters()
    adapter_jobs.reset()
    yield h
    adapter_jobs.reset()
    scan.forget_adapters()


@pytest.fixture(scope="module")
def ctx():
    from desk.marketdata.bundle import SnapshotDir
    from desk.orchestrator import prepare

    return prepare(SnapshotDir(SNAPS), B.isoformat())


def _wait(c: TestClient, url: str, key: str = "running", timeout: float = 90.0) -> dict:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = c.get(url).json()
        if not r[key]:
            return r
        time.sleep(0.2)
    raise AssertionError(f"{url}: still running")


def _run(store_url: str, ctx) -> dict:
    from desk.orchestrator import value
    from desk.publish import desk_run
    from desk.valuation.positions import merge, plan

    with store.connect(store_url) as con:
        inp = store.positions_for(con, B)
        bp = plan(ctx, inp)
        pos = merge(bp, value(ctx, bp.requests, position_ids=bp.position_ids))
        rid, st, _ = desk_run.publish(con, ctx, pos, prev=None, inputs=inp, business_date=B)
        assert st == "complete"
    return TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/ocp").json()


def test_choices_shape_is_checked() -> None:
    decl = {"choices": "ocp_derivative_types"}
    ok = {"kind": "ocp_derivative_types", "save": {"tab": "ocp_policy", "key": "derivative_types"},
          "options": [{"value": None, "title": "нет"}, {"value": "fx_forward", "title": "форвард"}],
          "rows": [{"key": "A", "value": None, "suggested": "fx_forward", "future": 2}]}
    res = tools.check_action_result({"ok": True, "files": [], "choices": ok}, decl)
    assert res["choices"]["rows"][0]["suggested"] == "fx_forward" and res["warnings"] == []
    for bad in ({**ok, "kind": "other"}, {**ok, "save": {"tab": "Bad Tab", "key": "x"}},
                {**ok, "replay": {"source": "s", "from": "x"}}, {**ok, "rows": "A"}, {**ok, "risky_unset": [None]}):
        res = tools.check_action_result({"ok": True, "files": [], "choices": bad}, decl)
        assert "choices" not in res and res["warnings"] and res["ok"], bad  # the action stands, its choices go
    # a bad row (a key on «_», a repeated key, a wrong value or future) goes alone, the other rows stay
    rows = [{"key": "A", "value": None, "suggested": None}, {"key": "_x", "value": None, "suggested": None},
            {"key": "A", "value": None, "suggested": None}, {"key": "B", "value": "swap", "suggested": None},
            {"key": "C", "value": None, "suggested": None, "future": -1}, "junk",
            {"key": "D", "value": None, "suggested": "fx_forward", "future": 1}]
    res = tools.check_action_result({"ok": True, "files": [], "choices": {**ok, "rows": rows}}, decl)
    assert [r["key"] for r in res["choices"]["rows"]] == ["A", "D"] and not res["choices"]["truncated"]
    assert len(res["warnings"]) == 5 and all(w.endswith("row skipped") for w in res["warnings"]), res["warnings"]
    big = [{"key": f"K{n}", "value": None, "suggested": None} for n in range(tools.MAX_CHOICE_ROWS + 5)]
    res = tools.check_action_result({"ok": True, "files": [], "choices": {**ok, "rows": big}}, decl)
    assert len(res["choices"]["rows"]) == tools.MAX_CHOICE_ROWS and res["choices"]["truncated"] is True
    assert len(res["warnings"]) == 1 and "only the first" in res["warnings"][0]
    assert "warnings" not in res["choices"]
    res = tools.check_action_result({"ok": True, "files": [], "choices": ok}, {})
    assert "choices" not in res and res["warnings"] == ["choices: the action does not declare choices, ignored"]
    acts, warn = tools.check_actions({"actions": [{"id": "a", "choices": "nope"}]}, True)
    assert acts[0]["choices"] is None and warn


def test_the_screen_gets_the_row_types_and_refuses_what_they_do_not_have(home: Path, store_url: str) -> None:
    st = load()
    with store.connect(store_url) as con:
        res = scan.scan(con, st)
        assert res.ok and res.files.get("accepted") == 4, res.text()
    c = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))

    # the replay page: the sources with their newest accepted date, the default period, the estimate
    rp = c.get("/api/v1/inbox/replay", params={"source": "typed_ocp", "from": "2026-10-15", "to": "2026-10-15"}).json()
    typed = next(s for s in rp["sources"] if s["source"] == "typed_ocp")
    assert typed["latest"] == "2026-10-15" and typed["names"] == ["ОВП"] and rp["estimate"] == {
        "files": 1, "dates": ["2026-10-15"]}
    assert rp["job"] is None and not rp["running"]

    # the screen: no result yet; the action builds it
    ch = c.get("/api/v1/adapter/choices/ocp_derivative_types").json()
    assert ch["choices"] is None and ch["action"]["id"] == "row_types"
    assert c.post("/api/v1/adapter/choices/ocp_derivative_types", json={"values": {}}).status_code == 409
    assert c.post("/api/v1/adapter/actions/row_types/run", json={}).status_code == 202
    ch = _wait(c, "/api/v1/adapter/choices/ocp_derivative_types")
    rows = {r["key"]: r for r in ch["choices"]["rows"]}
    assert set(rows) == {"FAKE_CASH", "FAKE_FWD"} and not ch["stale"]
    assert (rows["FAKE_FWD"]["value"], rows["FAKE_FWD"]["suggested"], rows["FAKE_FWD"]["future"]) == (
        None, "fx_forward", 1)
    assert rows["FAKE_FWD"]["replaces"] == [" fake_fwd "] and len(rows["FAKE_FWD"]["children"]) == 2
    assert ch["choices"]["replay"] == {"source": "typed_ocp", "from": "2026-10-15", "to": "2026-10-15"}
    assert c.post("/api/v1/adapter/choices/ocp_derivative_types",
                  json={"values": {"NOPE": "fx_forward"}}).status_code == 422
    assert c.post("/api/v1/adapter/choices/ocp_derivative_types",
                  json={"values": {"FAKE_FWD": "bogus"}}).status_code == 422
    assert c.post("/api/v1/adapter/choices/ocp_derivative_types",
                  json={"values": {"FAKE_FWD": "fx_forward"}, "base_sha256": "0" * 64}).status_code == 409


def test_end_to_end_ocp_before_and_after_the_mapping(home: Path, store_url: str, ctx) -> None:
    st = load()
    with store.connect(store_url) as con:
        res = scan.scan(con, st)
        assert res.ok and res.files.get("accepted") == 4, res.text()
    c = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))

    # before: the report holds the forward at its nominal, nothing apart, said in the gaps
    before = _run(store_url, ctx)
    d1 = {(x["book_id"], x["ccy"]): x for x in before["published"]["by_book"]}[("D1", "USD")]
    assert d1["base_kind"] == "with_derivatives" and d1["parts"]["base"] == 415_000
    assert d1["parts"]["derivatives"] == 0
    assert "report_without_derivatives_split" in {g["code"] for g in before["gaps"]}

    # the owner: the action, one choice, one button
    c.post("/api/v1/adapter/actions/row_types/run", json={})
    _wait(c, "/api/v1/adapter/choices/ocp_derivative_types")
    r = c.post("/api/v1/adapter/choices/ocp_derivative_types",
               json={"values": {"FAKE_FWD": "fx_forward", "FAKE_CASH": None}, "replay": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["changed"] == 1 and out["saved"]["revision"] and out["replay"]["started"]
    saved = json.loads((Path(st.private_adapter_config) / "ocp_cash.json").read_text(encoding="utf-8"))
    assert saved == {"derivative_types": {"FAKE_FWD": "fx_forward"}, "_note": "kept"}  # the old spelling replaced
    rp = _wait(c, "/api/v1/inbox/replay")
    j = rp["job"]
    assert j["state"] == "done" and j["result"]["revisions"] >= 1, j
    assert j["estimate"] == {"files": 1, "dates": ["2026-10-15"]} and j["request"]["reason"].startswith("сохранено")
    assert c.get("/api/v1/adapter/choices/ocp_derivative_types").json()["stale"]  # the file changed since the table

    # after: the report without the forward + its PV from the positions; the nominal is not added on top
    after = _run(store_url, ctx)
    d1 = {(x["book_id"], x["ccy"]): x for x in after["published"]["by_book"]}[("D1", "USD")]
    assert d1["base_kind"] == "without_derivatives" and d1["split_kinds"] == ["fx_forward"]
    assert d1["parts"]["base"] == 155_000  # the cash and the settled line of the forward's type
    pv = {(x["book_id"], x["kind"]): x["amount"] for x in after["derivatives_pv"]}[("D1", "fx_forward")]
    fwd = [i for i in d1["contributions"]["derivatives"]["items"] if i.get("position_id") == "D1|FWD"]
    assert fwd and fwd[0]["basis"] == "spot_equivalent" and "marker" not in fwd[0]  # valued by core: a real PV
    assert d1["parts"]["derivatives"] == pytest.approx(pv) and pv != 260_000 and pv != 250_000
    assert d1["amount"] == pytest.approx(155_000 + pv + d1["parts"]["outside"] + d1["parts"]["trades"]
                                         + d1["parts"]["bonds"])
    assert d1["fallback"] == [] and d1["no_pv"] == []
    rec = {(x["book_id"], x["ccy"]): x for x in after["reconciliation"]}[("D1", "USD")]
    assert rec["reported"] == 415_000 and rec["reported_base"] == 155_000  # the whole report still reconciled
    assert "report_without_derivatives_split" not in {g["code"] for g in after["gaps"]}


def test_a_derivative_without_bound_terms_keeps_the_reports_nominal_marked(home: Path, store_url: str, ctx) -> None:
    """The same report and mapping, but the forward's trade terms are not bound (no PV): the report's nominal of the
    kind, marked «номинал, PV нет», not the position's own notional and never zero (the owner, 03.11.2026)."""
    st = load()
    _csv(Path(st.inbox_dirs[0]) / "trade_terms_20261015.csv", TERMS)
    old = time.time() - 3600
    os.utime(Path(st.inbox_dirs[0]) / "trade_terms_20261015.csv", (old, old))
    cfg = Path(st.private_adapter_config) / "ocp_cash.json"
    cfg.write_text(json.dumps({"derivative_types": {"FAKE_FWD": "fx_forward"}}), encoding="utf-8")
    with store.connect(store_url) as con:
        assert scan.scan(con, load()).files.get("accepted") == 4
    d1 = {(x["book_id"], x["ccy"]): x for x in _run(store_url, ctx)["published"]["by_book"]}[("D1", "USD")]
    assert d1["parts"]["base"] == 155_000 and d1["parts"]["derivatives"] == 260_000
    assert d1["fallback"] == ["fx_forward"] and d1["no_pv"] == ["fx_forward"]
    assert [i["marker"] for i in d1["contributions"]["derivatives"]["items"]] == ["номинал, PV нет"]


def test_the_replay_the_adapter_asks_for_is_checked_like_the_owners(home: Path, store_url: str, monkeypatch) -> None:
    """The adapter's choices carry the replay period and source: after the save they are checked as the replay page
    checks the owner's (a bad period, an undeclared source), the saved file stays, nothing is started."""
    from types import SimpleNamespace

    from desk import inbox_replay
    from desk.api import adapter_routes

    st = load()
    started = []
    monkeypatch.setattr(inbox_replay, "start", lambda *a, **kw: started.append(a))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    ok = {"source": "typed_ocp", "from": "2026-10-15", "to": "2026-10-15"}
    assert adapter_routes._start_replay(st, req, ok, "why")["started"] is True and len(started) == 1
    for bad in ({**ok, "from": "2026-10-16"},  # first after last
                {**ok, "from": "2020-01-01"},  # longer than the page allows
                {**ok, "source": "nope"}, {**ok, "from": "x"}, {**ok, "source": None}, None):
        out = adapter_routes._start_replay(st, req, bad, "why")
        assert out["started"] is False and out["error"], bad
    assert len(started) == 1
