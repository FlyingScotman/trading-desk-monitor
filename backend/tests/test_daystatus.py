"""Day status (S7b, issue #25): ``desk.daystatus`` and ``/api/v1/inbox``, ``/api/v1/inbox/days``, ``/api/v1/events``.
What arrived per date, what was rejected and why, which runs superseded which, and the event feed with its cursor.
Synthetic data only (the fixtures of tests/fixtures/private/v1)."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import cli, daystatus, store
from desk.api import auth, create_app
from desk.inbox import scan
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
PRIV, CONFIG, SNAPS = FIX / "private" / "v1", FIX / "private" / "v1" / "config", FIX / "mdhub_v1"
GOOD = str(FIX / "adapters" / "good_adapter")
D = date.fromisoformat


@pytest.fixture(autouse=True)
def _fresh_process():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


@pytest.fixture
def con(store_url):
    with store.connect(store_url) as c:
        yield c


def put(inbox: Path, *names: str) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.copy(PRIV / n, inbox / n)


def cfg(tmp_path: Path, inbox: Path) -> Settings:
    return Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                    private_adapter=GOOD, private_adapter_config=CONFIG, inbox_poll_seconds=0, inbox_lookback_days=0)


def test_a_rejected_book_says_why_and_a_stale_copy_is_shown(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261015.csv", "positions_20261017.csv")
    s = cfg(tmp_path, inbox)
    scan.scan(con, s)
    put(inbox, "positions_20261019.csv")  # the Saturday file's bytes under Monday's name
    scan.scan(con, s)

    d = daystatus.day(con, s, D("2026-10-14"))  # the report of 15.10: B2 bad_number, B3 closed by omission
    assert d["date"] == "2026-10-14"
    pos = next(i for i in d["inputs"] if i["dataset"] == "positions")
    assert pos["state"] == "partial" and pos["units"]["rejected"] == 1 and pos["units"]["accepted"] >= 1
    assert {i["dataset"]: i["state"] for i in d["inputs"]}["ocp"] == "missing"
    (f,) = d["files"]
    assert f["name"] == "positions_20261015.csv" and f["status"] == "partially_accepted" and f["datasets"] == ["positions"]
    b2 = next(u for u in f["units"] if u["scope"].get("book_id") == "B2")
    assert b2["status"] == "rejected" and b2["why"] == "число не разобрано"
    assert b2["issues"][0] == {"severity": "error", "code": "bad_number", "count": 1, "text": "число не разобрано"}
    assert d["summary"]["state"] == "bad" and "Позиции: принято частично" in d["summary"]["warnings"]
    assert "нет основного прогона" in d["summary"]["problems"] and d["runs"] == []
    b = {x["book_id"]: x for x in d["positions"]["books"]}  # reconcile's per-book view, reused
    assert "unit_rejected" in b["B2"]["flags"] and b["B2"]["units_rejected"] == 1
    assert [e["type"] for e in d["events"]] == ["input.partially_accepted"]
    assert "Позиции за 14.10" in d["events"][0]["text"] and d["events"][0]["level"] == "warn"
    assert "B1" not in json.dumps(d["events"])  # the feed: codes and counts, no books or file names

    d = daystatus.day(con, s, D("2026-10-16"))
    (c,) = d["copies"]
    assert c["kind"] == "stale_copy" and c["name"] == "positions_20261019.csv" and c["content_report_date"] == "2026-10-17"
    assert "старая копия отчёта под новым именем" in d["summary"]["warnings"]
    assert d["market"]["eod_final"] is None  # the fixture cache has the close of 15.10 only

    got = daystatus.days(con, s, D("2026-10-20"), 10)
    by = {x["date"]: x for x in got["days"]}
    assert list(by) == sorted(by, reverse=True) and "2026-10-15" in by  # the cached close counts
    assert by["2026-10-14"]["inputs"]["positions"]["rejected"] == 1 and by["2026-10-14"]["state"] == "bad"
    assert by["2026-10-15"]["eod_final"] is True and by["2026-10-15"]["runs"] == 0
    # 15.10 has no file of its own: positions carried from 14.10 (B2's book was rejected there: none), no run yet
    assert (by["2026-10-15"]["problems"], by["2026-10-15"]["warnings"]) == (["нет основного прогона"],
                                                                            ["книг без позиций: 1"])
    assert_strip_matches_days(con, s, got)

    # a date with nothing on it (Sunday 18.10): positions carried forward, not «нет позиций»
    empty = daystatus.day(con, s, D("2026-10-18"))["summary"]
    assert empty["problems"] == ["нет основного прогона"] and "нет позиций" not in empty["problems"]
    assert "2026-10-18" not in by  # the strip lists dates with something on them only
    before = daystatus.day(con, s, D("2026-10-01"))["summary"]  # before the first report: nothing in force
    assert "нет позиций" in before["problems"]

    # a file lost by match (match_failed) that names the date is a problem of the date in both views
    sha = "f" * 64
    con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, 'x', 'x.csv', 1, now())",
                (sha,))
    con.execute("INSERT INTO inbox_file_status (sha256, status, adapter_fingerprint, path, valid_date)"
                " VALUES (%s, 'match_failed', 'fp', 'x', '2026-10-16')", (sha,))
    got = daystatus.days(con, s, D("2026-10-20"), 10)
    strip = next(x for x in got["days"] if x["date"] == "2026-10-16")
    assert strip["files_lost"] == 1 and "файлов с ошибкой: 1" in strip["problems"] and strip["state"] == "bad"
    assert_strip_matches_days(con, s, got)

    # n calendar days ending on to, not n + 1
    one = daystatus.days(con, s, D("2026-10-15"), 1)
    assert one["from"] == one["to"] == "2026-10-15" and [x["date"] for x in one["days"]] == ["2026-10-15"]
    assert daystatus.days(con, s, D("2026-10-20"), 30)["from"] == "2026-09-21"


def assert_strip_matches_days(con, s, got) -> None:
    """The strip (days) and each date's headline (day) judge a date by one rule."""
    for x in got["days"]:
        head = daystatus.day(con, s, D(x["date"]))["summary"]
        assert (x["state"], x["problems"], x["warnings"]) == (head["state"], head["problems"], head["warnings"]), x["date"]


def test_runs_supersede_with_what_changed_and_the_feed_pages_by_event_id(store_url, tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261016.csv")
    for k, v in {"DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": store_url,
                 "DESK_INBOX_DIRS": str(inbox), "DESK_INBOX_ARCHIVE": str(tmp_path / "arch"),
                 "DESK_SNAPSHOT_CACHE": str(SNAPS), "DESK_PRIVATE_ADAPTER_CONFIG": str(CONFIG),
                 "DESK_INBOX_POLL_SECONDS": "0", "DESK_INBOX_LOOKBACK_DAYS": "0", "DESK_HOME": str(tmp_path / "h")}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["inbox", "scan"]) == 0
    assert cli.main(["eod", "--date", "2026-10-15", "--snapshots", str(SNAPS)]) == 0
    put(inbox, "positions_20261016_2.csv")  # the correction (seq 2): a new position set for B1
    assert cli.main(["inbox", "scan"]) == 0
    assert cli.main(["eod", "--date", "2026-10-15", "--snapshots", str(SNAPS)]) == 0

    client = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    d = client.get("/api/v1/inbox", params={"on": "2026-10-15"}).json()
    r1, r2 = d["runs"]
    assert (r1["run_id"], r2["run_id"]) == (1, 2) and r1["superseded_by"] == [2] and r2["supersedes_run_id"] == 1
    assert r2["is_default"] and not r1["is_default"] and r2["changed"] == ["positions"]
    assert r2["changed_text"] == ["позиции"] and r2["label"] == "15.10 · закрытие"
    assert d["market"]["eod_final"]["snapshot_id"].startswith("d6c6d35a")
    assert d["summary"]["default_run_id"] == 2 and d["positions"]["run_id"] == 2
    files = {f["name"]: f for f in d["files"]}
    assert files["positions_20261016_2.csv"]["status"] == "accepted"
    pos = next(i for i in d["inputs"] if i["dataset"] == "positions")
    assert pos["state"] == "ok" and pos["stored_sets"] >= 1 and pos["stored_rows"] > 0
    # the correction replaced the books of the original: each of B1, B2, B3 counts once, not twice
    assert pos["units"] == {"accepted": 3, "rejected": 0, "superseded": 0}
    assert d["summary"]["state"] == "ok" and d["summary"]["problems"] == d["summary"]["warnings"] == []
    strip = client.get("/api/v1/inbox/days", params={"to": "2026-10-15", "n": 1}).json()["days"]
    assert [(x["date"], x["state"], x["inputs"]["positions"]["accepted"]) for x in strip] == [("2026-10-15", "ok", 3)]

    # reconcile's no_positions and rows_differ colour the date too, in the headline and in the strip
    def both() -> tuple[dict, dict, dict]:
        day = client.get("/api/v1/inbox", params={"on": "2026-10-15"}).json()
        (row,) = client.get("/api/v1/inbox/days", params={"to": "2026-10-15", "n": 1}).json()["days"]
        return day["summary"], row, {b["book_id"]: b["flags"] for b in day["positions"]["books"]}

    with store.connect(store_url) as con:
        store.store_books(con, [{"book_id": "B4", "name": "B4", "desk": "Rates", "book_type": "TRADING",
                                 "entity": "ENT1", "active": True}], "test")
    head, row, flags = both()
    assert flags["B4"] == ["no_positions"]
    assert head["state"] == row["state"] == "warn" and head["warnings"] == row["warnings"] == ["книг без позиций: 1"]
    with store.connect(store_url) as con:  # one more row in B1's set than the run valued: the run lost a row
        sid = con.execute("SELECT s.set_id FROM run r JOIN position_set s ON s.set_id = ANY(r.position_set_ids)"
                          " WHERE r.run_id = 2 AND s.book_id = 'B1'").fetchone()[0]
        con.execute("INSERT INTO position_input (set_id, position_id, secid, quantity) VALUES (%s, 'X1', 'X', 1)",
                    (sid,))
    head, row, flags = both()
    assert flags["B1"] == ["rows_differ"]
    assert head["warnings"] == row["warnings"] == ["книг без позиций: 1", "в прогоне не все строки позиций: книг 1"]
    types = [e["type"] for e in d["events"]]
    assert types.count("run.published") == 2 and "input.accepted" in types
    assert client.get("/api/v1/inbox", params={"on": "15.10"}).status_code == 400
    assert client.get("/api/v1/inbox/days", params={"to": "2026-10-20"}).json()["days"][0]["date"] == "2026-10-15"

    # the feed: newest first page, then forward from a cursor (what a poll or the SSE stream does)
    page = client.get("/api/v1/events").json()
    ids = [e["id"] for e in page["events"]]
    assert ids == sorted(ids) and page["last_id"] == ids[-1] and not page["has_more"]
    pub = [e for e in page["events"] if e["type"] == "run.published"]
    assert pub[-1]["run_id"] == 2 and pub[-1]["detail"]["supersedes_run_id"] == 1 and pub[-1]["valid_date"] == "2026-10-15"
    assert pub[-1]["text"] == "Прогон закрытия #2 за 15.10 (закрытие) опубликован, заменил #1"
    runs = client.get("/api/v1/events", params={"type": "run."}).json()["events"]
    assert [e["run_id"] for e in runs] == [1, 2]
    assert client.get("/api/v1/events", params={"type": "run"}).json()["events"] == runs  # a prefix with or without "."
    assert client.get("/api/v1/events", params={"after": pub[-1]["id"]}).json()["events"] == []
    first = client.get("/api/v1/events", params={"after": 0, "limit": 1}).json()
    assert len(first["events"]) == 1 and first["has_more"] and first["events"][0]["id"] == ids[0]
    older = client.get("/api/v1/events", params={"before": ids[-1], "limit": 1}).json()
    assert [e["id"] for e in older["events"]] == [ids[-2]] and older["has_more"]
    assert client.get("/api/v1/events", params={"on": "2026-10-14"}).json()["events"] == []

    # sign-in on: a viewer reads the whole day status from another computer (the owner's decision in #25)
    with store.connect(store_url) as con:
        auth.add_user(con, "vic", "viewer", "viewer-pass-1", actor="test")
    remote = TestClient(create_app(store_url, auth=auth.AuthConfig(enabled=True)), base_url="http://10.0.0.7:8000",
                        client=("10.0.0.7", 50000))
    for path in ("/api/v1/inbox", "/api/v1/inbox/days", "/api/v1/events"):
        assert remote.get(path).status_code == 401, path  # not signed in
    assert remote.post("/api/v1/auth/login", json={"username": "vic", "password": "viewer-pass-1"}).status_code == 200
    seen = remote.get("/api/v1/inbox", params={"on": "2026-10-15"}).json()
    assert seen["files"][0]["name"].startswith("positions_") and seen["positions"]["books"][0]["book_id"] == "B1"
    assert remote.get("/api/v1/inbox/days", params={"to": "2026-10-15"}).status_code == 200
    srcs = {e["source"] for e in remote.get("/api/v1/events", params={"type": "input."}).json()["events"]}
    assert srcs == set(seen["inputs"][0]["sources"]) and None not in srcs  # sources stay in the feed for every user
    assert remote.get("/concepts/c14-day-status.html").status_code == 200


def test_a_reaped_run_writes_run_failed(con):
    rid = con.execute(
        "INSERT INTO run (run_type, business_date, cut_type, started_at, market_snapshot_id, static_snapshot_id,"
        " private_inputs_hash, position_set_ids, positions_known_at, core_version, backend_version, lineage)"
        " VALUES ('EOD', '2026-10-15', 'eod_preliminary', now() - interval '2 hours', 'm', 's', 'h', '{}', now(),"
        " 'c', 'b', '{}') RETURNING run_id").fetchone()[0]
    assert store.reap_orphans(con) == [rid]
    (e,) = daystatus.events(con)["events"]
    assert e["type"] == "run.failed" and e["level"] == "error" and e["run_id"] == rid
    assert e["text"] == f"Прогон закрытия #{rid} за 15.10 (закрытие предв.) не удался"
    d = daystatus._runs(con, D("2026-10-15"))
    assert d[0]["status"] == "failed" and d[0]["error"].startswith("orphaned")


def test_swap_terms_without_a_positions_row_warn_on_the_date_and_in_the_strip(store_url, tmp_path, monkeypatch):
    """TDM#468: the default run's ``lineage.positions.unlinked_swap_terms`` is a warning of the day status (headline and
    strip), with the number of trades and no amounts."""
    inbox = tmp_path / "in"
    put(inbox, "positions_20261016.csv")
    for k, v in {"DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": store_url,
                 "DESK_INBOX_DIRS": str(inbox), "DESK_INBOX_ARCHIVE": str(tmp_path / "arch"),
                 "DESK_SNAPSHOT_CACHE": str(SNAPS), "DESK_PRIVATE_ADAPTER_CONFIG": str(CONFIG),
                 "DESK_INBOX_POLL_SECONDS": "0", "DESK_INBOX_LOOKBACK_DAYS": "0", "DESK_HOME": str(tmp_path / "h")}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["inbox", "scan"]) == 0
    assert cli.main(["eod", "--date", "2026-10-15", "--snapshots", str(SNAPS)]) == 0
    client = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))

    def both() -> tuple[dict, dict]:
        day = client.get("/api/v1/inbox", params={"on": "2026-10-15"}).json()
        (row,) = client.get("/api/v1/inbox/days", params={"to": "2026-10-15", "n": 1}).json()["days"]
        return day["summary"], row

    head, row = both()
    assert not any("свопов" in w for w in head["warnings"] + row["warnings"])
    with store.connect(store_url) as con:
        con.execute("ALTER TABLE run DISABLE TRIGGER USER")  # a published run is frozen
        con.execute("UPDATE run SET lineage = jsonb_set(lineage, '{positions,unlinked_swap_terms}',"
                    " '{\"trades\": 2, \"books\": {\"IRB\": 2}}') WHERE run_id = 1")
    head, row = both()
    want = "условия свопов без строки позиций, не оцениваются: сделок 2"
    assert want in head["warnings"] and head["warnings"] == row["warnings"] and head["state"] == row["state"] == "warn"
