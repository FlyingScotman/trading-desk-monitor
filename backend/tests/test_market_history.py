"""Market history for the bond charts (R7): the store's md_* tables filled from cached and hub snapshots,
``GET /api/v1/bonds/history`` over them with the KBD rate at the bond's term, and the autopilot's history step. The
fixtures hold one date; other dates are the 2026-10-15 market snapshot shifted (prices, yields, the KBD level and
its published standard yields), written as bundles of their own."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import shutil
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import test_slice_s3
from fastapi.testclient import TestClient
from rub_fixed_income import kbd_effective_yield
from test_slice_s3 import TOKEN

from desk import autopilot, cli, store
from desk.api import create_app
from desk.autopilot import HISTORY_PER_CYCLE, Autopilot
from desk.marketdata import history, hubhistory
from desk.marketdata.adapter import zcyc_parameters
from desk.marketdata.bundle import MANIFEST_INDEX, read_bundle
from desk.marketdata.hub import NO_HISTORY_API, HistoryUnavailable
from desk.settings import Settings, check_settings_doc, load

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
INDEX = json.loads((FIX / "golden_index.json").read_text())["fixtures"]
MARKET, STATIC = INDEX["market_eod_final"]["snapshot_id"], INDEX["static_daily"]["snapshot_id"]
DATE = "2026-10-15"
OFZ, OFZ_ISIN, FLOATER, USD_FACE = "SU26238RMFS4", "RU000A1038V6", "SU29008RMFS8", "RU000A10BRM5"
server, stub = test_slice_s3.server, test_slice_s3.stub  # the stub hub of slice S3
NO_API = f"; history API: {NO_HISTORY_API}"  # the stub hubs are 0.1.x: the history step reads the bundles
PRICES = ("legal_close_pct", "close_pct", "waprice_pct", "market_price2_pct", "market_price3_pct", "open_pct",
          "high_pct", "low_pct")


def market_doc() -> dict:
    with gzip.open(FIX / f"{MARKET}.json.gz") as f:
        return json.load(f)


def shifted(d: str, step: int, *, known_at: str | None = None, light: bool = True) -> dict:
    """The fixture market snapshot as of ``d``: prices down ``0.1 * step`` %, MOEX yields up ``0.02 * step`` %, the
    KBD level (B1) up ``5 * step`` bp with its standard yields recomputed (so ``desk eod`` accepts it). ``light``
    drops the datasets the history does not read."""
    doc = market_doc()
    doc["business_date"], doc["known_at"] = d, known_at or f"{d}T18:00:00Z"
    if light:
        for k in ("fixings", "fx_rates", "cross_rates", "curve_quote_sets", "cbr_meetings"):
            doc["data"][k] = []
    for q in doc["data"]["bond_quotes"]:
        q["trade_date"] = d
        for k in PRICES:
            if q.get(k) is not None:
                q[k] = str(Decimal(q[k]) - Decimal("0.1") * step)
        sa = q.get("source_analytics") or {}
        if sa.get("effective_yield_pct") is not None:
            sa["effective_yield_pct"] = str(Decimal(sa["effective_yield_pct"]) + Decimal("0.02") * step)
    z = doc["data"]["zcyc_params"][0]
    z["trade_date"] = d
    z["params"]["B1"] = str(Decimal(z["params"]["B1"]) + 5 * step)
    kbd = zcyc_parameters(d, z["params"])
    for r in z["standard_yields"]:
        r["yield_pct"] = f"{kbd_effective_yield(kbd, float(r['tenor_years'])) * 100:.6f}"
    return doc


def write(folder: Path, doc: dict) -> str:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    sid = hashlib.sha256(raw).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sid}.json.gz").write_bytes(gzip.compress(raw))
    return sid


def weekdays_before(d: str, n: int) -> list[str]:
    """The ``n`` weekdays before ``d``, newest first."""
    out, x = [], date.fromisoformat(d)
    while len(out) < n:
        x -= timedelta(days=1)
        if x.weekday() < 5:
            out.append(x.isoformat())
    return out


def fixture_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    for p in FIX.glob("*.json.gz"):
        shutil.copy(p, cache / p.name)
    return cache


def rows(con, sql: str, *args) -> list[tuple]:
    return con.execute(sql, args).fetchall()


def test_cache_fills_the_tables_once(store_url, tmp_path, monkeypatch):
    cache = fixture_cache(tmp_path)
    prev = write(cache, shifted("2026-10-14", 1))
    with store.connect(store_url) as con:
        got = history.load_cache(con, cache)
        assert sorted(got.dates) == ["2026-10-14", DATE] and got.statics == 1 and not got.errors
        assert got.text() == "history tables: 2 dates 2026-10-14..2026-10-15, static 1"
        assert rows(con, "SELECT business_date, k.snapshot_id, count(*) FROM md_bond_eod JOIN md_kbd k"
                    " USING (business_date) GROUP BY 1, 2 ORDER BY 1") == [
            (date(2026, 10, 14), prev, 10), (date(2026, 10, 15), MARKET, 10)]  # 11 quotes, one bond on two boards
        (ofz,) = rows(con, "SELECT board, clean_pct, price_basis, yield_pct, yield_date, accrued_per_bond,"
                      " currency_id, value FROM md_bond_eod WHERE business_date = %s AND secid = %s", DATE, OFZ)
        assert ofz[:5] + ofz[6:] == ("TQOB", 51.45, "legal_close", 16.2094, date(2041, 5, 15), "SUR", 904977682.05)
        assert rows(con, "SELECT board FROM md_bond_eod WHERE secid = %s", USD_FACE) == [("TQCB",), ("TQCB",)]
        assert rows(con, "SELECT isin, coupon_type, face_unit, maturity_date FROM md_instrument WHERE secid = %s",
                    OFZ) == [(OFZ_ISIN, "fixed", "RUB", date(2041, 5, 15))]
        (params,) = rows(con, "SELECT params FROM md_kbd WHERE business_date = %s", DATE)[0]
        assert params == market_doc()["data"]["zcyc_params"][0]["params"]  # the published strings
        assert {s for (s,) in rows(con, "SELECT status FROM md_snapshot_load")} == {"loaded"}

        def no_read(path):
            raise AssertionError(f"read again: {path}")

        monkeypatch.setattr(history, "read_bundle", no_read)  # a pass with nothing new reads no bundle
        again = history.load_cache(con, cache)
        assert again.dates == [] and again.text() == ""
        assert history.load_snapshot(con, read_bundle(cache / f"{MARKET}.json.gz")) == "loaded"  # idempotent
        assert rows(con, "SELECT count(*) FROM md_bond_eod")[0][0] == 20


def test_the_newest_snapshot_of_a_date_wins(store_url, tmp_path):
    folder = tmp_path / "x"
    first = write(folder, shifted("2026-10-14", 1, known_at="2026-10-14T18:00:00Z"))
    fixed = shifted("2026-10-14", 2, known_at="2026-10-15T09:00:00Z")  # a correction: other prices, one bond fewer
    fixed["data"]["bond_quotes"] = [q for q in fixed["data"]["bond_quotes"] if q["secid"] != FLOATER]
    corr = write(folder, fixed)
    stale = write(folder, shifted("2026-10-14", 3, known_at="2026-10-14T12:00:00Z"))  # older, read last
    with store.connect(store_url) as con:
        snap = lambda sid: read_bundle(folder / f"{sid}.json.gz")
        assert history.load_snapshot(con, snap(first)) == "loaded"
        assert history.load_snapshot(con, snap(corr)) == "loaded"
        assert history.load_snapshot(con, snap(stale)) == "older"
        assert rows(con, "SELECT snapshot_id FROM md_kbd") == [(corr,)]
        assert rows(con, "SELECT count(*), max(snapshot_id) FROM md_bond_eod b JOIN md_kbd USING (business_date)"
                    " WHERE b.secid = %s", FLOATER)[0][0] == 0  # the correction's rows only
        assert rows(con, "SELECT clean_pct FROM md_bond_eod WHERE secid = %s", OFZ) == [(51.25,)]
        broken = market_doc()
        broken["data"]["zcyc_params"] = []  # unreadable: recorded, nothing written, not tried again
        bad = write(folder, broken)
        assert history.load_snapshot(con, snap(bad)) == "error"
        assert rows(con, "SELECT status, note LIKE '%%zcyc_params%%' FROM md_snapshot_load WHERE snapshot_id = %s",
                    bad) == [("error", True)]
        assert rows(con, "SELECT count(*) FROM md_kbd") == [(1,)]


@pytest.fixture
def desk_with_history(store_url, tmp_path):
    """A default run of 2026-10-15 (desk eod over the cache: 10 OFZ 26238 and 5 OFZ-PK 29008), 30 earlier dates of
    market history loaded straight from bundles (not in the cache), the 2026-10-14 one in the cache too."""
    cache = fixture_cache(tmp_path)
    write(cache, shifted("2026-10-14", 1, light=False))
    hist = tmp_path / "hub"
    days = weekdays_before(DATE, 30)
    for i, d in enumerate(days):
        write(hist, shifted(d, i + 1, known_at=f"{d}T17:00:00Z"))
    book = tmp_path / "book.csv"
    book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},10\nP2,OFZ,{FLOATER},5\n",
                    encoding="utf-8")
    lines: list[str] = []
    rid = cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=cache), DATE, store_url=store_url,
                      book=str(book), no_pnl=True, out=lines.append)
    assert rid == 1, lines
    with store.connect(store_url) as con:  # desk eod loaded the snapshots it read
        assert rows(con, "SELECT business_date FROM md_kbd ORDER BY 1") == [(date(2026, 10, 14),),
                                                                          (date(2026, 10, 15),)]
        got = history.load_cache(con, hist)
        assert len(got.dates) == 29  # 2026-10-14 is the cached one, newer: kept
    return TestClient(create_app(store_url)), days


def test_history_route_gives_every_quoted_day_with_the_curve_rate(desk_with_history, store_url):
    c, days = desk_with_history
    h = c.get(f"/api/v1/bonds/history?instrument_id={OFZ}&curve=own").json()
    s = h["series"]
    assert [p["date"] for p in s] == sorted(days) + [DATE]
    assert (h["instrument_id"], h["isin"], h["ccy"], h["curve"]) == (OFZ, OFZ_ISIN, "RUB", "RUB OFZ")
    assert "КБД" in h["spread_to_curve_note"]
    last, before = s[-1], s[-2]
    with store.connect(store_url) as con:
        fact = rows(con, "SELECT fact FROM run_position WHERE run_id = 1 AND position_id = 'P1'")[0][0]
        kbd = {d.isoformat(): history.kbd(t, p) for d, t, p in rows(con, "SELECT business_date, trade_date, params"
                                                                     " FROM md_kbd")}
    assert last["source"] == "market + desk run" and last["face_amount"] == pytest.approx(10_000.0)
    assert last["z_spread_bp"] == pytest.approx(fact["spread_bp"]) and last["clean_price"] == 51.45
    assert (last["ytm"], last["price_basis"], last["yield_date"]) == (16.2094, "legal_close", "2041-05-15")
    assert last["volume_rub"] == 904977682.05 and last["g_spread_bp"] == -24
    for p in (last, before):
        t = (date(2041, 5, 15) - date.fromisoformat(p["date"])).days / 365.25
        rate = kbd_effective_yield(kbd[p["date"]], t) * 100
        assert p["tenor_y"] == pytest.approx(t) and p["curve_rate"] == pytest.approx(rate)
        assert p["spread_to_curve_bp"] == pytest.approx((p["ytm"] - rate) * 100)
    assert before["source"] == "market" and before["face_amount"] is None and before["z_spread_bp"] is None
    assert before["clean_price"] == pytest.approx(51.35) and before["curve_rate"] > last["curve_rate"]  # B1 +5 bp
    assert all(p["curve_rate"] is not None for p in s)

    by_isin = c.get(f"/api/v1/bonds/history?isin={OFZ_ISIN}").json()  # the ISIN through the static snapshot
    assert by_isin["series"] == s and by_isin["name"] == h["name"]
    part = c.get(f"/api/v1/bonds/history?instrument_id={OFZ}&from={days[4]}&to={days[0]}").json()["series"]
    assert [p["date"] for p in part] == sorted(days[:5])
    other = c.get(f"/api/v1/bonds/history?instrument_id={OFZ}&curve=RUB RUONIA").json()
    assert len(other["series"]) == 31 and all(p["curve_rate"] is None for p in other["series"])
    assert other["curve"] == "RUB RUONIA" and "RUB RUONIA" in other["spread_to_curve_note"]

    fl = c.get(f"/api/v1/bonds/history?instrument_id={FLOATER}").json()  # a floater: no curve rate
    assert len(fl["series"]) == 31 and fl["series"][-1]["face_amount"] == pytest.approx(5_000.0)
    assert all(p["curve_rate"] is None and p["spread_to_curve_bp"] is None for p in fl["series"])
    assert "floating" in fl["spread_to_curve_note"]
    usd = c.get(f"/api/v1/bonds/history?isin={USD_FACE}").json()  # a USD face, not held: by ISIN = secid
    assert len(usd["series"]) == 31 and usd["ccy"] == "USD" and usd["series"][0]["curve_rate"] is None
    assert "не в рублях" in usd["spread_to_curve_note"]
    assert c.get("/api/v1/bonds/history?isin=XS999").json() == {
        "instrument_id": None, "isin": "XS999", "name": None, "ccy": None, "curve": "RUB OFZ",
        "spread_to_curve_note": None, "series": []}


class FakeHub:
    """The hub's list and bundles for ``dates`` (newest first): shifted fixture snapshots, built on demand. A hub
    before 0.2.0: no history API (the history step falls back to the bundles, R24)."""

    host, url = "hub.test", "https://hub.test"

    def __init__(self, dates: list[str]) -> None:
        self.dates, self.bodies, self.lists, self.bundles = dates, {}, 0, []
        for i, d in enumerate(dates):
            raw = json.dumps(shifted(d, i), sort_keys=True).encode()
            self.bodies[d] = (hashlib.sha256(raw).hexdigest(), gzip.compress(raw))

    def list_snapshots(self, kind=None, cut_type=None, business_date=None, latest=False):
        if business_date is None:
            self.lists += 1
        for d in self.dates:
            if business_date in (None, d):
                yield {"snapshot_id": self.bodies[d][0], "business_date": d, "kind": "market",
                       "cut_type": "eod_final"}

    def history_index(self):
        raise HistoryUnavailable(NO_HISTORY_API, 404, "no_api")

    def bundle(self, sid: str) -> bytes:
        self.bundles.append(sid)
        return next(b for s, b in self.bodies.values() if s == sid)


def test_autopilot_history_step_backfills_a_few_dates_per_cycle(store_url, tmp_path, monkeypatch):
    dates = [DATE, *weekdays_before(DATE, 15)]  # the hub's dates, newest first; the cache holds 2026-10-15 only
    hub = FakeHub(dates)
    monkeypatch.setattr(autopilot, "HubClient", lambda settings: hub)
    cache = fixture_cache(tmp_path)
    s = Settings(home=tmp_path / "home", snapshot_cache=cache, store_url=store_url, hub_url="https://hub.test",
                 history_days=12)
    ap = Autopilot(settings_loader=lambda: s, today=lambda: date(2026, 10, 19))
    hubhistory.forget()
    with store.connect(store_url) as con:
        assert ap._history(s, con) == ("ok", (f"loaded {', '.join(dates[1:6])}; 6 dates in the next cycles"
                                             f" (history_days 12: 12 on the hub back to {dates[11]}){NO_API}"))
        # the cached one: the sync's
        assert len(hub.bundles) == HISTORY_PER_CYCLE == 5 and sorted(p.name for p in cache.iterdir() if p.name != MANIFEST_INDEX) == sorted(
            p.name for p in FIX.glob("*.json.gz"))  # not cached
        ap._history(s, con)
        st = ap._history(s, con)
        assert st[1].startswith(f"loaded {dates[11]}") and "next cycles" not in st[1]
        assert ap._history(s, con) == ("ok", f"up to date (history_days 12: 12 on the hub back to {dates[11]})"
                                             + NO_API)
        assert hub.lists == 1 and len(hub.bundles) == 11  # the list read once per hour; 12 dates less the cached
        assert len(history.loaded_dates(con)) == 11  # the cached date loads in the sync step, not here
        ap._sync_later = 2
        assert ap._history(s, con) == ("skipped", "after the sync (2 dates next cycle)")
        ap._sync_later, ap._hub_failed = 0, True
        assert ap._history(s, con)[0] == "skipped"
        ap._hub_failed = False
        deeper = dataclasses.replace(s, history_days=15)  # a deeper history: the list is read again
        assert ap._history(deeper, con)[1].startswith(f"loaded {', '.join(dates[12:15])}")
        assert hub.lists == 2


def test_cycle_loads_the_cache_and_runs_the_history_step(stub, store_url, tmp_path):
    s = Settings(home=tmp_path / "home", snapshot_cache=tmp_path / "cache", store_url=store_url,
                 hub_url=f"http://127.0.0.1:{stub.server_address[1]}", hub_token=TOKEN, hub_timeout_s=5)
    ap = Autopilot(settings_loader=lambda: s, today=lambda: date(2026, 10, 19))
    by = {x["name"]: (x["status"], x["detail"]) for x in ap.run_cycle()["steps"]}
    assert "pulled 2026-10-15" in by["sync"][1] and "history tables: date 2026-10-15, static 1" in by["sync"][1]
    assert by["history"] == ("ok", "up to date (history_days 260: 1 on the hub back to 2026-10-15)" + NO_API)
    stub.requests.clear()
    by = {x["name"]: (x["status"], x["detail"]) for x in ap.run_cycle()["steps"]}
    assert "history tables" not in by["sync"][1] and not any("/bundle" in r for r in stub.requests)
    with store.connect(store_url) as con:
        assert rows(con, "SELECT count(*) FROM md_bond_eod") == [(10,)]
    off = dataclasses.replace(s, history_days=0)
    assert "history" not in {x["name"] for x in Autopilot(settings_loader=lambda: off).run_cycle()["steps"]}


def test_history_days_setting(tmp_path):
    env = {"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "none.env")}
    assert load(env).history_days == 260
    assert load({**env, "DESK_HISTORY_DAYS": "0"}).history_days == 0  # off: the cached dates only
    (tmp_path / "settings.json").write_text('{"history_days": 30}', encoding="utf-8")
    assert load(env).history_days == 30 and dict(load(env).describe())["history_days"] == "30"
    assert check_settings_doc({"history_days": -1}) == ["history_days: must be a whole number >= 0"]
