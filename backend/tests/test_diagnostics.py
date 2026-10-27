"""The diagnostics export (R13): built on a store with a fixture run made by an autopilot cycle (stub hub, reference
adapter, canonical inbox fixtures), it holds the key sections and nothing private: no ISIN, book, trader, position
or instrument id, report file name, path, hub host or token. Synthetic data only."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, time

import pytest
import test_slice_s3
from fastapi.testclient import TestClient
from test_autopilot import CONFIG, PRIV, SNAPS, TODAY, cfg, put
from test_slice_s3 import TOKEN

from desk import cli, diagnostics
from desk.api import create_app
from desk.autopilot import Autopilot
from desk.diagnostics import Scrubber, signature
from desk.inbox import scan
from desk.inbox.timesem import MSK

server, stub = test_slice_s3.server, test_slice_s3.stub  # the stub hub's fixtures
# The export's clock, pinned to the fixture's day: the serve.log lines below are dated TODAY, and the logs section
# keeps only the last LOG_DAYS days from `now` (the real clock would drop them a week later, #240-style).
NOW = datetime.combine(TODAY, time(12, 0), MSK)
SECTIONS = ("app", "clock", "settings", "autopilot", "hub", "snapshots", "static", "inbox", "positions", "runs",
            "logs", "db")


@pytest.fixture(autouse=True)
def _fresh_process():
    scan._handles.clear()
    scan._last_seen.clear()
    diagnostics._INFO.clear()
    diagnostics._RATES.clear()
    yield
    scan._handles.clear()
    scan._last_seen.clear()


def private_values() -> set[str]:
    """Every value of the fixture inbox and books that must not leave the machine."""
    out = set()
    for name in ("positions_20261016.csv",):
        with open(PRIV / name, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                out |= {row[k] for k in ("position_id", "source_instrument_id", "source_instrument_name", "isin")
                        if row.get(k) and len(row[k]) >= 4}
    with open(CONFIG / "books.csv", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            out |= {row[k] for k in ("name", "trader", "entity") if row.get(k)}
    index = json.loads((SNAPS / "golden_index.json").read_text())
    return out | set(index["instruments"]) | {"positions_20261016", TOKEN}


@pytest.fixture
def diag_run(stub, tmp_path, store_url):
    """An autopilot cycle against the stub hub: sync, scan of positions_20261016.csv, EOD run 1 of 15.10; plus a
    serve.log error line naming an ISIN and a report path (what a failing step writes)."""
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    ap = Autopilot(settings_loader=lambda: s, today=lambda: TODAY)
    st = ap.run_cycle()
    assert st["state"] == "idle", st["steps"]
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    (s.logs_dir / "serve.log").write_text(
        f"{TODAY} 10:00:00,123 INFO desk.api abc GET /api/v1/runs 200 3ms\n"
        f"{TODAY} 10:00:01,456 ERROR desk.api read failed for RU000A0ZZZZ1 (RU000A1038V6) in "
        f"{tmp_path / 'in' / 'positions_20261016.csv'}, {tmp_path / 'other' / 'x.txt'}: PnL 1,234,567 RUB for B1\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "ValueError: boom\n", encoding="utf-8")
    return s, ap


def test_diagnostics_sections_and_no_private_data(diag_run, tmp_path, stub):
    s, ap = diag_run
    doc = diagnostics.build(s, store_url=s.store_url, autopilot=ap, hub=True, now=NOW)
    text = json.dumps(doc, ensure_ascii=False)

    assert doc["schema"] == "desk-diagnostics/1"
    for k in SECTIONS:
        assert k in doc and not (isinstance(doc[k], dict) and doc[k].get("error")), (k, doc.get(k))
    # privacy: none of the fixture's private values, no path, no hub host, no token
    leaked = [v for v in private_values() if v in text]
    assert not leaked, leaked
    assert not re.search(r"\bB[1-3]\b", text), re.findall(r".{40}\bB[1-3]\b.{40}", text)
    assert str(tmp_path) not in text and str(s.home) not in text and "127.0.0.1" not in text
    assert not re.search(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", text)
    assert "1,234,567" not in text and TOKEN not in text

    # settings: flags and numbers as they are, paths only set/exists
    assert doc["settings"]["autopilot"] is True and doc["settings"]["inbox_lookback_days"] == 0
    assert doc["settings"]["inbox_dirs"] == {"count": 1, "exist": 1, "recursive": 0}
    assert doc["settings"]["hub_token"] == "set" and doc["settings"]["hub_url"] == {"set": True, "scheme": "http"}
    # autopilot: the in-process cycle with step durations, the same cycle read from autopilot.log
    cyc = doc["autopilot"]["cycles"][0]
    assert [x["name"] for x in cyc["steps"]][:3] == ["sync", "scan", "eod"]
    assert all(isinstance(x["duration_s"], float) for x in cyc["steps"])
    assert "hub <host>" in cyc["steps"][0]["detail"] or "hub <ip>" in cyc["steps"][0]["detail"]
    log_cyc = doc["autopilot"]["from_log"]["last_cycles"][0]
    assert log_cyc["started_at"] and [x["name"] for x in log_cyc["steps"]][:3] == ["sync", "scan", "eod"]
    assert doc["autopilot"]["intraday"]["window"] in ("before_intraday", "intraday", "preliminary")
    # hub: live check and its list, marked cached
    hub = doc["hub"]
    assert hub["reachable"] and hub["token_accepted"] and hub["latest_eod_final"]["business_date"] == "2026-10-15"
    assert hub["remote"][0]["cached"] and hub["remote"][0]["cut_type"] == "eod_final"
    # snapshots: curve quote sets per source/dataset and the curve builds (R14, R15)
    snap = doc["snapshots"]["market"][0]
    assert snap["business_date"] == "2026-10-15" and snap["zcyc_present"] and snap["bond_quotes"] == 11
    assert snap["ruonia_ois_quotes"] == {"otc_monitor/ruonia_ois": 17, "ratescalcs/ruonia_ois": 17}
    assert "ratescalcs/usd_futures_yields" in snap["expected_missing"]
    assert snap["rates_check"]["ruonia_ois"]["built"] and snap["rates_check"]["ruonia_ois"]["source"] == "ratescalcs"
    assert snap["rates_check"]["key_rate"]["built"]
    assert doc["static"][0]["instruments"] == 10 and doc["static"][0]["by_coupon_type"]["floating"] == 2
    # inbox and positions: counts only
    assert doc["inbox"]["files_by_status"] == {"accepted": 1} and doc["inbox"]["adapter"]["loaded"]
    assert doc["inbox"]["extensions"] == {".csv": 1}
    assert doc["positions"]["today"]["positions"] == 5 and doc["positions"]["books"]["in_force"] == 4
    # runs: the EOD run of 15.10 with qualities, curves, risk per curve, coupons and the PnL reason
    run = doc["runs"]["last"][0]
    assert run["run_id"] == 1 and run["as_of"] == "2026-10-15" and run["run_type"] == "EOD" and run["is_current"]
    assert run["positions"] == 5 and run["books"] == 2 and sum(run["quality"].values()) == 5
    assert run["risk_by_curve"]["RUB RUONIA"]["has_risk"]
    assert {c["curve_id"] for c in run["curves"] if c["built"]} >= {"RUB OFZ", "RUB RUONIA", "RUB KEY"}
    assert run["discount_curve"].get("RUB RUONIA")
    assert run["pnl"]["available"] is False and run["pnl"]["run_reason"] == "no_prev_market"
    assert run["coupon"]["bonds"] >= 1 and "float" in run["coupon"]["by_type"]
    assert run["rates"]["ruonia_ois"]["source"] == "ratescalcs"
    # logs: the error line scrubbed, with its exception
    err = next(x for x in doc["logs"]["recent_warnings_errors"] if x["file"] == "serve.log")
    assert err["exception"] == "ValueError"
    assert err["text"] == "read failed for <ISIN> (<ISIN>) in <path>, <path>: PnL # RUB for <book>"
    assert doc["db"]["migration_level"] and doc["db"]["tables"]["run"] == 1


def test_diagnostics_api_and_cli(diag_run, tmp_path, monkeypatch, capsys, stub):
    s, _ = diag_run
    for k, v in {"DESK_HOME": str(s.home), "DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": s.store_url,
                 "DESK_HUB_URL": s.hub_url, "DESK_HUB_TOKEN": TOKEN, "DESK_SNAPSHOT_CACHE": str(s.snapshot_cache),
                 "DESK_INBOX_DIRS": str(tmp_path / "in"), "DESK_INBOX_ARCHIVE": str(s.inbox_archive),
                 "DESK_PRIVATE_ADAPTER_CONFIG": str(CONFIG), "DESK_INBOX_LOOKBACK_DAYS": "0"}.items():
        monkeypatch.setenv(k, v)
    app = create_app(s.store_url)
    local = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    r = local.get("/api/v1/diagnostics", params={"hub": 0})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["schema"] == "desk-diagnostics/1" and doc["hub"]["checked"] is False
    assert doc["lan"]["bind"] == "loopback" and doc["lan"]["tls"] is False and "probe" not in doc["lan"]
    app.state.lan_probe = {"at": "2026-10-22T10:00:00+00:00", "summary": "s",
                           "loopback": {"address": "127.0.0.1", "status": "ok", "ms": 3, "detail": "HTTP 200"},
                           "lan": [{"address": "10.1.2.3", "status": "no_response", "ms": 3000, "detail": "x"}]}
    lan_doc = local.get("/api/v1/diagnostics", params={"hub": 0}).json()["lan"]
    assert [r["status"] for r in lan_doc["probe"]["lan"]] == ["no_response"] and "10.1.2.3" not in str(lan_doc)
    st = local.get("/api/v1/lan/status").json()  # the settings page's «Доступ из сети»
    assert st["bind_host"] and st["probe"] is None and st["last"]["lan"][0]["status"] == "no_response"
    assert doc["app"]["pid_uptime_s"] is not None and doc["runs"]["last"][0]["run_id"] == 1
    assert doc["app"]["env"]["DESK_HUB_TOKEN"] == "set" and doc["app"]["env"]["DESK_INBOX_LOOKBACK_DAYS"] == "0"
    assert TOKEN not in r.text and str(tmp_path) not in r.text
    r = local.get("/api/v1/diagnostics", params={"hub": 0, "download": 1})
    assert re.fullmatch(r'attachment; filename="desk-diagnostics-\d{8}T\d{6}Z\.json"',
                        r.headers["content-disposition"])
    remote = TestClient(app, base_url="http://127.0.0.1:8000", client=("10.1.2.3", 50000))
    assert remote.get("/api/v1/diagnostics").status_code == 403  # this computer only, like the settings

    out = tmp_path / "diag.json"
    assert cli.main(["diagnostics", "--out", str(out), "--no-hub"]) == 0
    assert "diagnostics written" in capsys.readouterr().out
    text = out.read_text(encoding="utf-8")
    assert json.loads(text)["runs"]["last"][0]["as_of"] == "2026-10-15"
    leaked = [v for v in private_values() if v in text]
    assert not leaked and str(tmp_path) not in text


def test_diagnostics_without_a_store_still_reports(tmp_path):
    """No store: the file-based sections go out, the store's say why they are missing."""
    from desk.settings import Settings

    s = Settings(home=tmp_path / "home", snapshot_cache=tmp_path / "none", store_url="postgresql://u@127.0.0.1:1/x")
    doc = diagnostics.build(s, hub=False)
    assert doc["app"]["version"] and doc["settings"]["store"] == "external"
    assert "store unavailable" in doc["runs"]["error"] and doc["snapshots"]["cache_exists"] is False
    assert "127.0.0.1" not in json.dumps(doc)


def test_scrubber_text():
    sc = Scrubber({"Rates trading": "<book>", "B1|SEC-26238": "<position>", "B1": "<book>"}, names=["ivanov"])
    t = sc.text(r"book B1 (Rates trading) B1|SEC-26238 RU000A1038V6 C:\Users\Ivanov\Documents\Отчёты дня\pos.xlsx "
                r"\\fs01\share\x.csv /home/ivanov/x https://hub.example:8443/v1?token=abc 10.0.0.5:3128 "
                "user IVANOV pnl 1,234,567.89 RUB, 9876543 and 12.5 ₽; on 2026-10-19 run 12 of 19.10")
    assert t == ("book <book> (<book>) <position> <ISIN> <path> <path> <path> <url> <ip> user <name> pnl # RUB, # "
                 "and # ₽; on 2026-10-19 run 12 of 19.10")
    assert signature(sc, "eod 2026-10-15 run 17 (no default run) 4f173275220b") == "eod <date> run # (no default run) <id>"
