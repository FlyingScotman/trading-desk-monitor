"""The day's trades on the screens (TDM#585): on C20 every trade says where it came from (a report folder, the services'
feed of the day, the fetch of past dates, a managed acquisition) and when the monitor received it, with its book's
legal entity, desk and portfolio for the header's filter; ``GET /api/v1/trades/summary`` counts them for the general
screens (C01, C03, C05, C14) under the books of the header. Synthetic data only (``fixtures/private/v1/datasets``)."""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import store, trades
from desk.api import create_app
from desk.inbox import scan
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
DS = FIX / "private" / "v1" / "datasets"
DS_CONFIG, SNAPS = DS / "config", FIX / "mdhub_v1"
D = date.fromisoformat


@pytest.fixture(autouse=True)
def _fresh_process():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


def _settings(tmp_path: Path, inbox: Path) -> Settings:
    return Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                    private_adapter=None, private_adapter_config=DS_CONFIG, inbox_poll_seconds=0,
                    inbox_lookback_days=0, home=tmp_path / "h")


def test_the_origin_of_a_file_by_where_it_was_first_seen() -> None:
    rid = uuid.uuid4().hex
    day = D("2026-10-20")
    same_day = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    next_day = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)  # 10:00 Moscow the day after
    feed = f"/home/desk/adapter-services/out/{rid}/trades_20261020_1500.csv"
    assert trades.origin_of(feed, same_day, day) == "feed"
    assert trades.origin_of(feed, next_day, day) == "history"
    assert trades.origin_of(rf"C:\Desk\data\adapter-services\out\{rid}\trades.csv", same_day, day) == "feed"
    assert trades.origin_of(f"/home/desk/acquisition/{rid}/trades.csv", same_day, day) == "request"
    assert trades.origin_of("/reports/in/trades_20261020_1500.csv", same_day, day) == "file"
    # a folder of the owner's that only looks like it: not the services' own run folder
    assert trades.origin_of("/reports/adapter-services/out/trades.csv", same_day, day) == "file"
    assert trades.origin_of(None, None, None) == "file"
    assert set(trades.ORIGIN_TEXT) == set(trades.ORIGINS)
    assert set(trades.PROBLEMS) < set(trades.SKIP_REASONS)
    assert set(trades.STATE_TEXT) == {"applied", *trades.SKIP_REASONS, "pending", "unknown", "off"}


def test_each_trade_says_its_source_and_when_it_came(store_url, tmp_path: Path) -> None:
    inbox = tmp_path / "in"
    inbox.mkdir()
    for n in ("trades_20261020_1100.csv", "trades_20261020_1500.csv"):
        shutil.copy(DS / n, inbox / n)
    s = _settings(tmp_path, inbox)
    with store.connect(store_url) as con:
        assert scan.scan(con, s).ok
        # the services' feed delivered the deltas of 21.10 (recorded today, after 21.10: the fetch of a past date)
        out = tmp_path / "h" / "adapter-services" / "out" / uuid.uuid4().hex
        out.mkdir(parents=True)
        for n in ("trades_delta_20261021_1000.csv", "trades_delta_20261021_1200.csv"):
            shutil.copy(DS / n, out / n)
            r = scan.ingest_feed(con, s, out / n, source="trades_delta_csv", handle=scan.adapter(s))
            assert r.files == {"accepted": 1}, r.text()
    c = TestClient(create_app(store_url))
    v = c.get("/api/v1/trades", params={"date": "2026-10-20"}).json()
    assert {r["origin"] for r in v["rows"]} == {"file"} and all(r["received_at"] for r in v["rows"])
    (src,) = v["sources"]
    assert src["origin"] == "file" and src["text"] == "из папки с отчётами" and src["trades"] == 5
    assert src["last_at"] == max(r["received_at"] for r in v["rows"])
    assert v["state_text"] == trades.STATE_TEXT and v["origin_text"] == trades.ORIGIN_TEXT
    assert {f["origin"] for f in v["files"]} == {"file"} and all(f["received_at"] for f in v["files"])
    assert v["counts"]["trades"] == 5 and v["counts"]["live"] == 4 and v["counts"]["cancelled"] == 1
    assert v["counts"]["last_trade_ts"].startswith("2026-10-")
    # the header's filter: the book's legal entity and desk from the books in force, the portfolio by the one rule
    for r in v["rows"]:
        assert "entity" in r and "desk" in r and "portfolio" in r
    assert all("applied" in b and "entity" in b for b in v["books"])
    w = c.get("/api/v1/trades", params={"date": "2026-10-21"}).json()
    assert {r["origin"] for r in w["rows"]} == {"history"}
    assert [x["origin"] for x in w["sources"]] == ["history"] and w["sources"][0]["text"] == "догрузка прошлых дат"


def test_the_summary_counts_the_books_of_the_header(store_url, tmp_path: Path) -> None:
    inbox = tmp_path / "in"
    inbox.mkdir()
    for n in ("trades_20261020_1100.csv", "trades_20261020_1500.csv"):
        shutil.copy(DS / n, inbox / n)
    with store.connect(store_url) as con:
        assert scan.scan(con, _settings(tmp_path, inbox)).ok
    c = TestClient(create_app(store_url))
    every = c.get("/api/v1/trades/summary", params={"date": "2026-10-20"}).json()
    full = c.get("/api/v1/trades", params={"date": "2026-10-20"}).json()
    assert every["books"] is None and every["trades"] == full["summary"]["trades"] == 5
    assert (every["live"], every["cancelled"]) == (4, 1)
    assert set(every["by_book"]) == {"D1", "T1"} and every["by_book"]["T1"]["trades"] == 3
    assert every["by_book"]["T1"]["last_trade_ts"] is not None
    assert len(every["top"]) == 4 and all(t["origin"] == "file" for t in every["top"])  # the live ones, the latest first
    ts = [t["trade_ts"] for t in every["top"]]
    assert ts == sorted(ts, reverse=True)
    assert [x["origin"] for x in every["sources"]] == ["file"]
    assert "rows" not in every
    t1 = c.get("/api/v1/trades/summary", params={"date": "2026-10-20", "books": ["T1"]}).json()
    assert t1["books"] == ["T1"] and t1["trades"] == 3 and set(t1["by_book"]) == {"T1"}
    assert all(t["book_id"] == "T1" for t in t1["top"])
    both = c.get("/api/v1/trades/summary", params=[("date", "2026-10-20"), ("books", "T1"), ("books", "D1")]).json()
    assert both["trades"] == 5
    none = c.get("/api/v1/trades/summary", params={"date": "2026-10-20", "books": ["X9"]}).json()
    assert none["trades"] == 0 and none["top"] == [] and none["sources"] == []
    assert c.get("/api/v1/trades/summary", params={"date": "20.10.2026"}).status_code == 400
    assert c.get("/api/v1/trades/summary").json()["date"] == "2026-10-20"  # the latest date with trades
    # no run of the date: the trades wait for it, and the states say so in words
    assert {x["state"]: x["text"] for x in every["states"]}.get("pending") == "ждёт прогона"
