"""R22, position loading fast and complete: warmed workers and match batches in parallel, a second try for the
files of a call that timed out or crashed, the compact instruments index, one directory listing, the lookback by
report date, the EOD runs held while the inbox drains, and ``desk inbox reconcile`` (CLI, API, diagnostics).
Synthetic data only."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import cli, diagnostics, store
from desk.api import create_app
from desk.autopilot import Autopilot
from desk.inbox import conformance, scan, static_index
from desk.inbox.loader import load_adapter
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
PRIV, CONFIG, SNAPS, ADAPTERS = FIX / "private" / "v1", FIX / "private" / "v1" / "config", FIX / "mdhub_v1", \
    FIX / "adapters"
GOOD = str(ADAPTERS / "good_adapter")
STATIC = SNAPS / "4f173275220b492c558acad982351bac01fe73baea6d50dcb0fcc38400953c4a.json.gz"
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


def cfg(tmp_path: Path, inbox: Path, adapter: str | None = GOOD, **kw) -> Settings:
    return Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                    private_adapter=adapter, private_adapter_config=CONFIG,
                    **{"inbox_poll_seconds": 0, "inbox_lookback_days": 0, **kw})


def put(inbox: Path, *names: str) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.copy(PRIV / n, inbox / n)


def slow_adapter(tmp_path: Path, name: str, *, import_s: float = 0.0, match_s: float = 0.0,
                 crash_on: str | None = None) -> str:
    """The good adapter's layout with a slow import, a slow ``match`` per file, or a ``match`` that kills its
    process on files whose name holds ``crash_on``."""
    d = tmp_path / name
    shutil.copytree(ADAPTERS / "good_adapter", d)
    src = (d / "__init__.py").read_text(encoding="utf-8")
    src = src.replace("import os\n", f"import os\nimport time\ntime.sleep({import_s})\n", 1)
    body = f"    time.sleep({match_s})\n"
    if crash_on:
        body += f"    if {crash_on!r} in os.path.basename(path):\n        os._exit(3)\n"
    src = src.replace("def match(path, ctx):\n", "def match(path, ctx):\n" + body, 1)
    (d / "__init__.py").write_text(src.replace('"name": "good_adapter"', f'"name": "{name}"'), encoding="utf-8")
    return str(d)


# ---- the match step -------------------------------------------------------------------------------------------


def test_a_slow_adapter_import_does_not_eat_the_match_timeout(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv", "positions_20261015.csv", "positions_20261017.csv")
    s = cfg(tmp_path, inbox, slow_adapter(tmp_path, "slow_import", import_s=1.5), adapter_match_timeout_s=1.0)
    r = scan.scan(con, s)
    assert r.files == {"accepted": 2, "partially_accepted": 1} and not r.match, r.text()  # warmed: no timeout
    assert "match_s" in r.timings and "match: " not in r.text()


def test_files_of_a_timed_out_match_call_are_matched_again_with_more_time(con, tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "MATCH_RETRY_MIN_S", 30.0)
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv", "positions_20261015.csv", "positions_20261017.csv")
    s = cfg(tmp_path, inbox, slow_adapter(tmp_path, "slow_match", match_s=0.6), adapter_match_timeout_s=1.0)
    r = scan.scan(con, s)  # 3 files x 0.6 s in one call of 1 s: timed out, then again in calls of 10 with 30 s
    assert r.match == {"retried": 3, "recovered": 3} and r.files == {"accepted": 2, "partially_accepted": 1}, r.text()
    assert "match: recovered 3, retried 3" in r.text()


def test_a_crashing_match_is_isolated_and_offered_again_hours_later(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv", "positions_20261015.csv")
    shutil.copy(PRIV / "positions_20261017.csv", inbox / "positions_20261017_9.csv")
    s = cfg(tmp_path, inbox, slow_adapter(tmp_path, "crashy", crash_on="_9"))
    now = datetime.now(UTC)
    r = scan.scan(con, s, now=now)  # the crash takes the whole first call; the second try isolates the file
    assert r.match == {"retried": 3, "recovered": 2}, r.text()
    assert r.files == {"accepted": 1, "partially_accepted": 1, "match_failed": 1}, r.text()
    assert con.execute("SELECT error_type FROM inbox_file_status WHERE status = 'match_failed'").fetchone() == \
        ("runner_exit",)
    assert scan.scan(con, s, now=now + timedelta(hours=1)).files == {}  # not asked again at once
    r = scan.scan(con, s, now=now + timedelta(hours=scan.TRANSIENT_RETRY_H + 1))  # hours later: once more
    assert r.files == {"match_failed": 1} and r.match == {"retried": 1}, r.text()


def barrier_adapter(tmp_path: Path, name: str, parties: int, wait_s: float) -> tuple[str, Path]:
    """The good adapter whose worker, on its first ``match`` call, leaves a mark and waits (at most ``wait_s``) for
    the marks of ``parties`` workers, then notes whether they came. Every worker seeing them all means the last one
    to come found the others still inside their first call: all of them held a batch at the same moment. Batches
    handed out one after another leave the first worker waiting alone, so it notes that they did not come."""
    d = Path(slow_adapter(tmp_path, name))
    marks = tmp_path / f"{name}_marks"
    marks.mkdir()
    src = (d / "__init__.py").read_text(encoding="utf-8")
    body = (f"    global _waited\n"
            f"    if not _waited:\n"
            f"        _waited = True\n"
            f"        marks = {str(marks)!r}\n"
            f"        open(os.path.join(marks, f'{{os.getpid()}}.in'), 'w').close()\n"
            f"        end, met = time.monotonic() + {wait_s}, False\n"
            f"        while time.monotonic() < end and not met:\n"
            f"            met = len([n for n in os.listdir(marks) if n.endswith('.in')]) >= {parties}\n"
            f"            time.sleep(0.01)\n"
            f"        open(os.path.join(marks, f'{{os.getpid()}}.{{\"met\" if met else \"alone\"}}'), 'w').close()\n")
    src = src.replace("def match(path, ctx):\n", "_waited = False\n\n\ndef match(path, ctx):\n" + body, 1)
    (d / "__init__.py").write_text(src, encoding="utf-8")
    return str(d), marks


def test_match_batches_go_to_all_workers_at_once(con, tmp_path, monkeypatch):
    monkeypatch.setenv(scan.READ_WORKERS_ENV, "3")
    inbox = tmp_path / "in"
    inbox.mkdir()
    for i in range(150):  # 3 batches of 50: one for each worker
        (inbox / f"old_{i:03d}.xls").write_bytes(b"x")
    # no wall-clock threshold (flaky under CPU load, #478): the workers wait for each other, with a wide margin
    adapter, marks = barrier_adapter(tmp_path, "par", parties=3, wait_s=20.0)
    r = scan.scan(con, s := cfg(tmp_path, inbox, adapter))
    assert r.files == {"ignored": 150}, r.text()
    assert s.adapter_match_timeout_s > 20.0 and "match_s" in r.timings
    seen = Counter(n.split(".")[1] for n in os.listdir(marks))
    assert seen == {"in": 3, "met": 3}, seen  # three workers, each in its first batch while the others were


def test_warm_reports_workers_that_fail_to_start(tmp_path):
    h = load_adapter(slow_adapter(tmp_path, "warm_slow", import_s=2.0), str(CONFIG), None)
    with scan.runner.pooled(2):
        assert scan.runner.call_warm(h, None, 0.5) == {"timeout": 2}
        assert scan.runner.call_warm(h, None, 10) == {"ok": 2}
    assert scan.runner.call_warm(h, None, 10) == {}  # no pool: nothing to warm


# ---- the instruments index ------------------------------------------------------------------------------------


def _static(folder: Path, business_date: str, edit) -> str:
    """A copy of the fixture's static snapshot for another date, its instruments changed by ``edit``."""
    with gzip.open(STATIC) as f:
        doc = json.load(f)
    doc["business_date"] = business_date
    doc["data"]["instruments"] = edit(doc["data"]["instruments"])
    raw = json.dumps(doc).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    (folder / f"{sha}.json.gz").write_bytes(gzip.compress(raw))
    return sha


def test_the_index_holds_older_snapshots_as_differences(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    old = _static(cache, "2026-09-01", lambda ins: ins + [{**ins[0], "secid": "OLDONLY1", "isin": "RU000OLD0001"}])
    new = _static(cache, "2026-10-15", lambda ins: [{**i, "name": i["name"] + " (new)"} for i in ins])
    doc = static_index.build_index(cache, tmp_path / "idx.json", ["2026-09-10"])
    assert doc["format"] == "desk.inbox.instruments/2" and [x["id"] for x in doc["snapshots"]] == [old, new]
    assert "delta" in doc["snapshots"][0] and "secid" in doc["snapshots"][1]
    delta = doc["snapshots"][0]["delta"]
    assert set(delta["secid"]["set"]) == {i for i in delta["secid"]["set"]} and "OLDONLY1" in delta["secid"]["set"]
    inst = static_index.Instruments.from_file(tmp_path / "idx.json")
    assert inst.lookup("isin", "RU000OLD0001", "2026-09-10")["instrument_id"] == "OLDONLY1"
    assert inst.lookup("isin", "RU000OLD0001") is None  # not in the newest snapshot
    assert inst.lookup("isin", "RU000A1038V6", "2026-09-10")["name"].endswith("(new)") is False
    assert inst.lookup("isin", "RU000A1038V6")["name"].endswith("(new)")
    full = {"format": "desk.inbox.instruments/1", "snapshots": [  # format 1 documents still read
        {"id": "a", "business_date": "2026-01-01", "secid": {"S1": {"instrument_id": "S1"}}, "isin": {"I1": "S1"},
         "regnum": {}}]}
    assert static_index.Instruments(full).lookup("isin", "I1") == {"instrument_id": "S1"}


def test_a_scan_rebuilds_the_index_at_most_once_and_the_next_not_at_all(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv", "positions_20261015.csv")
    s = cfg(tmp_path, inbox)
    r = scan.scan(con, s)
    assert r.timings.get("index_builds") == 1, r.text()
    put(inbox, "positions_20261017.csv")
    r = scan.scan(con, s)
    assert "index_builds" not in r.timings and r.files == {"accepted": 1}, r.text()


# ---- listing and the lookback ---------------------------------------------------------------------------------


def test_one_listing_with_stats_skips_temp_hidden_and_linked_folders(tmp_path):
    root = tmp_path / "in"
    (root / "sub" / "deeper").mkdir(parents=True)
    for p in ("a.csv", "~$a.xlsx", ".hidden", "b.part", "sub/c.csv", "sub/deeper/d.csv"):
        (root / p).write_text("x", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "e.csv").write_text("x", encoding="utf-8")
    try:
        os.symlink(other, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    flat = conformance.list_entries([str(root)])
    assert [Path(p).name for p, _ in flat] == ["a.csv"] and flat[0][1].st_size == 1
    deep = [Path(p).relative_to(root).as_posix() for p in conformance.list_files([f"{root}/**"])]
    assert deep == ["a.csv", "sub/c.csv", "sub/deeper/d.csv"]  # a linked folder is not followed


def test_a_recursive_listing_skips_dot_folders_such_as_the_settings_checks_copy(tmp_path):
    """TDM#130: the settings page's check copies the config folder into ``.desk-settings-check-*`` beside it, with
    its subfolders linked; a recursive inbox folder above it offers none of that, only the real files."""
    root = tmp_path / "in"
    cfg = root / "adapter-config"
    (cfg / "reports").mkdir(parents=True)
    (cfg / "options.json").write_text("{}", encoding="utf-8")
    (cfg / "reports" / "r.csv").write_text("x", encoding="utf-8")
    check = root / ".desk-settings-check-x"
    (check / "deeper").mkdir(parents=True)
    (check / "options.json").write_text("{}", encoding="utf-8")
    (check / "deeper" / "d.csv").write_text("x", encoding="utf-8")
    try:
        os.symlink(cfg / "reports", check / "reports", target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    deep = [Path(p).relative_to(root).as_posix() for p in conformance.list_files([f"{root}/**"])]
    assert deep == ["adapter-config/options.json", "adapter-config/reports/r.csv"]
    assert conformance.list_files([str(check)]) == [str(check / "options.json")]  # a folder named as such: listed


def test_the_lookback_goes_by_report_date_not_by_modification_time(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261017.csv", "positions_20261015.csv")
    now = datetime(2026, 10, 27, 9, tzinfo=UTC)  # lookback 10: report dates from 2026-10-17 on
    weeks = datetime(2026, 9, 30, 12, tzinfo=UTC).timestamp()  # modified before the window, within the slack
    for n in ("positions_20261017.csv", "positions_20261015.csv"):
        os.utime(inbox / n, (weeks, weeks))
    r = scan.scan(con, cfg(tmp_path, inbox, inbox_lookback_days=10), now=now)
    assert r.files == {"accepted": 1, "ignored_too_old": 1} and not r.skipped, r.text()


# ---- EOD runs while the inbox drains ---------------------------------------------------------------------------


def _market(folder: Path, business_date: str) -> None:
    doc = {"kind": "market", "cut_type": "eod_final", "business_date": business_date}
    raw = json.dumps(doc).encode()
    (folder / f"{hashlib.sha256(raw).hexdigest()}.json.gz").write_bytes(gzip.compress(raw))


def test_eod_waits_for_the_inbox_backlog_except_the_latest_date(tmp_path, store_url, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    for d in ("2026-10-14", "2026-10-15", "2026-10-16"):
        _market(cache, d)
    shutil.copy(STATIC, cache / STATIC.name)
    inbox = tmp_path / "in"
    put(inbox, "positions_20261015.csv", "positions_20261016.csv", "positions_20261017.csv")
    s = Settings(home=tmp_path / "home", snapshot_cache=cache, store_url=store_url, inbox_dirs=(inbox,),
                 inbox_archive=tmp_path / "archive", private_adapter=GOOD, private_adapter_config=CONFIG,
                 inbox_poll_seconds=0, inbox_lookback_days=0)
    with store.connect(store_url) as c:
        assert scan.scan(c, s).files.get("accepted")
    ran: list[str] = []
    monkeypatch.setattr(cli, "eod_run", lambda settings, d, **kw: ran.append(d))
    waiting = Counter(waiting_calendar=4)
    monkeypatch.setattr(scan, "scan", lambda *a, **kw: scan.ScanResult("ok stub", files=waiting))
    ap = Autopilot(settings_loader=lambda: s, today=lambda: date(2026, 10, 19))
    later = [2]

    def sync(*a, **kw):  # the sync left hub dates for the next cycle
        ap._sync_later = later[0]
        return "ok", "stub"

    monkeypatch.setattr(ap, "_sync", sync)
    st = ap.run_cycle()
    eod = next(x for x in st["steps"] if x["name"] == "eod")
    assert ran == ["2026-10-16"] and "2 dates wait for the inbox backlog (4 files wait" in eod["detail"], eod
    waiting.clear()  # the backlog is in
    later[0] = 0
    ap.run_cycle()
    assert ran[1:] == ["2026-10-16", "2026-10-14", "2026-10-15"]  # the latest first while it has no run


# ---- reconcile ------------------------------------------------------------------------------------------------


def test_reconcile_follows_each_books_rows_and_names_the_losses(con, tmp_path):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261014.csv", "positions_20261015.csv", "positions_20261017.csv")
    (inbox / "not_ours.txt").write_text("x", encoding="utf-8")
    s = cfg(tmp_path, inbox)
    scan.scan(con, s)
    r = scan.reconcile(con, s, D("2026-10-16"))
    assert r["date"] == "2026-10-16" and r["run_id"] is None
    books = {b["book_id"]: b for b in r["books"]}
    assert set(books) == {"B1", "B2", "B3"}  # B9 is inactive
    b1 = books["B1"]
    assert b1["valid_date"] == "2026-10-16" and b1["file"] == "positions_20261017.csv"
    assert b1["rows_in_report"] == b1["rows_accepted"] == 3 and b1["rows_in_run"] is None and b1["flags"] == []
    assert books["B2"]["units_rejected"] == 0 and books["B2"]["flags"] == []  # 13.10 rejected, later ones in
    assert books["B3"]["rows_accepted"] == 0 and books["B3"]["issues"] == {"book_closed_by_omission": 1}
    assert r["files"] == {"accepted": 2, "ignored": 1, "partially_accepted": 1}
    assert {x["code"] for x in r["losses"]} == {"ignored", "book_closed_by_omission"}
    assert r["last_scan"]["files"]["ignored"] == 1

    r = scan.reconcile(con, s, D("2026-10-14"))  # B2 rejected in the report of 14.10: 13.10 still in force
    b2 = next(b for b in r["books"] if b["book_id"] == "B2")
    assert b2["flags"] == ["unit_rejected"] and b2["issues"] == {"bad_number": 1} and b2["valid_date"] == "2026-10-13"
    losses = {x["code"]: x for x in r["losses"]}
    assert losses["unit_rejected"]["count"] == 1 and "отклонена" in losses["unit_rejected"]["what"]
    text = scan.reconcile_text(r)
    assert text.startswith("positions for 2026-10-14, no default run") and "unit_rejected" in text

    d = diagnostics.reconcile_summary(r)  # the diagnostics: totals and codes, no book or file
    assert d["books_by_flag"] == {"unit_rejected": 1} and d["losses"]["unit_rejected"] == 1
    flat = json.dumps(d)
    assert "B1" not in flat and "positions_20261017" not in flat


def test_reconcile_cli_and_api_with_a_run(store_url, tmp_path, monkeypatch, capsys):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261016.csv")
    for k, v in {"DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": store_url,
                 "DESK_INBOX_DIRS": str(inbox), "DESK_INBOX_ARCHIVE": str(tmp_path / "arch"),
                 "DESK_SNAPSHOT_CACHE": str(SNAPS), "DESK_PRIVATE_ADAPTER_CONFIG": str(CONFIG),
                 "DESK_INBOX_POLL_SECONDS": "0", "DESK_INBOX_LOOKBACK_DAYS": "0", "DESK_HOME": str(tmp_path / "h")}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["inbox", "scan"]) == 0
    assert cli.main(["eod", "--date", "2026-10-15", "--snapshots", str(SNAPS)]) == 0
    capsys.readouterr()
    assert cli.main(["inbox", "reconcile", "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["date"] == "2026-10-15" and r["run_id"] == 1
    b1 = next(b for b in r["books"] if b["book_id"] == "B1")
    assert b1["rows_accepted"] == b1["rows_in_run"] == 3 and b1["rows_valued"] == 3 and b1["flags"] == []
    assert r["totals"]["rows_in_run"] == r["totals"]["rows_accepted"]
    assert cli.main(["inbox", "reconcile"]) == 0
    assert "default run 1" in capsys.readouterr().out

    client = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    got = client.get("/api/v1/inbox/reconcile", params={"on": "2026-10-15"})
    assert got.status_code == 200 and got.json()["run_id"] == 1
    assert client.get("/api/v1/inbox/reconcile", params={"on": "15.10"}).status_code == 400
