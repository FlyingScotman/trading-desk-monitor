"""The aggregated risk table currency x instrument type (UI-02): the instrument kind of a fact (``concepts.instr_kind``,
one mapping table; ``lib/instr.js`` mirrors it), the currency attribution of a cross-currency kind (``legs``, the
provisional default, or ``position``), missing risk shown as missing, totals only where they sum; and the route
``/api/v1/runs/{id}/risk-table`` and the ``instr_kind`` dimension of ``/aggregates`` on a run of the golden
fixtures (test_api's book)."""

from __future__ import annotations

import pytest
from test_api import book, client, run  # noqa: F401 - fixtures

from desk.api import concepts

FX = {"USD": 80.0, "EUR": 90.0, "CNY": 11.0, "CHF": 100.0}


def bond(pid, ccy="RUB", seg="Корп", ac="Корпоративные", cpn="fix", dv01=-1000.0, curve=None, **kw):
    curve = curve or {"RUB": "RUB RUONIA", "USD": "USD SOFR", "CNY": "CNY", "CHF": "CHF SARON"}[ccy]
    rate = 1.0 if ccy == "RUB" else FX[ccy]
    f = {"instr_type": "BOND", "segment": seg, "asset_class": ac, "coupon_type": cpn, "ccy": ccy,
         "notional": 1e6, "notional_rub": 1e6 * rate, "mv_rub": 1e6 * rate, "cs01": -50.0, "ofz_spr01": 0.0,
         "risk": {curve: {"1Y": dv01 / 2, "3Y": dv01 / 2}}, **({"fx_delta": {ccy: 1e6}} if ccy != "RUB" else {})}
    return {"position_id": pid, "fact": {**f, **kw}}


def der(pid, group, ccy, risk, fx_delta=None, it="OTC", quality="computed", **kw):
    return {"position_id": pid, "fact": {
        "instr_type": it, "ccy": ccy, "notional": 1e6, "notional_rub": 1e6 * FX.get(ccy, 1.0), "mv_rub": 5.0,
        "cs01": 0.0, "ofz_spr01": 0.0, "risk": risk, "fx_delta": fx_delta or {},
        "extra": {"quality": quality, **({"reported": {"x_product_group": group}} if group else {})}, **kw}}


@pytest.mark.parametrize(("fact", "want"), [
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПД"}, "OFZ_PD"),
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПК", "coupon_type": "float"}, "OFZ_PK"),
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ИН"}, "OFZ_IN"),
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПД", "ccy": "USD"}, "SOV_FX"),
    ({"instr_type": "BOND", "segment": "Суб", "asset_class": "Субфедеральные"}, "SUBFED"),
    ({"instr_type": "BOND", "segment": "Суб", "asset_class": "Муниципальные"}, "MUNI"),
    ({"instr_type": "BOND", "segment": "Корп", "coupon_type": "float"}, "CORP_FLT"),
    ({"instr_type": "BOND", "segment": "Корп", "ccy": "USD"}, "CORP_FIX"),
    ({"instr_type": "BOND", "segment": "Секьюритизация"}, "SECUR"),
    ({"instr_type": "BOND", "segment": "Корп", "asset_class": "Не сопоставлено"}, "BOND_NA"),
    ({"instr_type": "BOND", "segment": "Корп", "extra": {"reported": {"x_product_group": "bond_custom"}}}, "CORP_FIX"),
    *[({"instr_type": it, "extra": {"reported": {"x_product_group": pg}}}, k) for pg, k, it in (
        ("irs", "IRS", "OTC"), ("ccs", "CCS", "OTC"), ("cds", "CDS", "OTC"), ("trs", "TRS", "OTC"),
        ("bond_future", "BOND_FUT", "OTC"), ("bond_forward", "BOND_FWD", "OTC"), ("fx_future", "FX_FUT", "FX"),
        ("fx_ndf", "FX_FWD", "FX"), ("equity", "EQUITY", "OTHER"))],
    ({"instr_type": "OTC"}, "UNCLASSIFIED"), ({"instr_type": "OTC", "extra": {"reported": {"x_product_group": "new"}}},
                                              "UNCLASSIFIED"),
    ({"instr_type": "OTHER"}, "UNCLASSIFIED"), ({"instr_type": "FX"}, "FX"),
    ({"instr_type": "FX", "ocp_transfer": True}, "OCP"), ({"instr_type": "FX", "asset_class": "Перенос ОВП"}, "OCP"),
    ({"instr_type": "IRS"}, "IRS"), ({"instr_type": "XCCY"}, "CCS"), ({"instr_type": "CDS"}, "CDS"),  # legacy tdm
    ({"ccy": "USD", "asset_class": "fx", "extra": {"reported": {}}}, "FX"),  # unvalued: the report row
    ({"ccy": "RUB", "asset_class": "otc", "extra": {"reported": {"x_product_group": "irs"}}}, "IRS"),
    ({"ccy": "RUB", "asset_class": "bond", "extra": {"reported": {"x_product_group": "bond"}}}, "BOND_NA"),
    ({"ccy": "RUB", "asset_class": None}, "BOND_NA"),
])
def test_instr_kind(fact, want):
    assert concepts.instr_kind(fact) == want


def test_one_mapping_table():
    kinds = [k for k, _, _ in concepts.INSTR_KINDS]
    assert len(kinds) == len(set(kinds)) and "UNCLASSIFIED" in kinds
    assert set(concepts.PRODUCT_KIND.values()) | set(concepts.LEGACY_KIND.values()) <= set(kinds)
    assert set(concepts.BOND_KIND.values()) | set(concepts.CROSS_CCY_KINDS) <= set(kinds)
    assert {g for _, g, _ in concepts.INSTR_KINDS} == set(concepts.GROUPS)
    # a bond kind's group is kind_of's group: the group subtotals reconcile with the cockpit's risk matrix
    assert all(concepts.KIND_GROUP[k] == g for (g, _), k in concepts.BOND_KIND.items())


def rows_of(t, ccy):
    return {(r["level"], r["group"], r["kind"]): r for r in t["rows"] if r["ccy"] == ccy}


ITEMS = [
    bond("OFZ", seg="Гос", ac="ОФЗ-ПД", dv01=-3000.0, ofz_spr01=-2500.0, cs01=0.0),
    bond("CORP"), bond("FLT", cpn="float", dv01=-100.0),
    bond("USDC", ccy="USD", dv01=-800.0), bond("SOV", ccy="USD", seg="Гос", ac="ОФЗ-ПД", dv01=-400.0, cs01=0.0),
    bond("CHF", ccy="CHF", dv01=-200.0),
    der("IRS", "irs", "RUB", {"RUB RUONIA": {"2Y": 700.0}, "RUB KEY": {"1Y": -50.0}}),
    der("SI", "fx_future", "USD", {"USD SOFR": {"3M": 30.0}, "RUB RUONIA": {"3M": -30.0}}, {"USD": -5e6}, it="FX"),
    der("CCS", "ccs", "USD", {"USD SOFR": {"3Y": 400.0}, "RUB RUONIA": {"3Y": -380.0}}, {"USD": 2e6}),
    der("FWD", "fx_ndf", "CNY", {}, {"CNY": 7e6}, it="FX", quality="reported_value"),  # no rate risk reported
    der("TRS", None, "RUB", {"RUB RUONIA": {"1Y": 10.0}}),  # no product group: unclassified, kept
    {"position_id": "NV", "fact": None, "input": {"ccy": "RUB", "asset_class": "otc",
                                                    "extra": {"reported": {"x_product_group": "irs"}}}},
]


def test_legs_each_currency_on_its_own_curves():
    t = concepts.risk_table(ITEMS, FX)
    assert t["attribution"] == "legs" and t["attribution_provisional"] and t["cross_ccy_kinds"]
    assert [r["ccy"] for r in t["rows"] if r["level"] == "ccy"] == ["RUB", "USD", "CNY", "CHF"]
    rub, usd = rows_of(t, "RUB"), rows_of(t, "USD")
    # a currency's DV01 is the sum over every position of its curves (the cockpit's risk of the currency)
    for ccy in ("RUB", "USD", "CHF"):
        want = sum(v for it in ITEMS if it["fact"] for c, bk in it["fact"]["risk"].items()
                   if concepts.CURVE_MAP.get(c, c).split()[0] == ccy for v in bk.values())
        assert rows_of(t, ccy)[("ccy", None, None)]["dv01_rub"] == pytest.approx(want)
    assert rows_of(t, "CHF")[("ccy", None, None)]["dv01_rub"] == pytest.approx(-200.0)  # CHF never falls into RUB
    # a currency's total is the sum of its kinds; a group's the sum of its kinds
    for ccy in ("RUB", "USD"):
        rs = rows_of(t, ccy)
        kinds = [r for (lv, _, _), r in rs.items() if lv == "kind"]
        for key in ("dv01_rub", "mv_rub", "cs01_rub", "ofz_basis_rub", "fx_delta_ccy", "notional_rub"):
            assert rs[("ccy", None, None)][key] == pytest.approx(sum(r[key] for r in kinds)), (ccy, key)
    assert rub[("group", "GOV", None)]["dv01_rub"] == pytest.approx(-3000.0)
    assert rub[("kind", "GOV", "OFZ_PD")]["ofz_basis_rub"] == pytest.approx(-2500.0)
    # the RUB legs of the future and the CCS are in RUB, their USD legs, FX delta, notional and MV in USD
    assert rub[("kind", "DER", "FX_FUT")]["dv01_rub"] == pytest.approx(-30.0)
    assert rub[("kind", "DER", "FX_FUT")]["n_positions"] == 0 and rub[("kind", "DER", "FX_FUT")]["n_legs_only"] == 1
    assert rub[("kind", "DER", "CCS")]["dv01_rub"] == pytest.approx(-380.0) and rub[("kind", "DER", "CCS")]["mv_rub"] == 0
    assert usd[("kind", "DER", "CCS")]["dv01_rub"] == pytest.approx(400.0)
    assert usd[("kind", "DER", "CCS")]["dv01_ccy"] == pytest.approx(400.0 / 80) and usd[("kind", "DER", "CCS")]["mv_rub"] == 5
    assert usd[("kind", "DER", "FX_FUT")]["fx_delta_ccy"] == -5e6 and usd[("kind", "GOV", "SOV_FX")]["n_positions"] == 1
    assert usd[("ccy", None, None)]["fx_delta_ccy"] == pytest.approx(1e6 + 1e6 - 5e6 + 2e6)
    assert all(r["dv01_foreign_rub"] == 0 for r in t["rows"])
    assert rub[("kind", "OTH", "UNCLASSIFIED")]["n_positions"] == 1  # shown, never dropped


def test_position_attribution_keeps_foreign_curves_apart():
    t = concepts.risk_table(ITEMS, FX, "position")
    rub, usd = rows_of(t, "RUB"), rows_of(t, "USD")
    assert ("kind", "DER", "FX_FUT") not in rub and ("kind", "DER", "CCS") not in rub
    ccs = usd[("kind", "DER", "CCS")]
    assert ccs["dv01_rub"] == pytest.approx(400.0)  # the block's DV01: its own currency's curves only
    assert ccs["dv01_by_curve_rub"]["RUB RUONIA"] == pytest.approx(-380.0) and ccs["dv01_foreign_rub"] == -380.0
    assert usd[("ccy", None, None)]["dv01_foreign_rub"] == pytest.approx(-380.0 - 30.0)
    # the totals over all currencies are the same whatever the rule
    legs = concepts.risk_table(ITEMS, FX)["total"]
    for key in ("mv_rub", "notional_rub", "cs01_rub", "dv01_all_curves_rub", "n_positions"):
        assert t["total"][key] == pytest.approx(legs[key]), key
    with pytest.raises(ValueError):
        concepts.risk_table(ITEMS, FX, "foreign")


def test_missing_risk_is_missing_not_zero():
    t = concepts.risk_table(ITEMS, FX)
    fwd = rows_of(t, "CNY")[("kind", "DER", "FX_FWD")]
    assert fwd["n_risk_missing"] == 1 and fwd["risk_missing"][0]["reason"] == "reported_no_risk"
    nv = rows_of(t, "RUB")[("kind", "DER", "IRS")]
    assert nv["n_positions"] == 2 and nv["n_risk_missing"] == 1 and nv["risk_missing"][0]["reason"] == "not_valued"
    assert rows_of(t, "RUB")[("ccy", None, None)]["n_risk_missing"] == 1
    assert t["total"]["n_risk_missing"] == 2 and t["total"]["n_positions"] == len(ITEMS)
    fx_only = concepts.risk_table([der("SPOT", None, "USD", {}, {"USD": 1e6}, it="FX")], FX)
    assert fx_only["total"]["n_risk_missing"] == 0  # an FX spot has no rate risk by nature
    htm = bond("HTM", dv01=0.0, cs01=0.0, risk={}, accrual_only=True)
    assert concepts.risk_table([htm], FX)["total"]["n_risk_missing"] == 0  # accrual-only: no market risk by the rule
    assert concepts.risk_table([bond("B", dv01=0.0, cs01=0.0, risk={})], FX)["total"]["n_risk_missing"] == 1


def test_totals_only_where_they_sum():
    t = concepts.risk_table(ITEMS, FX)["total"]
    assert set(t) == {"n_positions", "notional_rub", "mv_rub", "dv01_by_curve_rub", "dv01_all_curves_rub",
                      "ofz_basis_rub", "cs01_rub", "fx_delta_rub", "n_risk_missing"}  # no "dv01_rub" of all
    assert t["fx_delta_rub"] == pytest.approx((1e6 + 1e6 - 5e6 + 2e6) * 80 + 7e6 * 11 + 1e6 * 100)
    assert concepts.risk_table(ITEMS, {"USD": 80.0})["total"]["fx_delta_rub"] is None  # a rate missing: no sum
    assert concepts.risk_table(ITEMS, {"USD": 80.0})["rows"][-1]["dv01_ccy"] is None


def test_route_and_dimension(store_url, book, client):  # noqa: F811
    rid = run(store_url, book)
    t = client.get(f"/api/v1/runs/{rid}/risk-table").json()
    ps = client.get(f"/api/v1/runs/{rid}/positions").json()["positions"]
    assert t["attribution"] == "legs" and t["total"]["n_positions"] == len(ps)
    blocks = [r for r in t["rows"] if r["level"] == "ccy"]
    assert sum(r["n_positions"] for r in blocks) == len(ps)
    assert t["total"]["mv_rub"] == pytest.approx(sum(p["fact"]["mv_rub"] for p in ps if p["fact"]))
    assert t["total"]["n_risk_missing"] >= sum(p["fact"] is None for p in ps) >= 1  # the bond unknown to the static
    one = client.get(f"/api/v1/runs/{rid}/risk-table?attribution=position&book=OFZ").json()
    assert one["attribution"] == "position" and one["total"]["n_positions"] == sum(p["book_id"] == "OFZ" for p in ps)
    assert client.get(f"/api/v1/runs/{rid}/risk-table?attribution=x").status_code == 422
    rows = client.get(f"/api/v1/runs/{rid}/aggregates?dims=ccy,instr_kind").json()["rows"]
    assert sum(r["n_positions"] for r in rows) == len(ps)
    want = {concepts.instr_kind(p["fact"]) for p in ps if p["fact"]} | {None}
    assert {r["key"]["instr_kind"] for r in rows} == want
    sql = client.get(f"/api/v1/runs/{rid}/aggregates?dims=ccy").json()["rows"]
    for ccy in {r["key"]["ccy"] for r in sql}:
        assert sum(r["pv_rub"] or 0 for r in rows if r["key"]["ccy"] == ccy) == pytest.approx(
            next(r["pv_rub"] or 0 for r in sql if r["key"]["ccy"] == ccy))
    spec = client.get("/openapi.json").json()
    assert "instr_kind" in spec["paths"]["/api/v1/runs/{run_id}/aggregates"]["get"]["parameters"][1]["description"]
