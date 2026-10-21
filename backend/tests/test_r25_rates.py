"""R25: the hub's rates of contract 1.3 / hub 0.3.0 (PR «rates-fx-cpi»): SOFR and €STR fixings (one series per day by
the hub's source order), the CBR's CPI y/y, the cross rates (EUR/USD, USD/CNY) of the new ``/v1/history/fx`` kinds,
fetched only when the history index lists them, the CBR cross when it does not, and the government par-yield proxies
(UST par, ECB AAA) labelled «не OIS», shown and never used for risk unless ``proxy_curves_for_risk`` is on. A linker's
CPI y/y beside its ICPI. Stub hub: ``hub_history_stub`` with the 1.3 rows added here (synthetic values)."""

from __future__ import annotations

import copy
import dataclasses
from datetime import date

import hub_history_stub as hs
import pytest
from fastapi.testclient import TestClient
from test_hub_history import LINKER, TODAY, client, desk_on_api, hub, server, sync  # noqa: F401 - fixtures
from test_market_history import DATE, FIX, OFZ, fixture_cache

from desk import cli, store
from desk.api import create_app
from desk.curves import proxy
from desk.marketdata import hubhistory
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.publish import tdm_run
from desk.settings import Settings, load

W = hs.WRITTEN


def fixing(code, src, d, v, unit="pct"):
    return {"index_code": code, "source": src, "fixing_date": d.isoformat(), "value": v, "unit": unit,
            "effective_date": None, "published_on": None, "published_at": None,
            "hub_ingested_at": f"{d.isoformat()}T16:00:00+00:00", "origin": "md.fixing", "labels": [],
            "details": None, "_w": W}


def fx(kind, pair, d, v, src):
    b, q = pair.split("/")
    return {"kind": kind, "pair": pair, "base_ccy": b, "quote_ccy": q, "date": d.isoformat(), "rate": v,
            "secid": None, "board": None, "tenor": None, "close": None, "waprice": None, "quote_units": None,
            "close_per_unit": None, "waprice_per_unit": None, "num_trades": None, "nominal": None, "set_date": None,
            "trade_time": None, "_source": src, "_w": W}


def with_13(data):
    """The stub's rows plus the 1.3 rates of September-October 2026."""
    days = hs.weekdays(date(2026, 9, 1), hs.END)
    for i, d in enumerate(days):
        data["fixings"] += [fixing("ESTR", "ecb_data_portal", d, 1.93), fixing("ESTR", "fred", d, 1.90),
                            fixing("SOFR", "nyfed", d, round(4.30 - i * 0.002, 4)), fixing("SOFR", "fred", d, 9.99)]
        data["fx"] += [fx("ecb", "EUR/USD", d, round(1.08 + i * 0.001, 4), "ecb"),
                       fx("cbr_cross", "USD/CNY", d, 7.15, "cbr")]
        for pos, (ten, months) in enumerate((("1M", 1), ("1Y", 12), ("2Y", 24), ("5Y", 60), ("10Y", 120), ("30Y", 360))):
            data["curves"].append({"_ds": "usd_ust_par", "snapshot_date": d.isoformat(), "source": "us_treasury",
                                   "quote_set_id": 900000 + i, "fetched_at": f"{d.isoformat()}T20:59:59+00:00",
                                   "quote_basis": "mid_only", "labels": ["mid_only", "proxy_ust_par"], "tenor": ten,
                                   "position": pos, "tenor_months": months, "tenor_days": None, "bid_pct": None,
                                   "mid_pct": round(4.0 + months / 360, 4), "ask_pct": None, "_w": W})
    m = date(2025, 8, 1)
    while m <= date(2026, 9, 1):
        data["fixings"].append(fixing("CPI_RU_YOY", "cbr", m, 7.5 if m < date(2026, 9, 1) else 6.8))
        m = (m.replace(day=28) + __import__("datetime").timedelta(days=4)).replace(day=1)
    return data


def test_cross_kinds_only_when_the_index_lists_them():
    base = [{"dataset": "fx", "series": "USD/RUB", "unit": "cbr_official", "date_from": "2010-01-01"},
            {"dataset": "fixings", "series": "KEY_RATE", "unit": "pct", "date_from": "2013-09-17"}]
    assert "fx_cross" not in {s.name for s in hubhistory.specs(base)}
    new = base + [{"dataset": "fx", "series": "EUR/USD", "unit": "ecb", "date_from": "1999-01-04"},
                  {"dataset": "fx", "series": "USD/CNY", "unit": "fred_h10", "date_from": "2015-01-02"},
                  {"dataset": "curves", "series": "usd_ust_par", "unit": "pct", "date_from": "2024-01-02"}]
    sp = {s.name: s for s in hubhistory.specs(new)}
    assert dict(sp["fx_cross"].params)["kind"] == "ecb,fred_h10" and sp["fx_cross"].start == date(2010, 1, 1)
    assert dict(sp["fx"].params)["kind"] == "cbr_official,moex_eod"  # the RUB rates as before
    assert "curves/usd_ust_par" in sp  # the proxies come with the index like any curve


@pytest.fixture
def hub13(hub):  # noqa: F811 - the fixture
    hub.reset(with_13(hs.synthetic()))
    return hub


def _run(tmp_path, store_url):
    cache = fixture_cache(tmp_path)
    book = tmp_path / "book.csv"
    book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},10\nP3,OFZ,{LINKER},7\n", encoding="utf-8")
    assert cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=cache), DATE, store_url=store_url,
                       book=str(book), no_pnl=True, out=[].append) == 1


def test_market_endpoint_with_the_13_rates(hub13, tmp_path, store_url):
    _run(tmp_path, store_url)
    with store.connect(store_url) as con:
        rep = sync(con, client(hub13, tmp_path))
        assert not rep.errors and "fx_cross tail" in rep.done
        assert con.execute("SELECT count(*) FROM md_hub_fx WHERE kind = 'ecb'").fetchone()[0] > 30
    app = TestClient(create_app(store_url))
    m = app.get("/api/v1/history/market?run_id=1").json()
    ind = m["indices"]
    assert ind["€STR"]["value"] == 1.93  # ecb_data_portal before fred, per day (the hub's order)
    assert ind["SOFR"]["value"] == pytest.approx(4.30 - (len(hs.weekdays(date(2026, 9, 1), hs.END)) - 1) * 0.002)
    assert ind["SOFR"]["chgBp"][0] == pytest.approx(-0.2, abs=1e-6)  # nyfed, not fred's 9.99
    assert ind["CPI г/г"]["value"] == 6.8 and ind["CPI г/г"]["source"].startswith("ЦБ")
    cr = m["crosses"]
    assert cr["EUR/USD"]["kind"] == "ecb" and cr["EUR/USD"]["label"] == "ЕЦБ" and cr["EUR/USD"]["value"] > 1.08
    assert cr["USD/CNY"]["kind"] == "cbr_cross" and cr["USD/CNY"]["value"] == 7.15
    ust = m["curves"]["usd_ust_par"]
    assert ust["proxy"] == "прокси (UST par), не OIS" and ust["ccy"] == "USD" and ust["pts"]["asOf"][3] is not None
    h = app.get("/api/v1/history/positions?run_id=1&from=2025-09-01").json()
    lk = h["series"]["P3"]
    assert "cpi" not in h["series"]["P1"]  # CPI y/y beside ICPI, linkers only
    assert lk["cpi"][-1] == 7.5  # September's 6.8 is known from 16.10 (45 days after the month), not on 15.10
    assert lk["cpi"][h["dates"].index("2025-09-16")] == 7.5 and lk["cpi"][0] is None  # August 2025 from 15.09


def test_without_the_13_rates(hub, tmp_path, store_url):  # noqa: F811 - the fixture
    _run(tmp_path, store_url)
    with store.connect(store_url) as con:
        assert not any(j.startswith("fx_cross") for j in sync(con, client(hub, tmp_path)).done)
    m = TestClient(create_app(store_url)).get("/api/v1/history/market?run_id=1").json()
    assert "SOFR" not in m["indices"] and "€STR" not in m["indices"]
    assert m["indices"]["CPI г/г"]["source"].startswith("расчёт по ИПЦ м/м")
    eu = m["crosses"]["EUR/USD"]
    assert eu["kind"] == "cbr_derived" and eu["value"] == pytest.approx(m["fx"]["EUR"]["cbr"] / m["fx"]["USD"]["cbr"],
                                                                        rel=1e-4)
    assert not any(c.get("proxy") for c in m["curves"].values())


def test_proxy_curves_only_with_the_setting(tmp_path):
    ctx = prepare(SnapshotDir(fixture_cache(tmp_path)), DATE)
    m = copy.copy(ctx.market)
    m.curve_quote_sets = list(ctx.market.curve_quote_sets) + [
        {"dataset": "usd_ust_par", "source": "us_treasury", "labels": ["proxy_ust_par"],
         "quotes": [{"tenor": "1Y", "tenor_months": 12, "tenor_days": None, "mid_pct": "4.10"},
                    {"tenor": "10Y", "tenor_months": 120, "tenor_days": None, "mid_pct": "4.40"}]}]
    rows = [{"risk": {"USD": {"5Y": -100.0}}}]
    off = tdm_run.curves_reference(dataclasses.replace(ctx, market=m), None, rows, [{"ccy": "USD"}])
    usd = next(c for c in off if c["curve_id"] == "USD")
    assert usd["points"] == [] and "кривой нет" in usd["name"]  # the default: the proxy is never the USD risk curve
    on = tdm_run.curves_reference(dataclasses.replace(ctx, market=m, proxy_curves=True), None, rows, [{"ccy": "USD"}])
    usd = next(c for c in on if c["curve_id"] == "USD")
    assert "прокси (UST par), не OIS" in usd["name"] and len(usd["points"]) == 11
    assert usd["points"][0]["rate"] == 4.1 and usd["points"][-1]["rate"] == 4.4  # flat beyond the quotes
    assert usd["points"][5]["rate"] == pytest.approx(4.1 + 0.3 * 24 / 108, abs=1e-4)  # 3Y, linear


def test_proxy_helpers_and_setting(tmp_path):
    assert proxy.is_proxy("eur_aaa_par") and proxy.is_proxy("x", ["proxy_bund_par"]) and not proxy.is_proxy("ruonia_ois")
    assert proxy.label("eur_bund_par") == "прокси (Bund par), не OIS"
    assert proxy.points([{"dataset": "eur_bund_par", "quotes": [{"tenor": "2Y", "mid_pct": "2.5"}]}], "EUR") == \
        ("eur_bund_par", [(2.0, 2.5)])
    assert not load({"DESK_HOME": str(tmp_path)}).proxy_curves_for_risk
    assert load({"DESK_HOME": str(tmp_path), "DESK_PROXY_CURVES_FOR_RISK": "1"}).proxy_curves_for_risk


def test_run_publishes_the_13_fixings(tmp_path):
    import gzip
    import json

    from test_market_history import MARKET, write
    doc = json.loads(gzip.decompress((FIX / f"{MARKET}.json.gz").read_bytes()))
    doc["schema_version"] = "market_snapshot/1.3"
    doc["data"]["fixings"] += [
        {**fixing("SOFR", "nyfed", date(2026, 10, 14), "4.28"), "value": "4.28"},
        {**fixing("ESTR", "ecb_data_portal", date(2026, 10, 14), "1.93"), "value": "1.93"},
        {**fixing("CPI_RU_YOY", "cbr", date(2026, 9, 1), "6.80"), "value": "6.80"}]
    for f in doc["data"]["fixings"]:
        f.pop("_w", None)
    cache = fixture_cache(tmp_path)
    (cache / f"{MARKET}.json.gz").unlink()
    write(cache, doc)
    ctx = prepare(SnapshotDir(cache), DATE)
    c = tdm_run.run_content(ctx, [], None, {})
    got = {r["index_name"]: r["value"] for r in c.reference["indices"]}
    assert got["SOFR"] == 4.28 and got["€STR"] == 1.93 and got["CPI г/г"] == 6.8 and "КС" in got
    assert TODAY  # the stub hub's date (imported for the fixtures)


def test_floater_with_a_cap_and_no_floor(tmp_path):
    """Four floaters of the hub's static of 21.10.2026 have a cap without a floor: the description crashed
    (``floor {None:.2%}``) and the bonds failed the static checks (``error_TypeError``)."""
    from desk.valuation.bonds import floater_rule

    t = prepare(SnapshotDir(fixture_cache(tmp_path)), DATE).static.bonds["RU000A109XR1"]
    t = dataclasses.replace(t, floater_formula={**t.floater_formula, "floor_pct": None, "cap_pct": "25"})
    rule, fixing, _, desc = floater_rule(t)
    assert fixing == "KEY_RATE" and desc.endswith("floor — cap 25.00%") and rule.bounds is not None
