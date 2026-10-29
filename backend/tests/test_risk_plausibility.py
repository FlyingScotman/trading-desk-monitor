"""TDM#170: a position's impossible rate risk (a broken curve shift, a quantity in the wrong unit) is a problem of the
run, never a number in a sum: ``summary.risk_problem`` against the position's size and an absolute bound, the run's
summary (``riskBad``, the history's DV01 by curve), the risk table (missing ``implausible``), the run log. Ordinary
OFZ, floaters, long bonds and swaps are not flagged. Synthetic data only."""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient
from test_market_history import OFZ
from test_r23_concepts import cache_11, desk, extras  # noqa: F401 - fixtures

from desk import cli, store
from desk.api import concepts, create_app
from desk.publish import summary
from desk.publish.summary import risk_problem


@pytest.mark.parametrize(("risk", "dv01", "pv", "notional", "bond"), [
    # an OFZ: 1 bn PV, modified duration 10
    ({"RUB OFZ": {"7Y": -6e5, "10Y": -4e5}}, -1e6, 1e9, 1e9, True),
    # a 30-year zero bought at a deep discount: PV 0.2 of its face, modified duration ~27 on the PV
    ({"RUB RUONIA": {"20Y+": -5.4e5}}, -5.4e5, 2e8, 1e9, True),
    # a key-rate floater: projection and discount legs offsetting, each about notional x tenor
    ({"RUB KEY": {"1Y": 2e4, "5Y": 3e4}, "RUB RUONIA": {"1Y": -2.1e4, "5Y": -3.1e4}}, -2e3, 1e8, 1e8, True),
    # a 10-year swap: PV near zero, its risk on its notional
    ({"RUB RUONIA": {"10Y": 8e5}}, 8e5, 5.0, 1e9, False),
    # a swap from the report: no notional, its MV shown as one (direct_row) -- not a size for its risk
    ({"RUB RUONIA": {"5Y": -850.25}}, -850.25, 125000.5, 125000.5, False),
    # an FX forward valued by core: rate risk on its legs, the row's notional a small net amount
    ({"USD FX": {"1Y": 2181.0}}, 2181.0, 100.0, 34709.0, False),
    # an FX row without rate risk, a row without a size or risk
    ({}, 0.0, 1e7, 1e7, False), (None, None, None, None, True),
])
def test_ordinary_positions_are_plausible(risk, dv01, pv, notional, bond):
    assert risk_problem(risk, dv01, pv, notional, bond) is None


@pytest.mark.parametrize(("risk", "dv01", "pv", "notional", "bond", "word"), [
    ({"RUB RUONIA": {"5Y": 1e11}}, 1e11, 1e8, 1e8, True, "RUB RUONIA"),  # a broken curve: 1e11 RUB/bp on 100 mm
    ({"RUB RUONIA": {"5Y": 1e7}}, 1e7, 1e8, 1e8, True, "50 лет"),  # 1000 years of duration on a bond
    ({"RUB RUONIA": {"5Y": math.inf}}, math.inf, 1e8, 1e8, True, "не конечен"),  # a shift that gave no number
    ({"RUB RUONIA": {"5Y": math.nan}}, None, 1e8, 1e8, False, "не конечен"),
    ({"RUB RUONIA": {"3Y": 5e10, "5Y": -5e10}}, 0.0, 1e8, 1e8, True, "RUB RUONIA 3Y"),  # buckets cancelling in the sum
    ({"RUB RUONIA": {"5Y": 2e9}}, 2e9, None, None, True, "1,000,000,000"),  # no size: the absolute bound
    ({"RUB RUONIA": {"5Y": 1e11}}, 1e11, 5.0, 1e9, False, "1,000,000,000"),  # a swap: the absolute bound
    ({"RUB RUONIA": {"5Y": -1e11}}, -1e11, 1e14, 1e14, True, "1,000,000,000"),  # quantity x1e6: PV scaled with it
])
def test_impossible_risk_is_a_problem(risk, dv01, pv, notional, bond, word):
    why = risk_problem(risk, dv01, pv, notional, bond)
    assert why and word in why


def _row(pid: str, risk: dict, pv: float, dv01: float, book: str = "B") -> dict:
    return {"position_id": pid, "book_id": book, "pv_rub": pv, "risk": risk, "cs01": -50.0, "ofz": 0.0,
            "instr_type": "BOND", "segment": "Корп", "asset_class": "Корпоративные", "coupon_type": "fix",
            "ccy": "RUB", "notional": pv, "notional_rub": pv, "price": 100.0, "spread_bp": 100.0, "dv01": dv01,
            "dm_bp": None, "pnl": None, "ofz_basis": None, "quality": "exact"}


def test_the_summary_keeps_an_impossible_risk_out_of_the_sums():
    s = summary.build([_row("OK", {"RUB RUONIA": {"3Y": -3000.0}}, 1e7, -3000.0),
                       _row("BAD", {"RUB RUONIA": {"5Y": 1e11}}, 1e8, 1e11)])
    assert s["dv01"] == pytest.approx({"RUB RUONIA": -3000.0}) and s["dv01Book"] == pytest.approx({"B": -3000.0})
    assert s["cs01"] == pytest.approx(-50.0)  # its CS01 neither: the whole risk of the row is in doubt
    assert list(s["riskBad"]) == ["BAD"] and "невозможен" in s["riskBad"]["BAD"]
    assert s["pos"]["BAD"][1] == 0 and s["pos"]["BAD"][8] == {} and s["pos"]["BAD"][4] == pytest.approx(1e8)
    assert summary.build([_row("OK", {"RUB RUONIA": {"3Y": -3000.0}}, 1e7, -3000.0)])["riskBad"] == {}


def test_the_risk_table_counts_an_impossible_risk_as_missing():
    def item(pid, risk, mv):
        return {"position_id": pid, "fact": {"instr_type": "BOND", "segment": "Корп", "asset_class": "Корпоративные",
                                             "coupon_type": "fix", "ccy": "RUB", "notional": mv, "notional_rub": mv,
                                             "mv_rub": mv, "cs01": -50.0, "ofz_spr01": 0.0, "risk": risk}}

    t = concepts.risk_table([item("OK", {"RUB RUONIA": {"3Y": -3000.0}}, 1e7),
                             item("BAD", {"RUB RUONIA": {"5Y": 1e11}}, 1e8)], {})
    assert t["total"]["dv01_by_curve_rub"] == pytest.approx({"RUB RUONIA": -3000.0})
    assert t["total"]["cs01_rub"] == pytest.approx(-50.0) and t["total"]["mv_rub"] == pytest.approx(1.1e8)
    assert t["total"]["n_risk_missing"] == 1 and t["total"]["n_positions"] == 2
    cell = next(r for r in t["rows"] if r["level"] == "kind")
    assert cell["risk_missing"] == [{"reason": "implausible", "text": concepts.MISSING_TEXT["implausible"], "n": 1}]


def test_history_of_dv01_by_curve_leaves_the_position_out_and_says_so(desk, store_url):  # noqa: F811
    with store.connect(store_url) as con:
        rid = con.execute("SELECT min(run_id) FROM run").fetchone()[0]
        log = [m for (m,) in con.execute("SELECT message FROM run_log")]
    assert not any("невозможен" in m for m in log)  # the fixture's ordinary book: no false alarm
    before = extras(desk)
    assert before["hist"]["riskBad"] == [[], []] and not any("невозможный риск" in n for n in before["notes"])
    with store.connect(store_url) as con:  # the first date's floater as a broken curve shift would leave it
        con.execute("ALTER TABLE run_position DISABLE TRIGGER USER")  # a published run is frozen
        con.execute("UPDATE run_position SET fact = jsonb_set(fact, '{risk}', '{\"RUB RUONIA\": {\"5Y\": 1e11}}')"
                    " WHERE run_id = %s AND position_id = 'P2'", (rid,))
        con.execute("DELETE FROM run_summary")
    after = extras(TestClient(create_app(store_url)))
    assert after["hist"]["riskBad"] == [["P2"], []]
    ruo = after["hist"]["dv01"]["RUB RUONIA"]
    assert abs(ruo[0]) < 1e7 and ruo[1] == pytest.approx(before["hist"]["dv01"]["RUB RUONIA"][1])
    note = next(n for n in after["notes"] if "невозможный риск" in n)
    assert "P2" in note and "1 дат" in note
    t = desk.get(f"/api/v1/runs/{rid}/risk-table").json()
    assert any(m["reason"] == "implausible" for m in t["rows"][0]["risk_missing"])


def test_the_run_log_names_the_position(store_url, tmp_path, monkeypatch):
    from desk.publish import tdm_run

    real = tdm_run.risk_problem
    monkeypatch.setattr(tdm_run, "risk_problem", lambda risk, dv01, pv, n, bond: "риск RUB RUONIA 1 ₽/бп невозможен: тест"
                        if dv01 and abs(dv01) > 0 and n and abs(n) < 1e5 else real(risk, dv01, pv, n, bond))
    from desk.settings import Settings

    cache, *_ = cache_11(tmp_path)
    book = tmp_path / "book.csv"
    book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},1\n", encoding="utf-8")
    lines: list[str] = []
    assert cli.eod_run(Settings(home=tmp_path / "home", snapshot_cache=cache), "2026-10-15", store_url=store_url,
                       book=str(book), out=lines.append), lines
    with store.connect(store_url) as con:
        log = [m for (m,) in con.execute("SELECT message FROM run_log WHERE level = 'WARN'")]
        fact = con.execute("SELECT fact FROM run_position WHERE position_id = 'P1'").fetchone()[0]
    assert any(m.startswith("P1 ") and "невозможен" in m and "не в суммах" in m for m in log), log
    assert "невозможен" in fact["extra"]["risk_problem"]
