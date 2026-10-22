"""Deep market history from the hub's history API (R24): the client (paging, gzip, ETag / 304, Decimal, 401 / 403 /
404), the sync into the md_hub_* tables (full, incremental, resumable within a budget, corrections, rows the hub
dropped), the fallback to the snapshots with a visible reason, and the readers (bond history, positions history,
the lazy market endpoint) over the API's rows. The hub is ``hub_history_stub`` (synthetic data from the golden
fixture market, weekdays from 2024-01-03 for bonds and 2023-01-02 for the rest)."""

from __future__ import annotations

import dataclasses
import gzip
import json
from datetime import date, timedelta
from decimal import Decimal

import hub_history_stub as hs
import pytest
from fastapi.testclient import TestClient
from test_market_history import DATE, FIX, FLOATER, OFZ, fixture_cache, rows

from desk import autopilot, cli, store
from desk.api import create_app
from desk.marketdata import hubhistory
from desk.marketdata.hub import (
    NO_HISTORY_API,
    NO_READ_SCOPE,
    HistoryUnavailable,
    HubClient,
    HubError,
    hub_check,
)
from desk.settings import Settings, check_settings_doc, load

TODAY = date(2026, 10, 16)
LINKER = "SU52005RMFS4"


@pytest.fixture(scope="module")
def server():
    srv = hs.start()
    yield srv
    srv.shutdown()


@pytest.fixture
def hub(server, monkeypatch):
    server.reset()
    hubhistory.forget()
    monkeypatch.setattr(HubClient, "backoff_s", 0.0)
    return server


def settings(srv, tmp_path, **kw) -> Settings:
    return Settings(hub_url=hs.url(srv), hub_token=hs.TOKEN, hub_timeout_s=5, home=tmp_path / "home", **kw)


def client(srv, tmp_path) -> HubClient:
    return HubClient(settings(srv, tmp_path))


def sync(con, c, **kw) -> hubhistory.Report:
    kw.setdefault("budget_s", 120)
    return hubhistory.sync(con, c, today=TODAY, **kw)


# ---- the client ---------------------------------------------------------------------------------------------------


def test_page_is_gzipped_decimal_and_revalidates(hub, tmp_path):
    c = client(hub, tmp_path)
    p = c.history_page("/bonds", {"from": "2024-01-03", "to": "2024-12-31", "secid": OFZ, "limit": 100})
    assert p.columns[:3] == ["trade_date", "secid", "board"] and len(p.rows) == 100 and p.next
    assert all(isinstance(v, Decimal) for v in (p.rows[0][p.columns.index("close_pct")],
                                               p.rows[0][p.columns.index("effective_yield_pct")]))
    raw = len(json.dumps({"columns": p.columns, "rows": p.rows}, default=str))
    assert 0 < p.wire_bytes < raw / 3  # gzip
    assert p.etag.startswith('W/"') and p.data_version == hs.WRITTEN
    again = c.history_page("/bonds", {"from": "2024-01-03", "to": "2024-12-31", "secid": OFZ, "limit": 100},
                           etag=p.etag)
    assert again.not_modified and again.rows == [] and hub.requests[-1][1] == p.etag
    pages = list(c.history_pages("/bonds", {"from": "2024-01-03", "to": "2024-12-31", "secid": OFZ, "limit": 100}))
    assert len(pages) == 3 and sum(len(x.rows) for x in pages) == len(hs.weekdays(date(2024, 1, 3), date(2024, 12, 31)))
    keys = [(r[0], r[1]) for x in pages for r in x.rows]
    assert len(keys) == len(set(keys))  # no page overlaps another


def test_decimal_digits_are_kept(monkeypatch, tmp_path):
    """97.80 stays Decimal('97.80'): the hub sends the stored digits."""
    from desk.marketdata import hub as hubmod

    body = gzip.compress(b'{"dataset":"bonds","columns":["trade_date","secid","board","close_pct"],'
                         b'"rows":[["2026-10-15","SU1","TQOB",97.80]],"as_of":"x","known_at":null,'
                         b'"data_version":null,"next":null,"meta":{}}')
    from email.message import Message

    h = Message()
    h["Content-Encoding"] = "gzip"
    h["ETag"] = 'W/"a"'
    c = HubClient(Settings(hub_url="https://hub.test", hub_token="t", home=tmp_path))
    monkeypatch.setattr(c.transport, "get", lambda url, token, headers=None: hubmod.Response(200, h, body))
    p = c.history_page("/bonds", {"from": "2026-10-15"})
    assert p.rows[0][3] == Decimal("97.80") and str(p.rows[0][3]) == "97.80"


@pytest.mark.parametrize(("mode", "code", "reason"), [("no_api", "no_api", NO_HISTORY_API),
                                                      ("no_read", "no_read", NO_READ_SCOPE)])
def test_unavailable_api_has_a_reason(hub, tmp_path, mode, code, reason):
    hub.mode = mode
    with pytest.raises(HistoryUnavailable) as e:
        client(hub, tmp_path).history_index()
    assert (e.value.code, str(e.value)) == (code, reason)
    chk = hub_check(settings(hub, tmp_path))
    assert chk["history_api"] == {"available": False, "code": code, "reason": reason, "datasets": None,
                                  "series": None}


def test_errors_and_retries(hub, tmp_path):
    c = client(hub, tmp_path)
    hub.fail = 2  # 503 twice, then the answer
    assert c.history_index()
    with pytest.raises(HubError, match="400"):  # a cursor of another query
        c.history_page("/bonds", {"from": "2024-01-03", "limit": 10},
                       cursor=c.history_page("/zcyc", {"from": "2024-01-03", "limit": 10}).next)
    with pytest.raises(HubError, match="404"):
        c.history_page("/curves/nope", {"from": "2024-01-03"})
    bad = HubClient(dataclasses.replace(settings(hub, tmp_path), hub_token="wrong"))
    with pytest.raises(HistoryUnavailable, match="401"):
        bad.history_index()
    chk = hub_check(settings(hub, tmp_path))
    assert chk["history_api"]["available"] and "bonds" in chk["history_api"]["datasets"]


# ---- the sync -----------------------------------------------------------------------------------------------------


def test_full_sync_then_incremental(hub, tmp_path, store_url):
    c = client(hub, tmp_path)
    with store.connect(store_url) as con:
        r = sync(con, c, held=[OFZ])
        assert r.available and not r.errors and r.left == 0, r.text()
        assert rows(con, "SELECT count(*), count(DISTINCT secid), min(trade_date) FROM md_hub_bond") == [
            (len(hub.data["bonds"]), 10, date(2024, 1, 3))]
        assert rows(con, "SELECT count(*) FROM md_hub_zcyc") == [(len(hub.data["zcyc"]),)]
        assert rows(con, "SELECT count(*) FROM md_hub_curve") == [(len(hub.data["curves"]),)]
        assert rows(con, "SELECT count(*) FROM md_hub_fixing") == [(len(hub.data["fixings"]),)]
        assert rows(con, "SELECT count(*) FROM md_hub_fx") == [(len(hub.data["fx"]),)]
        assert rows(con, "SELECT secid FROM md_hub_held") == [(OFZ,)]
        assert rows(con, "SELECT value, unit FROM md_hub_fixing WHERE index_code = 'RUSFARIND'"
                         " ORDER BY fixing_date DESC LIMIT 1")[0][1] == "index"
        # the jobs: a tail per dataset, months of bonds, years of the rest
        jobs = dict(rows(con, "SELECT dataset || ' ' || key, status FROM md_hub_job"))
        assert jobs["bonds tail"] == jobs["bonds 2024-01"] == jobs["zcyc 2023"] == jobs["held held"] == "done"
        assert "bonds 2026-09" in jobs and "bonds 2026-10" not in jobs  # October is the tail's
        n = len(hub.requests)
        assert sync(con, c).requests == 0 and len(hub.requests) == n  # nothing due: no request (index memo)
        # the tail is due again after TAIL_POLL_S: one conditional request per dataset, all 304
        con.execute("UPDATE md_hub_job SET checked_at = checked_at - interval '1 hour' WHERE key = 'tail'")
        r = sync(con, c)
        assert r.requests == r.not_modified == 6 and r.rows == 0
        # a day later every closed chunk is revalidated: 304s, nothing fetched
        con.execute("UPDATE md_hub_job SET checked_at = checked_at - interval '2 days'")
        r = sync(con, c)
        assert r.requests == r.not_modified == len(jobs) - 1 and r.rows == 0  # the held batch is not rechecked


def test_tail_starts_on_the_first_of_the_month_two_weeks_back():
    assert hubhistory.tail_from(date(2026, 10, 16)) == date(2026, 10, 1)
    assert hubhistory.tail_from(date(2026, 10, 10)) == date(2026, 9, 1)
    spec = next(s for s in hubhistory.specs([{"dataset": "bonds", "date_from": "2024-01-03"}]) if s.name == "bonds")
    ch = hubhistory.chunks(spec, date(2026, 10, 10))
    assert (ch[0].key, ch[0].period_to) == ("2026-08", date(2026, 8, 31))
    assert (ch[-1].key, ch[-1].period_from) == ("2024-01", date(2024, 1, 3))
    rates = next(s for s in hubhistory.specs([{"dataset": "zcyc", "date_from": "2014-01-06"}]))
    yr = hubhistory.chunks(rates, date(2026, 10, 10))
    assert (yr[0].key, yr[0].period_from, yr[0].period_to) == ("2026", date(2026, 1, 1), date(2026, 8, 31))
    assert yr[-1].period_from == date(2014, 1, 6)


def test_paging_is_resumable_within_the_budget(hub, tmp_path, store_url, monkeypatch):
    monkeypatch.setattr(hubhistory, "PAGE_LIMIT", 500)
    ticks = iter(range(10_000))
    clock = lambda: next(ticks) * 1.0  # one "second" per call of the clock
    c = client(hub, tmp_path)
    with store.connect(store_url) as con:
        partial, resumed = 0, 0
        for _ in range(300):
            r = hubhistory.sync(con, c, today=TODAY, budget_s=12, clock=clock)
            if r.partial:  # the budget ran out between two pages: the cursor is kept
                partial += 1
                kept = rows(con, "SELECT count(*) FROM md_hub_job WHERE status = 'partial' AND cursor IS NOT NULL")
                assert kept == [(len(r.partial),)]
            elif partial:
                resumed += 1
            if not r.left:
                break
        assert partial and resumed, "some query was left mid-way and went on in a later pass"
        assert r.left == 0 and not r.errors
        assert rows(con, "SELECT count(*) FROM md_hub_curve") == [(len(hub.data["curves"]),)]
        assert rows(con, "SELECT count(*) FROM md_hub_bond") == [(len(hub.data["bonds"]),)]
        assert rows(con, "SELECT count(*) FROM md_hub_job WHERE status <> 'done'") == [(0,)]
        assert rows(con, "SELECT max(pages) FROM md_hub_job WHERE dataset = 'curves/ruonia_ois'")[0][0] > 1


def test_corrections_and_dropped_rows(hub, tmp_path, store_url):
    c = client(hub, tmp_path)
    with store.connect(store_url) as con:
        sync(con, c)
        # the hub corrects a past day of a closed month and drops another bond's day
        assert hub.touch("bonds", lambda r: r["secid"] == OFZ and r["trade_date"] == "2025-03-12",
                         legal_close_pct=55.55) == 1
        hub.data["bonds"] = [r for r in hub.data["bonds"]
                             if not (r["secid"] == FLOATER and r["trade_date"] == "2025-03-13")]
        # and the last weeks (the tail)
        hub.touch("fixings", lambda r: r["index_code"] == "RUONIA" and r["fixing_date"] == "2026-10-14",
                  value=16.01)
        con.execute("UPDATE md_hub_job SET checked_at = checked_at - interval '2 days'")
        r = sync(con, c)
        assert "bonds 2025-03" in r.done and "fixings tail" in r.done, r.text()
        assert len([d for d in r.done if d.startswith("bonds 20")]) == 1  # only the corrected month is fetched again
        assert rows(con, "SELECT clean_pct FROM md_hub_bond WHERE secid = %s AND trade_date = '2025-03-12'",
                    OFZ) == [(55.55,)]
        assert rows(con, "SELECT count(*) FROM md_hub_bond WHERE secid = %s AND trade_date = '2025-03-13'",
                    FLOATER) == [(0,)]
        assert rows(con, "SELECT value FROM md_hub_fixing WHERE index_code = 'RUONIA'"
                         " AND fixing_date = '2026-10-14'") == [(Decimal("16.01"),)]


def test_new_positions_are_fetched_first(hub, tmp_path, store_url):
    c = client(hub, tmp_path)
    with store.connect(store_url) as con:
        r = sync(con, c, held=[LINKER, OFZ], budget_s=0.0001)
        assert r.requests <= 2  # the index and at most one page before the budget ran out
        st = hubhistory.Sync(con, c, today=TODAY, held=[LINKER, OFZ])
        plan = st.plan(c.history_index())
        kinds = [k for k, _ in plan]
        assert kinds.index("held") < kinds.index("chunk") and plan[kinds.index("held")][1].secids == (OFZ, LINKER)


@pytest.mark.parametrize(("mode", "reason"), [("no_api", NO_HISTORY_API), ("no_read", NO_READ_SCOPE)])
def test_autopilot_falls_back_to_the_snapshots(hub, tmp_path, store_url, monkeypatch, mode, reason):
    from test_market_history import FakeHub, weekdays_before

    hub.mode = mode
    fake = FakeHub([DATE, *weekdays_before(DATE, 3)])
    real = client(hub, tmp_path)
    fake.history_index = real.history_index  # the history API of the stub, the snapshots of the fake
    fake.history_page = real.history_page
    fake.url = real.url
    monkeypatch.setattr(autopilot, "HubClient", lambda settings: fake)
    s = Settings(home=tmp_path / "home", snapshot_cache=fixture_cache(tmp_path), store_url=store_url,
                 hub_url="https://hub.test")
    ap = autopilot.Autopilot(settings_loader=lambda: s, today=lambda: TODAY)
    with store.connect(store_url) as con:
        status, detail = ap._history(s, con)
        assert status == "ok" and detail.startswith("loaded ") and detail.endswith(f"; history API: {reason}")
        assert len(fake.bundles) == 3  # the bundle path
        assert rows(con, "SELECT code, reason FROM md_hub_state") == [(mode, reason)]
        st = hubhistory.status(con)
        assert st["api"]["available"] is False and st["api"]["reason"] == reason
        n = len(hub.requests)
        ap._history(s, con)
        assert len(hub.requests) == n  # not asked again within UNAVAILABLE_TTL_S
    out = TestClient(create_app(store_url)).get("/api/v1/history/status").json()
    assert out["api"]["reason"] == reason


def test_autopilot_uses_the_api_and_skips_the_bundles(hub, tmp_path, store_url, monkeypatch):
    from test_market_history import FakeHub

    fake = FakeHub([DATE])
    real = client(hub, tmp_path)
    for k in ("history_index", "history_page", "url", "host"):
        setattr(fake, k, getattr(real, k))
    monkeypatch.setattr(autopilot, "HubClient", lambda settings: fake)
    s = Settings(home=tmp_path / "home", snapshot_cache=tmp_path / "cache", store_url=store_url,
                 hub_url="https://hub.test")
    ap = autopilot.Autopilot(settings_loader=lambda: s, today=lambda: TODAY)
    with store.connect(store_url) as con:
        status, detail = ap._history(s, con)
        assert status == "ok" and detail.startswith("history API: ") and "up to date" in detail, detail
        assert fake.bundles == [] and fake.lists == 0


# ---- the readers --------------------------------------------------------------------------------------------------


@pytest.fixture
def desk_on_api(hub, tmp_path, store_url):
    """A default run of 2026-10-15 (OFZ 26238, the floater 29008, the linker 52005) and the API's history."""
    cache = fixture_cache(tmp_path)
    book = tmp_path / "book.csv"
    book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},10\nP2,OFZ,{FLOATER},5\n"
                    f"P3,OFZ,{LINKER},7\n", encoding="utf-8")
    out: list[str] = []
    assert cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=cache), DATE, store_url=store_url,
                       book=str(book), no_pnl=True, out=out.append) == 1, out
    with store.connect(store_url) as con:
        r = sync(con, client(hub, tmp_path), held=hubhistory.held_secids(con))
        assert not r.errors and hubhistory.held_secids(con) == sorted([OFZ, FLOATER, LINKER])
    return TestClient(create_app(store_url))


def test_bond_history_reads_the_api_rows(desk_on_api, hub):
    s = desk_on_api.get(f"/api/v1/bonds/history?instrument_id={OFZ}").json()["series"]
    days = hs.weekdays(date(2024, 1, 3), date(2026, 10, 15))
    assert [p["date"] for p in s] == [d.isoformat() for d in days]  # 2024-01-03 on, every weekday of the hub
    assert {p["origin"] for p in s} == {"hub"}  # the API's row wins over the snapshot's of 2026-10-15
    last = s[-1]
    assert last["face_amount"] == pytest.approx(10_000.0) and last["source"] == "market + desk run"
    assert last["moex_zspread_bp"] is not None and last["curve_rate"] is not None
    assert all(p["curve_rate"] is not None for p in s)  # the KBD of every day from the API
    lk = desk_on_api.get(f"/api/v1/bonds/history?instrument_id={LINKER}").json()["series"]
    assert lk[-1]["icpi"] is not None and lk[-1]["bei"] is not None and lk[-1]["ir"] is not None


def test_positions_history_deep_with_spreads_and_linkers(desk_on_api):
    h = desk_on_api.get("/api/v1/history/positions?run_id=1&from=2024-01-03").json()
    assert h["dates"][0] == "2024-01-03" and h["dates"][-1] == DATE
    ofz, flt, lk = h["series"]["P1"], h["series"]["P2"], h["series"]["P3"]
    assert ofz["zspr"][-1] is not None and "icpi" not in ofz and "dm" not in ofz
    assert flt["ytm"] is None  # no yield for a floater: a series empty on every date is sent as null (R23)
    assert flt["dm"][-1] is not None and flt["dm"][0] is None
    # batch 1, item 6: the DM from the price on every date of the price history, the run's on its date
    px = flt["dmPx"]
    assert len(px) == len(h["dates"]) and px[0] is not None and px[-1] == pytest.approx(flt["dm"][-1], abs=0.01)
    assert sum(v is not None for v in px) == sum(v is not None for v in flt["price"])
    assert all(abs(v - flt["dm"][-1]) < 500 for v in px if v is not None)
    assert "dmPx" not in ofz and "dmPx" not in lk
    assert lk["icpi"][0] is not None and lk["bei"][-1] is not None
    one_year = desk_on_api.get("/api/v1/history/positions?run_id=1").json()
    assert one_year["dates"][0] >= "2025-10-15"


def test_market_endpoint(desk_on_api):
    r = desk_on_api.get("/api/v1/history/market?run_id=1")
    m = r.json()
    assert m["marketDate"] == DATE and m["notes"] == []
    assert m["periods"] == {"1D": "2026-10-14", "1W": "2026-10-08", "1M": "2026-09-15", "3M": "2026-07-15",
                            "YTD": "2025-12-31"}
    ofz = m["bonds"][OFZ]
    assert len(ofz["y"]) == 5 and all(v is not None for v in ofz["y"] + ofz["p"])
    assert set(m["indices"]) == {"КС", "RUONIA", "RUSFAR", "RUSFAR CNY", "CPI г/г"}
    ks = m["indices"]["КС"]
    assert ks["value"] == 16.5 and ks["chgBp"] == [0.0, 0.0, 0.0, -50.0, -50.0]  # 17 % until 2026-07-27
    assert 3 < m["indices"]["CPI г/г"]["value"] < 7 and len(ks["series"]) > 200
    assert set(m["fx"]) == {"USD", "EUR", "CNY"} and m["fx"]["CNY"]["moexTom"] is not None
    cv = m["curves"]
    assert {"RUB RUONIA", "RUB KEY", "OFZ"} <= set(cv)
    ruo = cv["RUB RUONIA"]
    assert ruo["days"]["asOf"] == DATE and len(ruo["pts"]["asOf"]) == 11
    assert ruo["pts"]["asOf"][0] is None and ruo["pts"]["asOf"][2] is not None  # 1M: shorter than the 3M quote
    assert ruo["pts"]["asOf"][-1] is None  # 20Y: beyond 10Y, no extrapolation
    assert len(ruo["series"]["dates"]) > 400 and len(ruo["series"]["5Y"]) == len(ruo["series"]["dates"])
    assert cv["OFZ"]["pts"]["1M"] is not None
    tag = r.headers["etag"]
    assert desk_on_api.get("/api/v1/history/market?run_id=1", headers={"If-None-Match": tag}).status_code == 304


def test_market_endpoint_ruonia_lag_and_mid_only_curves(hub, tmp_path, store_url):
    """As on the live hub: RUONIA of a day is published the next business day, the end-of-day OTC quote set is
    mid-only and another source (andromeda) has bid/ask: the two-sided one is chosen, the lag is said."""
    hub.data["fixings"] = [r for r in hub.data["fixings"]
                           if not (r["index_code"] == "RUONIA" and r["fixing_date"] == DATE)]
    for r in hub.data["curves"]:
        if r["source"] == "otc_monitor":
            r.update(bid_pct=None, ask_pct=None, quote_basis="mid_only", labels=["mid_only"])
        else:
            r.update(source="andromeda", quote_basis="bid_ask_mid", bid_pct=r["mid_pct"] - 0.1,
                     ask_pct=r["mid_pct"] + 0.1, labels=[])
    cache = fixture_cache(tmp_path)
    book = tmp_path / "book.csv"
    book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},10\n", encoding="utf-8")
    assert cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=cache), DATE, store_url=store_url,
                       book=str(book), no_pnl=True, out=[].append) == 1
    with store.connect(store_url) as con:
        assert not sync(con, client(hub, tmp_path)).errors
    m = TestClient(create_app(store_url)).get("/api/v1/history/market?run_id=1").json()
    assert m["indices"]["RUONIA"]["lag"] and m["indices"]["RUONIA"]["date"] == "2026-10-14"
    assert any(n.startswith("RUONIA за 15.10.2026 ещё не опубликована") for n in m["notes"])
    assert m["curves"]["RUB RUONIA"]["sources"]["asOf"] == "andromeda"


def test_market_endpoint_without_the_api(store_url, tmp_path):
    cache = fixture_cache(tmp_path)
    book = tmp_path / "book.csv"
    book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},10\n", encoding="utf-8")
    assert cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=cache), DATE, store_url=store_url,
                       book=str(book), no_pnl=True, out=[].append) == 1
    m = TestClient(create_app(store_url)).get("/api/v1/history/market?run_id=1").json()
    assert m["marketDate"] == DATE and OFZ in m["bonds"] and m["indices"] == {} and m["curves"].keys() == {"OFZ"}
    assert "по снимкам" in m["notes"][0]


def test_cli_history_status_and_sync(hub, tmp_path, store_url, monkeypatch, capsys):
    monkeypatch.setenv("DESK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("DESK_HUB_URL", hs.url(hub))
    monkeypatch.setenv("DESK_HUB_TOKEN", hs.TOKEN)
    monkeypatch.setenv("DESK_STORE_URL", store_url)
    assert cli.main(["history", "sync", "--budget", "120"]) == 0
    out = capsys.readouterr().out
    assert "history API: " in out and "up to date" in out and hs.TOKEN not in out
    assert cli.main(["history", "status"]) == 0
    out = capsys.readouterr().out
    assert "history API: available" in out and "bonds     2024-01-03..2026-10-15" in out
    assert cli.main(["hub-check"]) in (0, 1)
    assert "history  API available" in capsys.readouterr().out
    hub.mode = "no_read"
    hubhistory.forget()
    assert cli.main(["history", "sync"]) == 1
    assert NO_READ_SCOPE in capsys.readouterr().out


def test_history_from_settings(tmp_path):
    env = {"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "none.env")}
    s = load(env)
    assert (s.history_bonds_from, s.history_rates_from) == (date(2024, 1, 3), date(2010, 1, 1))
    (tmp_path / "settings.json").write_text('{"history_bonds_from": "2025-01-01"}', encoding="utf-8")
    assert load(env).history_bonds_from == date(2025, 1, 1)
    assert load({**env, "DESK_HISTORY_RATES_FROM": "2015-01-01"}).history_rates_from == date(2015, 1, 1)
    assert check_settings_doc({"history_bonds_from": "2025-13-01"}) == ["history_bonds_from: must be a date YYYY-MM-DD"]
    assert dict(load(env).describe())["history_bonds_from"] == "2025-01-01"
    assert (date(2024, 1, 3) - timedelta(days=0)) == hubhistory.BONDS_FROM


def test_snapshot_rows_carry_linker_analytics(store_url, tmp_path):
    """Contract 1.2 quotes: MOEX's icpi / ir / bei / cbr into md_bond_eod (the fixture's 1.0 quotes have none)."""
    from test_market_history import market_doc, write

    from desk.marketdata import history
    from desk.marketdata.bundle import read_bundle

    doc = market_doc()
    q = next(q for q in doc["data"]["bond_quotes"] if q["secid"] == LINKER)
    q["source_analytics"].update({"icpi": "1.2345", "ir": "6.1", "bei": "9.3", "cbr": "16.5"})
    folder = tmp_path / "c"
    sid = write(folder, doc)
    with store.connect(store_url) as con:
        assert history.load_snapshot(con, read_bundle(folder / f"{sid}.json.gz")) == "loaded"
        assert rows(con, "SELECT icpi, ir, bei, cbr FROM md_bond_eod WHERE secid = %s", LINKER) == [
            (1.2345, 6.1, 9.3, 16.5)]
        assert rows(con, "SELECT origin, icpi FROM md_bond_day WHERE secid = %s", LINKER) == [("snapshot", 1.2345)]
    assert FIX.is_dir()
