"""Intraday mode (R11): the autopilot's intraday step on the stub hub of slice S3. Today's book (the latest position
sets, carried forward) valued on the hub's newest intraday cut of today, at most one hub question per half-hour slot,
on the preliminary close in the evening, superseded by the eod_final run the next morning; one intraday run per day;
the flash PnL basis named when it is not the previous business day's close; the setting off. The cuts are the
2026-10-15 fixture shifted to other dates and cuts (``test_market_history.shifted``). Synthetic data only."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from datetime import date, datetime
from pathlib import Path

import pytest
import test_slice_s3
from fastapi.testclient import TestClient
from test_autopilot import cfg, put, steps
from test_market_history import shifted

from desk import cli, store
from desk.api import create_app
from desk.autopilot import Autopilot
from desk.inbox import scan
from desk.inbox.timesem import MSK
from desk.marketdata import cache
from desk.marketdata.bundle import MANIFEST_INDEX, SnapshotDir, previous_market
from desk.settings import JSON_KEYS, Settings, check_settings_doc, load

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
STATIC = test_slice_s3.STATIC
server, stub = test_slice_s3.server, test_slice_s3.stub  # the stub hub's fixtures
FRI, MON = date(2026, 10, 16), date(2026, 10, 19)  # positions_20261016.csv: start of day 16.10, valid 15.10


@pytest.fixture(autouse=True)
def _fresh_process():
    scan._handles.clear()
    scan._last_seen.clear()
    yield
    scan._handles.clear()
    scan._last_seen.clear()


def cut_doc(d: date, cut: str, cut_ts: str, step: int) -> dict:
    """The fixture market snapshot as a ``cut`` of ``d`` (prices and the KBD moved by ``step``); an intraday cut
    carries its quotes in ``bond_quotes_intraday`` as the hub's (latest trade at the cut)."""
    doc = shifted(d.isoformat(), step, known_at=cut_ts, light=False)
    doc.update(cut_type=cut, cut_ts=cut_ts, supersedes=None)
    if cut == "intraday":
        doc["data"]["bond_quotes_intraday"] = [{
            "secid": q["secid"], "board": q["board"], "ts": cut_ts, "source_ts": cut_ts, "latency": None,
            "bid_pct": None, "offer_pct": None, "close_price_pct": None,
            "last_pct": q.get("legal_close_pct") or q.get("close_pct") or q.get("waprice_pct"),
            "waprice_pct": q.get("waprice_pct"), "market_price_pct": q.get("market_price2_pct"),
            "num_trades": q.get("num_trades"), "vol_today": q.get("volume_bonds"), "val_today": q.get("value_ccy"),
            "trading_status": "T", "trading_session": None, "source_analytics": {}}
            for q in doc["data"]["bond_quotes"]]
        doc["data"]["bond_quotes"] = []
    return doc


def body(doc: dict) -> tuple[str, bytes]:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest(), gzip.compress(raw)


def publish(hub, d: date, cut: str, cut_ts: str, step: int) -> str:
    """A cut on the stub hub: its list row and its bundle."""
    sid, gz = body(cut_doc(d, cut, cut_ts, step))
    hub.bundles[sid] = gz
    hub.rows.append({"snapshot_id": sid, "kind": "market", "cut_type": cut, "business_date": d.isoformat(),
                     "cut_ts": cut_ts, "static_snapshot_id": STATIC, "published_at": cut_ts, "supersedes": None})
    return sid


def asked(hub, cut: str) -> int:
    return sum(f"cut_type={cut}" in r for r in hub.requests)


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=MSK)


def run_rows(store_url: str) -> list[tuple]:
    with store.connect(store_url) as con:
        return con.execute("SELECT run_id, business_date, run_type, cut_type, prev_date, prev_cut_type,"
                           " supersedes_run_id FROM run ORDER BY run_id").fetchall()


def test_intraday_day_preliminary_evening_and_final_next_morning(stub, tmp_path, store_url):
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    clock = {"t": at(FRI, 9, 0)}
    ap = Autopilot(settings_loader=lambda: s, now=lambda: clock["t"])
    by = steps(ap.run_cycle())
    assert by["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    assert by["intraday"] == ("skipped", "before 10:00 MSK") and asked(stub, "intraday") == 0

    a = publish(stub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)  # the 10:00 cut
    clock["t"] = at(FRI, 10, 6)
    stub.requests.clear()
    st = ap.run_cycle()
    by = steps(st)
    assert by["intraday"] == ("ok", "16.10 10:00 МСК · внутри дня: run 2 (new cut)"), by["intraday"]
    assert asked(stub, "intraday") == 1 and any(f"/{a}/bundle" in r for r in stub.requests)
    assert st["message"] == "Пересчитано: 16.10 10:00 МСК · внутри дня"
    assert {k: st["latest_run"][k] for k in ("run_id", "run_type", "cut_type", "label", "pnl_basis")} == {
        "run_id": "2", "run_type": "INTRADAY", "cut_type": "intraday", "label": "16.10 10:00 МСК · внутри дня",
        "pnl_basis": None}
    with store.connect(store_url) as con:
        rtype, bd, cut, mid, prev, pcut, cut_ts, pnl = con.execute(
            "SELECT run_type, business_date, cut_type, market_snapshot_id, prev_date, prev_cut_type, cut_ts,"
            " totals->'_total'->'pnl'->>'total' FROM run WHERE run_id = 2").fetchone()
        assert (rtype, bd, cut, mid, prev, pcut) == ("INTRADAY", FRI, "intraday", a, date(2026, 10, 15), "eod_final")
        assert cut_ts == at(FRI, 10, 0) and pnl is not None
        assert store.current_sets(con, FRI)[0] == con.execute("SELECT position_set_ids FROM run WHERE run_id = 1"
                                                              ).fetchone()[0]  # the book of 15.10 carried forward

    api = TestClient(create_app(store_url))
    d = api.get("/api/v1/runs/default").json()
    assert (d["run_id"], d["is_current"], d["is_default"], d["label"], d["cut_ts"]) == (
        2, True, False, "16.10 10:00 МСК · внутри дня", "2026-10-16T07:00:00Z")
    assert api.get("/api/v1/runs/default?business_date=2026-10-15").json()["run_id"] == 1
    src = api.get("/api/v1/compat/desk").json()["source"]
    assert (src["runId"], src["runType"], src["cutType"], src["slice"], src["pnlBasis"]) == (
        2, "INTRADAY", "intraday", "16.10 10:00 МСК · внутри дня", None)

    stub.requests.clear()
    clock["t"] = at(FRI, 10, 20)  # the same half-hour slot: the hub is not asked again
    by = steps(ap.run_cycle())
    assert by["intraday"][0] == "ok" and by["intraday"][1].startswith("up to date: run 2 on 16.10 10:00 МСК")
    assert asked(stub, "intraday") == 0
    b = publish(stub, FRI, "intraday", "2026-10-16T07:30:00Z", 2)  # the 10:30 cut
    clock["t"] = at(FRI, 10, 34)  # still the slot of 10:05
    assert steps(ap.run_cycle())["intraday"][0] == "ok" and asked(stub, "intraday") == 0 and len(run_rows(store_url)) == 2
    clock["t"] = at(FRI, 10, 36)
    by = steps(ap.run_cycle())
    assert by["intraday"] == ("ok", "16.10 10:30 МСК · внутри дня: run 3 (new cut); removed intraday run 2")
    assert asked(stub, "intraday") == 1
    with store.connect(store_url) as con:  # one intraday run per day, its rows gone with it
        assert [r for (r,) in con.execute("SELECT run_id FROM run WHERE run_type = 'INTRADAY'")] == [3]
        assert con.execute("SELECT count(*) FROM run_position WHERE run_id = 2").fetchone()[0] == 0
        assert con.execute("SELECT market_snapshot_id FROM run WHERE run_id = 3").fetchone()[0] == b
        with pytest.raises(Exception, match="cannot change"):  # an EOD run stays immutable
            con.execute("DELETE FROM run WHERE run_id = 1")

    stub.requests.clear()
    clock["t"] = at(FRI, 11, 6)  # a new slot, no newer cut: one question, no run
    by = steps(ap.run_cycle())
    assert by["intraday"] == ("ok", "up to date: run 3 on 16.10 10:30 МСК · внутри дня (asked the hub)")
    assert asked(stub, "intraday") == 1 and len(run_rows(store_url)) == 2

    p = publish(stub, FRI, "eod_preliminary", "2026-10-16T16:45:00Z", 3)  # the evening's preliminary close
    stub.requests.clear()
    clock["t"] = at(FRI, 19, 50)
    st = ap.run_cycle()
    assert steps(st)["intraday"] == ("ok", "16.10 · закрытие предв.: run 4 (new cut)")
    assert asked(stub, "eod_preliminary") == 1 and asked(stub, "intraday") == 0
    assert st["latest_run"]["run_id"] == "4" and st["latest_run"]["label"] == "16.10 · закрытие предв."
    assert run_rows(store_url)[-1] == (4, FRI, "EOD", "eod_preliminary", date(2026, 10, 15), "eod_final", None)
    stub.requests.clear()
    clock["t"] = at(FRI, 20, 40)  # a run on the preliminary cut exists: the hub is not asked for it again
    assert steps(ap.run_cycle())["intraday"][0] == "ok" and asked(stub, "eod_preliminary") == 0
    runs = {r["run_id"]: r for r in api.get("/api/v1/runs").json()["runs"]}
    assert runs[4]["is_current"] and runs[4]["is_default"] and not runs[3]["is_current"]
    assert runs[3]["label"] == "16.10 10:30 МСК · внутри дня" and runs[4]["label"] == "16.10 · закрытие предв."

    publish(stub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    clock["t"] = at(MON, 8, 30)  # Monday morning: the final close of Friday supersedes the preliminary run
    st = ap.run_cycle()
    by = steps(st)
    assert by["eod"] == ("ok", "2026-10-16 run 5 (market snapshot changed)"), by["eod"]
    assert by["intraday"] == ("skipped", "before 10:00 MSK")
    assert run_rows(store_url)[-1] == (5, FRI, "EOD", "eod_final", date(2026, 10, 15), "eod_final", 4)
    assert st["latest_run"]["run_id"] == "5" and st["latest_run"]["label"] == "16.10 · закрытие"
    assert api.get("/api/v1/runs/default").json()["run_id"] == 5
    assert SnapshotDir(tmp_path / "cache").market_id("2026-10-16", "eod_preliminary") == p  # pruned only later


def test_pnl_against_an_earlier_close_is_labelled(stub, tmp_path, store_url):
    """Monday's intraday run without Friday's final close: the PnL is against Thursday's close, and says so."""
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    publish(stub, MON, "intraday", "2026-10-19T07:00:00Z", 1)
    ap = Autopilot(settings_loader=lambda: s, now=lambda: at(MON, 10, 10))
    st = ap.run_cycle()
    by = steps(st)
    assert by["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    assert by["intraday"] == ("ok", ("19.10 10:00 МСК · внутри дня: run 2 (new cut); "
                                    "PnL от 15.10 (закрытие): нет закрытия 16.10")), by["intraday"]
    assert st["latest_run"]["pnl_basis"] == "PnL от 15.10 (закрытие): нет закрытия 16.10"
    with store.connect(store_url) as con:
        basis = con.execute("SELECT lineage->'pnl_basis' FROM run WHERE run_id = 2").fetchone()[0]
    assert basis == {"prev_date": "2026-10-15", "prev_cut_type": "eod_final", "expected_date": "2026-10-16",
                     "label": "PnL от 15.10 (закрытие): нет закрытия 16.10"}
    api = TestClient(create_app(store_url))
    assert api.get("/api/v1/runs/default").json()["pnl_basis"] == basis["label"]
    assert api.get("/api/v1/compat/desk").json()["source"]["pnlBasis"] == basis["label"]


def test_pnl_against_a_preliminary_close_when_there_is_no_final(tmp_path, store_url):
    """No earlier eod_final at all: the flash PnL is measured against the preliminary close, labelled."""
    folder = tmp_path / "cache"
    folder.mkdir()
    shutil.copy(FIX / f"{STATIC}.json.gz", folder)
    sid, gz = body(cut_doc(date(2026, 10, 15), "eod_preliminary", "2026-10-15T16:45:00Z", 0))
    (folder / f"{sid}.json.gz").write_bytes(gz)
    iid, gz = body(cut_doc(FRI, "intraday", "2026-10-16T08:00:00Z", 1))
    (folder / f"{iid}.json.gz").write_bytes(gz)
    assert previous_market(SnapshotDir(folder), FRI.isoformat()) == ("2026-10-15", "eod_preliminary")
    probe = tmp_path / "probe"  # the previous day's preliminary close beats an older final; its own final beats it
    shutil.copytree(folder, probe)
    for day, cut in ((date(2026, 10, 14), "eod_final"), (date(2026, 10, 15), "eod_final")):
        pid, pgz = body(cut_doc(day, cut, f"{day}T05:00:00Z", 0))
        (probe / f"{pid}.json.gz").write_bytes(pgz)
        assert previous_market(SnapshotDir(probe), FRI.isoformat()) == (
            ("2026-10-15", "eod_preliminary") if day.day == 14 else ("2026-10-15", "eod_final"))
    with store.connect(store_url) as con:
        store.load_positions(con, [("P1", "B1", "SU26238RMFS4", 100.0)], valid_date=date(2026, 10, 15),
                             source_name="t.csv", source_sha256="0" * 64)
    lines: list[str] = []
    rid = cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=folder), FRI.isoformat(), store_url=store_url, cut_type="intraday",
                      market_snapshot_id=iid, run_type="INTRADAY", out=lines.append)
    with store.connect(store_url) as con:
        prev, pcut, basis, pnl = con.execute(
            "SELECT prev_date, prev_cut_type, lineage->'pnl_basis'->>'label', totals->'_total'->'pnl'->>'total'"
            " FROM run WHERE run_id = %s", (rid,)).fetchone()
    assert (prev, pcut) == (date(2026, 10, 15), "eod_preliminary") and pnl is not None
    assert basis == "PnL от 15.10 (закрытие предв.): нет окончательного закрытия 15.10"
    assert any(x.startswith("WARN flash PnL basis: PnL от 15.10 (закрытие предв.)") for x in lines)


def test_setting_off_and_weekend(stub, tmp_path, store_url):
    s = cfg(tmp_path, store_url, stub, intraday=False)
    put(tmp_path, "positions_20261016.csv")
    publish(stub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)
    by = steps(Autopilot(settings_loader=lambda: s, now=lambda: at(FRI, 12, 0)).run_cycle())
    assert by["intraday"] == ("skipped", "off in the settings (intraday)") and asked(stub, "intraday") == 0
    on = cfg(tmp_path, store_url, stub)
    by = steps(Autopilot(settings_loader=lambda: on, now=lambda: at(date(2026, 10, 17), 12, 0)).run_cycle())
    assert by["intraday"] == ("skipped", "2026-10-17 is not a business day") and asked(stub, "intraday") == 0
    assert [r[2] for r in run_rows(store_url)] == ["EOD"]


def test_intraday_setting(tmp_path):
    env = {"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "none.env")}
    assert load(env).intraday is True and ("intraday", "on") in load(env).describe()
    assert load({**env, "DESK_INTRADAY": "0"}).intraday is False
    (tmp_path / "settings.json").write_text('{"intraday": false}', encoding="utf-8")
    assert load(env).intraday is False and "intraday" in JSON_KEYS
    assert check_settings_doc({"intraday": "no"}) == ["intraday: must be true or false"]


def test_cache_prune_keeps_finals_today_and_shown_runs(tmp_path):
    folder = tmp_path / "cache"
    folder.mkdir()
    for p in FIX.glob("*.json.gz"):
        shutil.copy(p, folder / p.name)
    ids = {}
    for name, d, cut, ts in (("old", date(2026, 10, 12), "intraday", "2026-10-12T07:00:00Z"),
                             ("old_pre", date(2026, 10, 12), "eod_preliminary", "2026-10-12T16:45:00Z"),
                             ("kept", date(2026, 10, 13), "eod_preliminary", "2026-10-13T16:45:00Z"),
                             ("new", FRI, "intraday", "2026-10-16T07:00:00Z")):
        ids[name], gz = body(cut_doc(d, cut, ts, 0))
        (folder / f"{ids[name]}.json.gz").write_bytes(gz)
    (folder / f"{ids['old']}.hub.json").write_text("{}")
    gone = cache.prune(folder, "2026-10-14", keep={ids["kept"]})
    assert sorted(gone) == sorted([ids["old"], ids["old_pre"]])
    left = {p.name.split(".")[0] for p in folder.iterdir() if p.name != MANIFEST_INDEX}  # the listing's index aside
    assert left == {test_slice_s3.MARKET, STATIC, ids["kept"], ids["new"]}
