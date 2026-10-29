"""Why a dataset is not in force (TDM#445): ``desk.daystatus.dataset_files`` (``/api/v1/inbox/datasets/{dataset}``,
C16 «отчёта ОВП нет») and ``desk.daystatus.folders`` (``/api/v1/inbox/folders``, the settings page's «Папки с
отчётами»). The reference adapter on synthetic files: a folder without an OCP file, an OCP file rejected, one accepted
after the run, one behind the lookback, a file in a subfolder of a non-recursive folder."""

from __future__ import annotations

import os
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import daystatus, store
from desk.api import auth, create_app
from desk.inbox import scan
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
DATASETS, SNAPS = FIX / "private" / "v1" / "datasets", FIX / "mdhub_v1"
D = date.fromisoformat
BOM = "﻿"


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    monkeypatch.setattr(scan, "last_scan", None)
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


@pytest.fixture
def con(store_url):
    with store.connect(store_url) as c:
        yield c


def _csv(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((BOM + text.replace("\n", "\r\n")).encode())


def cfg(tmp_path: Path, inbox: Path, lookback: int = 0) -> Settings:
    return Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive", private_adapter=None,
                    private_adapter_config=DATASETS / "config", inbox_poll_seconds=0, inbox_lookback_days=lookback)


def test_no_ocp_file_then_rejected_then_accepted_after_the_run(con, tmp_path):
    inbox = tmp_path / "in"
    _csv(inbox / "notes.txt", "not a report\n")
    s = cfg(tmp_path, inbox)
    scan.scan(con, s)

    x = daystatus.dataset_files(con, s, "ocp", D("2026-10-20"))
    assert x["sources"] == ["ocp_csv"] and x["name"] == "ОВП" and x["adapter"].startswith("ok ")
    assert x["files"] == {"total": 0, "by_status": {}, "status_text": {}} and x["latest"] is None
    assert x["unread"] == {"ignored": 1, "too_old": 0}  # match declined it: its source is unknown
    assert x["in_force"] is None and x["folders"] == {"count": 1, "missing": 0} and x["last_scan"]["at"]

    # an OCP file whose only book the adapter's books do not know: the unit and the file are rejected
    _csv(inbox / "ocp_20261020.csv", "entity;book_id;ccy;ocp_ccy\nENT1;ZZ;USD;1000\n")
    scan.scan(con, s)
    x = daystatus.dataset_files(con, s, "ocp", D("2026-10-20"))
    assert x["files"]["total"] == 1 and x["files"]["by_status"] == {"rejected": 1}
    assert x["files"]["status_text"] == {"rejected": "отклонён"}
    lt = x["latest"]
    assert (lt["name"], lt["status"], lt["source"], lt["report_date"], lt["after"]) == \
        ("ocp_20261020.csv", "rejected", "ocp_csv", "2026-10-20", False)
    assert lt["units"] == {"rejected": 1} and "unknown_book" in [i["code"] for i in lt["issues"]]
    assert lt["why"] == scan.LOSS_REASONS["rejected"] and x["in_force"] is None
    assert "ZZ" not in str(lt) and "1000" not in str(lt)  # codes and counts, never a row's values

    # a good report of the 21st: on the 20th the latest file is still the rejected one, on the 21st it is in force
    shutil.copy(DATASETS / "ocp_20261021.csv", inbox / "ocp_20261021.csv")
    scan.scan(con, s)
    x20 = daystatus.dataset_files(con, s, "ocp", D("2026-10-20"))
    assert x20["latest"]["name"] == "ocp_20261020.csv" and x20["in_force"] is None
    x21 = daystatus.dataset_files(con, s, "ocp", D("2026-10-21"))
    assert x21["files"]["by_status"] == {"rejected": 1, "accepted": 1} and x21["latest"]["status"] == "accepted"
    assert x21["in_force"]["units"] == 1 and x21["in_force"]["valid_date"] == "2026-10-21"
    # only later reports: the newest is shown, said to be after the date
    x19 = daystatus.dataset_files(con, s, "ocp", D("2026-10-19"))
    assert x19["latest"]["name"] == "ocp_20261021.csv" and x19["latest"]["after"] is True and x19["in_force"] is None


def test_too_old_files_count_for_their_source(con, tmp_path):
    inbox = tmp_path / "in"
    _csv(inbox / "ocp_20200115.csv", "entity;book_id;ccy;ocp_ccy\nENT1;D1;USD;1\n")
    s = cfg(tmp_path, inbox, lookback=30)
    scan.scan(con, s)
    x = daystatus.dataset_files(con, s, "ocp", D("2026-10-20"))
    assert x["files"]["total"] == 0 and x["unread"] == {"ignored": 0, "too_old": 1} and x["lookback_days"] == 30
    cash = daystatus.dataset_files(con, s, "cash_balances", D("2026-10-20"))
    assert cash["unread"]["too_old"] == 0 and cash["sources"] == ["cash_balances_csv"]


def test_folders_say_what_each_folder_holds(con, tmp_path):
    inbox, other = tmp_path / "in", tmp_path / "other"
    _csv(inbox / "readme.txt", "x\n")
    shutil.copy(DATASETS / "ocp_20261020.csv", inbox / "ocp_20261020.csv")
    shutil.copy(DATASETS / "ocp_20261021.csv", inbox / "ocp_20261021.csv")
    shutil.copy(DATASETS / "cash_balances_20261020.csv", inbox / "cash_balances_20261020.csv")
    _csv(inbox / "sub" / "ocp_20261019.csv", "entity;book_id;ccy;ocp_ccy\nENT1;ZZ;USD;1\n")  # not scanned: no **
    _csv(inbox / "ocp_20261016.csv", "entity;book_id;ccy;ocp_ccy\nENT1;ZZ;USD;1\n")
    s = Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox, other / "**", tmp_path / "missing"),
                 inbox_archive=tmp_path / "archive", private_adapter=None, private_adapter_config=DATASETS / "config",
                 inbox_poll_seconds=0, inbox_lookback_days=0)
    _csv(other / "deep" / "ocp_20261015.csv", "entity;book_id;ccy;ocp_ccy\nENT1;D1;USD;5\n")
    scan.scan(con, s)

    doc = daystatus.folders(con, s)
    assert doc["adapter"].startswith("ok ") and doc["status_text"]["ignored"] == "не формат адаптера"
    f = {Path(x["path"]).name: x for x in doc["folders"]}
    a = f["in"]
    assert (a["exists"], a["recursive"], a["output"]) == (True, False, False)
    assert (a["files"], a["matched"], a["ignored"], a["too_old"], a["lost"]) == (5, 4, 1, 0, 1)
    assert a["by_status"] == {"accepted": 3, "rejected": 1}
    assert a["sources"] == [{"source": "cash_balances_csv", "datasets": ["cash_balances"], "files": 1,
                             "by_status": {"accepted": 1}},
                            {"source": "ocp_csv", "datasets": ["ocp"], "files": 3,
                             "by_status": {"accepted": 2, "rejected": 1}}]
    assert {d["dataset"]: (d["latest_valid_date"], d["files"]) for d in a["datasets"]} == \
        {"cash_balances": ("2026-10-20", 1), "ocp": ("2026-10-21", 2)}
    assert a["last_seen"] and a["dir"] == str(inbox)
    b = f["other"]  # recursive: its subfolders count
    assert b["recursive"] and b["matched"] == 1 and b["datasets"][0]["latest_valid_date"] == "2026-10-15"
    c = f["missing"]
    assert c["exists"] is False and c["files"] == 0 and c["sources"] == [] and c["last_seen"] is None
    x = daystatus.dataset_files(con, s, "ocp", D("2026-10-21"))
    assert x["folders"] == {"count": 3, "missing": 1} and x["unread"]["ignored"] == 1


def test_under_matches_the_scans_paths(con):
    con.execute("INSERT INTO inbox_unread (path, size_bytes, mtime, status, adapter_fingerprint) VALUES"
                " (%s, 1, now(), 'ignored', 'fp'), (%s, 1, now(), 'ignored', 'fp'), (%s, 1, now(), 'ignored', 'fp')",
                (os.path.join("/r", "a", "f.csv"), os.path.join("/r", "a", "s", "g.csv"),
                 os.path.join("/r", "ab", "h.csv")))

    def n(folders):
        cond, args = daystatus._under(folders)
        return con.execute(f"SELECT count(*) FROM inbox_unread WHERE {cond}", args).fetchone()[0]

    a = str(Path("/r/a"))
    assert n([(a, False)]) == 1 and n([(a, True)]) == 2 and n([(a + os.sep, True)]) == 2  # never /r/ab
    assert n([]) == 0 and n([(a, False), (str(Path("/r/ab")), False)]) == 2
    assert daystatus._folder(Path("/r/a") / "**") == (a, True) and daystatus._folder(Path("/r/a")) == (a, False)


def test_routes(store_url, tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    inbox.mkdir()
    shutil.copy(DATASETS / "ocp_20261021.csv", inbox / "ocp_20261021.csv")
    for k, v in {"DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": store_url,
                 "DESK_INBOX_DIRS": str(inbox), "DESK_INBOX_ARCHIVE": str(tmp_path / "arch"),
                 "DESK_SNAPSHOT_CACHE": str(SNAPS), "DESK_PRIVATE_ADAPTER_CONFIG": str(DATASETS / "config"),
                 "DESK_INBOX_POLL_SECONDS": "0", "DESK_INBOX_LOOKBACK_DAYS": "0", "DESK_HOME": str(tmp_path / "h")}.items():
        monkeypatch.setenv(k, v)
    from desk.settings import load
    with store.connect(store_url) as con:
        scan.scan(con, load())
    client = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    x = client.get("/api/v1/inbox/datasets/ocp", params={"on": "2026-10-21"})
    assert x.status_code == 200 and x.headers["cache-control"] == "no-store"
    assert x.json()["in_force"]["valid_date"] == "2026-10-21" and x.json()["latest"]["status"] == "accepted"
    assert client.get("/api/v1/inbox/datasets/ocp", params={"on": "21.10"}).status_code == 400
    assert client.get("/api/v1/inbox/datasets/a;b").status_code == 400
    f = client.get("/api/v1/inbox/folders")
    assert f.status_code == 200 and f.json()["folders"][0]["matched"] == 1

    # sign-in on: the dataset's files for every signed-in user (as the day status), the folders' paths for an admin
    with store.connect(store_url) as con:
        auth.add_user(con, "vic", "viewer", "viewer-pass-1", actor="test")
    remote = TestClient(create_app(store_url, auth=auth.AuthConfig(enabled=True)), base_url="http://10.0.0.7:8000",
                        client=("10.0.0.7", 50000))
    assert remote.get("/api/v1/inbox/datasets/ocp").status_code == 401
    assert remote.post("/api/v1/auth/login", json={"username": "vic", "password": "viewer-pass-1"}).status_code == 200
    assert remote.get("/api/v1/inbox/datasets/ocp", params={"on": "2026-10-21"}).status_code == 200
    assert remote.get("/api/v1/inbox/folders").status_code == 403
