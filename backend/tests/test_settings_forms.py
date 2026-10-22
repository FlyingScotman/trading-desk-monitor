"""Settings tabs as forms (08 §4 ``settings_tabs``, FND-4, R40): the schema check, the adapter's dry run, the write
with its revisions (``adapter_config_rev``, migration 0018), the lists the forms offer, and that a failing store
never blocks a save. The tabs are the trades-data-adapter's own (``fixtures/settings_tabs``: its describe() output
and synthetic files, no firm values)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from desk.api.app import create_app
from desk.inbox import scan
from desk.settings_forms import check_schema, file_text

FIX = Path(__file__).parent / "fixtures" / "settings_tabs"
TABS = json.loads((FIX / "tda_settings_tabs.json").read_text(encoding="utf-8"))
SCHEMA = {t["id"]: t["schema"] for t in TABS}
FILES = ["books.json", "entities.json", "funding.json", "options.json"]

ADAPTER = '''import json, os
SPI_VERSION = 1
_HERE = os.path.dirname(__file__)
TABS = json.load(open(os.path.join(_HERE, "tabs.json"), encoding="utf-8"))


def describe():
    return {"name": "forms_adapter", "version": "0.1.0", "spi": 1, "contract": "private/1.2",
            "sources": [{"source": "positions_csv", "dataset": "positions", "time_basis": "start_of_day",
                         "completeness": "full_snapshot", "scope": ["book_id"], "covers": "listed"}],
            "config_files": [{"name": n, "title": n, "required": False, "template": {}} for n in __FILES__],
            "settings_tabs": TABS}


def match(path, ctx):
    return None


def read(path, hit, ctx):
    return {}


def _rows(ctx, name, key):
    p = os.path.join(ctx.config_dir, name)
    if not os.path.isfile(p):
        return None
    return [{**r, "_src": "R%d" % (i + 1)} for i, r in enumerate(json.load(open(p, encoding="utf-8"))[key])]


def reference(ctx):
    books = [{k: v for k, v in b.items() if k != "codes"} for b in _rows(ctx, "books.json", "books") or []]
    ids = {b["book_id"] for b in books}
    issues = [{"severity": "warning", "code": "unknown_ocp_to", "src": b["_src"], "scope": {"book_id": b["book_id"]},
               "message": "books: ocp_to names no book"} for b in books if b.get("ocp_to") and b["ocp_to"] not in ids]
    out = {"books": books, "issues": issues}
    ents = _rows(ctx, "entities.json", "entities")
    if ents is not None:
        out["entities"] = ents
    return out
'''.replace("__FILES__", repr(FILES))


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    pkg = tmp_path / "forms_adapter_t"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(ADAPTER, encoding="utf-8")
    shutil.copy(FIX / "tda_settings_tabs.json", pkg / "tabs.json")
    cfg = h / "adapter-config"
    cfg.mkdir(parents=True)
    for name in FILES:
        shutil.copy(FIX / name, cfg / name)
    (h / "settings.json").write_text(json.dumps({"private_adapter": str(pkg)}), encoding="utf-8")
    scan.forget_adapters()
    return h


def _client(url: str) -> TestClient:
    return TestClient(create_app(url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


@pytest.fixture
def client(home: Path) -> TestClient:
    return _client("postgresql://unused@127.0.0.1:1/none")


def books(home: Path) -> dict:
    return json.loads((home / "adapter-config" / "books.json").read_text(encoding="utf-8"))


# ---- the schema check ------------------------------------------------------------------------------------------


def test_the_synthetic_files_pass_their_tabs() -> None:
    for tab, name in (("books", "books.json"), ("entities", "entities.json"), ("funding", "funding.json"),
                      ("sources", "options.json")):
        assert check_schema(SCHEMA[tab], json.loads((FIX / name).read_text(encoding="utf-8"))) == [], tab


def test_schema_issues_carry_the_adapters_codes_and_paths() -> None:
    doc = {"books": [{"book_id": "T1", "name": "", "desk": "D", "book_type": "SWAPS", "entity": "E",
                      "valid_from": "2026-13-01", "colour": "red", "x_portfolio": "P", "active": None},
                     {"book_id": "T2", "name": "N", "desk": "D", "book_type": "HTM"}],
           "_comment": "kept", "extra": 1}
    got = {(i["code"], tuple(i["path"]), i["severity"]) for i in check_schema(SCHEMA["books"], doc)}
    assert got == {("bad_value", ("books", 0, "name"), "error"), ("bad_value", ("books", 0, "book_type"), "error"),
                   ("bad_date", ("books", 0, "valid_from"), "error"),
                   ("unknown_field", ("books", 0, "colour"), "warning"),
                   ("missing_required", ("books", 1, "entity"), "error"),
                   ("unknown_config_key", ("extra",), "warning")}
    rules = {"rules": [{"rate_kind": "index_spread", "spread_bp": "1,5", "ccy": "RUBL", "day_count": "30/360"},
                       {"rate_kind": "fixed", "fixed_rate_pct": 5.5, "valid_from": "2026-10-19"}],
             "repo_policy": {"rate": None, "contract_rate_from_days": -1}}
    got = {(i["code"], tuple(i["path"])) for i in check_schema(SCHEMA["funding"], rules)}
    assert got == {("bad_number", ("rules", 0, "spread_bp")), ("bad_value", ("rules", 0, "ccy")),
                   ("bad_value", ("rules", 0, "day_count")), ("bad_number", ("repo_policy", "contract_rate_from_days"))}
    assert check_schema(SCHEMA["books"], [])[0]["code"] == "bad_config"
    codes = {"books": [{"book_id": "T", "name": "N", "desk": "D", "book_type": "HTM", "entity": "E",
                        "codes": {"no_such_source": ["A"], "*": [""]}}]}
    assert {i["code"] for i in check_schema(SCHEMA["books"], codes)} == {"bad_value"}
    assert file_text({"a": "Книга"}) == '{\n  "a": "Книга"\n}\n'


# ---- the routes -------------------------------------------------------------------------------------------------


def test_adapter_lists_its_settings_tabs(client: TestClient) -> None:
    j = client.get("/api/v1/adapter").json()
    assert j["loaded"] and [t["id"] for t in j["settings_tabs"]] == ["entities", "books", "funding", "sources"]
    assert j["settings_tabs"][1]["file"] == "books.json" and j["settings_tabs"][1]["schema"]["type"] == "object"
    r = client.get("/api/v1/adapter/config/books.json").json()
    assert r["exists"] and len(r["sha256"]) == 64


def test_check_reports_schema_then_the_adapters_dry_run(client: TestClient, home: Path) -> None:
    doc = books(home)
    doc["books"][0]["book_type"] = "SWAPS"
    j = client.post("/api/v1/adapter/settings/books/check", json={"data": doc}).json()
    assert not j["ok"] and [i["code"] for i in j["schema"]] == ["bad_value"] and not j["adapter_checked"]
    assert j["schema"][0]["path"] == ["books", 0, "book_type"]

    doc = books(home)
    doc["books"][1]["ocp_to"] = "NOPE"  # the adapter warns; the backend's contract check is silent on it
    doc["books"].append({"book_id": "N1", "name": "New", "desk": "SYN_DESK", "book_type": "HTM", "entity": "ENT9"})
    j = client.post("/api/v1/adapter/settings/books/check", json={"data": doc}).json()
    assert j["ok"] and j["adapter_checked"] and j["schema"] == []
    by_code = {i["code"]: i for i in j["adapter"]}
    assert by_code["unknown_ocp_to"]["row"] == 1 and by_code["unknown_ocp_to"]["new"]
    assert by_code["unknown_ocp_to"]["origin"] == "adapter"
    assert by_code["unknown_entity"]["origin"] == "backend" and by_code["unknown_entity"]["new"]
    assert j["text"] == file_text(doc) and j["current"]["text"] == (home / "adapter-config" / "books.json").read_text()
    assert books(home)["books"][1].get("ocp_to") is None  # a check writes nothing

    doc = books(home)
    doc["books"][0]["book_id"] = "D1"  # the backend's contract check: a repeated book_id is an error
    j = client.post("/api/v1/adapter/settings/books/check", json={"data": doc}).json()
    assert not j["ok"] and any(i["code"] == "duplicate_key" and i["new"] for i in j["adapter"])
    assert client.post("/api/v1/adapter/settings/nope/check", json={"data": {}}).status_code == 404
    assert client.post("/api/v1/adapter/settings/books/check", json={}).status_code == 422


def test_save_validates_guards_against_a_concurrent_edit_and_survives_no_store(client: TestClient, home: Path) -> None:
    cur = client.get("/api/v1/adapter/config/books.json").json()
    doc = books(home)
    doc["books"][0]["name"] = "Переименована"
    bad = json.loads(json.dumps(doc))
    bad["books"][0]["valid_from"] = "19.10.2026"
    r = client.put("/api/v1/adapter/settings/books", json={"data": bad, "base_sha256": cur["sha256"]})
    assert r.status_code == 422 and r.json()["details"]["issues"][0]["code"] == "bad_date"
    r = client.put("/api/v1/adapter/settings/books", json={"data": doc, "base_sha256": "0" * 64})
    assert r.status_code == 409
    fp = client.get("/api/v1/adapter").json()["fingerprint"]
    r = client.put("/api/v1/adapter/settings/books", json={"data": doc, "base_sha256": cur["sha256"]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["revision"] is None and "store unavailable" in j["history_error"]  # the file is written anyway
    assert books(home)["books"][0]["name"] == "Переименована" and j["text"] == file_text(doc)
    assert "Переименована" in (home / "adapter-config" / "books.json").read_text(encoding="utf-8")  # not \\u escapes
    assert client.get("/api/v1/adapter").json()["fingerprint"] != fp
    h = client.get("/api/v1/adapter/config/books.json/history").json()
    assert h["available"] is False and h["revisions"] == []
    ctx = client.get("/api/v1/adapter/settings-context").json()
    assert ctx["recent_books"] == [] and "RUB" in ctx["currencies"] and ctx["error"]


def test_revisions_who_when_and_a_hand_edit_in_between(home: Path, store_url: str) -> None:
    c = _client(store_url)
    path = home / "adapter-config" / "entities.json"
    first = json.loads(path.read_text(encoding="utf-8"))
    doc = json.loads(json.dumps(first))
    doc["entities"][0]["pnl_sweep"] = "daily"
    j = c.put("/api/v1/adapter/settings/entities", json={"data": doc}).json()
    assert j["revision"]["changed_by"] == "local" and j["revision"]["via"] == "form" and j["history_error"] is None
    path.write_text(json.dumps(first), encoding="utf-8")  # by hand, outside the page
    h = c.get("/api/v1/adapter/config/entities.json/history").json()
    assert h["available"] and h["changed_outside"] and len(h["revisions"]) == 2
    j = c.put("/api/v1/adapter/config/entities.json", json={"text": file_text(doc)}).json()
    assert j["revision"]["via"] == "raw"
    h = c.get("/api/v1/adapter/config/entities.json/history").json()
    assert [(r["via"], r["changed_by"]) for r in h["revisions"]] == [
        ("raw", "local"), ("file", "file"), ("form", "local"), ("file", "file")]
    assert h["revisions"][0]["current"] and not h["changed_outside"]
    assert h["revisions"][0]["tab_id"] is None and h["revisions"][2]["tab_id"] == "entities"
    rev = c.get(f"/api/v1/adapter/config/entities.json/history/{h['revisions'][1]['rev_id']}").json()
    assert json.loads(rev["text"]) == first
    assert c.get("/api/v1/adapter/config/entities.json/history/999999").status_code == 404
    with psycopg.connect(store_url, autocommit=True) as con, pytest.raises(psycopg.Error):
        con.execute("UPDATE adapter_config_rev SET changed_by = 'x'")


def test_context_offers_books_seen_in_recent_positions_and_the_hubs_indices(home: Path, store_url: str) -> None:
    with psycopg.connect(store_url, autocommit=True) as con:
        for book, d, rev in (("T1", "2026-10-20", 1), ("NEW_BOOK", "2026-10-19", 1), ("NEW_BOOK", "2026-10-19", 2),
                             ("OLD", "2026-08-01", 1)):
            sid = con.execute("INSERT INTO position_set (book_id, valid_date, revision, source_name, source_file_sha256) "
                              "VALUES (%s, %s, %s, 'f.csv', 'ab') RETURNING set_id", (book, d, rev)).fetchone()[0]
            for n in range(rev + 1):
                con.execute("INSERT INTO position_input VALUES (%s, %s, 'SU26238RMFS4', 1)", (sid, f"P{n}"))
        con.execute("INSERT INTO book_rev (book_id, name, desk, book_type, entity, active, adapter_fingerprint, "
                    "content_sha256) VALUES ('NEW_BOOK', 'NEW_BOOK', 'auto', 'TRADING', 'ООО Ромашка', true, 'fp', 's')")
        con.execute("INSERT INTO md_hub_fixing (index_code, source, fixing_date, value, unit, fetch_id) VALUES "
                    "('KEY_RATE', 'cbr', '2026-10-20', 16.5, 'pct', 1), ('RUONIA', 'cbr', '2026-10-19', 16.1, 'pct', 1)")
    j = _client(store_url).get("/api/v1/adapter/settings-context").json()
    assert j["error"] is None and [i["code"] for i in j["indices"]] == ["KEY_RATE", "RUONIA"]
    assert [(b["book_id"], b["last_date"], b["positions"]) for b in j["recent_books"]] == [
        ("NEW_BOOK", "2026-10-19", 3), ("T1", "2026-10-20", 2)]
    assert j["recent_books"][0]["entity"] == "ООО Ромашка" and j["recent_books"][1]["entity"] is None
