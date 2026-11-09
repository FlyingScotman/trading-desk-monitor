"""The adapter's services capability (08 §2.2, TDM#548) with a synthetic helper: capability containment, the panel
and its secrets, actions with tables and pick lists, the feeds' scheduling (windows, daily slots, backoff, the login
pause, a restart, ``after_positions``), the exact source guard of the ingestion, the runs made again after new terms
(TDM#547, also the manual acquisition's) and the routes' local-only rules. Synthetic data only."""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from test_r23_backfill import cache, load  # noqa: F401 - the synthetic market fixture

from desk import adapter_services as svcs
from desk import cli, helper_proc, store
from desk.acquisition import Acquisition, FlowError
from desk.api import create_app
from desk.autopilot import Autopilot
from desk.inbox import scan
from desk.inbox.timesem import MSK
from desk.marketdata.bundle import SnapshotDir
from desk.settings import Settings

SECRET = "SYNTH_SERVICES_SECRET_VALUE"
FORM = {"Origin": "http://127.0.0.1:8100", "Content-Type": "application/x-www-form-urlencoded"}
LOOP = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}
FRI, MON = date(2026, 10, 16), date(2026, 10, 19)
ACTIVE = ("queued", "waiting", "running")

ADAPTER = '''from desk.inbox.reference import SPI_VERSION, match, read, reference
from desk.inbox.reference import describe as original
def describe():
    d = original()
    d["name"] = "synthetic_services"
    d["services"] = SERVICES
    d["acquisition"] = {"version": 1, "entry": "helpers/services.py", "dataset": "trade_terms",
                        "source": "trade_terms_csv"}
    return d
'''
TRADES_HEAD = ("trade_id;trade_version;status;trade_date;trade_ts;book_id;position_id;asset_class;source_instrument_id;"
               "side;quantity;amount_ccy;ccy\n")
TERMS_HEADER = ['trade_id', 'book_id', 'product_kind', 'trade_date', 'value_date', 'base_ccy', 'quote_ccy', 'side',
                'notional_base', 'notional_quote', 'contract_rate']
HELPER = r'''import csv, hashlib, json, os, sys, time
from pathlib import Path
from urllib.parse import parse_qs
p = parse_qs(sys.stdin.buffer.read().decode(), keep_blank_values=True)
one = lambda k, d=None: p[k][0] if k in p else d
state = Path(one('state_dir'))
mode = (state / 'mode').read_text() if (state / 'mode').exists() else 'ok'
with (state / 'requests.jsonl').open('a') as log:
    log.write(json.dumps({'op': one('op'), 'keys': sorted(p), 'feed': one('feed'), 'date': one('date'),
                          'action': one('action'), 'arg': one('arg'), 'inbox_dir': p.get('inbox_dir', []), 'book': p.get('book', []),
                          'trades_file': p.get('trades_file', []),
                          'req': json.loads(Path(p['trades_file'][0]).read_text()) if 'trades_file' in p else None,
                          'env': sorted(k for k in os.environ if k.startswith(('DESK_', 'AWS_')))}) + '\n')
if 'f.password' in p:
    (state / 'secret.txt').write_text(one('f.password'))
saved = json.loads((state / 'saved.json').read_text()) if (state / 'saved.json').exists() else {}
def panel():
    return {"title": "Тестовые сервисы", "status": ["Среда: " + saved.get('env', 'test')],
            "sections": [{"id": "conn", "title": "Подключение", "help": "Синтетика <b>не HTML</b>",
                "fields": [{"key": "url", "label": "Адрес", "type": "url", "value": saved.get('url', '')},
                           {"key": "user", "label": "Логин", "type": "text", "value": saved.get('user', '')},
                           {"key": "password", "label": "Пароль", "type": "secret", "value": "LEAKED_BY_HELPER",
                            "set": (state / 'secret.txt').exists()},
                           {"key": "env", "label": "Среда", "type": "select", "value": saved.get('env', 'test'),
                            "options": [{"value": "test", "label": "Тест"}, {"value": "prod", "label": "Прод"}]},
                           {"key": "verbose", "label": "Подробно", "type": "bool", "value": saved.get('verbose') == 'true'},
                           {"key": "port", "label": "Порт", "type": "int", "value": 443},
                           {"key": "sched.terms.daily_at", "label": "Время", "type": "text",
                            "value": saved.get('sched.terms.daily_at', '')}],
                "actions": [{"id": "check", "label": "Проверить", "long": False},
                            {"id": "login", "label": "Войти", "long": True, "confirm": "Откроется окно входа"},
                            {"id": "pick", "label": "Найти", "long": False},
                            {"id": "list", "label": "Список", "long": False}]}]
            + ([{"id": "api_v2", "title": "Сервис сделок", "fields": [],
                 "actions": [{"id": "v2_connect", "label": "Подключить", "long": True}]}] if mode == 'connect' else [])}
def feeds():
    extra = [{"id": "owned", "title": "Сделки дня", "dataset": "trade_terms", "source": "trade_terms_csv",
              "enabled": True, "ready": True, "books": True, "schedule": None}] if mode == 'books_feed' else []
    if mode == 'plan_feed':
        extra += [{"id": "tlist", "title": "Список сделок", "dataset": "trade_list", "source": "trade_list_csv",
                   "enabled": True, "ready": True, "books": True, "schedule": None},
                  {"id": "planned", "title": "Условия по списку", "dataset": "trade_terms",
                   "source": "trade_terms_csv", "enabled": True, "ready": True, "trades_file": True,
                   "schedule": None}]
    if mode == 'history_feed':   # the trades of the day, which also answers for a past date (TDM#569)
        hf = (state / 'hist_from').read_text() if (state / 'hist_from').exists() else None
        extra += [{"id": "tradeshist", "title": "Сделки дня", "dataset": "trades", "source": "trades_csv",
                   "enabled": True, "ready": True, "books": True, "history": True, "schedule": None,
                   **({"history_from": hf} if hf else {})}]
    if mode == 'connect':        # the feed a connect hands its first load over to (TDM#615)
        extra += [{"id": "trades_intraday", "title": "Сделки дня", "dataset": "trades", "source": "trades_csv",
                   "enabled": False, "ready": True, "schedule": None}]
    if mode == 'history_plain':  # the same feed without the flag: the worker does not work for it
        extra += [{"id": "tradeshist", "title": "Сделки дня", "dataset": "trades", "source": "trades_csv",
                   "enabled": True, "ready": True, "books": True, "schedule": None}]
    return extra + [{"id": "terms", "title": "Условия сделок", "dataset": "trade_terms", "source": "trade_terms_csv",
             "enabled": True, "ready": True, "after_positions": True,
             "schedule": {"every_min": 30, "from": "08:00", "to": "20:00", "weekdays_only": True}},
            {"id": "eodpos", "title": "Позиции", "dataset": "positions", "source": "positions_csv", "enabled": False,
             "ready": True, "schedule": {"daily_at": ["08:30"]}},
            {"id": "wrongsrc", "title": "Чужой источник", "dataset": "trade_terms", "source": "positions_csv",
             "enabled": True, "ready": True, "schedule": None},
            {"id": "archive", "title": "Архив", "dataset": "marketdata", "source": "svc_archive", "enabled": False,
             "ready": True, "schedule": None}]
op = one('op')
if mode == 'hang' and op in ('action', 'feed'):
    time.sleep(60)
if mode == 'hang_feed' and op == 'feed':
    time.sleep(60)
if op == 'describe' and (state / 'describe_delay').exists():
    time.sleep(float((state / 'describe_delay').read_text()))
if op in ('describe', 'save'):
    if op == 'save':
        if one('f.url') == 'bad':
            print(json.dumps({"state": "blocked", "code": "config_invalid", "field": "url"}))
            sys.exit()
        saved.update({k[2:]: v[0] for k, v in p.items() if k.startswith('f.') and k != 'f.password'})
        (state / 'saved.json').write_text(json.dumps(saved))
        if mode == 'save_nofeeds':
            print(json.dumps({"state": "complete", "code": "saved", "panel": panel()}))
            sys.exit()
    print(json.dumps({"state": "complete", "code": "described", "panel": panel(), "feeds": feeds()}))
elif op == 'action':
    a = one('action')
    if a == 'login':
        (state / 'logged_in').write_text('1')
        r = {"state": "complete", "code": "logged_in", "lines": ["Вход выполнен"]}
    elif a == 'pick':
        r = {"state": "complete", "code": "found", "lines": ["Найдено 2"], "choose_action": "choose",
             "choices": [{"id": "x1", "label": "Первый"}, {"id": "x2", "label": "Второй"}]}
    elif a == 'list':  # choices without choose_action: no pick list
        r = {"state": "complete", "code": "listed", "lines": ["Список"], "choices": [{"id": "x1", "label": "Первый"}]}
    elif a == 'choose':
        r = {"state": "complete", "code": "chosen", "lines": ["Выбрано: " + one('arg')]}
    elif a == 'v2_connect':
        r = json.loads((state / 'connect_result').read_text())
    else:
        if (state / 'check_writes_config').exists():  # the action keeps what it found in the adapter's config folder
            (Path(one('config_dir')) / 'found.json').write_text(json.dumps({"env": "test"}))
        r = {"state": "complete", "code": "connected", "lines": ["Подключено"],
             "table": {"columns": ["Среда", "Отчётов"], "rows": [["test", 3]]}}
    print(json.dumps(r))
elif op == 'feed':
    out = Path(one('output_dir'))
    assert out.is_dir() and not any(out.iterdir())
    if mode == 'error':
        print(json.dumps({"state": "error", "code": "fetch_failed", "lines": ["Сервис не ответил"]}))
        sys.exit()
    if mode == 'invalid':
        print('not json')
        sys.exit()
    if mode == 'nochange':  # the adapter remembers what it produced: nothing new
        print(json.dumps({"state": "skipped", "code": "no_change", "files": [], "dates": ['2026-10-15']}))
        sys.exit()
    if one('feed') == 'tlist':  # the list the test put in the state folder, or nothing new
        if not (state / 'tlist.csv').exists():
            print(json.dumps({"state": "skipped", "code": "no_change", "files": []}))
            sys.exit()
        day = one('date')
        name = 'trade_list_' + day.replace('-', '') + '.csv'
        (out / name).write_text((state / 'tlist.csv').read_text())
        print(json.dumps({"state": "complete", "code": "listed", "dates": [day], "files": [
            {"file": name, "sha256": hashlib.sha256((out / name).read_bytes()).hexdigest()}]}))
        sys.exit()
    if one('feed') == 'tradeshist':
        if not (state / 'logged_in').exists():
            print(json.dumps({"state": "blocked", "code": "login_required", "lines": ["Нужен вход"]}))
            sys.exit()
        day = one('date') or '2026-10-15'
        hm = (state / ('hist_mode_' + day)).read_text() if (state / ('hist_mode_' + day)).exists() else (
            (state / 'hist_mode').read_text() if (state / 'hist_mode').exists() else 'ok')
        if hm == 'unavailable':
            print(json.dumps({"state": "error", "code": "date_unavailable", "lines": ["Нет данных за дату"]}))
        elif hm == 'unmapped':
            print(json.dumps({"state": "partial", "code": "books_unmapped", "dates": [day]}))
        elif hm == 'partial':
            print(json.dumps({"state": "partial", "code": "export_incomplete", "dates": [day]}))
        elif hm == 'error':
            print(json.dumps({"state": "error", "code": "fetch_failed", "lines": ["Сервис не ответил"]}))
        elif hm == 'no_records':
            print(json.dumps({"state": "complete", "code": "no_records", "dates": [day], "files": []}))
        elif hm == 'empty':
            print(json.dumps({"state": "complete", "code": "exported", "dates": [day], "files": []}))
        elif hm == 'wrongdate':
            print(json.dumps({"state": "complete", "code": "exported", "dates": ['2020-01-01'], "files": []}))
        else:  # ok, or no_records_file: a file with the header only
            late = (state / 'hist_late').read_text() if (state / 'hist_late').exists() else None
            name = 'trades_' + (late or day).replace('-', '') + '_' + ((state / 'hist_hhmm').read_text() if (state / 'hist_hhmm').exists() else '1845') + '.csv'
            # hist_priced[_day]: the trades carry the price columns (the first one is priced, the extra ones are not)
            priced = (state / ('hist_priced_' + day)).exists() or (state / 'hist_priced').exists()
            lines = ['TR-' + day + ';1;live;' + day + ';' + day + 'T10:30:00;OFZ;P1;bond;SU26238RMFS4;buy;1000;;RUB'
                     + (';99.5;1000' if priced else '')] if hm == 'ok' else []
            extra = state / ('hist_extra_' + day) if (state / ('hist_extra_' + day)).exists() else state / 'hist_extra'
            if extra.exists() and hm == 'ok':
                lines += [x + (';;' if priced else '') for x in extra.read_text().replace('DAY', day).splitlines()]
            if late:   # the real adapter dates its file by the moment of the export: a later date, the rows say theirs
                (out / name).write_text(TRADES_HEAD.rstrip('\n') + ';_report_date\n' + ''.join(x + ';' + day + '\n' for x in lines))
            else:
                (out / name).write_text(TRADES_HEAD.rstrip('\n') + (';price_pct;accrued_ccy' if priced else '') + '\n'
                                        + ''.join(x + '\n' for x in lines))
            print(json.dumps({"state": "complete", "code": "no_records" if hm == 'no_records_file' else "exported",
                              "dates": [day], "files": [{"file": name, "sha256": hashlib.sha256((out / name).read_bytes()).hexdigest()}]}))
        sys.exit()
    if one('feed') == 'trades_intraday':  # the first load handed over: nothing new here
        print(json.dumps({"state": "skipped", "code": "no_change", "files": []}))
        sys.exit()
    if one('feed') == 'archive':  # kept locally by the adapter: no files
        print(json.dumps({"state": "complete", "code": "archived", "files": [], "counts": {"rows": 3}}))
        sys.exit(0)
    if not (state / 'logged_in').exists():
        print(json.dumps({"state": "blocked", "code": "login_required", "lines": ["Нужен вход"]}))
        sys.exit()
    day = one('date') or '2026-10-15'
    name = ('positions_' if mode == 'wrong_source' else 'trade_terms_') + day.replace('-', '') + '.csv'
    tmp = out / (name + '.tmp')
    with tmp.open('w', newline='') as f:
        w = csv.writer(f, delimiter=';')
        if mode == 'wrong_source':
            w.writerow(['position_id', 'book_id', 'secid', 'quantity_bonds'])
            w.writerow(['P9', 'OFZ', 'SYNTH', '5'])
        else:
            w.writerow(HEADER)
            w.writerow(['SYNTH-TRADE', 'OFZ', 'fx_forward', '2026-10-01', '2026-12-17', 'USD', 'RUB', 'buy', 10, 800, 80])
    os.replace(tmp, out / name)
    print(json.dumps({"state": "complete", "code": "fetched", "dates": [day], "lines": ["Получена 1 сделка"],
                      "files": [{"file": name, "sha256": hashlib.sha256((out / name).read_bytes()).hexdigest()}]}))
'''.replace("HEADER", repr(TERMS_HEADER)).replace("TRADES_HEAD", repr(TRADES_HEAD))


def make(tmp_path: Path, services: object = None, **kw) -> Settings:
    root, cfg = tmp_path / "adapter", tmp_path / "config"
    root.mkdir(exist_ok=True)
    cfg.mkdir(exist_ok=True)
    cap = {"version": 1, "entry": "helpers/services.py"} if services is None else services
    (root / "__init__.py").write_text(ADAPTER.replace("SERVICES", repr(cap)))
    (root / "helpers").mkdir(exist_ok=True)
    (root / "helpers" / "services.py").write_text(HELPER)
    (cfg / "books.csv").write_text("book_id;name;desk;book_type;entity;active\n"
                                   "OFZ;Synthetic;Test;TRADING;TEST;1\nCORP;Other;Test;TRADING;TEST;1\n")
    return Settings(home=tmp_path / "home", private_adapter=str(root), private_adapter_config=cfg,
                    acquisition_python=Path(sys.executable), inbox_archive=tmp_path / "archive",
                    inbox_poll_seconds=0, inbox_lookback_days=0, **kw)


@pytest.fixture(autouse=True)
def _fresh():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    assert not helper_proc.HELPER_LOCK.locked()


def mode(st: Settings, value: str) -> None:
    d = svcs.state_dir(st)
    d.mkdir(parents=True, exist_ok=True)
    (d / "mode").write_text(value)


def requests(st: Settings) -> list[dict]:
    path = svcs.state_dir(st) / "requests.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def service(st: Settings, url: str = "", clock: dict | None = None) -> svcs.AdapterServices:
    clock = clock if clock is not None else {"t": datetime(2026, 10, 16, 9, 0, tzinfo=MSK)}
    return svcs.AdapterServices(lambda: st, url, now=lambda: clock["t"], today=lambda: FRI, tick_s=3600)


def wait_job(s: svcs.AdapterServices, *, refreshed: bool = True) -> dict:
    """The owner's job once finished; ``refreshed``: an action's also once the describe after it is done (its answer
    is published before that, TDM#615)."""
    until = time.monotonic() + 30
    while time.monotonic() < until:
        job = s.overview()["job"]
        if job["state"] not in ACTIVE and (not refreshed or job.get("refreshed", False) is not None):
            return job
        time.sleep(0.02)
    pytest.fail("the services job did not finish")


def submit(s: svcs.AdapterServices, op: str, **fields: str) -> dict:
    s.submit(op, {k: [v] for k, v in fields.items()})
    return wait_job(s)


def run_feed(s: svcs.AdapterServices, st: Settings, fid: str, clock: dict, day: str | None = None) -> dict:
    clock["t"] += timedelta(seconds=1)
    before = svcs.read_state(st)["runs"].get(fid, {}).get("last_finished")
    s.run_feed(fid, day)
    until = time.monotonic() + 60
    while time.monotonic() < until:
        run = svcs.read_state(st)["runs"].get(fid, {})
        if run.get("last_finished") not in (None, before) and s._running is None:
            return run
        time.sleep(0.05)
    pytest.fail("the feed did not finish")


def no_secret(*values: object) -> None:
    for v in values:
        text = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
        assert SECRET not in text and "LEAKED_BY_HELPER" not in text


# ---- capability --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cap,code", [
    (False, "services_unavailable"), ({"version": 2, "entry": "helpers/services.py"}, "capability_invalid"),
    ({"version": 1, "entry": "../outside.py"}, "capability_invalid"), ({"version": 1, "entry": "/abs.py"}, "capability_invalid"),
    ({"version": 1, "entry": "helpers/missing.py"}, "helper_unavailable"),
    ({"version": 1, "entry": "escape.py"}, "helper_unavailable"), (None, "available")])
def test_capability_detection_and_containment(tmp_path, cap, code):
    st = make(tmp_path, cap if cap is not False else None)
    if cap is False:
        (Path(st.private_adapter) / "__init__.py").write_text(
            ADAPTER.replace('    d["services"] = SERVICES\n', ""))
    if cap and cap.get("entry") == "escape.py":
        (tmp_path / "outside.py").write_text("print('{}')")
        os.symlink(tmp_path / "outside.py", Path(st.private_adapter) / "escape.py")
    a = service(st).availability()[0]
    assert a["code"] == code and a["available"] == (code == "available")
    if code == "capability_invalid":
        h = scan.adapter(st)
        assert h.ok and any(w.startswith("services:") for w in h.warnings)
    if code == "available":
        assert service(replace(st, acquisition_python=None)).availability()[0]["code"] == "runtime_unavailable"


# ---- the panel, its secrets, actions ------------------------------------------------------------------------------

def test_panel_save_secrets_actions_and_choices(tmp_path, monkeypatch, caplog):
    st = make(tmp_path)
    monkeypatch.setenv("DESK_HUB_TOKEN", SECRET)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    s = service(st)
    try:
        assert submit(s, "describe")["code"] == "described"
        o = s.overview()
        pw = next(f for f in o["panel"]["sections"][0]["fields"] if f["key"] == "password")
        assert pw == {"key": "password", "label": "Пароль", "type": "secret", "help": "", "placeholder": "",
                      "value": "", "set": False}
        assert {f["id"]: f["source_declared"] for f in o["feeds"]} == {
            "terms": True, "eodpos": True, "wrongsrc": False, "archive": False}
        assert all(f["ready"] for f in o["feeds"])  # an undeclared source does not reject the feed
        assert [f["enabled"] for f in o["feeds"]] == [True, False, True, False]

        job = submit(s, "save", **{"f.url": "https://synthetic.invalid", "f.user": "u", "f.password": SECRET,
                                   "f.env": "prod", "f.verbose": "true", "f.port": "443",
                                   "f.sched.terms.daily_at": "08:30"})
        assert (job["state"], job["code"]) == ("complete", "saved")
        o = s.overview()
        assert o["panel"]["status"] == ["Среда: prod"]
        assert next(f for f in o["panel"]["sections"][0]["fields"] if f["key"] == "sched.terms.daily_at")["value"] == "08:30"
        assert next(f for f in o["panel"]["sections"][0]["fields"] if f["key"] == "password")["set"] is True
        assert (svcs.state_dir(st) / "secret.txt").read_text() == SECRET  # reached the helper, by stdin only
        # an empty secret is "unchanged": not sent at all
        assert submit(s, "save", **{"f.url": "https://synthetic.invalid", "f.password": ""})["code"] == "saved"
        reqs = requests(st)
        assert [r["op"] for r in reqs] == ["describe", "save", "save"]
        assert "f.password" in reqs[1]["keys"] and "f.password" not in reqs[2]["keys"]
        assert {"adapter_root", "config_dir", "state_dir", "op"} <= set(reqs[0]["keys"]) and "output_dir" not in reqs[0]["keys"]
        assert all(r["env"] == [] for r in reqs)

        job = submit(s, "save", **{"f.url": "bad"})
        assert (job["state"], job["code"], job["field"]) == ("blocked", "config_invalid", "url")
        with pytest.raises(svcs.ServiceError, match="field_unknown"):
            s.submit("save", {"f.nope": ["x"]})
        with pytest.raises(svcs.ServiceError, match="invalid_request"):
            s.submit("save", {"output_dir": ["/tmp/x"]})  # trusted fields never come from a request
        with pytest.raises(svcs.ServiceError, match="action_unknown"):
            s.submit("action", {"action": ["choose"]})  # not offered yet

        job = submit(s, "action", action="check", **{"f.password": SECRET})
        assert (job["state"], job["code"], job["lines"]) == ("complete", "connected", ["Подключено"])
        assert job["table"] == {"columns": ["Среда", "Отчётов"], "rows": [["test", 3]]}
        job = submit(s, "action", action="pick")
        assert job["choose_action"] == "choose" and [c["id"] for c in job["choices"]] == ["x1", "x2"]
        job = submit(s, "action", action="choose", arg="x2")
        assert job["lines"] == ["Выбрано: x2"]
        job = submit(s, "action", action="list")
        assert job["lines"] == ["Список"] and "choices" not in job and job["refreshed"] is True
        mode(st, "save_nofeeds")  # a save answer without the feeds: described again
        assert submit(s, "save", **{"f.url": "https://synthetic.invalid"})["code"] == "saved"
        assert [r["op"] for r in requests(st)[-2:]] == ["save", "describe"]
        mode(st, "ok")
        ops = [r["op"] for r in requests(st)]
        assert all(ops[i + 1] == "describe" for i, op in enumerate(ops) if op == "action")  # every action

        doc = (svcs.state_dir(st) / "state.json").read_text(encoding="utf-8")
        no_secret(doc, s.overview(), caplog.text)
    finally:
        s.close()


def test_busy_cancel_and_the_lock_shared_with_acquisition(tmp_path):
    st = make(tmp_path)
    s = service(st)
    try:
        assert submit(s, "describe")["code"] == "described"
        mode(st, "hang")
        s.submit("action", {"action": ["check"]})
        until = time.monotonic() + 10
        while s.overview()["job"]["state"] != "running" and time.monotonic() < until:
            time.sleep(0.02)
        with pytest.raises(svcs.ServiceError, match="services_busy"):
            s.submit("action", {"action": ["check"]})
        acq = Acquisition(lambda: st, "")
        with pytest.raises(FlowError, match="services_busy"):  # never at the same time as a services helper
            acq.start({"op": "check", "username": "u", "password": "p"})
        s.cancel()
        assert wait_job(s)["code"] == "cancelled"
        mode(st, "ok")
        assert helper_proc.HELPER_LOCK.acquire(blocking=False)  # as an acquisition job holds it
        try:
            s.submit("action", {"action": ["check"]})
            until = time.monotonic() + 10
            while s.overview()["job"]["code"] != "waiting_acquisition" and time.monotonic() < until:
                time.sleep(0.02)
            assert s.overview()["job"]["code"] == "waiting_acquisition"
        finally:
            helper_proc.HELPER_LOCK.release()
        assert wait_job(s)["code"] == "connected"
    finally:
        s.close()


def test_an_action_answer_comes_before_its_describe_615(tmp_path):
    """The answer is published as soon as the helper returns; the describe after it runs as a describe, the owner's
    next operation is queued meanwhile; the job says its section and whether a login window may open."""
    st = make(tmp_path)
    s = service(st)
    try:
        assert submit(s, "describe")["code"] == "described"
        (svcs.state_dir(st) / "describe_delay").write_text("3")
        t0 = time.monotonic()
        s.submit("action", {"action": ["check"]})
        job = wait_job(s, refreshed=False)
        assert time.monotonic() - t0 < 2.5, "the answer waited for the describe"
        assert (job["state"], job["code"], job["lines"], job["refreshed"]) == ("complete", "connected", ["Подключено"], None)
        assert job["finished_at"] and (job["section"], job["login"]) == ("conn", False)
        assert s.overview()["running"]["kind"] == "describe"
        queued = s.submit("action", {"action": ["login"]})  # not services_busy: it waits for the describe
        assert (queued["state"], queued["code"], queued["login"], queued["section"]) == ("queued", "queued", True, "conn")
        (svcs.state_dir(st) / "describe_delay").unlink()
        job = wait_job(s)
        assert (job["code"], job["refreshed"]) == ("logged_in", True)
        assert [r["op"] for r in requests(st)[-4:]] == ["action", "describe", "action", "describe"]
        job = submit(s, "action", action="pick")
        assert job["section"] == "conn"
        assert submit(s, "action", action="choose", arg="x1")["section"] == "conn"  # the section that offered it
        assert submit(s, "save", **{"f.url": "https://synthetic.invalid"})["section"] is None
    finally:
        s.close()


def test_login_flag_and_the_sections_of_a_panel_615():
    panel = svcs.clean_panel({"sections": [
        {"id": "a", "actions": [{"id": "login", "label": "Войти", "login": False}, {"id": "go", "label": "Go"}]},
        {"id": "b", "actions": [{"id": "v2_connect", "label": "Подключить"}, {"id": "open", "label": "Открыть", "login": True}]}]})
    acts = {a["id"]: a for sec in panel["sections"] for a in sec["actions"]}
    assert "login" not in acts["go"] and acts["login"]["login"] is False and acts["open"]["login"] is True
    s = svcs.AdapterServices(lambda: None, "")  # type: ignore[arg-type,return-value]
    doc = {"panel": panel}
    assert s._of_action("action", {"action": "login"}, doc) == ("a", False)  # declared: not a login
    assert s._of_action("action", {"action": "v2_connect"}, doc) == ("b", True)  # by its id
    assert s._of_action("action", {"action": "open"}, doc) == ("b", True)
    assert s._of_action("action", {"action": "go"}, doc) == ("a", False)
    assert s._of_action("save", {}, doc) == (None, False)


CHECKLIST = ["Шаг", "Итог", "Секунд", "Код", "id"]


@pytest.mark.parametrize("state,code,queued", [
    ("complete", "connected", True), ("partial", "connected_partially", True), ("partial", "export_incomplete", True),
    ("error", "login_check_failed", False), ("blocked", "auth_timeout", False)])
def test_a_connect_that_got_in_runs_the_trades_feed_at_once_615(tmp_path, state, code, queued):
    """«Подключить сервис сделок»: its table (the checklist) comes back as it is; connected (or only its first load
    short) queues the feed the adapter hands the first load over to, once, after the describe."""
    st = make(tmp_path)
    mode(st, "connect")
    rows = [["Настройка входа", "✓", 1.2, "v2_configured", "setup"], ["Вход", "✓" if queued else "✗", 30, "logged_in", "login"],
            ["Первая загрузка сделок", "—", None, "", "trades"]]
    (svcs.state_dir(st) / "connect_result").write_text(json.dumps(
        {"state": state, "code": code, "lines": ["✓ Настройка входа: готово (1 с)"], "table": {"columns": CHECKLIST, "rows": rows}}))
    s = service(st)
    try:
        assert submit(s, "describe")["code"] == "described"
        job = submit(s, "action", action="v2_connect")
        assert (job["state"], job["code"], job["section"], job["login"]) == (state, code, "api_v2", True)
        assert job["table"] == {"columns": CHECKLIST, "rows": rows}
        assert job["feed_queued"] == ("trades_intraday" if queued else None)
        if queued:
            _until(lambda: any(r["op"] == "feed" and r["feed"] == "trades_intraday" for r in requests(st)), "not run")
            _until(lambda: svcs.read_state(st)["runs"].get("trades_intraday", {}).get("last_finished"), "not finished")
            assert svcs.read_state(st)["runs"]["trades_intraday"]["trigger"] == "manual"
            assert [r["op"] for r in requests(st)][-3:] == ["action", "describe", "feed"]  # after the describe
        else:
            time.sleep(0.3)
            assert not any(r["op"] == "feed" for r in requests(st))
        mode(st, "ok")  # an adapter without that feed: nothing queued
        submit(s, "describe")
        mode(st, "connect")
        with s._lock:
            s._manual.clear()
        st2 = svcs.read_state(st)
        st2["feeds"] = [f for f in st2["feeds"] if f["id"] != "trades_intraday"]
        svcs.write_state(st, st2)
        assert s._queue_feed(st, "trades_intraday") is None
    finally:
        s.close()


def _until(cond, what: str, limit: float = 15.0):
    until = time.monotonic() + limit
    while time.monotonic() < until:
        v = cond()
        if v:
            return v
        time.sleep(0.02)
    pytest.fail(what)


def test_the_owner_waits_for_a_named_feed_and_may_stop_it_615(tmp_path):
    """While a feed runs, the owner's action says which and since when; «Остановить загрузку» kills its helper at
    once: the run is cancelled (not a failure), due again by its schedule, and the owner's action runs."""
    st = make(tmp_path)
    clock = {"t": at(FRI, 9, 0)}
    s = service(st, "", clock)
    try:
        submit(s, "describe")
        submit(s, "action", action="login")
        mode(st, "hang_feed")
        s.run_feed("terms")
        _until(lambda: (s.overview()["running"] or {}).get("phase") == "fetching", "the feed did not start")
        job = s.submit("action", {"action": ["check"]})
        assert job["code"] == "waiting_feed" and job["waiting_for"]["feed"] == "terms"
        w = s.overview()["job"]
        assert (w["state"], w["code"]) == ("queued", "waiting_feed")
        assert {k: w["waiting_for"][k] for k in ("kind", "feed", "title", "phase")} == {
            "kind": "feed", "feed": "terms", "title": "Условия сделок", "phase": "fetching"}
        assert w["waiting_for"]["since"] == at(FRI, 9, 0).isoformat()
        t0 = time.monotonic()
        s.cancel("feed")
        job = wait_job(s)
        assert time.monotonic() - t0 < 10, "the feed's helper was not stopped at once"
        assert job["code"] == "connected" and "waiting_for" not in job
        run = svcs.read_state(st)["runs"]["terms"]
        assert (run["state"], run["code"], run["failures"], run["retry_at"]) == ("cancelled", "cancelled", 0, None)
        f = next(x for x in svcs.read_state(st)["feeds"] if x["id"] == "terms")
        assert svcs.next_due(f, run, clock["t"]) == at(FRI, 9, 30)  # by its schedule, no retry step
    finally:
        s.close()


def test_the_owner_goes_first_while_a_feed_waits_for_the_cycle_lock_615(tmp_path):
    """A feed whose files wait for the cycle lock runs no helper: the owner's action runs meanwhile; a stop then ends
    the wait at once and keeps the files for the next run."""
    from desk.autopilot import CycleLock
    st = make(tmp_path)
    clock = {"t": at(FRI, 9, 0)}
    s = service(st, "", clock)
    lock = CycleLock(st.home)
    try:
        submit(s, "describe")
        submit(s, "action", action="login")
        assert lock.acquire()  # an autopilot cycle
        s.run_feed("terms")
        _until(lambda: (s.overview()["running"] or {}).get("kind") == "feed"
               and any(r["op"] == "feed" for r in requests(st)), "the feed did not ask the helper")
        time.sleep(0.3)
        job = s.submit("action", {"action": ["check"]})
        assert job["code"] == "waiting_feed"
        job = wait_job(s)
        assert job["code"] == "connected"
        assert s.overview()["running"]["kind"] == "feed", "the feed still waits for the cycle lock"
        assert not helper_proc.HELPER_LOCK.acquire(blocking=False)  # kept by the waiting feed
        s.cancel("feed")
        _until(lambda: svcs.read_state(st)["runs"]["terms"].get("last_finished"), "the wait was not stopped", 5)
        run = svcs.read_state(st)["runs"]["terms"]
        assert (run["state"], run["code"], run["failures"]) == ("cancelled", "cancelled", 0)
        assert run["pending"]["files"][0]["file"] == "trade_terms_20261015.csv"
    finally:
        lock.release()
        s.close()


def _feed_waiting_for_the_cycle_lock(s: svcs.AdapterServices, st: Settings) -> None:
    s.run_feed("terms")
    _until(lambda: (s.overview()["running"] or {}).get("kind") == "feed"
           and any(r["op"] == "feed" for r in requests(st)), "the feed did not ask the helper")
    time.sleep(0.3)


def test_an_owner_action_that_changes_the_config_in_the_wait_runs_the_feed_again_615(tmp_path):
    """The owner's action run while a feed's files wait for the cycle lock writes the adapter's config folder (as a
    connect or a save does): the files were fetched under the old configuration. Not an error: no failure, no retry
    step, the files stay pending and the same feed is queued again at once, to run under the new configuration."""
    from desk.autopilot import CycleLock
    st = make(tmp_path)
    clock = {"t": at(FRI, 9, 0)}
    s = service(st, "", clock)
    lock = CycleLock(st.home)
    try:
        submit(s, "describe")
        submit(s, "action", action="login")
        (svcs.state_dir(st) / "check_writes_config").write_text("1")
        assert lock.acquire()  # an autopilot cycle
        _feed_waiting_for_the_cycle_lock(s, st)
        s.submit("action", {"action": ["check"]})
        assert wait_job(s)["code"] == "connected"
        assert (st.private_adapter_config / "found.json").is_file()
        assert s.overview()["running"]["kind"] == "feed", "the feed still waits for the cycle lock"
        again = mock.Mock()
        s._feed_by_id = again  # the run queued again is seen, not made (the worker's next step calls it)
        lock.release()
        _until(lambda: svcs.read_state(st)["runs"]["terms"].get("last_finished"), "the feed did not finish", 10)
        run = svcs.read_state(st)["runs"]["terms"]
        assert (run["state"], run["code"]) == ("cancelled", svcs.RECONFIGURED)
        assert not run.get("failures") and not run.get("retry_at")
        assert run["pending"]["files"][0]["file"] == "trade_terms_20261015.csv"
        f = next(x for x in svcs.read_state(st)["feeds"] if x["id"] == "terms")
        assert svcs.next_due(f, run, clock["t"]) == at(FRI, 9, 30)  # by its schedule, no retry step
        _until(lambda: again.called, "the feed was not queued again", 5)
        again.assert_called_once_with("terms", None, trigger="manual")
    finally:
        lock.release()
        s.close()


def test_a_config_change_in_the_wait_without_an_owner_action_is_still_adapter_changed_615(tmp_path):
    from desk.autopilot import CycleLock
    st = make(tmp_path)
    clock = {"t": at(FRI, 9, 0)}
    s = service(st, "", clock)
    lock = CycleLock(st.home)
    try:
        submit(s, "describe")
        submit(s, "action", action="login")
        assert lock.acquire()
        _feed_waiting_for_the_cycle_lock(s, st)
        (st.private_adapter_config / "found.json").write_text("{}")  # changed by something else than the owner's action
        lock.release()
        _until(lambda: svcs.read_state(st)["runs"]["terms"].get("last_finished"), "the feed did not finish", 10)
        run = svcs.read_state(st)["runs"]["terms"]
        assert (run["state"], run["code"], run["failures"]) == ("error", "adapter_changed", 1)
        assert run["retry_at"] == (clock["t"] + timedelta(minutes=5)).isoformat()
        assert run["pending"]["files"][0]["file"] == "trade_terms_20261015.csv"
        with s._lock:
            assert not s._manual  # not queued again
    finally:
        lock.release()
        s.close()


# ---- scheduling ----------------------------------------------------------------------------------------------------

def at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=MSK)


def test_schedule_windows_slots_backoff_and_pause():
    every = {"schedule": svcs.clean_schedule({"every_min": 30, "from": "08:00", "to": "20:00", "weekdays_only": True})}
    assert svcs.next_due(every, {}, at(FRI, 9, 0)) == at(FRI, 9, 0)  # never run: at once in the window
    assert svcs.next_due(every, {}, at(FRI, 7, 0)) == at(FRI, 8, 0)
    assert svcs.next_due(every, {}, at(FRI, 21, 0)) == at(MON, 8, 0)  # after the window, over the weekend
    assert svcs.next_due(every, {"last_started": at(FRI, 9, 0).isoformat()}, at(FRI, 9, 10)) == at(FRI, 9, 30)
    assert svcs.next_due(every, {"last_started": at(FRI, 19, 45).isoformat()}, at(FRI, 19, 50)) == at(MON, 8, 0)
    daily = {"schedule": svcs.clean_schedule({"daily_at": ["18:00", "08:30"]})}
    assert daily["schedule"]["daily_at"] == ["08:30", "18:00"]
    assert svcs.next_due(daily, {}, at(FRI, 15, 0)) == at(FRI, 8, 30)  # never run: today's passed slot, once
    assert svcs.next_due(daily, {"last_started": at(FRI, 15, 0).isoformat()}, at(FRI, 15, 1)) == at(FRI, 18, 0)
    # the monitor off for days: one catch-up run, not one per missed slot
    assert svcs.next_due(daily, {"last_started": at(date(2026, 10, 12), 8, 30).isoformat()}, at(FRI, 9, 0)) \
        == at(date(2026, 10, 12), 18, 0)
    assert svcs.next_due(daily, {"last_started": at(FRI, 9, 0).isoformat()}, at(FRI, 9, 1)) == at(FRI, 18, 0)
    # errors: 5, 15, then every 60 minutes; the retry wins over the schedule, inside its window
    assert [svcs.backoff(n) for n in (1, 2, 3, 4, 9)] == [timedelta(minutes=m) for m in (5, 15, 60, 60, 60)]
    run = {"last_started": at(FRI, 19, 50).isoformat(), "failures": 1, "retry_at": at(FRI, 20, 30).isoformat()}
    assert svcs.next_due(every, run, at(FRI, 19, 55)) == at(MON, 8, 0)
    assert svcs.next_due(daily, {**run, "retry_at": at(FRI, 20, 30).isoformat()}, at(FRI, 20, 0)) == at(FRI, 20, 30)
    assert svcs.next_due(every, {"needs_login": True}, at(FRI, 9, 0)) is None
    assert svcs.next_due({"schedule": None}, {}, at(FRI, 9, 0)) is None
    for bad in ({"every_min": 0}, {"every_min": 5, "from": "20:00", "to": "08:00"}, {"daily_at": ["25:00"]},
                {"daily_at": []}, "hourly", None):
        assert svcs.clean_schedule(bad) is None


def test_scheduler_login_pause_backoff_restart_and_after_positions(tmp_path, store_url, cache):  # noqa: F811
    st = make(tmp_path, snapshot_cache=cache, store_url=store_url)
    load(store_url, tmp_path, "2026-10-15", 10)
    clock = {"t": at(FRI, 9, 0)}
    s = service(st, store_url, clock)
    try:
        assert submit(s, "describe")["code"] == "described"
        assert s._scheduled() is True  # terms: never run, in its window; eodpos off by default; wrongsrc not ready
        run = svcs.read_state(st)["runs"]["terms"]
        assert (run["state"], run["code"], run["needs_login"], run["positions_seen"]) == (
            "blocked", "login_required", True, "2026-10-15")
        assert [r["feed"] for r in requests(st) if r["op"] == "feed"] == ["terms"]
        clock["t"] = at(FRI, 9, 40)
        assert s._scheduled() is False  # paused until a login
        day = {f["id"]: f for f in svcs.day_feeds(st, clock["t"])}
        assert day["terms"]["needs_login"] is True and day["terms"]["next_run"] is None
        assert day["eodpos"]["enabled"] is False

        assert submit(s, "action", action="login")["refreshed"] is True  # a describe after the login: ready again
        assert svcs.read_state(st)["runs"]["terms"]["needs_login"] is False
        mode(st, "error")
        clock["t"] = at(FRI, 9, 41)
        assert s._scheduled() is True
        run = svcs.read_state(st)["runs"]["terms"]
        assert (run["state"], run["code"], run["failures"], run["retry_at"]) == (
            "error", "fetch_failed", 1, at(FRI, 9, 46).isoformat())
        clock["t"] = at(FRI, 9, 45)
        assert s._scheduled() is False
        clock["t"] = at(FRI, 9, 46)
        assert s._scheduled() is True
        assert svcs.read_state(st)["runs"]["terms"]["retry_at"] == at(FRI, 10, 1).isoformat()
        mode(st, "invalid")  # a bad answer is an error too
        clock["t"] = at(FRI, 10, 1)
        assert s._scheduled() is True
        run = svcs.read_state(st)["runs"]["terms"]
        assert (run["code"], run["failures"], run["retry_at"]) == ("helper_response_invalid", 3, at(FRI, 11, 1).isoformat())
        mode(st, "ok")
        clock["t"] = at(FRI, 11, 1)
        assert s._scheduled() is True
        run = svcs.read_state(st)["runs"]["terms"]
        assert (run["state"], run["failures"], run["retry_at"]) == ("complete", 0, None)
        assert run["files"] == [{"file": "trade_terms_20261015.csv", "status": "accepted"}]

        restarted = service(st, store_url, clock)  # a restart does not fetch again at once
        try:
            assert restarted._scheduled() is False
            clock["t"] = at(FRI, 11, 31)
            n = len(requests(st))
            load(store_url, tmp_path, "2026-10-16", 11)  # a newer positions date: once, with that date
            assert restarted._scheduled() is True
            assert requests(st)[n]["date"] == "2026-10-16"
            run = svcs.read_state(st)["runs"]["terms"]
            assert (run["trigger"], run["date"], run["positions_seen"]) == ("after_positions", "2026-10-16", "2026-10-16")
            assert restarted._scheduled() is False
        finally:
            restarted.close()

        s.set_enabled("terms", False)
        clock["t"] = at(MON, 9, 0)
        assert s._scheduled() is False and svcs.read_state(st)["enabled"] == {"terms": False}
    finally:
        s.close()


# ---- ingestion and the runs made again -----------------------------------------------------------------------------

def morning_run(st: Settings) -> int:
    close = ("2026-10-15", "eod_final")
    return cli.eod_run(st, FRI.isoformat(), report_dir=None, out=lambda _: None, run_type="INTRADAY",
                       on_close=close, market_snapshot_id=SnapshotDir(st.snapshot_cache).market_id(*close))


def test_feed_ingests_as_its_source_and_recomputes_the_eod_and_todays_run(tmp_path, store_url, cache):  # noqa: F811
    st = make(tmp_path, snapshot_cache=cache, store_url=store_url)
    load(store_url, tmp_path, "2026-10-15", 10)
    cli.eod_run(st, "2026-10-15", report_dir=None, out=lambda _: None)
    old_morning = morning_run(st)
    with store.connect(store_url) as con:
        old_eod = store.default_runs(con, date(2026, 10, 15))[date(2026, 10, 15)]["run_id"]
        old_hash = store.positions_for(con, FRI).private_inputs_hash
    clock = {"t": at(FRI, 9, 0)}
    s = service(st, store_url, clock)
    try:
        submit(s, "describe")
        submit(s, "action", action="login")
        mode(st, "wrong_source")  # a file of another source than the feed's: rejected before it is read
        run = run_feed(s, st, "terms", clock)
        assert (run["state"], run["code"], run["files"]) == (
            "error", "files_not_accepted", [{"file": "positions_20261015.csv", "status": "rejected"}])
        with store.connect(store_url) as con:
            assert con.execute("SELECT error_code FROM inbox_file_status ORDER BY status_id DESC LIMIT 1"
                               ).fetchone()[0] == "feed_source_mismatch"
            assert con.execute("SELECT count(*) FROM position_set").fetchone()[0] == 2
            assert con.execute("SELECT count(*) FROM dataset_set").fetchone()[0] == 0

        mode(st, "ok")
        run = run_feed(s, st, "terms", clock)
        assert (run["state"], run["code"], run["failures"]) == ("complete", "fetched", 0)
        assert run["dates"] == ["2026-10-15"] and run["lines"] == ["Получена 1 сделка"]
        with store.connect(store_url) as con:
            new_eod = store.default_runs(con, date(2026, 10, 15))[date(2026, 10, 15)]
            today = store.positions_for(con, FRI)
            morning = con.execute("SELECT run_id, private_inputs_hash, cut_type FROM run WHERE business_date = %s"
                                  " AND run_type = 'INTRADAY'", (FRI,)).fetchall()
        assert today.private_inputs_hash != old_hash and today.trade_terms
        assert new_eod["run_id"] != old_eod and new_eod["private_inputs_hash"] != old_hash
        assert len(morning) == 1 and morning[0][0] != old_morning and morning[0][1:] == (today.private_inputs_hash,
                                                                                       "prev_close")
        assert [(r["business_date"], r["previous_run_id"]) for r in run["recomputed"]] == [
            ("2026-10-15", old_eod), ("2026-10-16", old_morning)]
        assert run["recomputed"][1]["label"] == "16.10 · закрытие 15.10" and run["recompute_failed"] == []

        run = run_feed(s, st, "terms", clock)  # the same file again: a copy, nothing made again
        assert run["files"] == [{"file": "trade_terms_20261015.csv", "status": "duplicate"}] and run["recomputed"] == []
        mode(st, "error")
        run = run_feed(s, st, "terms", clock)
        assert (run["failures"], run["retry_at"]) == (1, (clock["t"] + timedelta(minutes=5)).isoformat())
        with pytest.raises(svcs.ServiceError, match="feed_unknown"):
            s.run_feed("nope")
        with store.connect(store_url) as con:
            n_files = con.execute("SELECT count(*) FROM inbox_file_status").fetchone()[0]
        mode(st, "ok")
        run = run_feed(s, st, "wrongsrc", clock)  # files of a source the adapter does not declare: refused
        assert (run["state"], run["code"]) == ("error", "feed_source_invalid")
        run = run_feed(s, st, "archive", clock)  # an undeclared source without files: fine
        assert (run["state"], run["code"], run["files"], run["failures"]) == ("complete", "archived", [], 0)
        with store.connect(store_url) as con:
            assert con.execute("SELECT count(*) FROM inbox_file_status").fetchone()[0] == n_files
        # a trade_terms feed run without a date gets the latest positions business date
        assert next(r["date"] for r in requests(st) if r["op"] == "feed" and r["feed"] == "terms") == "2026-10-15"
    finally:
        s.close()


def test_files_not_taken_in_are_taken_in_at_the_next_run(tmp_path, store_url, cache, monkeypatch):  # noqa: F811
    st = make(tmp_path, snapshot_cache=cache, store_url=store_url)
    load(store_url, tmp_path, "2026-10-15", 10)
    clock = {"t": at(FRI, 9, 0)}
    s = service(st, store_url, clock)
    try:
        submit(s, "describe")
        submit(s, "action", action="login")
        busy = {"n": 1}
        real = s._wait_cycle_lock

        def wait(st_):  # an autopilot cycle holds the lock too long, once
            if busy["n"]:
                busy["n"] -= 1
                return None
            return real(st_)

        monkeypatch.setattr(s, "_wait_cycle_lock", wait)
        run = run_feed(s, st, "terms", clock)
        assert (run["state"], run["code"], run["failures"]) == ("error", "calculation_busy", 1)
        assert run["pending"]["files"][0]["file"] == "trade_terms_20261015.csv"
        with store.connect(store_url) as con:
            assert not store.positions_for(con, FRI).trade_terms
        for _ in range(svcs.KEEP_OUT + 2):  # the pending output survives the pruning
            (svcs.state_dir(st) / "out" / os.urandom(16).hex()).mkdir()
        svcs.AdapterServices._prune(st)
        assert (svcs.state_dir(st) / "out" / run["pending"]["dir"]).is_dir()
        mode(st, "nochange")  # the adapter will not produce them again
        run = run_feed(s, st, "terms", clock)
        assert (run["state"], run["code"], run["failures"], run["pending"]) == ("skipped", "no_change", 0, None)
        assert run["files"] == [{"file": "trade_terms_20261015.csv", "status": "accepted"}]
        assert [r["business_date"] for r in run["recomputed"]] == ["2026-10-15"]
        with store.connect(store_url) as con:
            assert store.positions_for(con, FRI).trade_terms
        run = run_feed(s, st, "terms", clock)
        assert (run["code"], run["files"], run["recomputed"]) == ("no_change", [], [])
    finally:
        s.close()


def test_a_cancelled_retry_waits_for_its_next_step(tmp_path):
    st = make(tmp_path)
    clock = {"t": at(FRI, 9, 0)}
    s = service(st, "", clock)
    try:
        submit(s, "describe")
        f = next(x for x in svcs.read_state(st)["feeds"] if x["id"] == "terms")
        prev = s._record(st, "terms", failures=2, retry_at=at(FRI, 8, 0).isoformat())
        s._finish(st, f, prev, {"state": "cancelled", "code": "cancelled"}, clock["t"])
        run = svcs.read_state(st)["runs"]["terms"]
        assert (run["failures"], run["retry_at"]) == (2, at(FRI, 9, 15).isoformat())
        assert svcs.next_due(f, run, clock["t"]) == at(FRI, 9, 15)
    finally:
        s.close()


def test_manual_acquisition_recomputes_todays_run_547(tmp_path, store_url, cache):  # noqa: F811
    """TDM#547: after «Получить условия сделок» for D-1 today's morning run is made again on the new terms."""
    import test_acquisition as ta

    root, cfg = tmp_path / "adapter", tmp_path / "config"
    root.mkdir()
    cfg.mkdir()
    (root / "__init__.py").write_text(ta.ADAPTER)
    (root / "collect.py").write_text(ta.HELPER)
    (cfg / "mode").write_text("success")
    (cfg / "books.csv").write_text("book_id;name;desk;book_type;entity;active\n"
                                   "OFZ;Synthetic;Test;TRADING;TEST;1\nCORP;Other;Test;TRADING;TEST;1\n")
    st = Settings(home=tmp_path / "home", private_adapter=str(root), private_adapter_config=cfg,
                  acquisition_python=Path(sys.executable), inbox_archive=tmp_path / "archive", inbox_poll_seconds=0,
                  inbox_lookback_days=0, snapshot_cache=cache, store_url=store_url)
    load(store_url, tmp_path, "2026-10-15", 10)
    cli.eod_run(st, "2026-10-15", report_dir=None, out=lambda _: None)
    old_morning = morning_run(st)
    service_ = Acquisition(lambda: st, store_url, today=lambda: FRI)
    try:
        service_.start(ta.fetch_fields(st))
        result = ta.wait(service_)
    finally:
        service_.close()
    assert result["code"] == "completed", result
    with store.connect(store_url) as con:
        want = store.positions_for(con, FRI).private_inputs_hash
        rows = con.execute("SELECT run_id, private_inputs_hash FROM run WHERE business_date = %s"
                           " AND run_type = 'INTRADAY'", (FRI,)).fetchall()
    assert len(rows) == 1 and rows[0][0] != old_morning and rows[0][1] == want
    assert [(r["run_id"], r["label"], r["previous_run_id"]) for r in result["recomputed"]] == [
        (rows[0][0], "16.10 · закрытие 15.10", old_morning)]


def test_autopilot_morning_run_follows_new_trade_terms(tmp_path, store_url, cache):  # noqa: F811
    """TDM#547 for terms that arrive by the inbox too: the morning step sees today's private inputs changed."""
    st = make(tmp_path, snapshot_cache=cache, store_url=store_url)
    load(store_url, tmp_path, "2026-10-15", 10)
    ap = Autopilot(settings_loader=lambda: st, now=lambda: at(FRI, 9, 0))
    with store.connect(store_url) as con:
        assert ap._morning(st, con, store_url, [])[2] == {"reason": "ran"}
        assert ap._morning(st, con, store_url, [])[2] == {"reason": "up_to_date"}
    f = tmp_path / "drop" / "trade_terms_20261015.csv"
    f.parent.mkdir()
    f.write_text(";".join(TERMS_HEADER) + "\nSYNTH-TRADE;OFZ;fx_forward;2026-10-01;2026-12-17;USD;RUB;buy;10;800;80\n")
    with store.connect(store_url) as con:
        res = scan.ingest_feed(con, st, f, source="trade_terms_csv", handle=scan.adapter(st))
        assert res.files == {"accepted": 1}
        _, detail, reason = ap._morning(st, con, store_url, [])
    assert reason == {"reason": "ran"} and "private inputs changed" in detail, detail


# ---- routes --------------------------------------------------------------------------------------------------------

def test_routes_are_local_admin_same_origin_and_never_echo_secrets(tmp_path, store_url, caplog):
    st = make(tmp_path)
    s = service(st)
    app = create_app(store_url)
    app.state.adapter_services.close()
    app.state.adapter_services = s
    with TestClient(app, **LOOP) as c:
        o = c.get("/api/v1/adapter-services").json()
        assert o["available"] and o["panel"] is None  # the describe is asked for in the background
        until = time.monotonic() + 30
        while c.get("/api/v1/adapter-services").json()["panel"] is None and time.monotonic() < until:
            time.sleep(0.05)
        body = urlencode({"f.url": "https://synthetic.invalid", "f.password": SECRET})
        for headers in ({"Origin": "null"}, {"Origin": "http://127.0.0.1:9999"}, {"Origin": "https://evil.example"},
                        {"X-Forwarded-For": "127.0.0.1"}, {"Content-Type": "application/json"}):
            r = c.post("/api/v1/adapter-services/save", content=body, headers={**FORM, **headers})
            assert r.status_code in (403, 415)
            no_secret(r.text)
        assert c.post("/api/v1/adapter-services/save", content=body,
                      headers={"Content-Type": FORM["Content-Type"]}).status_code == 403
        assert c.get("/api/v1/adapter-services", headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 403
        for bad in (b"\xff", b"f.nope=1", b"output_dir=/tmp", b"f.url=a&f.url=b", b"f.url=" + b"x" * 66000):
            r = c.post("/api/v1/adapter-services/save", content=bad, headers=FORM)
            assert r.status_code == 422, (bad[:20], r.text)
        r = c.post("/api/v1/adapter-services/save", content=body, headers=FORM)
        assert r.status_code == 202 and r.json()["state"] == "queued"
        no_secret(r.text)
        wait_job(s)
        o = c.get("/api/v1/adapter-services")
        assert o.json()["job"]["code"] == "saved" and o.headers["cache-control"] == "no-store"
        no_secret(o.text)
        r = c.post("/api/v1/adapter-services/action", content=urlencode({"action": "check"}), headers=FORM)
        assert r.status_code == 202 and wait_job(s)["code"] == "connected"
        r = c.post("/api/v1/adapter-services/feeds/eodpos/enabled", content="enabled=true", headers=FORM)
        assert r.status_code == 200 and svcs.read_state(st)["enabled"] == {"eodpos": True}
        assert next(f for f in c.get("/api/v1/adapter-services").json()["feeds"] if f["id"] == "eodpos")["enabled"]
        assert c.post("/api/v1/adapter-services/feeds/eodpos/enabled", content="enabled=1",
                      headers=FORM).status_code == 422
        assert c.post("/api/v1/adapter-services/feeds/nope/run", content="", headers=FORM).status_code == 404
        assert c.post("/api/v1/adapter-services/feeds/a%20b/run", content="", headers=FORM).status_code == 422
        assert c.post("/api/v1/adapter-services/feeds/terms/run", content="date=16.10.2026",
                      headers=FORM).status_code == 422
        assert c.post("/api/v1/adapter-services/cancel", content="target=all", headers=FORM).status_code == 422
        assert c.post("/api/v1/adapter-services/cancel", content="", headers=FORM).status_code == 200
    for client, host in ((("10.1.2.3", 1), "127.0.0.1"), (("127.0.0.1", 1), "evil.example")):
        c = TestClient(app, base_url=f"http://{host}:8100", client=client)
        assert c.post("/api/v1/adapter-services/save", content=body, headers=FORM).status_code == 403
        assert c.get("/api/v1/adapter-services").status_code == 403
    with store.connect(store_url) as con:
        rows = con.execute("SELECT action, target, detail FROM audit_log WHERE action = 'adapter_services'").fetchall()
    no_secret(rows, caplog.text, (svcs.state_dir(st) / "state.json").read_text(encoding="utf-8"))
    s.close()


def _listing(st: Settings, tmp_path: Path, store_url: str, name: str, body: str, source: str) -> None:
    f = tmp_path / "drop" / name
    f.parent.mkdir(exist_ok=True)
    f.write_text(body)
    with store.connect(store_url) as con:
        assert scan.ingest_feed(con, st, f, source=source, handle=scan.adapter(st)).files == {"accepted": 1}


def test_a_terms_feed_gets_the_request_file_of_the_trade_list_561(tmp_path, store_url, cache):  # noqa: F811
    """TDM#561: a ``trades_file`` feed gets, as its one extra field, a file with the live trades of the owner's
    books marked as needed (new, changed) or not, and the owner's line of counts; without a list: as before."""
    st = make(tmp_path, store_url=store_url)
    mode(st, "plan_feed")
    clock = {"t": datetime(2026, 10, 16, 9, 0, tzinfo=MSK)}
    s = service(st, store_url, clock)
    try:
        submit(s, "describe")
        assert [f["trades_file"] for f in svcs.read_state(st)["feeds"] if f["id"] == "planned"] == [True]
        load(store_url, tmp_path, "2026-10-15", 10)
        (svcs.state_dir(st) / "logged_in").write_text("1")
        run = run_feed(s, st, "planned", clock)  # no list stored: the helper is called without the field
        assert run["state"] == "complete" and "plan" not in run
        assert requests(st)[-1]["trades_file"] == [] and requests(st)[-1]["op"] == "feed"
        assert run["lines"][0].startswith("Список сделок за 15.10.2026 не получен") and "полным проходом" in run["lines"][0]
        _listing(st, tmp_path, store_url, "trade_terms_20261015.csv", ";".join(TERMS_HEADER) + "\n"
                 "T-1;OFZ;fx_forward;2026-10-01;2026-12-17;USD;RUB;buy;10;800;80\n"
                 "T-2;OFZ;fx_forward;2026-10-01;2026-12-17;USD;RUB;buy;10;800;80\n"
                 "T-3;OFZ;fx_forward;2026-10-01;2026-12-17;USD;RUB;buy;10;800;80\n", "trade_terms_csv")
        _listing(st, tmp_path, store_url, "trade_list_20261015.csv",
                 "trade_id;trade_version;book_id;status\nT-1;;OFZ;live\nT-2;;OFZ;live\nT-4;1;OFZ;live\n"
                 "T-3;;OFZ;cancelled\nT-9;1;CORP;live\n", "trade_list_csv")
        run = run_feed(s, st, "planned", clock)
        assert run["state"] == "complete", run
        sent = requests(st)[-1]
        assert len(sent["trades_file"]) == 1 and sent["keys"].count("trades_file") == 1
        req = sent["req"]
        assert req["format"] == "terms-request" and req["version"] == 1 and req["date"] == "2026-10-15"
        assert req["books"] == ["CORP", "OFZ"]
        assert {t["trade_id"]: t["need"] for t in req["trades"]} == {"T-1": False, "T-2": False, "T-4": True,
                                                                    "T-9": True}
        assert run["plan"] == {"live": 4, "books": 2, "requested": 2, "new": 2, "changed": 0, "have": 2, "closed": 1}
        assert run["lines"][0] == ("Список: сделок 4, книг 2; запрошено 2 (новых 2, изменённых 0), уже были 2, "
                                   "закрыто 1.")
        assert Path(sent["trades_file"][0]).parent == svcs.state_dir(st) / "requests"
        for i in range(7):  # only the last KEEP_REQUESTS request files stay
            run_feed(s, st, "planned", clock)
        assert len(list((svcs.state_dir(st) / "requests").glob("*.json"))) == svcs.KEEP_REQUESTS
        assert not list((svcs.state_dir(st) / "requests").glob("*.tmp"))
    finally:
        s.close()


def test_a_terms_feed_first_gets_the_list_of_its_date_561(tmp_path, store_url, cache):  # noqa: F811
    """TDM#561: the list of the feed's date is made before the terms request in the same turn (the enabled
    ``trade_list`` feed runs first), and a list of an earlier day (a stock carried forward) is no list: asked again."""
    st = make(tmp_path, store_url=store_url)
    mode(st, "plan_feed")
    clock = {"t": datetime(2026, 10, 16, 9, 0, tzinfo=MSK)}
    s = service(st, store_url, clock)
    try:
        submit(s, "describe")
        load(store_url, tmp_path, "2026-10-15", 10)
        with store.connect(store_url) as con:
            store.store_books(con, [{"book_id": b, "name": b, "desk": "Test", "book_type": "TRADING", "entity": "TEST",
                                     "active": True} for b in ("OFZ", "CORP")], "fp")
        (svcs.state_dir(st) / "logged_in").write_text("1")
        (svcs.state_dir(st) / "tlist.csv").write_text("trade_id;trade_version;book_id;status\nT-1;;OFZ;live\n")
        run = run_feed(s, st, "planned", clock)
        feeds_called = [(r["feed"], r["date"], bool(r["trades_file"])) for r in requests(st) if r["op"] == "feed"]
        assert feeds_called == [("tlist", "2026-10-15", False), ("planned", "2026-10-15", True)], feeds_called
        assert svcs.read_state(st)["runs"]["tlist"]["trigger"] == "dependency"
        assert run["plan"]["live"] == 1 and run["lines"][0].startswith("Список: сделок 1, книг 1")
        run_feed(s, st, "planned", clock)  # the list is of the day: not made again
        assert [r["feed"] for r in requests(st) if r["op"] == "feed"] == ["tlist", "planned", "planned"]
        with mock.patch.object(svcs, "MAX_REQUEST_TRADES", 0):  # too long a list: no file, and the line says so
            run = run_feed(s, st, "planned", clock)
        assert run["lines"][0].startswith("Заявка не построена: в списке больше 0 сделок") and requests(st)[-1][
            "trades_file"] == []
        load(store_url, tmp_path, "2026-10-16", 10)  # newer positions: the list of the 15th is stale
        run_feed(s, st, "planned", clock)
        assert [(r["feed"], r["date"]) for r in requests(st) if r["op"] == "feed"][-2:] == [
            ("tlist", "2026-10-16"), ("planned", "2026-10-16")]
        # no list feed made one (it answers no_change): the terms run as before, with the owner's line
        (svcs.state_dir(st) / "tlist.csv").unlink()
        load(store_url, tmp_path, "2026-10-19", 10)
        run = run_feed(s, st, "planned", clock)
        assert run["lines"][0].startswith("Список сделок за 19.10.2026 не получен")
    finally:
        s.close()


def test_a_feed_of_the_owners_books_gets_them_as_a_repeated_field(tmp_path, store_url, cache):  # noqa: F811
    """TDM#557: ``books: true`` feeds are called with a repeated ``book`` (active books with positions, sorted);
    with none, the feed is not called and says why."""
    st = make(tmp_path, store_url=store_url)
    mode(st, "books_feed")
    clock = {"t": datetime(2026, 10, 16, 9, 0, tzinfo=MSK)}
    s = service(st, store_url, clock)
    try:
        submit(s, "describe")
        assert [f["books"] for f in svcs.read_state(st)["feeds"]][:2] == [True, False]
        run = run_feed(s, st, "owned", clock)
        assert (run["state"], run["code"]) == ("blocked", "no_books") and run["lines"] and run["retry_at"] is None
        assert [r for r in requests(st) if r["op"] == "feed"] == []
        load(store_url, tmp_path, "2026-10-15", 10)  # positions of the books OFZ and CORP
        f = tmp_path / "old.csv"   # the inactive book has positions too: only the book list keeps it out
        f.write_text("position_id,book_id,secid,quantity_bonds\nP9,OLD,SU26238RMFS4,3\n", encoding="utf-8")
        with store.connect(store_url) as con:
            cli._load(con, f, "2026-10-15", out=lambda _: None)
        books = [{"book_id": b, "name": b, "desk": "Test", "book_type": "TRADING", "entity": "TEST", "active": a}
                 for b, a in (("OFZ", True), ("CORP", True), ("OLD", False))]
        with store.connect(store_url) as con:
            store.store_books(con, books, "fp")
        run = run_feed(s, st, "owned", clock)
        assert run["code"] == "login_required"  # the helper asked for a login: it was called
        (svcs.state_dir(st) / "logged_in").write_text("1")
        run = run_feed(s, st, "owned", clock)
        assert run["state"] == "complete", run
        assert [r["book"] for r in requests(st) if r["op"] == "feed"] == [["CORP", "OFZ"]] * 2
        run_feed(s, st, "terms", clock)
        assert [r["book"] for r in requests(st) if r["op"] == "feed"][-1] == []
    finally:
        s.close()


def test_feeds_declare_history_and_the_earliest_date_the_source_keeps(tmp_path):
    """TDM#569: ``history`` and ``history_from`` are optional keys of a FEED; anything but a flag and an ISO date is
    dropped; an old adapter's feed has neither."""
    base = {"id": "t", "title": "Сделки", "dataset": "trades", "source": "trades_csv", "enabled": True, "ready": True}
    got = svcs.clean_feeds([{**base, "history": True, "history_from": "2026-01-05"},
                            {**base, "id": "u", "history": "yes", "history_from": "05.01.2026"},
                            {**base, "id": "v", "history_from": "2026-13-45"}, {**base, "id": "w"}], None)
    assert [(f["history"], f["history_from"]) for f in got] == [(True, "2026-01-05"), (False, None), (False, None),
                                                                (False, None)]


def test_prune_keeps_the_pending_files_of_the_history_of_trades(tmp_path):
    from desk import trades_history

    st = make(tmp_path)
    keep = svcs.state_dir(st) / "out" / os.urandom(16).hex()
    keep.mkdir(parents=True)
    for _ in range(svcs.KEEP_OUT + 3):
        (svcs.state_dir(st) / "out" / os.urandom(16).hex()).mkdir()
    doc = trades_history.blank()
    doc["pending"] = {"dir": keep.name, "files": [], "state": "complete", "code": "exported", "date": "2026-10-15"}
    trades_history.write(st, doc)
    svcs.AdapterServices._prune(st)
    assert keep.is_dir()
