"""Why a dataset is not in force (TDM#445): ``desk.daystatus.dataset_files`` (``/api/v1/inbox/datasets/{dataset}``,
C16 «отчёта ОВП нет») and ``desk.daystatus.folders`` (``/api/v1/inbox/folders``, the settings page's «Папки с
отчётами»). The reference adapter on synthetic files: a folder without an OCP file, an OCP file rejected, one accepted
after the run, one behind the lookback, a file declined then read after an adapter change, a file in a subfolder of a
non-recursive folder, a folder saved but not scanned yet, files skipped by the scan."""

from __future__ import annotations

import json
import ntpath
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
BOM = "\ufeff"


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


def _good(inbox: Path) -> None:
    """A report folder with one good OCP report (of the 21st)."""
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.copy(DATASETS / "ocp_20261021.csv", inbox / "ocp_20261021.csv")


def cfg(tmp_path: Path, *inbox: Path, lookback: int = 0, **kw) -> Settings:
    return Settings(snapshot_cache=SNAPS, inbox_dirs=inbox, inbox_archive=tmp_path / "archive", private_adapter=None,
                    private_adapter_config=DATASETS / "config", inbox_poll_seconds=0, inbox_lookback_days=lookback,
                    home=tmp_path / "home", **kw)


def test_no_ocp_file_then_rejected_then_accepted_after_the_run(con, tmp_path):
    inbox = tmp_path / "in"
    _csv(inbox / "notes.txt", "not a report\n")
    s = cfg(tmp_path, inbox)
    x = daystatus.dataset_files(con, s, "ocp", D("2026-10-20"))
    assert x["last_scan"] is None and x["folders"] == {"count": 1, "scanned": 0, "missing": 0}
    scan.scan(con, s)

    x = daystatus.dataset_files(con, s, "ocp", D("2026-10-20"))
    assert x["sources"] == ["ocp_csv"] and x["name"] == "ОВП" and x["adapter"].startswith("ok ")
    assert x["files"]["total"] == 0 and x["files"]["later"]["total"] == 0 and x["latest"] is None
    assert x["unread"] == {"ignored": 1, "too_old": 0}  # match declined it: its source is unknown
    assert x["in_force"] is None and x["folders"] == {"count": 1, "scanned": 1, "missing": 0} and x["last_scan"]["at"]

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
    assert lt["reason"] == "книги нет в справочнике" and x["in_force"] is None
    assert "ZZ" not in str(lt) and "1000" not in str(lt)  # codes and counts, never a row's values

    # a good report of the 21st: on the 20th the latest file is still the rejected one, the 21st's is later
    shutil.copy(DATASETS / "ocp_20261021.csv", inbox / "ocp_20261021.csv")
    scan.scan(con, s)
    x20 = daystatus.dataset_files(con, s, "ocp", D("2026-10-20"))
    assert x20["latest"]["name"] == "ocp_20261020.csv" and x20["in_force"] is None
    assert x20["files"]["by_status"] == {"rejected": 1}
    assert x20["files"]["later"] == {"total": 1, "by_status": {"accepted": 1}, "first_date": "2026-10-21"}
    x21 = daystatus.dataset_files(con, s, "ocp", D("2026-10-21"))
    assert x21["files"]["by_status"] == {"rejected": 1, "accepted": 1} and x21["latest"]["status"] == "accepted"
    assert x21["latest"]["reason"] is None and x21["files"]["later"]["total"] == 0
    assert x21["in_force"]["units"] == 1 and x21["in_force"]["valid_date"] == "2026-10-21"
    # only later reports: the first after the date is shown, said to be after it
    x19 = daystatus.dataset_files(con, s, "ocp", D("2026-10-19"))
    assert x19["files"]["total"] == 0 and x19["files"]["later"]["total"] == 2
    assert x19["latest"]["name"] == "ocp_20261020.csv" and x19["latest"]["after"] is True and x19["in_force"] is None


def test_a_file_declined_then_read_is_counted_once(con, tmp_path):
    """The adapter fixed: a file ``ignored`` by the old fingerprint and read by the new one keeps its row in
    ``inbox_unread``; it counts as read, not as «не узнал» (review of #445)."""
    inbox = tmp_path / "in"
    _good(inbox)
    s = cfg(tmp_path, inbox)
    scan.scan(con, s)
    con.execute("INSERT INTO inbox_unread (path, size_bytes, mtime, status, adapter_fingerprint)"
                " SELECT path, size_bytes, mtime, 'ignored', 'old-fp' FROM inbox_sighting")
    (f,) = [x for x in daystatus.folders(con, s)["folders"] if not x["output"]]
    assert (f["files"], f["matched"], f["ignored"]) == (1, 1, 0)
    assert daystatus.dataset_files(con, s, "ocp", D("2026-10-21"))["unread"] == {"ignored": 0, "too_old": 0}
    # another folder's declined files are not this folder's, nor the dataset's «in the folders»
    con.execute("INSERT INTO inbox_unread (path, size_bytes, mtime, status, adapter_fingerprint)"
                " VALUES (%s, 1, now(), 'ignored', 'fp')", (str(tmp_path / "elsewhere" / "x.csv"),))
    assert daystatus.dataset_files(con, s, "ocp", D("2026-10-21"))["unread"]["ignored"] == 0


def test_too_old_files_count_for_their_source(con, tmp_path):
    inbox = tmp_path / "in"
    _csv(inbox / "ocp_20200115.csv", "entity;book_id;ccy;ocp_ccy\nENT1;D1;USD;1\n")
    s = cfg(tmp_path, inbox, lookback=30)
    scan.scan(con, s)
    x = daystatus.dataset_files(con, s, "ocp", D("2026-10-20"))
    assert x["files"]["total"] == 0 and x["unread"] == {"ignored": 0, "too_old": 1} and x["lookback_days"] == 30
    cash = daystatus.dataset_files(con, s, "cash_balances", D("2026-10-20"))
    assert cash["unread"]["too_old"] == 0 and cash["sources"] == ["cash_balances_csv"]
    (f,) = [x for x in daystatus.folders(con, s)["folders"] if not x["output"]]
    assert (f["files"], f["matched"], f["too_old"]) == (1, 0, 1)


def test_folders_say_what_each_folder_holds(con, tmp_path):
    inbox, other = tmp_path / "in", tmp_path / "other"
    _csv(inbox / "readme.txt", "x\n")
    shutil.copy(DATASETS / "ocp_20261020.csv", inbox / "ocp_20261020.csv")
    shutil.copy(DATASETS / "ocp_20261021.csv", inbox / "ocp_20261021.csv")
    shutil.copy(DATASETS / "cash_balances_20261020.csv", inbox / "cash_balances_20261020.csv")
    _csv(inbox / "sub" / "ocp_20261019.csv", "entity;book_id;ccy;ocp_ccy\nENT1;ZZ;USD;1\n")  # not scanned: no **
    _csv(inbox / "ocp_20261016.csv", "entity;book_id;ccy;ocp_ccy\nENT1;ZZ;USD;1\n")
    _csv(other / "deep" / "ocp_20261015.csv", "entity;book_id;ccy;ocp_ccy\nENT1;D1;USD;5\n")
    s = cfg(tmp_path, inbox, other / "**", tmp_path / "missing")
    scan.scan(con, s)

    doc = daystatus.folders(con, s)
    assert doc["adapter"].startswith("ok ") and doc["status_text"]["ignored"] == "не формат адаптера"
    assert doc["dataset_names"]["ocp"] == "ОВП" and doc["poll_seconds"] == 0
    f = {Path(x["path"]).name: x for x in doc["folders"]}
    a = f["in"]
    assert (a["scanned"], a["exists"], a["listed"], a["recursive"], a["output"]) == (True, True, 5, False, False)
    assert (a["files"], a["matched"], a["ignored"], a["too_old"], a["lost"]) == (5, 4, 1, 0, 1)
    assert a["by_status"] == {"accepted": 3, "rejected": 1}
    assert a["sources"] == [{"source": "cash_balances_csv", "datasets": ["cash_balances"], "files": 1,
                             "by_status": {"accepted": 1}},
                            {"source": "ocp_csv", "datasets": ["ocp"], "files": 3,
                             "by_status": {"accepted": 2, "rejected": 1}}]
    assert {d["dataset"]: (d["name"], d["latest_valid_date"], d["files"]) for d in a["datasets"]} == \
        {"cash_balances": ("Остатки на счетах", "2026-10-20", 1), "ocp": ("ОВП", "2026-10-21", 2)}
    assert "last_seen" not in a and a["dir"] == str(inbox)
    b = f["other"]  # recursive: its subfolders count
    assert b["recursive"] and b["matched"] == 1 and b["listed"] == 1 and b["datasets"][0]["latest_valid_date"] == "2026-10-15"
    c = f["missing"]
    assert (c["scanned"], c["exists"], c["listed"], c["files"], c["sources"]) == (True, False, 0, 0, [])
    x = daystatus.dataset_files(con, s, "ocp", D("2026-10-21"))
    assert x["folders"] == {"count": 3, "scanned": 3, "missing": 1} and x["unread"]["ignored"] == 1

    # a folder saved after the last scan: not scanned yet, not «no files»
    s2 = cfg(tmp_path, inbox, other / "**", tmp_path / "missing", tmp_path / "new")
    new = {Path(x["path"]).name: x for x in daystatus.folders(con, s2)["folders"]}["new"]
    assert (new["scanned"], new["exists"], new["listed"], new["files"]) == (False, None, None, 0)
    assert daystatus.dataset_files(con, s2, "ocp", D("2026-10-21"))["folders"]["scanned"] == 3


def test_a_file_whose_current_status_is_declined_is_not_matched(con, tmp_path):
    inbox = tmp_path / "in"
    _good(inbox)
    s = cfg(tmp_path, inbox)
    scan.scan(con, s)
    sha = con.execute("SELECT sha256 FROM inbox_sighting").fetchone()[0]
    con.execute("INSERT INTO inbox_file_status (sha256, status, adapter_fingerprint, path) VALUES"
                " (%s, 'ignored', 'fp2', 'x')", (sha,))  # offered again after a change, declined this time
    (f,) = [x for x in daystatus.folders(con, s)["folders"] if not x["output"]]
    assert (f["files"], f["matched"], f["ignored"], f["by_status"], f["sources"]) == (1, 0, 1, {}, [])
    assert daystatus.dataset_files(con, s, "ocp", D("2026-10-21"))["files"]["total"] == 0


def test_skipped_files_are_said_per_folder(con, tmp_path):
    inbox = tmp_path / "in"
    _good(inbox)
    s = cfg(tmp_path, inbox, inbox_max_file_mb=1e-6)  # every file too large: listed, skipped, never ledgered
    scan.scan(con, s)
    (f,) = [x for x in daystatus.folders(con, s)["folders"] if not x["output"]]
    assert (f["scanned"], f["listed"], f["skipped"], f["files"]) == (True, 1, {"too_large": 1}, 0)
    assert daystatus.dataset_files(con, s, "ocp", D("2026-10-21"))["last_scan"]["skipped"] == {"too_large": 1}


def test_entries_resolve_as_the_server_does(con, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"inbox_dirs": [
        {"path": "rel", "recursive": True}, {"path": str(tmp_path / "abs")}, {"path": "~/x"}, {"path": " "}]}))
    monkeypatch.setenv("HOME", str(tmp_path / "u"))
    s = cfg(tmp_path, home / "rel" / "**", tmp_path / "abs", tmp_path / "u" / "x")
    doc = daystatus.folders(con, s)
    assert [(e["path"], e["folder"]) for e in doc["entries"]] == [("rel", 0), (str(tmp_path / "abs"), 1), ("~/x", 2)]
    assert doc["from_env"] is False
    s1 = cfg(tmp_path, home / "rel", tmp_path / "abs")  # not recursive on the server: another folder
    assert [e["folder"] for e in daystatus.folders(con, s1)["entries"]] == [None, 1, None]
    monkeypatch.setenv("DESK_INBOX_DIRS", str(tmp_path / "env"))
    assert daystatus.folders(con, s1)["from_env"] is True


def test_in_folder(monkeypatch):
    a = str(Path("/r/a"))
    p = os.path.join
    assert scan.in_folder(p("/r", "a", "f.csv"), a, False) and not scan.in_folder(p("/r", "a", "s", "g.csv"), a, False)
    assert scan.in_folder(p("/r", "a", "s", "g.csv"), a, True) and scan.in_folder(p("/r", "a", "f.csv"), a + os.sep, True)
    assert not scan.in_folder(p("/r", "ab", "h.csv"), a, True)  # never /r/ab
    assert scan.folder_of(Path("/r/a") / "**") == (a, True) and scan.folder_of(Path("/r/a")) == (a, False)
    with monkeypatch.context() as m:  # Windows: regardless of case, in Python on both sides (Cyrillic too)
        m.setattr(os.path, "normcase", ntpath.normcase)
        m.setattr(os, "sep", "\\")
        assert scan.in_folder("C:\\Отчёты\\ОВП\\f.csv", "c:\\отчёты\\овп", False)
        assert not scan.in_folder("C:\\Отчёты\\ОВП\\s\\f.csv", "C:\\Отчёты\\ОВП", False)


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
    assert x.json()["settings_link"] is True  # this computer, sign-in off: the settings answer it
    assert client.get("/api/v1/inbox/datasets/ocp", params={"on": "21.10"}).status_code == 400
    assert client.get("/api/v1/inbox/datasets/a;b").status_code == 400
    f = client.get("/api/v1/inbox/folders")
    assert f.status_code == 200 and f.json()["folders"][0]["matched"] == 1 and f.json()["from_env"] is True

    # sign-in on: the dataset's files for every signed-in user (as the day status), the folders' paths for an admin
    with store.connect(store_url) as con:
        auth.add_user(con, "vic", "viewer", "viewer-pass-1", actor="test")
    remote = TestClient(create_app(store_url, auth=auth.AuthConfig(enabled=True)), base_url="http://10.0.0.7:8000",
                        client=("10.0.0.7", 50000))
    assert remote.get("/api/v1/inbox/datasets/ocp").status_code == 401
    assert remote.post("/api/v1/auth/login", json={"username": "vic", "password": "viewer-pass-1"}).status_code == 200
    seen = remote.get("/api/v1/inbox/datasets/ocp", params={"on": "2026-10-21"})
    assert seen.status_code == 200 and seen.json()["settings_link"] is False  # a viewer: no link to the settings
    assert remote.get("/api/v1/inbox/folders").status_code == 403
