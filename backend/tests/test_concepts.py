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
    assert all(len(v) == len(d) for k, v in s.items() if v is not None and k not in ("curves", "pt"))
    assert s["notional"][-1] == pytest.approx(10 * 1000) and s["notional"][-2] == pytest.approx(8 * 1000)
    assert s["notional"][0] is None and s["price"][0] is not None and s["bench"][-1] is not None
    assert s["ytm"][-1] is not None and h["series"]["P2"]["ytm"] is None  # a floater: no yield (a null series)
    assert s["dv01"][-1] is not None and s["mv"][-1] > 0 and s["cs01"] is None  # an OFZ: no credit spread risk
    assert set(s["curves"]) == {"RUB RUONIA"} and s["curves"]["RUB RUONIA"][-1] == pytest.approx(s["dv01"][-1])
    one = desk.get("/api/v1/history/positions?ids=P2").json()
    assert set(one["series"]) == {"P2"}
    assert desk.get("/api/v1/history/positions?run_id=999").status_code == 404


def test_positions_history_points(desk, store_url):
    """R44: a point of a position's history says which run it stands on and how that run valued it, so that a chart
    shows a date of another program, another method, no run or no valuation apart instead of as a move of risk; the
    number of bonds separates a trade from a change of risk per bond. The fixture is the owner's case in small: the
    market of 2026-10-14 has no RUONIA OIS curve, core cannot value the floater, the fallback ladder's duration proxy
    (time to the next coupon x PV) gives it another DV01 than core's on 2026-10-15 — a step that is not a risk move."""
    h = desk.get("/api/v1/history/positions?ids=P2").json()
    d, ra, s = h["dates"], h["runAt"], h["series"]["P2"]
    i0, i1 = str(len(d) - 2), str(len(d) - 1)
    assert len(ra) == len(d) and ra[-1]["id"] == h["runId"] and ra[-2] is not None and ra[0] is None  # market-only dates
    assert "old" not in ra[-1] and "cut" not in ra[-1]  # the current program, the final close
    assert s["pt"][i0][0] == "method" and "рыночной стоимости" in s["pt"][i0][1] and "RUB_RUONIA_OIS" in s["pt"][i0][1]
    assert s["pt"][i1] == ["minor", "оценка ядра с допущением"]  # core's numbers (the floater's formula inferred)
    assert s["dv01"][-2] != pytest.approx(s["dv01"][-1], rel=0.1)  # the two methods differ by more than the day's move
    assert s["qty"][-1] == pytest.approx(5) and s["qty"][-2] == pytest.approx(5) and s["qty"][0] is None
    with store.connect(store_url) as con:  # the earlier date by an older program; a trade of P1 today
        con.execute("ALTER TABLE run DISABLE TRIGGER USER")  # a published run is frozen
        con.execute("ALTER TABLE run_position DISABLE TRIGGER USER")
        prev, cur = (r[0] for r in con.execute("SELECT run_id FROM run ORDER BY business_date"))
        con.execute("UPDATE run SET lineage = jsonb_set(lineage, '{logic_version}', '\"0.1.9+v8+core0.32.0\"')"
                    " WHERE run_id = %s", (prev,))
        con.execute("UPDATE run_position SET quantity = 7 WHERE run_id = %s AND position_id = 'P1'", (cur,))
    h = TestClient(create_app(store_url)).get("/api/v1/history/positions").json()
    ra, p1, p3 = h["runAt"], h["series"]["P1"], h["series"]["P3"]
    assert ra[-2]["old"] == "0.1.9 (оценка v8, ядро 0.32.0)" and "old" not in ra[-1]
    assert p1["qty"][-2:] == [pytest.approx(8), pytest.approx(7)] and i1 not in p1["pt"]
    assert p3["pt"][i0][0] == "unvalued" and p3["dv01"][-2] is None  # held, not valued: no number, not a zero
    flag = concepts.point_flag
    assert flag((5, "exact", None, "OFZ"), "2026-10-15", date(2026, 10, 15), True) is None
    assert flag((5, "exact", None, "OFZ"), "2026-10-14", date(2026, 10, 15), True) == [
        "minor", "позиции книги OFZ — из отчёта за 2026-10-14 (за дату отчёта нет)"]
    assert flag((5, "par_proxy", "core_x", "OFZ"), None, date(2026, 10, 15), True)[0] == "method"


def test_positions_history_marks_another_curve_risk_unit(desk, store_url):
    """#171: a run made with the other setting ``quote_curve_risk`` than the shown run's has its foreign curves' risk
    in another unit (quotes, zero rates): its points say so (``unit``), by the run's own lineage flag; the program is
    the same (no ``old``)."""
    with store.connect(store_url) as con:
        con.execute("ALTER TABLE run DISABLE TRIGGER USER")  # a published run is frozen
        prev, cur = (r[0] for r in con.execute("SELECT run_id FROM run ORDER BY business_date"))
        assert con.execute("SELECT lineage->'quote_curve_risk' FROM run WHERE run_id = %s", (cur,)).fetchone()[0] \
            is False  # the default: off, recorded
        con.execute("UPDATE run SET lineage = jsonb_set(lineage, '{quote_curve_risk}', 'true') WHERE run_id = %s",
                    (prev,))
    ra = TestClient(create_app(store_url)).get("/api/v1/history/positions").json()["runAt"]
    assert ra[-2]["unit"] == concepts.UNIT_TEXT[True] and "old" not in ra[-2] and "unit" not in ra[-1]
    with store.connect(store_url) as con:  # the shown run in quotes: the earlier one is in zero rates
        con.execute("UPDATE run SET lineage = jsonb_set(lineage, '{quote_curve_risk}', 'true') WHERE run_id = %s",
                    (cur,))
        con.execute("UPDATE run SET lineage = lineage - 'quote_curve_risk' WHERE run_id = %s", (prev,))
    ra = TestClient(create_app(store_url)).get("/api/v1/history/positions").json()["runAt"]
    assert ra[-2]["unit"] == concepts.UNIT_TEXT[False] and "unit" not in ra[-1]


def test_unknown_days_and_old_runs_note(desk, store_url):
    """R44/R45: the year to date is not partial for want of history before the year's first close (2026: 5 January,
    after the New Year holidays); a history computed partly by an older program says so and where to recompute it."""
    assert concepts.unknown_days(date(2026, 1, 1), date(2026, 1, 5)) == []
    assert concepts.unknown_days(date(2026, 1, 1), date(2026, 1, 12)) == [date(2026, 1, 9)]
    with store.connect(store_url) as con:
        con.execute("ALTER TABLE run DISABLE TRIGGER USER")  # a published run is frozen
        con.execute("UPDATE run SET lineage = jsonb_set(lineage, '{logic_version}', '\"0.1.9+v8+core0.32.0\"')"
                    " WHERE business_date = %s", (date.fromisoformat(PREV),))
    x = extras(TestClient(create_app(store_url)))
    note = next(n for n in x["notes"] if n.startswith("история: 1 из 2 дат"))
    assert concepts.BACKFILL_LINK in note


@pytest.mark.parametrize(("fact", "want"), [
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПК", "coupon_type": "float"}, ("GOV", "PK")),
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПД", "ccy": "USD"}, ("GOV", "SOVFX")),
    ({"instr_type": "BOND", "segment": "Суб", "coupon_type": "float"}, ("SUB", "FLT")),
    ({"instr_type": "BOND", "segment": "Корп", "coupon_type": "zero"}, ("CORP", "FIX")),
    ({"instr_type": "OTC"}, ("DER", "OTC")), ({"instr_type": "FX"}, ("DER", "FX")), ({"instr_type": "OTHER"}, ("OTH", "OTH")),
])
def test_kind_of(fact, want):
    assert concepts.kind_of(fact) == want
