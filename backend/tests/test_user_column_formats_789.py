"""The format of a column's numbers a user sets (TDM#789): migration 0029 (``user_column_formats``) and the routes of
api.prefs_routes (``/api/v1/me/column-formats``): the caller's own formats only, the checks of a key and of the values
(digits 0..6, units 1 / k / mm / bn), a format with nothing set is not kept, the bound on the columns of one user, the
removal with the user."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from desk.api import auth, create_app
from desk.api.prefs_routes import MAX_FORMATS

URL = "/api/v1/me/column-formats"
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


PW = {"ann": "viewer-pass-1", "bob": "trader-pass-1", "cat": "admin-pass-1"}
ROLE = {"ann": "viewer", "bob": "trader", "cat": "admin"}


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


def test_migration_creates_the_table_with_its_checks(store_url):
    assert "0029_user_column_formats" in [r[0] for r in sql(store_url, "SELECT version FROM schema_migration")]
    assert sql(store_url, "SELECT count(*) FROM user_column_formats") == [(0,)]
    ok = "INSERT INTO user_column_formats (username, col_key, decimals, scale) VALUES ('local', %s, %s, %s)"
    sql(store_url, ok, ("dv01", 2, None))
    sql(store_url, ok, ("mv", None, "bn"))
    assert sql(store_url, "SELECT col_key, decimals, scale FROM user_column_formats ORDER BY col_key") == \
        [("dv01", 2, None), ("mv", None, "bn")]
    with psycopg.connect(store_url, autocommit=True) as con:
        # nothing set is the default format: no row; the digits and the units are bounded; the keys are the page's
        for args in (("a", None, None), ("a", 7, None), ("a", -1, None), ("a", None, "tys"), ("a", None, ""),
                     ("-a", 1, None), ("a b", 1, None), ("x" * 65, 1, None), ("", 1, None)):
            with pytest.raises(psycopg.errors.CheckViolation):
                con.execute(ok, args)
        with pytest.raises(psycopg.errors.UniqueViolation):
            con.execute(ok, ("dv01", 3, None))  # one row per user and column
        with pytest.raises(psycopg.errors.CheckViolation):
            con.execute("INSERT INTO user_column_formats (username, col_key, decimals) VALUES ('Bad Name', 'a', 1)")


def test_save_read_replace_and_reset(local):
    assert local.get(URL).json() == {"user": "local", "formats": {}}
    r = local.put(URL + "/mv", json={"decimals": 3, "scale": "bn"})
    assert r.status_code == 200, r.text
    assert r.json()["decimals"] == 3 and r.json()["scale"] == "bn" and r.json()["updated_at"]
    got = local.get(URL).json()
    assert got["user"] == "local" and list(got["formats"]) == ["mv"]
    assert (got["formats"]["mv"]["decimals"], got["formats"]["mv"]["scale"]) == (3, "bn") and got["formats"]["mv"]["updated_at"]
    # each part alone: null (or left out) is the column's own default for that part
    r = local.put(URL + "/mv", json={"decimals": 0, "scale": None})
    assert (r.json()["decimals"], r.json()["scale"]) == (0, None)
    r = local.put(URL + "/mv", json={"decimals": None, "scale": "k"})
    assert (r.json()["decimals"], r.json()["scale"]) == (None, "k"), "replaced, not merged: decimals left out is the default"
    for scale in ("1", "k", "mm", "bn"):
        assert local.put(URL + "/dv01", json={"decimals": 6, "scale": scale}).json()["scale"] == scale
    assert list(local.get(URL).json()["formats"]) == ["dv01", "mv"]  # by key
    # both null: the default format, the row goes (also when there was none)
    r = local.put(URL + "/mv", json={"decimals": None, "scale": None})
    assert r.status_code == 200 and r.json()["decimals"] is None and r.json()["scale"] is None
    assert list(local.get(URL).json()["formats"]) == ["dv01"]
    assert local.put(URL + "/never-set", json={"decimals": None, "scale": None}).status_code == 200
    assert sql(local.app.state.store_url, "SELECT col_key FROM user_column_formats") == [("dv01",)]
    # DELETE: back to the default, no error when there is nothing to remove
    hdr = {"Content-Type": "application/json"}
    assert local.delete(URL + "/dv01", headers=hdr).status_code == 204
    assert local.delete(URL + "/dv01", headers=hdr).status_code == 204
    assert local.get(URL).json()["formats"] == {}
    assert sql(local.app.state.store_url, "SELECT count(*) FROM user_column_formats") == [(0,)]


def test_the_digits_are_zero_to_six_integers_and_the_units_a_known_word(local):
    for body in ({"decimals": 7, "scale": None}, {"decimals": -1, "scale": None}, {"decimals": 1.5, "scale": None}, {"decimals": "2", "scale": None}, {"decimals": True, "scale": None},
                 {"decimals": [2], "scale": None}, {"decimals": None, "scale": "tys"}, {"decimals": None, "scale": "MM"}, {"decimals": None, "scale": ""}, {"decimals": None, "scale": 6}, {"decimals": None, "scale": ["mm"]},
                 {"decimals": None, "scale": True}, {"decimals": 2, "scale": "million"}):
        r = local.put(URL + "/mv", json=body)
        assert r.status_code == 422, (body, r.status_code)
    for d in range(7):
        assert local.put(URL + "/mv", json={"decimals": d, "scale": None}).json()["decimals"] == d
    # a rejected body changes nothing
    assert local.put(URL + "/mv", json={"decimals": 9, "scale": None}).status_code == 422
    assert local.get(URL).json()["formats"]["mv"]["decimals"] == 6
    # not a JSON object
    assert local.put(URL + "/mv", json=[1]).status_code == 422
    assert local.put(URL + "/mv", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 422


def test_a_misspelt_or_empty_body_is_a_422_not_a_reset(local):
    # the reset is explicit: both keys null, or DELETE; anything else that does not name both parts is refused and changes nothing
    assert local.put(URL + "/mv", json={"decimals": 3, "scale": "bn"}).status_code == 200
    for body in ({}, {"digits": 3}, {"digits": 3, "scale": None}, {"decimals": 3, "units": "bn"}, {"decimals": 2}, {"scale": "k"},
                 {"decimals": 2, "scale": "k", "extra": 1}, {"decimals": None}, {"scale": None}):
        r = local.put(URL + "/mv", json=body)
        assert r.status_code == 422, (body, r.status_code)
    got = local.get(URL).json()["formats"]["mv"]
    assert (got["decimals"], got["scale"]) == (3, "bn"), "the refused bodies did not reset or change the format"
    r = local.put(URL + "/mv", json={"decimals": None, "scale": None})
    assert r.status_code == 200 and local.get(URL).json()["formats"] == {}


def test_bad_column_key(local):
    for col in ("-x", "_x", ".x", "a b", "a%20b", "a<script>", "x" * 65, "я"):
        assert local.put(URL + "/" + col, json={"decimals": 1, "scale": None}).status_code in (404, 422), col
        assert local.put(URL + "/" + col, json={"decimals": 1, "scale": None}).status_code != 200, col
        assert local.delete(URL + "/" + col, headers={"Content-Type": "application/json"}).status_code in (404, 422), col
    # the 422 is the monitor's own error with its code
    r = local.put(URL + "/x" + "y" * 64, json={"decimals": 1, "scale": None})
    assert r.status_code == 422 and r.json()["code"] == "invalid_parameter"
    # keys of the catalog: letters of both cases, digits, . _ : - inside, up to 64
    for col in ("mv", "dv01", "Z.kbd", "a_b-c:d", "A" * 64, "7"):
        assert local.put(URL + "/" + col, json={"decimals": 1, "scale": None}).status_code == 200, col
    assert local.get(URL).json()["formats"].keys() == {"mv", "dv01", "Z.kbd", "a_b-c:d", "A" * 64, "7"}


def test_the_limit_of_columns_of_one_user(local):
    for i in range(MAX_FORMATS):
        assert local.put(URL + f"/c{i}", json={"decimals": 1, "scale": None}).status_code == 200
    over = local.put(URL + "/c-over", json={"decimals": None, "scale": "mm"})
    assert over.status_code == 409 and over.json()["code"] == "invalid_parameter"
    assert "c-over" not in local.get(URL).json()["formats"]
    # a column that has a format is replaced at the limit; one reset makes room; the default format needs no room
    assert local.put(URL + "/c5", json={"decimals": 2, "scale": "k"}).status_code == 200
    assert local.put(URL + "/c-over", json={"decimals": None, "scale": None}).status_code == 200
    assert local.delete(URL + "/c7", headers={"Content-Type": "application/json"}).status_code == 204
    assert local.put(URL + "/c-over", json={"decimals": None, "scale": "mm"}).status_code == 200
    assert len(local.get(URL).json()["formats"]) == MAX_FORMATS


def test_formats_are_per_user_and_survive_a_new_app(store_url, home):
    ann, bob = user_client(store_url, "ann"), user_client(store_url, "bob")
    for c in (ann, bob):
        assert c.get(URL).json()["formats"] == {}
    assert ann.put(URL + "/mv", json={"decimals": 3, "scale": "bn"}, headers=csrf(ann)).status_code == 200
    assert bob.put(URL + "/mv", json={"decimals": 0, "scale": None}, headers=csrf(bob)).status_code == 200
    assert bob.put(URL + "/dv01", json={"decimals": None, "scale": "mm"}, headers=csrf(bob)).status_code == 200
    a, b = ann.get(URL).json(), bob.get(URL).json()
    assert a["user"] == "ann" and b["user"] == "bob"
    assert {k: (v["decimals"], v["scale"]) for k, v in a["formats"].items()} == {"mv": (3, "bn")}
    assert {k: (v["decimals"], v["scale"]) for k, v in b["formats"].items()} == {"mv": (0, None), "dv01": (None, "mm")}
    # a restart of the server is a new app on the same store; the user signs in again
    again = user_client(store_url, "bob")
    assert again.get(URL).json()["formats"]["dv01"]["scale"] == "mm"
    # "local" (sign-in off, this computer) is another user
    assert TestClient(create_app(store_url), **LOCAL).get(URL).json() == {"user": "local", "formats": {}}
    # the reset is the caller's own
    assert ann.delete(URL + "/mv", headers={**csrf(ann), "Content-Type": "application/json"}).status_code == 204
    assert ann.get(URL).json()["formats"] == {}
    assert bob.get(URL).json()["formats"]["mv"]["decimals"] == 0
    # the formats and the table columns are two stores: a reset of the table columns does not touch a format
    bob.put("/api/v1/me/table-columns/c01-positions", json={"hidden": ["isin"], "added": ["zkbd"]}, headers=csrf(bob))
    bob.delete("/api/v1/me/table-columns/c01-positions", headers={**csrf(bob), "Content-Type": "application/json"})
    assert sorted(bob.get(URL).json()["formats"]) == ["dv01", "mv"]
    assert sql(store_url, "SELECT count(*) FROM user_table_columns") == [(0,)]


def test_there_is_no_way_to_name_another_user(store_url, home):
    ann, bob = user_client(store_url, "ann"), user_client(store_url, "bob")
    bob.put(URL + "/mv", json={"decimals": 5, "scale": None}, headers=csrf(bob))
    for q in ("?user=bob", "?username=bob", "?as=bob"):
        assert ann.get(URL + q).json()["formats"] == {}, q
    # a username in the body or the query is not read: the row is the caller's, bob's stays
    r = ann.put(URL + "/mv?username=bob", json={"decimals": 1, "scale": None}, headers=csrf(ann))
    assert r.status_code == 200
    # a username in the body is not read either: another key is a 422, bob's row is untouched
    r = ann.put(URL + "/mv", json={"decimals": 1, "scale": None, "user": "bob", "username": "bob"}, headers=csrf(ann))
    assert r.status_code == 422
    assert bob.get(URL).json()["formats"]["mv"]["decimals"] == 5
    assert sql(store_url, "SELECT username, decimals FROM user_column_formats ORDER BY username") == [("ann", 1), ("bob", 5)]


def test_sign_in_on_needs_a_session_and_the_csrf_token(store_url, home):
    anon = TestClient(create_app(store_url, auth=ON), **LAN)
    assert anon.get(URL).status_code == 401
    assert anon.put(URL + "/mv", json={"decimals": 1, "scale": None}).status_code == 401
    assert anon.delete(URL + "/mv", headers={"Content-Type": "application/json"}).status_code == 401
    ann = user_client(store_url, "ann")
    for headers in (None, {"X-CSRF-Token": "forged"}, {**csrf(ann), "Origin": "https://evil.example"}):
        r = ann.put(URL + "/mv", json={"decimals": 1, "scale": None}, headers=headers)
        assert r.status_code == 403 and r.json()["code"] == "forbidden", headers
    assert ann.delete(URL + "/mv", headers={"Content-Type": "application/json"}).status_code == 403
    assert ann.get(URL).json()["formats"] == {}
    # the monitor's own account on a loopback address works the same: the user is the account, not "local"
    mine = TestClient(create_app(store_url, auth=ON), **LOCAL)
    assert mine.post("/api/v1/auth/login", json={"username": "ann", "password": PW["ann"]}).status_code == 200
    assert mine.get(URL).json()["user"] == "ann"


def test_deleting_a_user_deletes_their_formats(store_url, home):
    ann, bob = user_client(store_url, "ann"), user_client(store_url, "bob")
    ann.put(URL + "/mv", json={"decimals": 2, "scale": None}, headers=csrf(ann))
    ann.put(URL + "/dv01", json={"decimals": None, "scale": "mm"}, headers=csrf(ann))
    bob.put(URL + "/mv", json={"decimals": 4, "scale": None}, headers=csrf(bob))
    boss = user_client(store_url, "cat")
    assert boss.delete("/api/v1/users/ann", headers={**csrf(boss), "Content-Type": "application/json"}).status_code == 204
    assert sql(store_url, "SELECT username FROM user_column_formats") == [("bob",)]
    # an account made again under the same name starts with the default
    assert user_client(store_url, "ann").get(URL).json()["formats"] == {}
