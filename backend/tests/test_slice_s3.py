"""Slice S3: hub client, snapshot cache and the hub commands against an in-process stub hub.

The stub serves the vendored golden fixtures (``fixtures/mdhub_v1``) on ``http://127.0.0.1``
with the hub's paths, paging and headers; no network. ``pytest -m live`` checks the real hub
named in ``DESK_HUB_URL`` (skipped unless a hub address and a token are configured).
"""

from __future__ import annotations

import gzip
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from desk import cli
from desk.marketdata import cache
from desk.marketdata.bundle import SnapshotDir, SnapshotError
from desk.marketdata.hub import HubClient, HubError
from desk.settings import Settings, load

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
INDEX = json.loads((FIX / "golden_index.json").read_text())["fixtures"]
MARKET = INDEX["market_eod_final"]["snapshot_id"]
STATIC = INDEX["static_daily"]["snapshot_id"]
TOKEN = "mdh_test_secret_token_0123456789"
ROWS = [{"snapshot_id": MARKET, "kind": "market", "cut_type": "eod_final", "business_date": "2026-10-15",
         "static_snapshot_id": STATIC, "published_at": "2026-10-16T15:50:00+00:00"},
        {"snapshot_id": STATIC, "kind": "static", "cut_type": "daily", "business_date": "2026-10-15",
         "static_snapshot_id": None, "published_at": "2026-10-16T15:45:00+00:00"}]


class Stub(ThreadingHTTPServer):
    """Knobs: ``snapshots`` (False = hub without the snapshot layer), ``status`` (forced code for
    /v1), ``fail`` (5xx answers before success), ``body`` (override bundle bytes)."""

    def reset(self) -> None:
        self.snapshots, self.status, self.fail, self.body, self.page = True, None, 0, None, 1
        self.requests: list[str] = []


class Handler(BaseHTTPRequestHandler):
    server: Stub

    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json", **headers: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, doc: dict) -> None:
        self._send(code, json.dumps(doc).encode())

    def do_GET(self) -> None:
        s, u = self.server, urlsplit(self.path)
        s.requests.append(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/health/ready":
            return self._json(200, {"status": "ok", "version": "0.1.0", "migrations":
                                    {"applied": 25, "known": 25, "pending": 0, "checksum_mismatch": 0}})
        if not u.path.startswith("/v1/snapshots") or not s.snapshots:
            return self._json(404, {"error": {"status": 404, "message": "Not Found"}})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._json(401, {"error": {"status": 401, "message": "unauthorized"}})
        if s.status:
            return self._json(s.status, {"error": {"status": s.status, "message": "forced"}})
        if s.fail:
            s.fail -= 1
            return self._json(503, {"error": {"status": 503, "message": "database unavailable"}})
        if u.path == "/v1/snapshots":
            rows = [r for r in ROWS if all(r.get(k) == q[k] for k in ("kind", "cut_type", "business_date") if k in q)]
            if q.get("latest") == "true":
                return self._json(200, {"data": rows[:1], "meta": {"returned": len(rows[:1]), "latest": True}})
            off = int(q.get("offset", 0))
            chunk = rows[off:off + s.page]
            return self._json(200, {"data": chunk, "meta": {
                "next_offset": off + s.page if off + s.page < len(rows) else None}})
        sid, _, leaf = u.path.removeprefix("/v1/snapshots/").partition("/")
        if sid not in (MARKET, STATIC):
            return self._json(404, {"error": {"status": 404, "message": "snapshot not found"}})
        if leaf == "manifest":
            row = next(r for r in ROWS if r["snapshot_id"] == sid)
            return self._json(200, {"data": {}, "meta": {"published_at": row["published_at"], "superseded_by": None}})
        body = s.body if s.body is not None else (FIX / f"{sid}.json.gz").read_bytes()
        return self._send(200, body, "application/gzip", ETag=f'"{sid}"', X_Snapshot_Sha256=sid)


@pytest.fixture(scope="module")
def server():
    srv = Stub(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def stub(server, monkeypatch):
    server.reset()
    monkeypatch.setattr(HubClient, "backoff_s", 0.0)
    return server


def settings(stub: Stub, token: str | None = TOKEN) -> Settings:
    return Settings(hub_url=f"http://127.0.0.1:{stub.server_address[1]}", hub_token=token, hub_timeout_s=5)


def test_no_hub_address_makes_no_request():
    with pytest.raises(HubError, match="DESK_HUB_URL"):
        HubClient(Settings())


def test_list_pages_and_latest(stub):
    c = HubClient(settings(stub))
    assert [r["snapshot_id"] for r in c.list_snapshots()] == [MARKET, STATIC]  # two pages of one row
    assert sum("offset=1" in r for r in stub.requests) == 1
    assert [r["snapshot_id"] for r in c.list_snapshots(kind="market", latest=True)] == [MARKET]
    assert list(cache.hub_dates(c)) == ["2026-10-15"]


def test_sync_downloads_verifies_and_is_append_only(stub, tmp_path):
    c = HubClient(settings(stub))
    [s] = cache.sync(c, tmp_path, ["2026-10-15"])
    assert (s.market_id, s.static_id, sorted(s.downloaded)) == (MARKET, STATIC, sorted([MARKET, STATIC]))
    sd = SnapshotDir(tmp_path)
    assert sd.market("2026-10-15").id == MARKET
    assert sd.provenance(MARKET)["published_at"] == ROWS[0]["published_at"]
    assert sd.provenance(STATIC)["published_at"] == ROWS[1]["published_at"]  # from the manifest
    assert not list(tmp_path.glob("*.part"))
    mtime = (tmp_path / f"{MARKET}.json.gz").stat().st_mtime_ns
    stub.requests.clear()
    [again] = cache.sync(c, tmp_path, ["2026-10-15"])
    assert again.downloaded == [] and not any("/bundle" in r for r in stub.requests)
    assert (tmp_path / f"{MARKET}.json.gz").stat().st_mtime_ns == mtime


def test_missing_date_is_reported(stub, tmp_path):
    [s] = cache.sync(HubClient(settings(stub)), tmp_path, ["2026-10-14"])
    assert s.market_id is None and not list(tmp_path.iterdir())


@pytest.mark.parametrize("body", [b"not gzip", None])
def test_bad_bundle_is_rejected_and_nothing_written(stub, tmp_path, body):
    raw = gzip.decompress((FIX / f"{STATIC}.json.gz").read_bytes())
    stub.body = body if body is not None else gzip.compress(raw[:-1] + b" ")  # wrong hash
    with pytest.raises(SnapshotError):
        cache.sync(HubClient(settings(stub)), tmp_path, ["2026-10-15"])
    assert not list(tmp_path.glob("*.json.gz")) and not list(tmp_path.glob("*.part"))


def test_truncated_gzip_is_rejected(stub, tmp_path):
    stub.body = (FIX / f"{MARKET}.json.gz").read_bytes()[:-100]
    with pytest.raises(SnapshotError, match="not a complete gzip"):
        cache.fetch(HubClient(settings(stub)), tmp_path, MARKET)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(("status", "token", "message"), [
    (None, "mdh_wrong", "token rejected"),
    (403, TOKEN, "lacks scope 'snapshots'"),
])
def test_auth_errors(stub, status, token, message):
    stub.status = status
    with pytest.raises(HubError, match=message):
        next(HubClient(settings(stub, token)).list_snapshots())


def test_hub_without_snapshot_layer(stub):
    stub.snapshots = False
    with pytest.raises(HubError, match=r"version 0\.1\.0\) has no snapshot layer"):
        next(HubClient(settings(stub)).list_snapshots())


def test_server_errors_are_retried(stub):
    stub.fail = 3
    assert next(HubClient(settings(stub)).list_snapshots())["snapshot_id"] == MARKET
    stub.fail = 4
    with pytest.raises(HubError, match="unreachable after 4 attempts: 503"):
        next(HubClient(settings(stub)).list_snapshots())


def test_unreachable_hub(monkeypatch):
    monkeypatch.setattr(HubClient, "backoff_s", 0.0)
    with pytest.raises(HubError, match="unreachable"):
        HubClient(Settings(hub_url="http://127.0.0.1:9", hub_token=TOKEN, hub_timeout_s=1)).health()


def test_import_drop(tmp_path):
    drop, dest = tmp_path / "drop", tmp_path / "cache"
    drop.mkdir()
    shutil.copy(FIX / f"{MARKET}.json.gz", drop)
    (drop / f"{'0' * 64}.json.gz").write_bytes(gzip.compress(b"{}"))
    (drop / "export.json.gz").write_bytes(b"")
    imported, rejected = cache.import_drop(drop, dest)
    assert imported == [MARKET]
    assert sorted(n for n, _ in rejected) == sorted([f"{'0' * 64}.json.gz", "export.json.gz"])
    assert sorted(p.name for p in dest.iterdir()) == [f"{MARKET}.json.gz"]


def _token_env(stub, tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("DESK_HUB_URL", f"http://127.0.0.1:{stub.server_address[1]}")
    monkeypatch.setenv("DESK_HUB_TOKEN", TOKEN)
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    return str(tmp_path / "cache")


def _no_token(out) -> None:
    assert TOKEN not in out.out + out.err and TOKEN[4:-4] not in out.out + out.err


def test_cli_commands_never_print_the_token(stub, tmp_path, monkeypatch, capsys):
    snaps = _token_env(stub, tmp_path, monkeypatch)
    assert cli.main(["config"]) == 0
    assert cli.main(["hub-check"]) == 0
    assert cli.main(["sync", "--last", "5", "--snapshots", snaps]) == 0
    monkeypatch.setenv("DESK_HUB_TOKEN", "mdh_wrong")
    assert cli.main(["sync", "--date", "2026-10-15", "--snapshots", snaps]) == 1
    out = capsys.readouterr()
    assert "token rejected" in out.err
    assert "published 2026-10-16T15:50:00+00:00" in out.out
    _no_token(out)


def test_cli_eod_never_prints_the_token(stub, tmp_path, monkeypatch, capsys, store_url):
    snaps = _token_env(stub, tmp_path, monkeypatch)
    monkeypatch.setenv("DESK_STORE_URL", store_url)  # eod reads the books of the desk store (S5)
    assert cli.main(["eod", "--date", "2026-10-15", "--snapshots", snaps, "--sync", "--no-pnl"]) == 0
    out = capsys.readouterr()
    assert "published 2026-10-16T15:50:00+00:00" in out.out and "no positions in the store" in out.out
    _no_token(out)


@pytest.mark.live
def test_live_hub(tmp_path):
    s = load()
    if not (s.hub_url and s.hub_token):
        pytest.skip("DESK_HUB_URL and a token are not configured")
    c = HubClient(s)
    assert c.health()["status"] == "ok"
    latest = next(c.list_snapshots(kind="market", cut_type="eod_final", latest=True))
    [synced] = cache.sync(c, tmp_path, [latest["business_date"]])
    assert SnapshotDir(tmp_path).market(latest["business_date"]).id == synced.market_id
