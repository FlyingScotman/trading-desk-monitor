"""Columns a user adds to a position table (TDM#778): migration 0027 (``user_table_columns.added``), and the routes of
api.prefs_routes (``/api/v1/me/table-columns``) taking and giving ``added`` next to ``hidden``: the order kept, an old
client that sends only ``hidden``, both lists empty removes the row, the same checks and bounds as ``hidden``."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from desk.api import auth, create_app

URL = "/api/v1/me/table-columns"
LOCAL = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}
LAN = {"base_url": "http://desk.example:8100", "client": ("10.1.2.3", 50000)}
ON = auth.AuthConfig(enabled=True, cache_s=0)


@pytest.fixture
def local(store_url) -> TestClient:
    return TestClient(create_app(store_url), **LOCAL)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    return h


def sql(url: str, q: str, args=()) -> list:
    with psycopg.connect(url, autocommit=True) as con:
        cur = con.execute(q, args or None)
        return cur.fetchall() if cur.description else []


def test_migration_adds_the_column_and_old_rows_read_as_nothing_added(store_url):
    assert "0027_user_table_columns_added" in [r[0] for r in sql(store_url, "SELECT version FROM schema_migration")]
    # a row written by the version before: no ``added`` in the statement, the default is an empty list
    sql(store_url, "INSERT INTO user_table_columns (username, table_key, hidden) VALUES ('local', 'c01-positions', '[\"isin\"]')")
    assert sql(store_url, "SELECT hidden, added FROM user_table_columns") == [(["isin"], [])]
    with psycopg.connect(store_url, autocommit=True) as con:
        for bad in ('{"a": 1}', '"zkbd"', "null"):
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation)):
                con.execute("INSERT INTO user_table_columns (username, table_key, hidden, added) VALUES"
                            " ('local', 'c12', '[]', %s::jsonb)", (bad,))
    # the route reads the old row as before, with nothing added
    app = TestClient(create_app(store_url), **LOCAL)
    got = app.get(URL).json()["tables"]["c01-positions"]
    assert got["hidden"] == ["isin"] and got["added"] == []


def test_save_read_replace_and_reset_with_added(local):
    r = local.put(URL + "/c03-blotter:base", json={"hidden": ["spark"], "added": ["zkbd", "isin", "zkbd", "zdisc"]})
    assert r.status_code == 200, r.text
    assert r.json()["hidden"] == ["spark"] and r.json()["added"] == ["zkbd", "isin", "zdisc"]  # no repeats, the order added
    got = local.get(URL).json()["tables"]["c03-blotter:base"]
    assert got["hidden"] == ["spark"] and got["added"] == ["zkbd", "isin", "zdisc"] and got["updated_at"]
    # replaced, not added to; the order is the one given
    r = local.put(URL + "/c03-blotter:base", json={"hidden": [], "added": ["zdisc", "zkbd"]})
    assert r.json()["hidden"] == [] and r.json()["added"] == ["zdisc", "zkbd"]
    # only added: the row stays (nothing hidden is not "the default" while a column is added)
    assert list(local.get(URL).json()["tables"]) == ["c03-blotter:base"]
    # both empty: the default, the row goes
    r = local.put(URL + "/c03-blotter:base", json={"hidden": [], "added": []})
    assert r.status_code == 200 and r.json()["hidden"] == [] and r.json()["added"] == []
    assert local.get(URL).json()["tables"] == {}
    assert sql(local.app.state.store_url, "SELECT count(*) FROM user_table_columns") == [(0,)]
    # "как было" removes both
    local.put(URL + "/c01-positions", json={"hidden": ["a"], "added": ["b"]})
    assert local.delete(URL + "/c01-positions", headers={"Content-Type": "application/json"}).status_code == 204
    assert local.get(URL).json()["tables"] == {}


def test_a_page_of_the_version_before_sends_only_hidden(local):
    # a table nobody added to: hidden only is what it always was
    r = local.put(URL + "/c01-positions", json={"hidden": ["isin"]})
    assert r.status_code == 200 and r.json()["hidden"] == ["isin"] and r.json()["added"] == []
    # added columns stay when the body leaves ``added`` out: the old page cannot know them
    local.put(URL + "/c01-positions", json={"hidden": ["isin"], "added": ["zkbd"]})
    r = local.put(URL + "/c01-positions", json={"hidden": ["tenor"]})
    assert r.json()["hidden"] == ["tenor"] and r.json()["added"] == ["zkbd"]
    assert local.get(URL).json()["tables"]["c01-positions"]["added"] == ["zkbd"]
    # an empty hidden list leaving ``added`` out does not remove a row that still has an added column
    r = local.put(URL + "/c01-positions", json={"hidden": []})
    assert r.json()["added"] == ["zkbd"] and "c01-positions" in local.get(URL).json()["tables"]
    # ``added: null`` is the same as leaving it out; an explicit empty list clears it
    assert local.put(URL + "/c01-positions", json={"hidden": ["a"], "added": None}).json()["added"] == ["zkbd"]
    assert local.put(URL + "/c01-positions", json={"hidden": ["a"], "added": []}).json()["added"] == []
    # nothing hidden, nothing added, nothing stored: the row goes
    assert local.put(URL + "/c01-positions", json={"hidden": []}).json()["hidden"] == []
    assert local.get(URL).json()["tables"] == {}


def test_bad_added(local):
    for added in (["a b"], [""], ["-x"], ["x" * 65], ["a<script>"], [1], "zkbd", {"a": 1}):
        r = local.put(URL + "/c01", json={"hidden": [], "added": added})
        assert r.status_code == 422, (added, r.status_code)
    assert local.put(URL + "/c01", json={"hidden": [], "added": [f"c{i}" for i in range(301)]}).status_code == 422
    r = local.put(URL + "/c01", json={"hidden": [], "added": [f"c{i}" for i in range(300)]})
    assert r.status_code == 200 and len(r.json()["added"]) == 300
    # the same bound for the model as for hidden: a huge body is refused fast, repeats are cut
    assert local.put(URL + "/c01", json={"hidden": [], "added": [f"c{i}" for i in range(50_000)]}).status_code == 422
    r = local.put(URL + "/c01", json={"hidden": [], "added": ["x"] * 1200})
    assert r.status_code == 200 and r.json()["added"] == ["x"]
    assert local.put(URL + "/c01", json={"hidden": [], "added": [f"c{i}" for i in range(1200)]}).status_code == 422
    # a rejected body changes nothing
    assert local.get(URL).json()["tables"]["c01"]["added"] == ["x"]


def test_the_table_limit_counts_a_table_with_only_added_columns(local):
    for i in range(300):
        assert local.put(URL + f"/t{i}", json={"hidden": [], "added": ["isin"]}).status_code == 200
    over = local.put(URL + "/t-over", json={"hidden": [], "added": ["isin"]})
    assert over.status_code == 409 and over.json()["code"] == "invalid_parameter"
    assert local.put(URL + "/t5", json={"hidden": [], "added": ["isin", "zkbd"]}).status_code == 200  # a known one is replaced


def test_added_is_per_user_and_survives_a_new_app(store_url, home):
    pw = {"ann": "viewer-pass-1", "bob": "trader-pass-1"}
    role = {"ann": "viewer", "bob": "trader"}

    def user(name):
        with psycopg.connect(store_url, autocommit=True) as con:
            if con.execute("SELECT 1 FROM app_user WHERE username = %s", (name,)).fetchone() is None:
                auth.add_user(con, name, role[name], pw[name], actor="test")
        c = TestClient(create_app(store_url, auth=ON), **LAN)
        assert c.post("/api/v1/auth/login", json={"username": name, "password": pw[name]}).status_code == 200
        return c

    ann, bob = user("ann"), user("bob")
    csrf = lambda c: {"X-CSRF-Token": c.cookies["desk_csrf"]}
    assert ann.put(URL + "/c01-positions", json={"hidden": [], "added": ["isin"]}, headers=csrf(ann)).status_code == 200
    assert bob.put(URL + "/c01-positions", json={"hidden": ["kind"], "added": ["zkbd", "zdisc"]}, headers=csrf(bob)).status_code == 200
    a, b = ann.get(URL).json()["tables"]["c01-positions"], bob.get(URL).json()["tables"]["c01-positions"]
    assert (a["hidden"], a["added"]) == ([], ["isin"]) and (b["hidden"], b["added"]) == (["kind"], ["zkbd", "zdisc"])
    # a restart of the server is a new app on the same store
    again = user("bob")
    assert again.get(URL).json()["tables"]["c01-positions"]["added"] == ["zkbd", "zdisc"]
    # the reset is the caller's own
    assert ann.delete(URL + "/c01-positions", headers={**csrf(ann), "Content-Type": "application/json"}).status_code == 204
    assert ann.get(URL).json()["tables"] == {}
    assert bob.get(URL).json()["tables"]["c01-positions"]["added"] == ["zkbd", "zdisc"]
