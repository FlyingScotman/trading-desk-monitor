"""Hidden columns of the position tables, per user (TDM#763): migration 0025, the routes of api.prefs_routes
(``/api/v1/me/table-columns``), whose they are (only the caller's own), what survives (a new app on the same store)
and what the deletion of a user takes with it."""

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
PW = {"ann": "viewer-pass-1", "bob": "trader-pass-1", "cat": "admin-pass-1"}
ROLE = {"ann": "viewer", "bob": "trader", "cat": "admin"}


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


def user_client(url: str, name: str) -> TestClient:
    with psycopg.connect(url, autocommit=True) as con:
        if con.execute("SELECT 1 FROM app_user WHERE username = %s", (name,)).fetchone() is None:
            auth.add_user(con, name, ROLE[name], PW[name], actor="test")
    c = TestClient(create_app(url, auth=ON), **LAN)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": PW[name]})
    assert r.status_code == 200, r.text
    return c


def csrf(c: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies["desk_csrf"]}


def test_migration_adds_the_table_and_checks_its_keys(store_url):
    assert "0025_user_table_columns" in [r[0] for r in sql(store_url, "SELECT version FROM schema_migration")]
    ins = "INSERT INTO user_table_columns (username, table_key, hidden) VALUES (%s, %s, %s::jsonb)"
    with psycopg.connect(store_url, autocommit=True) as con:
        con.execute(ins, ("local", "c03-blotter:base", '["spark"]'))
        for bad in (("Local", "c01", "[]"), ("local", "C01", "[]"), ("local", "c01 x", "[]"), ("local", "", "[]"),
                    ("local", "c01", '{"a": 1}'), ("local", "c01", '"spark"'), ("", "c01", "[]")):
            with pytest.raises(psycopg.errors.CheckViolation):
                con.execute(ins, bad)
        with pytest.raises(psycopg.errors.UniqueViolation):
            con.execute(ins, ("local", "c03-blotter:base", "[]"))


def test_save_read_replace_and_reset(local):
    assert local.get(URL).json() == {"user": "local", "tables": {}}
    r = local.put(URL + "/c01-positions", json={"hidden": ["isin", "spark", "isin"]})
    assert r.status_code == 200, r.text
    assert r.json()["hidden"] == ["isin", "spark"]  # no repeats, the order given
    local.put(URL + "/c03-blotter:base", json={"hidden": ["tenor"]})
    got = local.get(URL).json()
    assert got["user"] == "local" and sorted(got["tables"]) == ["c01-positions", "c03-blotter:base"]
    assert got["tables"]["c01-positions"]["hidden"] == ["isin", "spark"] and got["tables"]["c01-positions"]["updated_at"]
    first = got["tables"]["c01-positions"]["updated_at"]
    local.put(URL + "/c01-positions", json={"hidden": ["kind"]})  # replaced, not added to
    again = local.get(URL).json()["tables"]["c01-positions"]
    assert again["hidden"] == ["kind"] and again["updated_at"] >= first
    # an empty list is the default: the row goes
    assert local.put(URL + "/c01-positions", json={"hidden": []}).json()["hidden"] == []
    assert sorted(local.get(URL).json()["tables"]) == ["c03-blotter:base"]
    # "как было": the row goes; again with nothing to remove is not an error
    assert local.delete(URL + "/c03-blotter:base", headers={"Content-Type": "application/json"}).status_code == 204
    assert local.delete(URL + "/c03-blotter:base", headers={"Content-Type": "application/json"}).status_code == 204
    assert local.get(URL).json()["tables"] == {}
    assert sql(local.app.state.store_url, "SELECT count(*) FROM user_table_columns") == [(0,)]


def test_bad_input(local):
    for key in ("C01", "-c01", "c01 x", "x" * 129, "c01/x", "c01%00"):
        r = local.put(URL + "/" + key, json={"hidden": []})
        assert r.status_code in (404, 405, 422), (key, r.status_code)  # 404, 405: not this route
        if r.status_code == 422:
            assert r.json()["code"] == "invalid_parameter"
    for hidden in (["a b"], [""], ["-x"], ["x" * 65], ["a<script>"], [1], "spark", None, {"a": 1}):
        r = local.put(URL + "/c01", json={"hidden": hidden})
        assert r.status_code == 422, (hidden, r.status_code)
    assert local.put(URL + "/c01", json={}).status_code == 422
    assert local.put(URL + "/c01", json={"hidden": [f"c{i}" for i in range(301)]}).status_code == 422
    assert local.put(URL + "/c01", json={"hidden": [f"c{i}" for i in range(300)]}).status_code == 200
    # not JSON: a cross-site form cannot send these
    assert local.put(URL + "/c01", content='{"hidden": []}', headers={"Content-Type": "text/plain"}).status_code == 415
    assert local.delete(URL + "/c01").status_code == 415
    # a user cannot fill the table without bound: 300 tables, a 301st is refused, a known one is still replaced
    for i in range(299):
        assert local.put(URL + f"/t{i}", json={"hidden": ["a"]}).status_code == 200
    over = local.put(URL + "/t-over", json={"hidden": ["a"]})
    assert over.status_code == 409 and over.json()["code"] == "invalid_parameter"
    assert local.put(URL + "/t5", json={"hidden": ["b"]}).status_code == 200
    assert local.delete(URL + "/t0", headers={"Content-Type": "application/json"}).status_code == 204
    assert local.put(URL + "/t-over", json={"hidden": ["a"]}).status_code == 200


def test_sign_in_off_is_this_computer_only(store_url):
    app = create_app(store_url)
    remote = TestClient(app, **LAN)
    assert remote.get(URL).status_code == 403
    assert remote.put(URL + "/c01", json={"hidden": ["a"]}).status_code == 403
    assert remote.delete(URL + "/c01", headers={"Content-Type": "application/json"}).status_code == 403
    # a loopback address with someone else's Host (DNS rebinding) is refused too
    assert TestClient(app, base_url="http://evil.example:8100", client=("127.0.0.1", 1)).get(URL).status_code == 403
    assert sql(store_url, "SELECT count(*) FROM user_table_columns") == [(0,)]


def test_only_their_own_per_user(store_url, home):
    ann, bob = user_client(store_url, "ann"), user_client(store_url, "bob")
    for c in (ann, bob):  # a viewer and a trader: any role keeps its own
        assert c.get(URL).json()["tables"] == {}
    assert ann.put(URL + "/c01-positions", json={"hidden": ["isin"]}, headers=csrf(ann)).status_code == 200
    assert bob.put(URL + "/c01-positions", json={"hidden": ["spark", "kind"]}, headers=csrf(bob)).status_code == 200
    bob.put(URL + "/c03-blotter:risk", json={"hidden": ["dur"]}, headers=csrf(bob))
    a, b = ann.get(URL).json(), bob.get(URL).json()
    assert a["user"] == "ann" and {k: v["hidden"] for k, v in a["tables"].items()} == {"c01-positions": ["isin"]}
    assert b["user"] == "bob" and {k: v["hidden"] for k, v in b["tables"].items()} == {
        "c01-positions": ["spark", "kind"], "c03-blotter:risk": ["dur"]}
    # the reset is the caller's own: ann's reset leaves bob's row
    assert ann.delete(URL + "/c01-positions", headers={**csrf(ann), "Content-Type": "application/json"}).status_code == 204
    assert ann.get(URL).json()["tables"] == {}
    assert sorted(bob.get(URL).json()["tables"]) == ["c01-positions", "c03-blotter:risk"]
    # the request carries no user to name: a username in the body or the query changes nothing
    r = ann.put(URL + "/c01-positions?username=bob", json={"hidden": ["x"], "username": "bob"}, headers=csrf(ann))
    assert r.status_code == 200
    assert bob.get(URL).json()["tables"]["c01-positions"]["hidden"] == ["spark", "kind"]
    assert sql(store_url, "SELECT username, table_key FROM user_table_columns ORDER BY 1, 2") == [
        ("ann", "c01-positions"), ("bob", "c01-positions"), ("bob", "c03-blotter:risk")]


def test_sign_in_on_needs_a_session_and_the_csrf_token(store_url, home):
    anon = TestClient(create_app(store_url, auth=ON), **LAN)
    assert anon.get(URL).status_code == 401
    assert anon.put(URL + "/c01", json={"hidden": ["a"]}).status_code == 401
    ann = user_client(store_url, "ann")
    for headers in (None, {"X-CSRF-Token": "forged"}, {**csrf(ann), "Origin": "https://evil.example"}):
        r = ann.put(URL + "/c01", json={"hidden": ["a"]}, headers=headers)
        assert r.status_code == 403 and r.json()["code"] == "forbidden", headers
    assert ann.delete(URL + "/c01", headers={"Content-Type": "application/json"}).status_code == 403
    assert ann.put(URL + "/c01", content="x", headers={**csrf(ann), "Content-Type": "text/plain"}).status_code == 415
    assert ann.get(URL).json()["tables"] == {}
    # the monitor's own account on a loopback address works the same: the user is the account, not "local"
    mine = TestClient(create_app(store_url, auth=ON), **LOCAL)
    assert mine.post("/api/v1/auth/login", json={"username": "ann", "password": PW["ann"]}).status_code == 200
    assert mine.get(URL).json()["user"] == "ann"


def test_the_choice_survives_a_new_app_and_a_new_session(store_url, home):
    first = user_client(store_url, "bob")
    first.put(URL + "/c12-positions", json={"hidden": ["isin", "cs01"]}, headers=csrf(first))
    first.post("/api/v1/auth/logout", headers=csrf(first))
    # a restart of the server is a new app on the same store; the user signs in again
    second = user_client(store_url, "bob")
    assert second.get(URL).json()["tables"]["c12-positions"]["hidden"] == ["isin", "cs01"]
    assert TestClient(create_app(store_url), **LOCAL).get(URL).json()["tables"] == {}  # "local" is another user


def test_deleting_a_user_deletes_their_choices(store_url, home):
    ann, bob = user_client(store_url, "ann"), user_client(store_url, "bob")
    ann.put(URL + "/c01", json={"hidden": ["a"]}, headers=csrf(ann))
    bob.put(URL + "/c01", json={"hidden": ["b"]}, headers=csrf(bob))
    boss = user_client(store_url, "cat")
    assert boss.delete("/api/v1/users/ann", headers={**csrf(boss), "Content-Type": "application/json"}).status_code == 204
    assert sql(store_url, "SELECT username FROM user_table_columns") == [("bob",)]
    # an account made again under the same name starts with the default
    again = user_client(store_url, "ann")
    assert again.get(URL).json()["tables"] == {}


def test_a_huge_body_is_refused_fast(local):
    """A signed-in viewer cannot hold a worker with a body of tens of thousands of keys (repeats or not): the list is
    bounded by the model and checked in linear time."""
    import time

    for body in ([f"c{i}" for i in range(50_000)], ["same"] * 50_000, ["a b"] * 50_000, [f"c{i % 400}" for i in range(50_000)]):
        t0 = time.monotonic()
        r = local.put(URL + "/c01", json={"hidden": body})
        assert r.status_code == 422, r.text[:200]
        assert time.monotonic() - t0 < 3, "refused fast"
    # within the model's bound, repeats are cut in linear time and the limit counts the distinct keys
    t0 = time.monotonic()
    r = local.put(URL + "/c01", json={"hidden": ["x"] * 1200})
    assert r.status_code == 200 and r.json()["hidden"] == ["x"] and time.monotonic() - t0 < 3
    assert local.put(URL + "/c01", json={"hidden": [f"c{i}" for i in range(1200)]}).status_code == 422


def test_the_name_local_is_reserved(store_url, home):
    with psycopg.connect(store_url, autocommit=True) as con:
        with pytest.raises(auth.AuthError, match="local"):
            auth.add_user(con, "local", "viewer", "viewer-pass-1", actor="test")
        with pytest.raises(auth.AuthError, match="local"):
            auth.add_user(con, " Local ", "admin", "admin-pass-1", actor="test")
        assert con.execute("SELECT count(*) FROM app_user").fetchone()[0] == 0
    c = TestClient(create_app(store_url), **LOCAL)
    r = c.post("/api/v1/users", json={"username": "local", "role": "viewer", "password": "viewer-pass-1"})
    assert r.status_code == 422 and "local" in r.json()["message"]
    assert c.post("/api/v1/users", json={"username": "locale", "role": "viewer", "password": "viewer-pass-1"}).status_code == 201
