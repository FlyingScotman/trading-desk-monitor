"""Control center data (R20, ``GET /api/v1/cockpit``): the run's freshness and log, bond quotes D and D-1 and the market
universe from the history, CBR meetings and fixings from the run's market snapshot, the cash flow calendar of the held
bonds from its static snapshot (per bond in RUB, holdings per position), the earlier default runs; a bundle missing
from the cache leaves its part empty with the reason. Two default runs on the 2026-10-15 fixture and the same snapshot
shifted to 2026-10-14 (``test_market_history.shifted``). Synthetic data only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_market_history import STATIC, fixture_cache, shifted, write

from desk import cli
from desk.api import create_app
from desk.settings import Settings

BOOK = [("P1", "OFZ", "SU26238RMFS4", 120000), ("P2", "OFZ", "SU46020RMFS2", -30000), ("P3", "CORP", "RU000A0JXQ44", 40000),
        ("P4", "FLOAT", "RU000A109XR1", 60000), ("P5", "FX", "RU000A10BRM5", 15000), ("P6", "CORP", "RU000A0ZZZZ9", 1000)]


@pytest.fixture
def desk(store_url, tmp_path):
    cache = fixture_cache(tmp_path)
    write(cache, shifted("2026-10-14", 1, light=False))
    book = tmp_path / "book.csv"
    book.write_text("position_id,book_id,secid,quantity_bonds\n" + "".join(f"{a},{b},{c},{d}\n" for a, b, c, d in BOOK),
                    encoding="utf-8")
    st = Settings(home=tmp_path / "home", snapshot_cache=cache)
    lines: list[str] = []
    assert cli.eod_run(st, "2026-10-14", store_url=store_url, book=str(book), out=lines.append) == 1, lines
    assert cli.eod_run(st, "2026-10-15", store_url=store_url, book=str(book), out=lines.append) == 2, lines
    app = create_app(store_url)
    app.state.snapshots = cache
    return TestClient(app), cache


def test_cockpit_of_the_latest_run(desk):
    c, _ = desk
    j = c.get("/api/v1/cockpit").json()
    run = j["run"]
    assert (run["run_id"], run["business_date"], run["prev_date"], run["label"]) == (2, "2026-10-15", "2026-10-14",
                                                                                    "15.10 · закрытие")
    assert run["market_known_at"] and run["completed_at"] and run["static_snapshot_id"] == STATIC
    assert j["history_runs"] == [{"date": "2026-10-14", "run_id": 1}]
    assert j["log"]["counts"]["WARN"] == len([x for x in j["log"]["lines"] if x["level"] == "WARN"]) > 0
    assert any("P6" in x["message"] for x in j["log"]["lines"])  # the unmapped bond is a warning

    q = j["quotes"]
    assert (q["date"], q["prev_date"]) == ("2026-10-15", "2026-10-14")
    ofz = q["bonds"]["SU26238RMFS4"]
    assert (ofz["price"], ofz["prev"]["price"], ofz["g"]) == (51.45, pytest.approx(51.35), -24)
    assert "RU000A0ZZZZ9" not in q["bonds"]  # not quoted
    u = j["universe"]
    assert u["cols"] == ["secid", "name", "class", "ytm", "g", "dur_y", "value"]
    rows = {r[0]: r for r in u["rows"]}
    assert rows["SU26238RMFS4"][2] == "ofz_pd" and rows["SU26238RMFS4"][5] == pytest.approx(2480 / 365.25, abs=1e-4)
    assert all(r[4] is not None and r[5] > 0 for r in u["rows"])

    r = j["rates"]
    assert r["fixings"]["KEY_RATE"][-1] == ["2026-10-15", 14.0]
    assert all(d <= "2026-10-15" for d, _ in r["fixings"]["RUONIA"])
    assert r["cbr_ahead"][0]["date"] == "2026-10-23" and r["cbr_past"][-1]["date"] <= "2026-10-15"

    cal = j["calendar"]
    assert (cal["from"], cal["to"]) == ("2026-10-15", "2026-11-15")
    ev = {(e["secid"], e["kind"], e["date"]): e for e in cal["events"]}
    assert ev["RU000A0JXQ44", "coupon", "2026-10-15"]["per_bond_rub"] == 71.8
    assert ev["RU000A109XR1", "amortization", "2026-10-19"]["per_bond_rub"] == 100.0
    usd = ev["RU000A10BRM5", "coupon", "2026-10-29"]  # a USD face: the amount at the run's rate
    assert usd["unit"] == "USD" and usd["per_bond_rub"] == pytest.approx(6.37 * 83.9773, rel=1e-3)
    assert {e["secid"] for e in cal["events"]} <= {s for _, _, s, _ in BOOK}  # only held bonds
    assert all("2026-10-15" <= e["date"] <= "2026-11-15" for e in cal["events"])
    assert j["holdings"]["P2"] == {"secid": "SU46020RMFS2", "quantity": -30000.0}
    assert any("лимит" in g for g in j["gaps"])


def test_by_run_and_date_and_not_found(desk):
    c, _ = desk
    assert c.get("/api/v1/cockpit?run_id=1").json()["run"]["prev_date"] is None
    assert c.get("/api/v1/cockpit?as_of=2026-10-14").json()["run"]["run_id"] == 1
    r = c.get("/api/v1/cockpit?run_id=99")
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_missing_bundles_leave_their_parts_empty(desk):
    c, cache = desk
    for p in cache.glob("*.json.gz"):
        p.unlink()
    j = c.get("/api/v1/cockpit?run_id=2").json()
    assert j["rates"] is None and j["calendar"] is None
    assert any(g.startswith("ставки и заседания ЦБ: снимок") for g in j["gaps"])
    assert any(g.startswith("календарь выплат: снимок") for g in j["gaps"])
    assert j["quotes"]["bonds"]  # the history is in the store, not in the bundles
