"""TDM#577, D12: the scenario screen's FX shock from the published OCP. Synthetic runs only: the linear part is the
OCP lines without the non-linear derivatives, an option (or an FX gamma) is apart, no OCP is said."""

from __future__ import annotations

import pytest

from desk.scenario.fx_ocp import NO_OCP, fx_ocp, nonlinear_positions
from desk.scenario.taylor import risk_payload

DAY = {"run_id": 1, "business_date": "2026-11-03"}


def row(pid, book, fx_delta, gamma=None, product=None, rate=90.0, group=None):
    ex = {"fx_rate": rate}
    if product:
        ex["core"] = {"product": product}
    if group:
        ex["reported"] = {"x_product_group": group}
    fact = {"fx_delta": fx_delta, "risk": {}, "extra": ex, "gamma": gamma or []}
    return {"position_id": pid, "book_id": book, "secid": None, "quality": "core", "pv_rub": 0.0, "fact": fact}


def status(lines, rate=90.0):
    return {"asOf": "2026-11-03", "basis": "report_base_plus_day", "methodNote": None,
            "byCcy": {c: {"rate": rate} for c in {x["ccy"] for x in lines}},
            "byBook": [{"entity": "E", "books": [], **x} for x in lines]}


def build(rows):
    return risk_payload(DAY, rows, {})


def test_linear_part_is_ocp_without_options_and_gamma():
    rows = [row("bond", "B1", {"USD": 100.0}),
            row("fwd", "B1", {"USD": 40.0}),                              # a forward: linear
            row("opt", "B1", {"USD": 30.0}, product="fx_option"),         # an option by its product
            row("opt2", "B2", {"CNY": 7.0}, group="fx_option")]           # an option by the report's group
    # the gamma position: an FX x FX second derivative marks it, an FX x rate cross term does not
    p0 = build(rows)
    assert nonlinear_positions(p0, rows).keys() == {"opt", "opt2"}
    rows.append(row("gam", "B2", {"USD": 5.0}, gamma=[("FX|USD", "FX|USD", 2.0)]))
    rows.append(row("cross", "B2", {"USD": 5.0}, gamma=[("FX|USD", "CS", 2.0)]))
    pay = build(rows)
    assert nonlinear_positions(pay, rows).keys() == {"opt", "opt2", "gam"}
    out = fx_ocp(status([{"book": "B1", "ccy": "USD", "amount": 170.0}, {"book": "B2", "ccy": "USD", "amount": 10.0},
                         {"book": "B2", "ccy": "CNY", "amount": 7.0}]), pay, rows)
    assert out["available"] and out["nonlinear_ids"] == ["gam", "opt", "opt2"]
    usd = out["by_ccy"]["USD"]
    assert (usd["amount"], usd["nonlinear"], usd["linear"], usd["rate"]) == (180.0, 35.0, 145.0, 90.0)
    assert out["by_ccy"]["CNY"]["linear"] == 0.0
    b1 = next(x for x in out["by_book"] if x["book"] == "B1")
    assert (b1["nonlinear"], b1["linear"]) == (30.0, 140.0)
    # the linear part x a +10 % shock
    assert usd["linear"] * usd["rate"] * 10 / 100 == pytest.approx(1305.0)


def test_entity_level_line_takes_its_books_options():
    rows = [row("opt", "B1", {"USD": 30.0}, product="fx_option"), row("b", "B2", {"USD": 20.0})]
    out = fx_ocp(status([{"book": None, "books": ["B1", "B2"], "ccy": "USD", "amount": 50.0}]), build(rows), rows)
    (line,) = out["by_book"]
    assert line["book"] is None and line["books"] == ["B1", "B2"] and line["linear"] == 20.0


def test_no_published_ocp_says_so():
    rows = [row("b", "B1", {"USD": 20.0})]
    out = fx_ocp(None, build(rows), rows)
    assert out["available"] is False and out["reason"] == NO_OCP and out["by_book"] == [] and out["by_ccy"] == {}


def test_missing_rate_is_named():
    rows = [row("b", "B1", {"USD": 20.0})]
    st = status([{"book": "B1", "ccy": "USD", "amount": 20.0}])
    st["byCcy"]["USD"]["rate"] = None
    assert fx_ocp(st, build(rows), rows)["missing_rate"] == ["USD"]
