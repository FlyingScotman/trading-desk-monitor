"""The concept pages' data (prototypes/concepts/, lib/real.js): a bond's offer, flows and quote in its fact
(VALUATION_VERSION 6), ``/concepts-data.js`` (the desk's series over the default runs, per position PnL to date and
changes, flows, the market of the latest close) and ``/api/v1/history/positions``. Two default runs (2026-10-14 on
the shifted fixture market, 2026-10-15 on the fixture's) of a book with an OFZ, an OFZ-PK and a USD bond, and 30
earlier dates of market history (test_market_history's shifted bundles)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_market_history import DATE, FLOATER, OFZ, USD_FACE, fixture_cache, shifted, weekdays_before, write

from desk import cli, store
from desk.api import concepts, create_app
from desk.marketdata import history
from desk.settings import Settings

PREV = "2026-10-14"


@pytest.fixture
def desk(store_url, tmp_path):
    cache = fixture_cache(tmp_path)
    write(cache, shifted(PREV, 1, light=False))
    hist = tmp_path / "hub"
    for i, d in enumerate(weekdays_before(PREV, 30)):
        write(hist, shifted(d, i + 2, known_at=f"{d}T17:00:00Z"))
    settings = Settings(home=tmp_path / "home", snapshot_cache=cache)
    for d, n in ((PREV, 8), (DATE, 10)):  # a trade in the OFZ between the two dates
        book = tmp_path / f"book{d}.csv"
        book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},{n}\nP2,OFZ,{FLOATER},5\n"
                        f"P3,CORP,{USD_FACE},3\n", encoding="utf-8")
        lines: list[str] = []
        assert cli.eod_run(settings, d, store_url=store_url, book=str(book), out=lines.append), lines
    with store.connect(store_url) as con:
        history.load_cache(con, hist)
    return TestClient(create_app(store_url))


def extras(client, q: str = "") -> dict:
    js = client.get("/concepts-data.js" + q)
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    body = js.text
    assert body.startswith("window.DESK_EXTRAS = ") and body.endswith(";\n")
    return json.loads(body[len("window.DESK_EXTRAS = "):-2])


def test_fact_has_offer_flows_and_quote(desk, store_url):
    with store.connect(store_url) as con:
        facts = {r[0]: r[1] for r in con.execute(
            "SELECT position_id, fact FROM run_position p JOIN run r USING (run_id) WHERE r.business_date = %s",
            (date.fromisoformat(DATE),))}
    ofz = facts["P1"]["extra"]
    flows = ofz["cashflows"]
    assert flows and all(DATE < f[0] for f in flows) and {f[1] for f in flows} <= {"cpn", "amort", "put", "call", "mat"}
    assert all(f[2] > 0 for f in flows)  # a long position receives
    assert ofz["quote"]["gspread_bp"] == -24 and ofz["quote"]["value"] > 0
    assert "offer_date" not in ofz  # priced to maturity
    flt = facts["P2"]["extra"]["cashflows"]
    assert flt and any(f[3] for f in flt)  # a floater's future coupons are forecasts


def test_concepts_data_js(desk):
    x = extras(desk)
    assert x["histDates"] == [PREV, DATE] and x["asOf"] == DATE
    h = x["hist"]
    assert "RUB RUONIA" in h["dv01"] and "USD" not in h["dv01"] and "USD SOFR" in h["dv01"]
    assert all(len(v) == 2 for v in h["dv01"].values()) and len(h["cs01"]) == 2 and len(h["basis01"]) == 2
    assert set(h["dv01Book"]) == {"OFZ", "CORP"} and {"GOV.PD", "GOV.PK"} <= set(h["mvType"])
    assert h["pnl"]["total"][0] is None and h["pnl"]["total"][1] is not None  # no PnL on the first date
    p1 = x["positions"]["P1"]
    assert p1["trade"]["qty"] == pytest.approx(2 * 1000, rel=1e-6) and p1["prevPrice"] is not None
    assert p1["pnlMtd"] == pytest.approx(p1["pnlYtd"])
    assert x["cashflows"] and {c["pos"] for c in x["cashflows"]} >= {"P1", "P2"}
    assert x["marketDate"] == DATE and x["universeRub"] and x["marketMovers"]
    assert any(b["held"] for b in x["universeRub"]) and all(b["ytm"] is None for b in x["universeRub"] if b["flt"])
    assert any(b["ccy"] == "USD" for b in x["universeFx"])
    assert x["cbr"]["next"]["date"] == "2026-10-23" and x["cbr"]["next"]["estimated"]  # the fixture's cbr_meetings
    assert x["cbr"]["meetings"][0] == "2026-10-23" and x["cbr"]["past"][0]["date"] == "2026-09-11"
    assert x["cbr"]["keyRate"] == 14.0 and x["curveMap"]["USD"] == "USD SOFR"
    assert x["histSource"] == "runs" and not x["pnlPeriods"]["ytd"]["complete"]  # two runs of the year
    assert extras(desk, f"?as_of={PREV}")["histDates"] == [PREV]


def test_trades_only_against_the_previous_close(desk, store_url):
    """E2E 21.10.2026: with the history of runs still being backfilled the run before the latest was months older, and
    every position showed as a trade of the day. Trades and changes are against the run of the previous close only."""
    from datetime import timedelta

    from psycopg.rows import dict_row

    from desk.api import compat
    with store.connect(store_url) as con:
        con.row_factory = dict_row
        run = compat.resolve_run(con, date.fromisoformat(DATE))
        assert run["prev_date"].isoformat() == PREV
        x = concepts.extras(con, run)
        assert x["positions"]["P1"]["trade"] and not any("нет прогона за" in n for n in x["notes"])
        gap = concepts.extras(con, {**run, "prev_date": run["prev_date"] - timedelta(days=1)})  # no run of that close
        # a face that moves by itself (a linker's indexation, an amortization) with the same number of bonds: no trade
        con.execute("UPDATE run_summary SET data = jsonb_set(data, '{pos,P2,0}', to_jsonb((data->'pos'->'P2'->>0)::float8"
                    " * 1.01)) WHERE run_id = %s", (run["run_id"],))
        indexed = concepts.extras(con, run)
    assert "trade" not in indexed["positions"]["P2"]
    assert indexed["positions"]["P1"]["trade"]["qty"] == pytest.approx(x["positions"]["P1"]["trade"]["qty"])
    assert not any("trade" in p or "prevPrice" in p for p in gap["positions"].values())
    assert any("нет прогона за 2026-10-13" in n for n in gap["notes"])
    assert gap["positions"]["P1"]["pnlMtd"] == pytest.approx(x["positions"]["P1"]["pnlMtd"])  # periods unchanged


def test_concepts_data_js_etag_and_empty(desk, store_url):
    r = desk.get("/concepts-data.js")
    again = desk.get("/concepts-data.js", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and r.headers["cache-control"] == "no-cache"
    assert "DESK_EXTRAS" not in desk.get("/concepts-data.js?run_id=999").text.split("*/")[-1]
    empty = TestClient(create_app(store_url)).get("/concepts-data.js?demo=1").text
    assert empty.startswith("/*") and "window.DESK_EXTRAS =" not in empty


def test_positions_history(desk):
    h = desk.get("/api/v1/history/positions").json()
    d = h["dates"]
    assert d[-1] == DATE and len(d) == 32 and d == sorted(d)
    s = h["series"]["P1"]
    assert all(len(v) == len(d) for k, v in s.items() if v is not None and k != "curves")
    assert s["notional"][-1] == pytest.approx(10 * 1000) and s["notional"][-2] == pytest.approx(8 * 1000)
    assert s["notional"][0] is None and s["price"][0] is not None and s["bench"][-1] is not None
    assert s["ytm"][-1] is not None and h["series"]["P2"]["ytm"] is None  # a floater: no yield (a null series)
    assert s["dv01"][-1] is not None and s["mv"][-1] > 0 and s["cs01"] is None  # an OFZ: no credit spread risk
    assert set(s["curves"]) == {"RUB RUONIA"} and s["curves"]["RUB RUONIA"][-1] == pytest.approx(s["dv01"][-1])
    one = desk.get("/api/v1/history/positions?ids=P2").json()
    assert set(one["series"]) == {"P2"}
    assert desk.get("/api/v1/history/positions?run_id=999").status_code == 404


@pytest.mark.parametrize(("fact", "want"), [
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПК", "coupon_type": "float"}, ("GOV", "PK")),
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПД", "ccy": "USD"}, ("GOV", "SOVFX")),
    ({"instr_type": "BOND", "segment": "Суб", "coupon_type": "float"}, ("SUB", "FLT")),
    ({"instr_type": "BOND", "segment": "Корп", "coupon_type": "zero"}, ("CORP", "FIX")),
    ({"instr_type": "OTC"}, ("DER", "OTC")), ({"instr_type": "FX"}, ("DER", "FX")), ({"instr_type": "OTHER"}, ("OTH", "OTH")),
])
def test_kind_of(fact, want):
    assert concepts.kind_of(fact) == want
