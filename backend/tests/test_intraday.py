"""Intraday mode (R11): the autopilot's intraday step on the stub hub of slice S3. Today's book (the latest position
sets, carried forward) valued on the hub's newest intraday cut of today, at most one hub question per 10-minute slot,
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
from desk.marketdata.hub import HubClient, PythonTransport, TransportError
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
    assert by["morning"] == ("ok", "16.10 · закрытие 15.10: run 2 (no run of 2026-10-16 yet)")  # #21

    a = publish(stub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)  # the 10:00 cut
    clock["t"] = at(FRI, 10, 6)
    stub.requests.clear()
    st = ap.run_cycle()
    by = steps(st)
    assert by["intraday"] == ("ok", "16.10 10:00 МСК · внутри дня: run 3 (new cut); removed intraday run 2"), \
        by["intraday"]  # the morning run of 16.10 replaced by the first cut (#21)
    assert by["morning"] == ("skipped", "2026-10-16 has a run on its own market")
    assert asked(stub, "intraday") == 1 and any(f"/{a}/bundle" in r for r in stub.requests)
    assert st["message"] == "Пересчитано: 16.10 10:00 МСК · внутри дня"
    assert {k: st["latest_run"][k] for k in ("run_id", "run_type", "cut_type", "label", "pnl_basis")} == {
        "run_id": "3", "run_type": "INTRADAY", "cut_type": "intraday", "label": "16.10 10:00 МСК · внутри дня",
        "pnl_basis": None}
    with store.connect(store_url) as con:
        rtype, bd, cut, mid, prev, pcut, cut_ts, pnl = con.execute(
            "SELECT run_type, business_date, cut_type, market_snapshot_id, prev_date, prev_cut_type, cut_ts,"
            " totals->'_total'->'pnl'->>'total' FROM run WHERE run_id = 3").fetchone()
        assert (rtype, bd, cut, mid, prev, pcut) == ("INTRADAY", FRI, "intraday", a, date(2026, 10, 15), "eod_final")
        assert cut_ts == at(FRI, 10, 0) and pnl is not None
        assert store.current_sets(con, FRI)[0] == con.execute("SELECT position_set_ids FROM run WHERE run_id = 1"
                                                              ).fetchone()[0]  # the book of 15.10 carried forward

    api = TestClient(create_app(store_url))
    d = api.get("/api/v1/runs/default").json()
    assert (d["run_id"], d["is_current"], d["is_default"], d["label"], d["cut_ts"]) == (
        3, True, False, "16.10 10:00 МСК · внутри дня", "2026-10-16T07:00:00Z")
    assert api.get("/api/v1/runs/default?business_date=2026-10-15").json()["run_id"] == 1
    src = api.get("/api/v1/compat/desk").json()["source"]
    assert (src["runId"], src["runType"], src["cutType"], src["slice"], src["pnlBasis"]) == (
        3, "INTRADAY", "intraday", "16.10 10:00 МСК · внутри дня", None)

    stub.requests.clear()
    clock["t"] = at(FRI, 10, 12)  # the same 10-minute slot: the hub is not asked again
    by = steps(ap.run_cycle())
    assert by["intraday"][0] == "ok" and by["intraday"][1].startswith("up to date: run 3 on 16.10 10:00 МСК")
    assert asked(stub, "intraday") == 0
    b = publish(stub, FRI, "intraday", "2026-10-16T07:30:00Z", 2)  # the 10:30 cut
    clock["t"] = at(FRI, 10, 14)  # still the slot of 10:05
    assert steps(ap.run_cycle())["intraday"][0] == "ok" and asked(stub, "intraday") == 0 and len(run_rows(store_url)) == 2
    clock["t"] = at(FRI, 10, 36)
    by = steps(ap.run_cycle())
    assert by["intraday"] == ("ok", "16.10 10:30 МСК · внутри дня: run 4 (new cut); removed intraday run 3")
    assert asked(stub, "intraday") == 1
    with store.connect(store_url) as con:  # one intraday run per day, its rows gone with it
        assert [r for (r,) in con.execute("SELECT run_id FROM run WHERE run_type = 'INTRADAY'")] == [4]
        assert con.execute("SELECT count(*) FROM run_position WHERE run_id = 3").fetchone()[0] == 0
        assert con.execute("SELECT market_snapshot_id FROM run WHERE run_id = 4").fetchone()[0] == b
        with pytest.raises(Exception, match="cannot change"):  # an EOD run stays immutable
            con.execute("DELETE FROM run WHERE run_id = 1")

    stub.requests.clear()
    clock["t"] = at(FRI, 11, 6)  # a new slot, no newer cut: one question, no run
    by = steps(ap.run_cycle())
    assert by["intraday"] == ("ok", "up to date: run 4 on 16.10 10:30 МСК · внутри дня (asked the hub)")
    assert asked(stub, "intraday") == 1 and len(run_rows(store_url)) == 2

    p = publish(stub, FRI, "eod_preliminary", "2026-10-16T16:45:00Z", 3)  # the evening's preliminary close
    stub.requests.clear()
    clock["t"] = at(FRI, 19, 50)
    st = ap.run_cycle()
    assert steps(st)["intraday"] == ("ok", "16.10 · закрытие предв.: run 5 (new cut)")
    assert asked(stub, "eod_preliminary") == 1 and asked(stub, "intraday") == 0
    assert st["latest_run"]["run_id"] == "5" and st["latest_run"]["label"] == "16.10 · закрытие предв."
    assert run_rows(store_url)[-1] == (5, FRI, "EOD", "eod_preliminary", date(2026, 10, 15), "eod_final", None)
    stub.requests.clear()
    clock["t"] = at(FRI, 20, 40)  # a run on the preliminary cut exists: the hub is not asked for it again
    assert steps(ap.run_cycle())["intraday"][0] == "ok" and asked(stub, "eod_preliminary") == 0
    runs = {r["run_id"]: r for r in api.get("/api/v1/runs").json()["runs"]}
    assert runs[5]["is_current"] and runs[5]["is_default"] and not runs[4]["is_current"]
    assert runs[4]["label"] == "16.10 10:30 МСК · внутри дня" and runs[5]["label"] == "16.10 · закрытие предв."

    publish(stub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    clock["t"] = at(MON, 8, 30)  # Monday morning: the final close of Friday supersedes the preliminary run
    st = ap.run_cycle()
    by = steps(st)
    assert by["eod"] == ("ok", "2026-10-16 run 6 (market snapshot changed)"), by["eod"]
    assert by["intraday"] == ("skipped", "before 10:00 MSK")
    assert by["morning"] == ("ok", "19.10 · закрытие 16.10: run 7 (no run of 2026-10-19 yet)")  # Monday's book (#21)
    assert run_rows(store_url)[-2] == (6, FRI, "EOD", "eod_final", date(2026, 10, 15), "eod_final", 5)
    assert st["latest_run"]["run_id"] == "7" and st["latest_run"]["label"] == "19.10 · закрытие 16.10"
    assert api.get("/api/v1/runs/default?business_date=2026-10-16").json()["run_id"] == 6
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
    # no intraday run with the setting off nor on a weekend; the morning run of Friday (#21) needs no hub cut
    assert [(r[2], r[3]) for r in run_rows(store_url)] == [("EOD", "eod_final"), ("INTRADAY", "prev_close")]


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


def test_failed_sync_of_an_old_date_does_not_block_todays_intraday_run(stub, tmp_path, store_url):
    """#20: the hub sync of one old date fails (a body that does not verify), today's intraday cut is on the hub:
    the intraday run is still made, and the failed date is named apart in the sync step and the diagnostics."""
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    old = publish(stub, date(2026, 10, 14), "eod_final", "2026-10-15T05:00:00Z", -1)
    stub.bundles[old] = gzip.compress(b"not the snapshot")  # 14.10 cannot be synced
    publish(stub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)
    ap = Autopilot(settings_loader=lambda: s, now=lambda: at(FRI, 10, 6))
    st = ap.run_cycle()
    by = steps(st)
    assert by["sync"][0] == "error" and "failed 2026-10-14 (" in by["sync"][1], by["sync"]
    assert "pulled 2026-10-15" in by["sync"][1]
    sync = next(x for x in st["steps"] if x["name"] == "sync")
    assert sync["failed_dates"] == ["2026-10-14"]
    assert by["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    assert by["intraday"] == ("ok", "16.10 10:00 МСК · внутри дня: run 2 (new cut)"), by["intraday"]
    intraday = next(x for x in st["steps"] if x["name"] == "intraday")
    assert intraday["reason"] == "ran"
    assert [r[2] for r in run_rows(store_url)] == ["EOD", "INTRADAY"]
    diag = ap.diagnostics()
    assert diag["hub_failed_last_cycle"] is False and list(diag["hub_failed_dates"]) == ["2026-10-14"]
    app = create_app(store_url)
    app.state.autopilot = ap
    api = {x["name"]: x for x in TestClient(app).get("/api/v1/autopilot/status").json()["steps"]}
    assert api["intraday"]["reason"] == "ran" and api["sync"]["failed_dates"] == ["2026-10-14"]
    assert api["eod"]["reason"] is None and api["eod"]["failed_dates"] is None

    stub.bundles[old] = body(cut_doc(date(2026, 10, 14), "eod_final", "2026-10-15T05:00:00Z", -1))[1]  # fixed
    stub.rows = [r for r in stub.rows if r["snapshot_id"] != old]
    fixed = publish(stub, date(2026, 10, 14), "eod_final", "2026-10-15T05:00:00Z", -1)
    assert fixed == old
    ap._now = lambda: at(FRI, 10, 17)  # after the failed date's 10-minute wait (#146)
    st = ap.run_cycle()  # the date failed last cycle: asked again, and the failure is forgotten
    assert steps(st)["sync"][0] == "ok" and "failed" not in steps(st)["sync"][1]
    assert "2026-10-14" in SnapshotDir(tmp_path / "cache").market_dates()
    assert ap.diagnostics()["hub_failed_dates"] == {} and ap.diagnostics()["hub_problem_dates"] == {}


def test_permanently_broken_dates_back_off_and_do_not_hold_up_the_other_dates(stub, tmp_path, store_url):
    """#146: two hub dates whose snapshots never verify do not take the ``MAX_DATE_FAILURES`` limit in every cycle:
    after failing they wait for a retry that grows with the attempts, the other dates load in the next cycle and
    the history step runs; the broken dates are retried after their wait, listed with their attempts and the
    time of their next attempt, and load once the hub fixes them."""
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    bad = {}
    for d, step in ((date(2026, 10, 14), -1), (date(2026, 10, 13), -2)):  # the two newest dates not cached yet
        bad[d.isoformat()] = publish(stub, d, "eod_final", f"{d}T05:00:00Z", step)
        stub.bundles[bad[d.isoformat()]] = gzip.compress(b"not the snapshot")
    good = [date(2026, 10, 12), date(2026, 10, 9)]  # older than the broken ones: asked after them
    for d, step in zip(good, (-3, -4), strict=True):
        publish(stub, d, "eod_final", f"{d}T05:00:00Z", step)
    clock = {"t": at(FRI, 9, 0)}
    ap = Autopilot(settings_loader=lambda: s, now=lambda: clock["t"])
    by = steps(ap.run_cycle())  # 15.10, then the two broken dates take the limit: the good ones next cycle
    assert by["sync"][0] == "error" and "2 next cycle" in by["sync"][1], by["sync"]
    assert by["eod"] == ("ok", "2026-10-15 run 1 (no default run)")

    def cached() -> set[str]:
        return set(SnapshotDir(tmp_path / "cache").market_dates())

    clock["t"] = at(FRI, 9, 1)
    st = ap.run_cycle()  # the broken dates wait for their retry: the good dates load, the history step runs
    by = steps(st)
    assert {d.isoformat() for d in good} <= cached(), by["sync"]
    assert by["sync"][0] == "ok" and "next cycle" not in by["sync"][1], by["sync"]
    assert by["history"][0] != "skipped", by["history"]
    sync = next(x for x in st["steps"] if x["name"] == "sync")
    assert sync["failed_dates"] == []
    assert [(p["date"], p["attempts"], p["next_attempt"]) for p in sync["problem_dates"]] == [
        ("2026-10-13", 1, "2026-10-16T09:10:00+03:00"), ("2026-10-14", 1, "2026-10-16T09:10:00+03:00")]
    assert all(p["error"].startswith("SnapshotError: ") for p in sync["problem_dates"])
    diag = ap.diagnostics()["hub_problem_dates"]
    assert list(diag) == ["2026-10-13", "2026-10-14"] and diag["2026-10-14"]["attempts"] == 1

    stub.requests.clear()
    clock["t"] = at(FRI, 9, 9)
    ap.run_cycle()  # still waiting: the hub is not asked for them
    assert not any(f"/{sid}/bundle" in r for r in stub.requests for sid in bad.values())

    clock["t"] = at(FRI, 9, 11)
    st = ap.run_cycle()  # retried after 10 minutes, still broken: the next wait is 30 minutes
    sync = next(x for x in st["steps"] if x["name"] == "sync")
    assert sync["status"] == "error" and sync["failed_dates"] == ["2026-10-13", "2026-10-14"]
    assert [(p["attempts"], p["next_attempt"]) for p in sync["problem_dates"]] == [
        (2, "2026-10-16T09:41:00+03:00")] * 2

    old = bad["2026-10-14"]  # the hub fixes 14.10
    stub.bundles[old] = body(cut_doc(date(2026, 10, 14), "eod_final", "2026-10-14T05:00:00Z", -1))[1]
    clock["t"] = at(FRI, 9, 42)
    st = ap.run_cycle()
    sync = next(x for x in st["steps"] if x["name"] == "sync")
    assert "2026-10-14" in cached() and sync["failed_dates"] == ["2026-10-13"]
    assert [(p["date"], p["attempts"], p["next_attempt"]) for p in sync["problem_dates"]] == [
        ("2026-10-13", 3, "2026-10-16T11:42:00+03:00")]
    assert list(ap.diagnostics()["hub_problem_dates"]) == ["2026-10-13"]
    app = create_app(store_url)
    app.state.autopilot = ap
    api = {x["name"]: x for x in TestClient(app).get("/api/v1/autopilot/status").json()["steps"]}
    assert [p["date"] for p in api["sync"]["problem_dates"]] == ["2026-10-13"]
    assert api["eod"]["problem_dates"] is None


def test_intraday_waits_for_the_hub_with_a_reason(stub, tmp_path, store_url):
    """The hub down: the intraday step says why (a machine-readable ``reason`` for the header, #22)."""
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    ap = Autopilot(settings_loader=lambda: s, now=lambda: at(FRI, 9, 0))
    ap.run_cycle()
    stub.fail = 1000
    ap._now = lambda: at(FRI, 10, 6)
    st = ap.run_cycle()  # the sync found the hub down: the intraday step does not ask it again this cycle (#147)
    intraday = next(x for x in st["steps"] if x["name"] == "intraday")
    assert intraday["status"] == "skipped" and intraday["reason"] == "hub_error", intraday
    assert ap.diagnostics()["hub_failed_last_cycle"] is True


@pytest.mark.parametrize("down", ["503", "no answer"])
def test_hub_down_after_the_date_list_costs_one_retry_series_a_cycle(stub, tmp_path, store_url, monkeypatch, down):
    """#147: the hub answers the date list, then fails every request after the client's retries (a 503, or no
    answer at all): the cycle spends one retry series on it, not one per step (the sync's dates, intraday, history,
    backfill). The later steps do not ask the hub (``hub_error``) and ask again next cycle. A 503 for the date is
    the date's failure too (its retry waits, #146); no answer is the hub's alone: no wait for the date."""
    s = cfg(tmp_path, store_url, stub)
    (tmp_path / "cache").mkdir()
    for p in FIX.glob("*.json.gz"):  # 15.10 cached: the scan and the EOD run go on without the hub
        shutil.copy(p, tmp_path / "cache" / p.name)
    put(tmp_path, "positions_20261016.csv")
    publish(stub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)
    stub.page = 100  # the date list in one request
    listed, get, refused = cache.hub_dates, PythonTransport.get, []

    def no_answer(self, url, *a, **kw):
        refused.append(url)
        raise TransportError("connection refused")

    def then_down(client, *a, **kw):
        dates = list(listed(client, *a, **kw))
        if down == "503":  # the list answered: the hub goes down
            stub.fail = 10**6
        else:
            monkeypatch.setattr(PythonTransport, "get", no_answer)
        yield from dates

    monkeypatch.setattr(cache, "hub_dates", then_down)
    ap = Autopilot(settings_loader=lambda: s, now=lambda: at(FRI, 10, 6))
    st = ap.run_cycle()
    failed = 10**6 - stub.fail if down == "503" else len(refused)  # requests the hub did not serve
    assert 0 < failed <= HubClient.retries + 1, f"{failed} failed requests: more than one retry series"
    by = steps(st)
    assert by["sync"][0] == "error" and "unreachable after" in by["sync"][1], by["sync"]
    assert by["eod"] == ("ok", "2026-10-15 run 1 (no default run)")  # the cycle goes on with the cache
    intraday = next(x for x in st["steps"] if x["name"] == "intraday")
    assert intraday["status"] == "skipped" and intraday["reason"] == "hub_error", intraday
    assert by["history"][0] == "skipped" and st["message"] == "Ошибки: sync", st["message"]
    if down == "503":  # the date failed: it waits for its retry (#146)
        assert [(p["date"], p["attempts"]) for p in ap._problem_dates()] == [("2026-10-15", 1)]
    else:  # the hub down is not the date's fault: no wait for it
        assert not ap._sync_retry

    monkeypatch.setattr(cache, "hub_dates", listed)
    monkeypatch.setattr(PythonTransport, "get", get)
    stub.fail = 0  # back up: the next cycle asks again
    st = ap.run_cycle()
    by = steps(st)
    assert by["sync"][0] == "ok", by["sync"]
    assert by["intraday"][0] == "ok" and "внутри дня: run 3 (new cut)" in by["intraday"][1], by["intraday"]


def test_a_date_the_hub_always_answers_503_backs_off_and_the_other_dates_sync(stub, tmp_path, store_url):
    """#147 with #146: the hub answers the list, but one date's bundle gets a 503 every time. The cycle stops at
    it (one retry series, the later steps do not ask the hub), the date waits for its retry and is listed in
    ``problem_dates``; the next cycle syncs the other dates, and the date is asked again after its wait."""
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    bad = publish(stub, date(2026, 10, 14), "eod_final", "2026-10-14T05:00:00Z", -1)
    stub.broken.add(bad)
    good = [date(2026, 10, 13), date(2026, 10, 12)]  # older than the broken one: asked after it
    for d, step in zip(good, (-2, -3), strict=True):
        publish(stub, d, "eod_final", f"{d}T05:00:00Z", step)
    clock = {"t": at(FRI, 9, 0)}
    ap = Autopilot(settings_loader=lambda: s, now=lambda: clock["t"])
    st = ap.run_cycle()  # 15.10 pulled, 14.10 fails: the rest next cycle
    by = steps(st)
    assert sum(f"/{bad}/" in r for r in stub.requests) == HubClient.retries + 1  # one retry series
    assert by["sync"][0] == "error" and "failed 2026-10-14" in by["sync"][1] and "2 next cycle" in by["sync"][1]
    assert by["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    assert by["history"][0] == "skipped", by["history"]

    def cached() -> set[str]:
        return set(SnapshotDir(tmp_path / "cache").market_dates())

    stub.requests.clear()
    clock["t"] = at(FRI, 9, 1)
    st = ap.run_cycle()  # 14.10 waits for its retry: the other dates sync, the hub is not asked for 14.10
    sync = next(x for x in st["steps"] if x["name"] == "sync")
    assert {d.isoformat() for d in good} <= cached(), sync["detail"]
    assert sync["status"] == "ok" and sync["failed_dates"] == [], sync
    assert not any(f"/{bad}/" in r for r in stub.requests)
    assert [(p["date"], p["attempts"], p["next_attempt"]) for p in sync["problem_dates"]] == [
        ("2026-10-14", 1, "2026-10-16T09:10:00+03:00")]
    assert sync["problem_dates"][0]["error"].startswith("HubUnavailable: ")

    clock["t"] = at(FRI, 9, 11)
    st = ap.run_cycle()  # asked again after 10 minutes, still a 503: the next wait is 30 minutes
    sync = next(x for x in st["steps"] if x["name"] == "sync")
    assert sync["failed_dates"] == ["2026-10-14"]
    assert [(p["attempts"], p["next_attempt"]) for p in sync["problem_dates"]] == [(2, "2026-10-16T09:41:00+03:00")]


def morning_rows(store_url: str) -> list[tuple]:
    with store.connect(store_url) as con:
        return con.execute("SELECT run_id, business_date, run_type, cut_type, market_snapshot_id, prev_date,"
                           " lineage->'market_close' FROM run WHERE cut_type = 'prev_close' ORDER BY run_id").fetchall()


def test_morning_run_on_the_previous_close_without_the_hub(stub, tmp_path, store_url):
    """#21: the morning of a business day D, the positions of D in the store, no cut of D on the hub (the hub even
    down): a run of D on the previous business day's close, labelled so, made once (not every cycle), made again on
    a new revision of the book; the first intraday cut of D replaces it; the next morning's close of D runs as
    before and the next day gets its own morning run. Before 07:00 only once a report of D is in (a start_of_day
    report of D: valid D-1, report date D)."""
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261015.csv")  # start_of_day 15.10: valid 14.10, nothing of 16.10 yet
    fin = publish(stub, date(2026, 10, 15), "eod_final", "2026-10-16T05:00:00Z", 0)
    clock = {"t": at(FRI, 6, 30)}
    ap = Autopilot(settings_loader=lambda: s, now=lambda: clock["t"])
    by = steps(ap.run_cycle())
    assert by["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    assert by["morning"] == ("skipped", "before 07:00 MSK") and morning_rows(store_url) == []

    stub.fail = 1000  # the hub is down: the morning run does not need it
    put(tmp_path, "positions_20261016.csv")  # the start_of_day report of 16.10 (valid 15.10): before 07:00 too
    clock["t"] = at(FRI, 6, 45)
    st = ap.run_cycle()
    by = steps(st)
    assert by["sync"][0] == "error" and by["intraday"][0] == "skipped"
    assert by["eod"] == ("ok", "2026-10-15 run 2 (positions changed)"), by["eod"]
    assert by["morning"] == ("ok", "16.10 · закрытие 15.10: run 3 (no run of 2026-10-16 yet)"), by["morning"]
    morning = next(x for x in st["steps"] if x["name"] == "morning")
    assert morning["reason"] == "ran"
    mid = SnapshotDir(tmp_path / "cache").market_id("2026-10-15")
    assert mid == fin
    assert morning_rows(store_url) == [(3, FRI, "INTRADAY", "prev_close", fin, None,
                                        {"date": "2026-10-15", "cut_type": "eod_final"})]
    lr = st["latest_run"]
    assert (lr["run_id"], lr["as_of"], lr["cut_type"], lr["label"], lr["market_close"]) == (
        "3", "2026-10-16", "prev_close", "16.10 · закрытие 15.10", {"date": "2026-10-15", "cut_type": "eod_final"})
    assert lr["pnl_basis"].startswith("нет PnL")
    api = TestClient(create_app(store_url))
    d = api.get("/api/v1/runs/default").json()
    assert (d["run_id"], d["is_current"], d["is_default"], d["label"]) == (3, True, False, "16.10 · закрытие 15.10")
    src = api.get("/api/v1/compat/desk").json()["source"]  # the header's «позиции 16.10 · рынок 15.10 · ...» (#22)
    assert (src["cutType"], src["slice"], src["marketDate"], src["marketCutType"]) == (
        "prev_close", "16.10 · закрытие 15.10", "2026-10-15", "eod_final")
    assert api.get("/api/v1/autopilot/status").json()["latest_run"]["market_close"] == {
        "date": "2026-10-15", "cut_type": "eod_final"}

    for hh, mm in ((7, 15), (7, 25), (9, 55)):  # the next cycles: up to date, no new run
        clock["t"] = at(FRI, hh, mm)
        st = ap.run_cycle()
        assert steps(st)["morning"][0] == "ok" and steps(st)["morning"][1].startswith("up to date: run 3"), st
        assert next(x for x in st["steps"] if x["name"] == "morning")["reason"] == "up_to_date"
    assert len(morning_rows(store_url)) == 1

    put(tmp_path, "positions_20261016_2.csv")  # a new revision of the book: the morning run again, the old one gone
    scan_step = ap._scan

    def backlog(*a):  # the inbox still draining (R22): the morning run waits for it, as the older eod dates
        out = scan_step(*a)
        ap._inbox_waiting, ap._sync_later = 3, 2
        return out
    ap._scan = backlog
    clock["t"] = at(FRI, 9, 56)
    st = ap.run_cycle()
    assert steps(st)["eod"] == ("ok", "2026-10-15 run 4 (positions changed)"), steps(st)["eod"]
    assert steps(st)["morning"][0] == "skipped" and next(
        x for x in st["steps"] if x["name"] == "morning")["reason"] == "inbox_backlog", steps(st)
    assert [r[0] for r in morning_rows(store_url)] == [3]
    ap._scan = scan_step
    clock["t"] = at(FRI, 9, 58)
    by = steps(ap.run_cycle())
    assert by["morning"] == ("ok", "16.10 · закрытие 15.10: run 5 (positions changed); removed run 3"), by["morning"]
    assert [r[0] for r in morning_rows(store_url)] == [5]

    stub.fail = 0
    publish(stub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)
    clock["t"] = at(FRI, 10, 6)
    st = ap.run_cycle()
    by = steps(st)
    assert by["intraday"][0] == "ok" and "removed intraday run" in by["intraday"][1], by["intraday"]
    assert by["morning"][0] == "skipped" and next(
        x for x in st["steps"] if x["name"] == "morning")["reason"] == "superseded"
    assert morning_rows(store_url) == [] and st["latest_run"]["cut_type"] == "intraday"

    publish(stub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    clock["t"] = at(MON, 8, 30)  # Monday: Friday's close runs as before, Monday gets its morning run on it
    st = ap.run_cycle()
    by = steps(st)
    assert by["eod"][0] == "ok" and "2026-10-16 run" in by["eod"][1], by["eod"]
    assert by["morning"][0] == "ok" and by["morning"][1].startswith("19.10 · закрытие 16.10: run"), by["morning"]
    assert [(r[1], r[6]) for r in morning_rows(store_url)] == [(MON, {"date": "2026-10-16", "cut_type": "eod_final"})]
    assert st["latest_run"]["label"] == "19.10 · закрытие 16.10"
