"""Local acquisition boundary, isolated synthetic processes and the real reference/store/EOD pipeline."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import psutil
import pytest
from fastapi.testclient import TestClient
from test_r23_backfill import cache, load  # noqa: F401 - existing synthetic market fixture

from desk import acquisition as aq
from desk import cli, store
from desk.api import auth, create_app
from desk.autopilot import CycleLock
from desk.inbox import scan
from desk.settings import Settings, update_settings_json
from desk.settings import load as settings_load

DAY = date(2026, 10, 15)
USER, PASSWORD = "SYNTH_ACQUISITION_USER", "SYNTH_ACQUISITION_PASSWORD"
FORM = {"Origin": "http://127.0.0.1:8100", "Content-Type": "application/x-www-form-urlencoded"}
LOOP = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}

ADAPTER = '''from pathlib import Path
from desk.inbox.reference import SPI_VERSION, match, reference
from desk.inbox.reference import read as original_read
from desk.inbox.reference import describe as original
def read(path, hit, ctx):
    result = original_read(path, hit, ctx)
    if (Path(ctx.config_dir) / 'mode').read_text() == 'file_error':
        result['issues'].append({'severity':'error','code':'synthetic_file_error','message':'Synthetic rejection'})
    return result
def describe():
    d = original()
    d["name"] = "synthetic_acquisition"
    d["acquisition"] = {"version": 1, "entry": "collect.py", "dataset": "trade_terms", "source": "trade_terms_csv"}
    return d
'''
HELPER = '''import csv, hashlib, json, os, subprocess, sys, time
from pathlib import Path
from urllib.parse import parse_qs
p = parse_qs(sys.stdin.buffer.read().decode(), keep_blank_values=True)
cfg = Path(p['config_dir'][0])
mode = (cfg / 'mode').read_text()
out = Path(p['output_dir'][0])
if mode == 'raw':
    print(p.get('username'), p.get('password'))
    print(p.get('password'), file=sys.stderr)
    sys.exit(2)
if mode == 'hang':
    out.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    (out / 'child.pid').write_text(str(child.pid))
    time.sleep(60)
if mode == 'overflow':
    print('x' * 300000)
    sys.exit()
if mode == 'env':
    assert not any(k.startswith(('DESK_', 'MDH_', 'AWS_')) for k in os.environ)
    assert not any(v in ' '.join(sys.argv) for v in p.get('password', []))
if mode == 'auth':
    print(json.dumps({'state':'error', 'code':'auth_failed'}))
    sys.exit(2)
op = p['op'][0]
if op == 'inspect':
    report = {'id':'a'*64, 'sha256':'b'*64, 'name':'Synthetic_20261015.csv', 'books':['OFZ'],
              'policy':{'status':'VERIFIED', 'excluded_products':['Cash'],
                        'books':{'OFZ':{'eligible':1,'products':['FX']}}}}
    result = {'state':'complete', 'code':'scope_ready', 'date':p['date'][0], 'reports':[report]}
elif op == 'check':
    result = {'state':'complete', 'code':'connected'}
else:
    out.mkdir(parents=True)
    day = p['date'][0]
    prefix = 'positions_' if mode == 'wrong_dataset' else 'trade_terms_'
    named_day = '20261014' if mode == 'wrong_date' else day.replace('-', '')
    target = out / (prefix + named_day + '.csv')
    book = 'CORP' if mode == 'wrong_book' else p['book'][0]
    with target.open('w', newline='') as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(['trade_id','book_id','product_kind','trade_date','value_date','base_ccy','quote_ccy','side',
                         'notional_base','notional_quote','contract_rate'])
        writer.writerow(['SYNTH-TRADE',book,'fx_forward','2026-10-01','2026-12-17','USD','RUB','buy',10,800,80])
    result = {'state':'partial' if mode == 'partial' else 'complete', 'code':'acquired',
              'date':day,'books':p['book'],'file':target.name,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
print(json.dumps(result))
'''


@pytest.fixture
def configured(tmp_path):
    root, cfg = tmp_path / "adapter", tmp_path / "config"
    root.mkdir()
    cfg.mkdir()
    (root / "__init__.py").write_text(ADAPTER)
    (root / "collect.py").write_text(HELPER)
    (cfg / "mode").write_text("success")
    (cfg / "books.csv").write_text("book_id;name;desk;book_type;entity;active\n"
                                   "OFZ;Synthetic;Test;TRADING;TEST;1\nCORP;Other;Test;TRADING;TEST;1\n")
    st = Settings(home=tmp_path / "home", private_adapter=str(root), private_adapter_config=cfg,
                  acquisition_python=Path(sys.executable), inbox_archive=tmp_path / "archive",
                  inbox_poll_seconds=0, inbox_lookback_days=0)
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield st
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


def fields(op="check", **kw):
    p = {"op": op, "date": DAY.isoformat(), **kw}
    if op != "inspect":
        p.update(username=USER, password=PASSWORD)
    if op == "fetch":
        p.update(report="a" * 64, report_sha256="b" * 64, book=["OFZ"])
    return p


def fetch_fields(st):
    return fields("fetch", adapter_fingerprint=aq.provider(st).handle.fingerprint)


def wait(service, *, phase=None):
    until = time.monotonic() + 30
    while time.monotonic() < until:
        status = service.status()
        if phase and status["phase"] == phase or not phase and status["state"] != "running":
            return status
        time.sleep(0.02)
    pytest.fail("synthetic acquisition did not reach the expected state")


def no_secrets(value):
    text = json.dumps(value, default=str)
    assert USER not in text and PASSWORD not in text


def test_optional_capability_containment_pin_and_python(configured):
    st = configured
    p = aq.provider(st)
    assert p.entry.parent == p.root
    assert not aq.Acquisition(lambda: replace(st, private_adapter=None), "").availability()["available"]
    for changed, code in ((replace(st, acquisition_python=None), "runtime_unavailable"),
                          (replace(st, private_adapter_sha256="0" * 64), "adapter_unavailable")):
        with pytest.raises(aq.FlowError, match=code):
            aq.provider(changed)
    root = Path(st.private_adapter)
    for entry in ("../outside.py", str(root / "collect.py"), "missing.py"):
        (root / "__init__.py").write_text(ADAPTER.replace('"collect.py"', repr(entry)))
        with pytest.raises(aq.FlowError, match="capability_invalid|helper_unavailable"):
            aq.provider(st)


@pytest.mark.parametrize("mode,code", [("auth", "auth_failed"), ("raw", "provider_response_invalid"),
                                       ("overflow", "provider_response_invalid"), ("env", "connected")])
def test_process_failures_and_environment_are_safe(configured, monkeypatch, caplog, mode, code):
    st = configured
    (st.private_adapter_config / "mode").write_text(mode)
    monkeypatch.setenv("DESK_HUB_TOKEN", PASSWORD)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", PASSWORD)
    service = aq.Acquisition(lambda: st, "", timeout_s=3)
    service.start(fields())
    status = wait(service)
    assert status["code"] == code, status
    no_secrets(status)
    no_secrets(caplog.text)
    assert service._proc is None
    service.close()


@pytest.mark.parametrize("action", ["timeout", "cancel", "shutdown"])
def test_timeout_cancel_kill_descendants_and_no_ingestion(configured, action):
    st = configured
    (st.private_adapter_config / "mode").write_text("hang")
    service = aq.Acquisition(lambda: st, "", timeout_s=0.6 if action == "timeout" else 5)
    service.start(fields())
    pidfile = st.home / "acquisition" / service.status()["id"] / "child.pid"
    until = time.monotonic() + 5
    while not pidfile.exists() and time.monotonic() < until:
        time.sleep(0.01)
    assert pidfile.exists()
    if action == "cancel":
        service.cancel()
    elif action == "shutdown":
        service.close()
    status = wait(service)
    assert status["code"] == ("acquisition_timeout" if action == "timeout" else "cancelled")
    no_secrets(status)
    pid = int(pidfile.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert not list(st.home.rglob("*.csv"))
    service.close()


def test_http_invalid_payloads_origin_host_peer_and_admin(configured, store_url, caplog):
    st = configured
    service = aq.Acquisition(lambda: st, store_url)
    app = create_app(store_url)
    app.state.acquisition = service
    with TestClient(app, **LOOP) as c:
        assert c.get("/api/v1/acquisition").json()["available"]
        bodies = [b"password=" + PASSWORD.encode(), b"op=check&password=" + b"x" * 66000,
                  b"\xff" + PASSWORD.encode(), urlencode({"op": "check", "password": PASSWORD,
                                                         "username": USER, "extra": PASSWORD}).encode()]
        for body in bodies:
            r = c.post("/api/v1/acquisition/start", content=body, headers=FORM)
            assert r.status_code == 422
            no_secrets(r.json())
        for headers in ({"Origin": "null"}, {"Origin": "http://127.0.0.1:9999"},
                        {"Origin": "https://evil.example"}, {"X-Forwarded-For": "127.0.0.1"},
                        {"Content-Type": "application/json"}):
            r = c.post("/api/v1/acquisition/start", content=urlencode(fields()), headers={**FORM, **headers})
            assert r.status_code in (403, 415)
            no_secrets(r.json())
        assert c.post("/api/v1/acquisition/start", content=urlencode(fields()),
                      headers={"Content-Type": FORM["Content-Type"]}).status_code == 403
        r = c.post("/api/v1/acquisition/start", content=urlencode(fields()), headers=FORM)
        assert r.status_code == 202
        assert wait(service)["code"] == "connected"
        no_secrets(c.get("/api/v1/acquisition").json())
    for client, host in ((("10.1.2.3", 1), "127.0.0.1"), (("127.0.0.1", 1), "evil.example")):
        c = TestClient(app, base_url=f"http://{host}:8100", client=client)
        assert c.post("/api/v1/acquisition/start", content=urlencode(fields()), headers=FORM).status_code == 403
    # A network admin is allowed by the ordinary settings policy, but cannot send local bank credentials here.
    with store.connect(store_url) as con:
        auth.add_user(con, "admin", "admin", "Synthetic-admin-pass", actor="test")
        auth.add_user(con, "viewer", "viewer", "Synthetic-viewer-pass", actor="test")
    app = create_app(store_url, auth=auth.AuthConfig(enabled=True, cache_s=0))
    app.state.acquisition = service
    for username, peer in (("admin", "10.1.2.3"), ("viewer", "127.0.0.1")):
        c = TestClient(app, base_url=LOOP["base_url"], client=(peer, 1))
        assert c.post("/api/v1/auth/login", json={"username": username,
                      "password": f"Synthetic-{username}-pass"}).status_code == 200
        r = c.post("/api/v1/acquisition/start", content=urlencode(fields()),
                   headers={**FORM, "X-CSRF-Token": c.cookies["desk_csrf"]})
        assert r.status_code == 403
    c = TestClient(app, **LOOP)
    assert c.post("/api/v1/auth/login", json={"username": "admin", "password": "Synthetic-admin-pass"}).status_code == 200
    assert c.post("/api/v1/acquisition/start", content=urlencode(fields()),
                  headers={**FORM, "X-CSRF-Token": c.cookies["desk_csrf"]}).status_code == 202
    assert wait(service)["code"] == "connected"
    with store.connect(store_url) as con:
        no_secrets(con.execute("SELECT action, target, detail FROM audit_log WHERE action='acquisition'").fetchall())
    no_secrets(caplog.text)


@pytest.mark.parametrize("mode", ["success", "partial", "wrong_book", "wrong_date", "wrong_dataset", "file_error"])
def test_real_reference_ingest_and_fresh_eod_preserve_other_books(configured, store_url, cache, tmp_path, mode):  # noqa: F811
    st = replace(configured, snapshot_cache=cache, store_url=store_url)
    load(store_url, tmp_path, DAY.isoformat(), 10)
    (st.private_adapter_config / "mode").write_text(mode)
    cli.eod_run(st, DAY.isoformat(), report_dir=None, out=lambda _: None)
    with store.connect(store_url) as con:
        previous = store.default_runs(con, DAY)[DAY]
        old_hash = store.positions_for(con, DAY).private_inputs_hash
        old_other = [r for r in store.positions_for(con, DAY).rows if r[1] == "CORP"]
    service = aq.Acquisition(lambda: st, store_url)
    service.start(fetch_fields(st))
    result = wait(service)
    if mode in ("wrong_book", "wrong_date", "wrong_dataset", "file_error"):
        assert result["code"] == "snapshot_not_accepted", result
        with store.connect(store_url) as con:
            assert store.positions_for(con, DAY).private_inputs_hash == old_hash
            assert con.execute("SELECT count(*) FROM dataset_set").fetchone()[0] == 0
            assert store.default_runs(con, DAY)[DAY]["run_id"] == previous["run_id"]
    else:
        assert result["code"] == "completed", result
        assert result["run_id"] != previous["run_id"]
        if mode == "partial":
            assert result["state"] == "partial"
        with store.connect(store_url) as con:
            inputs = store.positions_for(con, DAY)
            assert result["private_inputs_hash"] == inputs.private_inputs_hash != old_hash
            assert [r for r in inputs.rows if r[1] == "CORP"] == old_other
            row = con.execute("SELECT private_inputs_hash, lineage FROM run WHERE run_id=%s",
                              (result["run_id"],)).fetchone()
            assert row[0] == row[1]["positions"]["private_inputs_hash"] == inputs.private_inputs_hash
            assert row[1]["positions"]["trade_terms"]
    no_secrets(result)
    service.close()


def test_missing_prerequisites_and_shared_cycle_lock(configured, store_url, cache, tmp_path):  # noqa: F811
    service = aq.Acquisition(lambda: configured, store_url)
    service.start(fetch_fields(configured))
    assert wait(service)["code"] == "market_missing"
    st = replace(configured, snapshot_cache=cache)
    service = aq.Acquisition(lambda: st, store_url)
    service.start(fetch_fields(st))
    assert wait(service)["code"] == "positions_missing"
    load(store_url, tmp_path, DAY.isoformat(), 10)
    with CycleLock(st.home):
        service.start(fetch_fields(st))
        assert wait(service)["code"] == "calculation_busy"
    with store.connect(store_url) as con:
        assert con.execute("SELECT count(*) FROM dataset_set").fetchone()[0] == 0
    service.close()


def test_runtime_setting_precedence_and_credentials_never_settings(tmp_path):
    update_settings_json({"acquisition_python": str(tmp_path / "bank-python")}, tmp_path)
    env = {"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "none.env")}
    assert settings_load(env).acquisition_python == tmp_path / "bank-python"
    env["DESK_ACQUISITION_PYTHON"] = str(tmp_path / "other-python")
    assert settings_load(env).acquisition_python == tmp_path / "other-python"
    assert not any(k in json.loads((tmp_path / "settings.json").read_text()) for k in ("username", "password"))


def test_changed_configuration_requires_new_preview_and_scope_must_be_listed(configured):
    st = configured
    params = fetch_fields(st)
    (st.private_adapter_config / "mode").write_text("partial")
    service = aq.Acquisition(lambda: st, "")
    with pytest.raises(aq.FlowError, match="scope_changed"):
        service.start(params)
    assert not params  # transient credentials released on this pre-spawn rejection too
    assert service.status()["state"] == "idle"
    root = Path(st.private_adapter)
    invalid = ADAPTER.replace('d["name"] =', '''next(s for s in d["sources"] if s["source"] == "trade_terms_csv")["covers"] = "all_active_books"
    d["name"] =''')
    (root / "__init__.py").write_text(invalid)
    with pytest.raises(aq.FlowError, match="capability_invalid"):
        aq.provider(st)


def test_manual_cli_eod_uses_same_cycle_lock(configured, monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(cli, "eod_run", lambda *a, **kw: calls.append(True))
    with CycleLock(configured.home):
        assert cli.cmd_eod(SimpleNamespace(settings=configured)) == 2
    assert not calls
