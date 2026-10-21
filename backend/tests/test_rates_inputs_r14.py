"""Owner reports R14, R15, R17 (20.10): what a run says when the hub's snapshot lacks a rates curve (R14), the key-rate
risk of a floater valued on a fallback rung (R15), and the autopilot with a late or corrected close (R17): the intraday
run measured against the previous close once it arrives, a hub correction of a cached or loaded date replacing the
superseded bundle. The 2026-10-15 fixture, edited (quote sets dropped, dates shifted, corrections). Synthetic data
only."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import shutil
from datetime import date
from pathlib import Path

import pytest
import test_slice_s3
from fastapi.testclient import TestClient
from test_autopilot import cfg, put, steps
from test_intraday import FRI, MON, STATIC, at, body, publish, run_rows
from test_market_history import FIX, MARKET, NO_API, FakeHub, market_doc, shifted, write

from desk import autopilot, cli, store
from desk.api import create_app
from desk.api.compat import risk_notes
from desk.autopilot import Autopilot
from desk.curves import rates
from desk.marketdata import history
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.settings import Settings
from desk.valuation.fallback import KEY_CURVE, _split, key_rate_floater_proxy

OFZ, CORP, KEY = "SU26238RMFS4", "RU000A0JXQ44", "RU000A109XR1"
DATE = "2026-10-15"
server, stub = test_slice_s3.server, test_slice_s3.stub  # the stub hub of slice S3


def cache_without(tmp_path: Path, *datasets: str, no_mids: str | None = None) -> Path:
    """The fixture cache with the market's quote sets of ``datasets`` dropped (``no_mids``: that dataset's quotes
    kept without a value)."""
    folder = tmp_path / "cache"
    folder.mkdir()
    shutil.copy(FIX / f"{STATIC}.json.gz", folder)
    doc = market_doc()
    doc["data"]["curve_quote_sets"] = [q for q in doc["data"]["curve_quote_sets"] if q["dataset"] not in datasets]
    for qs in doc["data"]["curve_quote_sets"]:
        if qs["dataset"] == no_mids:
            for q in qs["quotes"]:
                q["bid_pct"] = q["mid_pct"] = q["ask_pct"] = None
    write(folder, doc)
    return folder


# ---- R14: a missing curve is named, never a silent zero

@pytest.mark.parametrize("drop, codes, disc", [
    (("ruonia_ois",), ["curve_missing:ruonia_ois", "curve_unbuilt:key_rate_swap"], "RUB OFZ"),
    (("key_rate_swap",), ["curve_missing:key_rate_swap"], "RUB RUONIA"),
    ((), [], "RUB RUONIA")])
def test_missing_rates_inputs_are_named_and_the_risk_goes_where_it_says(tmp_path, drop, codes, disc):
    ctx = prepare(SnapshotDir(cache_without(tmp_path, *drop)), DATE)
    assert ctx.rates["codes"] == codes
    assert ctx.rates["status"]["ruonia_ois"] == ("missing" if "ruonia_ois" in drop else "ok")
    notes = rates.input_notes(ctx.rates, DATE)
    assert [n["code"] for n in notes] == codes
    if "ruonia_ois" in drop:
        assert notes[0]["curve"] == "RUB RUONIA" and notes[0]["text"].startswith("нет кривой RUONIA в срезе хаба за 15.10")
        assert "риск отнесён на ОФЗ (RUB OFZ)" in notes[0]["text"] and "no ruonia_ois quote set" in notes[0]["detail"]
        assert notes[1]["curve"] == "RUB KEY" and notes[1]["detail"] is None
    outs = {o.secid: o for o in value(ctx, [(s, 1000.0) for s in (OFZ, CORP, KEY)])}
    assert set(outs[OFZ].valuation.risk()) == set(outs[CORP].valuation.risk()) == {disc}
    key = outs[KEY].valuation
    if not drop:
        assert key.quality in ("exact", "assumed") and set(key.risk()) == {"RUB KEY", "RUB RUONIA"}
        return
    # R15: no key-rate projection, so core refuses the floater and the ladder values it; its projection is still on
    # RUB KEY (positive) against its discounting (negative), and summed over the curves it is the old single-curve proxy
    assert outs[KEY].reason == "core_missing_market_data" and key.quality == "market_value"
    r = key.risk()
    assert set(r) == {disc, KEY_CURVE} and sum(r[KEY_CURVE].values()) > 0 > sum(r[disc].values())
    assert math.fsum(x for bs in r.values() for x in bs.values()) == pytest.approx(key.dv01_rub)
    assert {b: r[disc][b] + r[KEY_CURVE][b] for b in key.buckets} == pytest.approx(key.buckets)
    assert any(a.startswith("key-rate floater (R15)") for a in key.assumptions)


def test_a_curve_that_cannot_be_built_is_an_error_not_a_gap(tmp_path):
    ctx = prepare(SnapshotDir(cache_without(tmp_path, no_mids="ruonia_ois")), DATE)
    assert ctx.rates["status"] == {"ruonia_ois": "error", "key_rate_swap": "no_discount", "fixings": "ok"}
    (n, _) = rates.input_notes(ctx.rates, date(2026, 10, 15))
    assert n["code"] == "curve_error:ruonia_ois" and "no usable RUONIA OIS quote" in n["detail"]
    assert n["text"].startswith("кривая RUONIA за 15.10 не построена")


def test_the_run_log_and_the_risk_pages_say_what_is_missing(tmp_path, store_url):
    folder = cache_without(tmp_path, "ruonia_ois")
    with store.connect(store_url) as con:
        store.load_positions(con, [("P1", "B1", OFZ, 100.0), ("P2", "B1", KEY, 50.0)], valid_date=date(2026, 10, 15),
                             source_name="t.csv", source_sha256="0" * 64)
    rid = cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=folder), DATE, store_url=store_url,
                      out=lambda _: None)
    with store.connect(store_url) as con:
        log = con.execute("SELECT level, message FROM run_log WHERE run_id = %s ORDER BY seq", (rid,)).fetchall()
        lineage = con.execute("SELECT lineage->'rates'->'codes' FROM run WHERE run_id = %s", (rid,)).fetchone()[0]
    assert lineage == ["curve_missing:ruonia_ois", "curve_unbuilt:key_rate_swap"]
    warn = [m for lvl, m in log if lvl == "WARN"]
    assert any(m.startswith("curve_missing:ruonia_ois: нет кривой RUONIA в срезе хаба за 15.10") for m in warn)
    assert any(m.startswith("curve_unbuilt:key_rate_swap: кривая ключевой ставки за 15.10 не строится") for m in warn)
    info = [m for lvl, m in log if lvl == "INFO"]
    assert "риск по кривым, позиций: RUB KEY 1, RUB OFZ 2" in info
    assert "флоатеры по индексам: KEY_RATE: market_value (core_missing_market_data) 1" in info
    desk = TestClient(create_app(store_url)).get("/api/v1/compat/desk").json()
    assert [n["code"] for n in desk["riskNotes"]] == lineage
    assert desk["riskNotes"][0]["text"].endswith("облигации дисконтированы по КБД, риск отнесён на ОФЗ (RUB OFZ)")
    assert set(desk["CURVES"]) == {"RUB OFZ", "RUB KEY"}  # no RUB RUONIA curve: the note says why


def test_risk_notes_of_older_runs():
    """A run made before the codes were recorded: inferred from its errors; one made before the RUONIA discounting."""
    old = {"business_date": date(2026, 10, 19), "lineage": {"logic_version": "0.1.0+v3+core0.9", "rates": {
        "curve_error": "2026-10-19: no ruonia_ois quote set dated the business date (present: [])", "fixings": {}}}}
    assert [n["code"] for n in risk_notes(old)] == ["curve_missing:ruonia_ois", "curve_unbuilt:key_rate_swap",
                                                    "run_before_ruonia"]
    kr = {"business_date": date(2026, 10, 19), "lineage": {"logic_version": "0.1.0+v4+core0.9", "rates": {
        "ruonia_ois": {}, "curve_error": "2026-10-19: no CBR meeting effective after the business date"}}}
    assert [(n["code"], n["detail"]) for n in risk_notes(kr)] == [
        ("curve_error:key_rate_swap", "2026-10-19: no CBR meeting effective after the business date")]
    assert risk_notes({"business_date": date(2026, 10, 19), "lineage": None}) == []


def test_key_rate_floater_proxy_splits_without_changing_the_total():
    pv, mac, reset = 1_000_000.0, 2.0, 0.25
    by = key_rate_floater_proxy("RUB RUONIA", mac, reset, pv)
    single = _split(reset, -reset * pv * 1e-4)
    assert {b: by["RUB RUONIA"][b] + by[KEY_CURVE][b] for b in single} == pytest.approx(single)
    assert sum(by["RUB RUONIA"].values()) == pytest.approx(-200.0)
    assert sum(by[KEY_CURVE].values()) == pytest.approx((mac - reset) * pv * 1e-4)


# ---- R17: late and corrected closes

def test_intraday_run_is_redone_when_the_previous_close_arrives_late(stub, tmp_path, store_url):
    """Monday's intraday run made before Friday's final close was published: the close arrives within the same
    half-hour slot (no newer cut), and today's run is redone against it, positions still carried from 15.10."""
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    publish(stub, MON, "intraday", "2026-10-19T07:00:00Z", 1)
    clock = {"t": at(MON, 10, 10)}
    ap = Autopilot(settings_loader=lambda: s, now=lambda: clock["t"])
    by = steps(ap.run_cycle())
    assert by["intraday"] == ("ok", ("19.10 10:00 МСК · внутри дня: run 2 (new cut); "
                                    "PnL от 15.10 (закрытие): нет закрытия 16.10")), by["intraday"]
    publish(stub, FRI, "eod_final", "2026-10-19T07:20:00Z", 3)  # Friday's close, published late
    clock["t"] = at(MON, 10, 20)
    st = ap.run_cycle()
    by = steps(st)
    assert "pulled 2026-10-16" in by["sync"][1]
    assert by["eod"] == ("ok", "2026-10-16 run 3 (no default run)")
    assert by["intraday"] == ("ok", ("19.10 10:00 МСК · внутри дня: run 4 (previous close changed); "
                                    "removed intraday run 2")), by["intraday"]
    assert run_rows(store_url)[-1] == (4, MON, "INTRADAY", "intraday", FRI, "eod_final", None)
    assert st["latest_run"]["run_id"] == "4" and st["latest_run"]["pnl_basis"] is None
    assert TestClient(create_app(store_url)).get("/api/v1/compat/desk").json()["source"]["runId"] == 4
    clock["t"] = at(MON, 10, 25)
    assert steps(ap.run_cycle())["intraday"][1].startswith("up to date: run 4")


def correction(of: str, d: str, step: int, known_at: str) -> tuple[str, bytes, dict]:
    doc = shifted(d, step, known_at=known_at, light=False)
    doc["supersedes"] = of
    sid, gz = body(doc)
    return sid, gz, doc


def test_a_hub_correction_of_a_cached_date_replaces_it_and_reruns(stub, tmp_path, store_url, monkeypatch):
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    ap = Autopilot(settings_loader=lambda: s, today=lambda: date(2026, 10, 19))
    assert steps(ap.run_cycle())["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    sid, gz, _ = correction(MARKET, DATE, 1, "2026-10-20T06:00:00Z")  # WEB-11: the hub re-publishes its history
    stub.bundles[sid] = gz
    stub.rows.append({"snapshot_id": sid, "kind": "market", "cut_type": "eod_final", "business_date": DATE,
                      "static_snapshot_id": STATIC, "published_at": "2026-10-20T06:00:00+00:00", "supersedes": MARKET})
    monkeypatch.setattr(autopilot, "RECHECK_S", 0.0)  # an hour later: the cached date is asked again
    by = steps(ap.run_cycle())
    assert "pulled 2026-10-15" in by["sync"][1] and "history tables: date 2026-10-15" in by["sync"][1]
    assert by["eod"] == ("ok", "2026-10-15 run 2 (market snapshot changed)")
    assert SnapshotDir(tmp_path / "cache").market_id(DATE) == sid
    with store.connect(store_url) as con:
        assert con.execute("SELECT snapshot_id FROM md_kbd WHERE business_date = %s", (DATE,)).fetchone()[0] == sid
        assert con.execute("SELECT market_snapshot_id FROM run WHERE run_id = 2").fetchone()[0] == sid


def test_the_later_correction_wins_when_the_cache_missed_a_link(tmp_path):
    """A <- B <- C with B never fetched: C (the later known_at) is read, whichever id sorts first."""
    folder = tmp_path / "cache"
    folder.mkdir()
    shutil.copy(FIX / f"{MARKET}.json.gz", folder)
    shutil.copy(FIX / f"{STATIC}.json.gz", folder)
    b, _, _ = correction(MARKET, DATE, 1, "2026-10-19T06:00:00Z")
    for step in range(2, 40):  # a C whose id sorts below A's: the old order (cut time, id) picked A
        c, gz, _ = correction(b, DATE, step, "2026-10-20T06:00:00Z")
        if c < MARKET:
            break
    assert c < MARKET
    (folder / f"{c}.json.gz").write_bytes(gz)
    assert SnapshotDir(folder).market_id(DATE) == c


class CorrectingHub:
    """The hub's list (with ``superseded_by``) and bundles of shifted fixture snapshots, corrections included."""

    host, url = "hub.test", "https://hub.test"

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.bodies: dict[str, bytes] = {}

    def add(self, d: str, step: int, known_at: str, supersedes: str | None = None) -> str:
        raw = json.dumps({**shifted(d, step, known_at=known_at), "supersedes": supersedes}, sort_keys=True).encode()
        sid = hashlib.sha256(raw).hexdigest()
        self.bodies[sid] = gzip.compress(raw)
        self.rows.insert(0, {"snapshot_id": sid, "business_date": d, "kind": "market", "cut_type": "eod_final",
                             "supersedes": supersedes})
        return sid

    def list_snapshots(self, kind=None, cut_type=None, business_date=None, latest=False):
        rows = [{**r, "superseded_by": next((o["snapshot_id"] for o in self.rows if o["supersedes"] == r["snapshot_id"]),
                                            None)} for r in self.rows if business_date in (None, r["business_date"])]
        yield from [r for r in rows if not r["superseded_by"]][:1] if latest else rows

    def bundle(self, sid: str) -> bytes:
        return self.bodies[sid]

    history_index = FakeHub.history_index  # a hub before 0.2.0: the history step reads the bundles (R24)


def test_history_step_reloads_a_date_the_hub_corrected(store_url, tmp_path, monkeypatch):
    hub = CorrectingHub()
    monkeypatch.setattr(autopilot, "HubClient", lambda settings: hub)
    s = Settings(home=tmp_path / "home", snapshot_cache=tmp_path / "cache", store_url=store_url,
                 hub_url="https://hub.test", history_days=5)
    ap = Autopilot(settings_loader=lambda: s, today=lambda: date(2026, 10, 19))
    a = hub.add("2026-10-14", 1, "2026-10-14T18:00:00Z")
    with store.connect(store_url) as con:
        assert ap._history(s, con) == ("ok", "loaded 2026-10-14 (history_days 5: 1 on the hub back to 2026-10-14)"
                                       + NO_API)
        c = hub.add("2026-10-14", 2, "2026-10-20T06:00:00Z", supersedes=a)
        ap._history_dates = None  # the list is read again once an hour
        assert ap._history(s, con) == ("ok", ("loaded 2026-10-14 (correction) "
                                             "(history_days 5: 1 on the hub back to 2026-10-14)" + NO_API))
        assert con.execute("SELECT snapshot_id FROM md_kbd").fetchall() == [(c,)]
        ap._history_dates = None
        assert ap._history(s, con) == ("ok", "up to date (history_days 5: 1 on the hub back to 2026-10-14)" + NO_API)
        assert history.superseded_dates(con, [("2026-10-14", c)]) == []
