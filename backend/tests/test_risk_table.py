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
        "extra": {"quality": quality, **({"reported": {"x": {"x_product_group": group}}} if group else {})}, **kw}}


@pytest.mark.parametrize(("fact", "want"), [
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПД"}, "OFZ_PD"),
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПК", "coupon_type": "float"}, "OFZ_PK"),
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ИН"}, "OFZ_IN"),
    ({"instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПД", "ccy": "USD"}, "MINFIN_FX"),
    # R39: the Minfin's FX bonds by the hub's class (derived from its INN), never a corporate or a foreign sovereign
    ({"instr_type": "BOND", "segment": "Корп", "asset_class": "sovereign_fx", "ccy": "USD"}, "MINFIN_FX"),
    ({"instr_type": "BOND", "segment": "Корп", "asset_class": "sovereign_fx", "ccy": "EUR"}, "MINFIN_FX"),
    ({"instr_type": "BOND", "segment": "Корп", "asset_class": "foreign_sovereign", "ccy": "USD"}, "CORP_FIX"),
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


def test_a_valued_fact_keeps_the_report_fields_under_reported_x():
    # a valued position's fact keeps the report's x_* under extra.reported.x (tdm_run.reported of the stored row):
    # a valued report-based swap is an IRS, not «Не классифицировано» (OTC) in the currency x type table
    from desk.publish.tdm_run import reported

    fact = {"instr_type": "OTC", "ccy": "RUB", "extra": {"quality": "reported_value",
                                                         **reported({"amount_ccy": 1e8, "x": {"x_product_group": "irs"}})}}
    assert fact["extra"]["reported"]["x"] == {"x_product_group": "irs"}
    assert concepts.instr_kind(fact) == "IRS"
    for pg, kind in concepts.PRODUCT_KIND.items():
        nested = {"instr_type": "OTC", "extra": {"reported": {"x": {"x_product_group": pg}}}}
        flat = {"instr_type": "OTC", "extra": {"reported": {"x_product_group": pg}}}
        assert concepts.instr_kind(nested) == concepts.instr_kind(flat) == kind, pg
    assert concepts.product_group({"x": {"x_product": "Swap"}}) is None and concepts.product_group(None) is None
    t = concepts.risk_table([der("SW", "irs", "RUB", {"RUB RUONIA": {"2Y": 700.0}}, quality="reported_value")], FX)
    assert {r["kind"] for r in t["rows"] if r["level"] == "kind"} == {"IRS"}


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
    # the default view, «RUONIA + базис» (TDM#205): the basis apart; «КБД» has none (TDM#173)
    assert rub[("kind", "GOV", "OFZ_PD")]["ofz_basis_rub"] == pytest.approx(-2500.0)
    assert rows_of(concepts.risk_table(ITEMS, FX, ofz_view="kbd"), "RUB")[("kind", "GOV", "OFZ_PD")][
        "ofz_basis_rub"] == 0
    # the RUB legs of the future and the CCS are in RUB, their USD legs, FX delta, notional and MV in USD
    assert rub[("kind", "DER", "FX_FUT")]["dv01_rub"] == pytest.approx(-30.0)
    assert rub[("kind", "DER", "FX_FUT")]["n_positions"] == 0 and rub[("kind", "DER", "FX_FUT")]["n_legs_only"] == 1
    assert rub[("kind", "DER", "CCS")]["dv01_rub"] == pytest.approx(-380.0) and rub[("kind", "DER", "CCS")]["mv_rub"] == 0
    assert usd[("kind", "DER", "CCS")]["dv01_rub"] == pytest.approx(400.0)
    assert usd[("kind", "DER", "CCS")]["dv01_ccy"] == pytest.approx(400.0 / 80) and usd[("kind", "DER", "CCS")]["mv_rub"] == 5
    assert usd[("kind", "DER", "FX_FUT")]["fx_delta_ccy"] == -5e6 and usd[("kind", "GOV", "MINFIN_FX")]["n_positions"] == 1
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
                      "ofz_basis_rub", "ofz_spread_kbd_rub", "dv01_ofz_in_proxy_rub", "cs01_rub", "fx_delta_rub",
                      "n_risk_missing"}  # no "dv01_rub" of all
    assert t["fx_delta_rub"] == pytest.approx((1e6 + 1e6 - 5e6 + 2e6) * 80 + 7e6 * 11 + 1e6 * 100)
    assert concepts.risk_table(ITEMS, {"USD": 80.0})["total"]["fx_delta_rub"] is None  # a rate missing: no sum
    assert concepts.risk_table(ITEMS, {"USD": 80.0})["rows"][-1]["dv01_ccy"] is None


def test_minfin_by_the_issuer():
    # R39: the issuer the run's reference flags as the Minfin (is_gov: the hub's is_sovereign, INN 7710168360) makes a
    # bond the Minfin's whatever its class says; in roubles an OFZ kind, in a foreign currency MINFIN_FX
    eurobond = {"segment": "Корп", "asset_class": "corporate", "issuer": "Минфин России"}
    gov = frozenset({"Минфин России"})
    usd = {**bond("E", ccy="USD")["fact"], **eurobond}
    assert concepts.instr_kind(usd) == "CORP_FIX"
    assert concepts.instr_kind(usd, gov) == "MINFIN_FX"
    assert concepts.instr_kind({**usd, "ccy": "RUB"}, gov) == "OFZ_PD"
    assert concepts.instr_kind({**usd, "asset_class": "foreign_sovereign"}, gov) == "CORP_FIX"
    regional = {"instr_type": "BOND", "segment": "Суб", "asset_class": "Субфедеральные",
                "issuer": "Министерство финансов Амурской области"}
    assert concepts.instr_kind(regional, gov) == "SUBFED"  # a regional Minfin is not the Minfin
    t = concepts.risk_table([{"position_id": "E", "fact": usd}], FX, gov_issuers=gov)
    assert [c["kind"] for c in t["columns"]] == ["MINFIN_FX"]
    assert concepts.KIND_LABEL["MINFIN_FX"].startswith("Минфин")


def test_matrix_rows_currencies_columns_kinds():
    # R38: rows = currencies, columns = the kinds present; a cell = the (ccy, kind) row, a row total = the ccy row,
    # a column total = the kind over all currencies (what sums, in RUB)
    t = concepts.risk_table(ITEMS, FX, ofz_view="basis")
    assert t["ccys"] == ["RUB", "USD", "CNY", "CHF"]
    order = [k for k, _, _ in concepts.INSTR_KINDS]
    kinds = [c["kind"] for c in t["columns"]]
    assert kinds == sorted(kinds, key=order.index)
    assert set(kinds) == {r["kind"] for r in t["rows"] if r["level"] == "kind"}
    cells = {(r["ccy"], r["kind"]): r for r in t["rows"] if r["level"] == "kind"}
    for col in t["columns"]:
        mine = [r for (_, k), r in cells.items() if k == col["kind"]]
        for key in ("mv_rub", "notional_rub", "cs01_rub", "ofz_basis_rub", "ofz_spread_kbd_rub", "dv01_ofz_in_proxy_rub"):
            assert col[key] == pytest.approx(sum(r[key] for r in mine)), (col["kind"], key)
        assert col["dv01_all_curves_rub"] == pytest.approx(sum(sum(r["dv01_by_curve_rub"].values()) for r in mine))
    for key in ("mv_rub", "notional_rub", "cs01_rub", "dv01_all_curves_rub", "n_risk_missing"):
        assert sum(c[key] for c in t["columns"]) == pytest.approx(t["total"][key]), key
    assert sum(c["fx_delta_rub"] or 0.0 for c in t["columns"]) == pytest.approx(t["total"]["fx_delta_rub"])
    ccs = next(c for c in t["columns"] if c["kind"] == "CCS")
    assert ccs["n_positions"] == 1 and ccs["dv01_by_curve_rub"] == {"USD SOFR": 400.0, "RUB RUONIA": -380.0}
    assert [m["measure"] for m in t["measures"]] == ["dv01", "dv01_curve", "cs01", "ofz_basis", "ofz_spread_kbd",
                                                       "fx_delta", "mv", "notional", "n_positions"]
    # «КБД»: no basis measure, the OFZ's risk on its curve; the default is «RUONIA + базис» (TDM#205)
    assert concepts.risk_table(ITEMS, FX)["measures"] == t["measures"]
    assert "ofz_basis" not in [m["measure"] for m in concepts.risk_table(ITEMS, FX, ofz_view="kbd")["measures"]]
    no_rate = concepts.risk_table(ITEMS, {"USD": 80.0})["columns"]
    assert next(c for c in no_rate if c["kind"] == "FX_FWD")["fx_delta_rub"] is None  # CNY without its rate


def test_route_and_dimension(store_url, book, client):  # noqa: F811
    rid = run(store_url, book)
    t = client.get(f"/api/v1/runs/{rid}/risk-table?ofz_view=kbd").json()
    ps = client.get(f"/api/v1/runs/{rid}/positions").json()["positions"]
    assert t["attribution"] == "legs" and t["total"]["n_positions"] == len(ps)
    blocks = [r for r in t["rows"] if r["level"] == "ccy"]
    assert sum(r["n_positions"] for r in blocks) == len(ps)
    assert t["ccys"] == [r["ccy"] for r in blocks] and sum(c["n_positions"] for c in t["columns"]) == len(ps)
    assert "OFZ_PD" in {c["kind"] for c in t["columns"]}  # the Minfin's rouble bonds are OFZ kinds
    assert t["total"]["mv_rub"] == pytest.approx(sum(p["fact"]["mv_rub"] for p in ps if p["fact"]))
    assert t["total"]["n_risk_missing"] >= sum(p["fact"] is None for p in ps) >= 1  # the bond unknown to the static
    one = client.get(f"/api/v1/runs/{rid}/risk-table?attribution=position&book=OFZ").json()
    assert one["attribution"] == "position" and one["total"]["n_positions"] == sum(p["book_id"] == "OFZ" for p in ps)
    assert client.get(f"/api/v1/runs/{rid}/risk-table?attribution=x").status_code == 422
    # TDM#173: the OFZ view is named in the answer; «RUONIA + базис» by default (TDM#205), «КБД» on request
    basis = client.get(f"/api/v1/runs/{rid}/risk-table").json()
    assert basis == client.get(f"/api/v1/runs/{rid}/risk-table?ofz_view=basis").json()
    assert t["ofz_view"] == "kbd" and basis["ofz_view"] == "basis" and t["ofz_view_rule"] != basis["ofz_view_rule"]
    assert t["total"]["ofz_basis_rub"] == 0 and basis["total"]["ofz_basis_rub"] != 0  # the golden book has OFZ
    pk = next(r for r in basis["rows"] if r["level"] == "kind" and r["kind"] == "OFZ_PK")["ofz_basis_rub"]
    assert t["total"]["dv01_ofz_in_proxy_rub"] < 0  # the golden book's OFZ-IN: a nominal proxy, out of the basis
    assert next(r for r in basis["rows"] if r["level"] == "kind" and r["kind"] == "OFZ_IN")["ofz_basis_rub"] == 0
    assert t["total"]["dv01_by_curve_rub"]["RUB OFZ"] == pytest.approx(
        basis["total"]["ofz_basis_rub"] - pk + t["total"]["dv01_ofz_in_proxy_rub"])
    assert "RUB OFZ" not in basis["total"]["dv01_by_curve_rub"]
    assert t["total"]["dv01_all_curves_rub"] == pytest.approx(basis["total"]["dv01_all_curves_rub"])
    assert client.get(f"/api/v1/runs/{rid}/risk-table?ofz_view=both").status_code == 422
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


# TDM#173: the OFZ view of the risk table and the OFZ-IN's nominal proxy. A run of 0.2.6: a fixed OFZ on the KBD (risk on
# RUB OFZ, its par DV01 there is the basis ofz_basis01, ofz_spr01 its spread to the KBD), an OFZ-PK on RUONIA (its
# basis is its spread to RUONIA), an OFZ-IN valued by the fallback (a nominal duration proxy on the KBD), a corporate
KBD_RUN = [
    bond("PD", seg="Гос", ac="ОФЗ-ПД", cs01=0.0, risk={"RUB OFZ": {"3Y": -1000.0, "7Y": -2000.0}},
         ofz_spr01=-2500.0, extra={"ofz_basis01": -3000.0, "quality": "exact"}),
    bond("PK", seg="Гос", ac="ОФЗ-ПК", cpn="float", cs01=0.0, dv01=-20.0, ofz_spr01=-400.0,
         extra={"quality": "exact"}),
    bond("IN", seg="Гос", ac="ОФЗ-ИН", cs01=0.0, risk={"RUB OFZ": {"5Y": -600.0}}, ofz_spr01=-550.0,
         extra={"ofz_basis01": -600.0, "quality": "market_value"}),
    bond("CORP"),
    der("IRS", "irs", "RUB", {"RUB RUONIA": {"2Y": 700.0}}),
]


def test_ofz_view_kbd_counts_each_risk_once():
    t = concepts.risk_table(KBD_RUN, FX, ofz_view="kbd")
    assert t["ofz_view"] == "kbd" and t["ofz_view_note"] is None
    rub = rows_of(t, "RUB")[("ccy", None, None)]
    assert rub["dv01_by_curve_rub"] == pytest.approx({"RUB OFZ": -3600.0, "RUB RUONIA": -20.0 - 1000.0 + 700.0})
    assert rub["ofz_basis_rub"] == 0 and t["total"]["ofz_basis_rub"] == 0
    assert rub["ofz_spread_kbd_rub"] == pytest.approx(-2500.0 - 550.0 - 400.0)  # the OFZ-PK's spread to RUONIA too
    assert rub["dv01_ofz_in_proxy_rub"] == pytest.approx(-600.0)  # a memo: already on RUB OFZ
    # the per-curve DV01 plus the basis field: every bp of risk once
    assert sum(rub["dv01_by_curve_rub"].values()) + rub["ofz_basis_rub"] == pytest.approx(rub["dv01_rub"])
    assert rows_of(t, "RUB")[("kind", "GOV", "OFZ_IN")]["dv01_by_curve_rub"] == {"RUB OFZ": -600.0}


def test_ofz_view_basis_and_the_ofz_in_proxy_out_of_the_basis():
    t = concepts.risk_table(KBD_RUN, FX, ofz_view="basis")
    kbd = concepts.risk_table(KBD_RUN, FX, ofz_view="kbd")
    assert t["ofz_view"] == "basis" and "never add" in t["ofz_view_rule"]
    rs = rows_of(t, "RUB")
    rub = rs[("ccy", None, None)]
    assert rub["dv01_by_curve_rub"] == pytest.approx({"RUB RUONIA": -3600.0 - 20.0 - 1000.0 + 700.0})
    # the basis: the fixed OFZ's KBD DV01 and the OFZ-PK's spread to RUONIA, never the OFZ-IN's nominal proxy
    assert rub["ofz_basis_rub"] == pytest.approx(-3000.0 - 400.0)
    assert rs[("kind", "GOV", "OFZ_IN")]["ofz_basis_rub"] == 0
    assert rs[("kind", "GOV", "OFZ_IN")]["dv01_ofz_in_proxy_rub"] == pytest.approx(-600.0)
    assert rs[("kind", "GOV", "OFZ_IN")]["dv01_by_curve_rub"] == {"RUB RUONIA": -600.0}
    assert rub["ofz_spread_kbd_rub"] == pytest.approx(-2500.0 - 550.0)
    # the DV01 is the same in both views: only where the KBD risk is shown moves
    for key in ("dv01_rub", "dv01_ofz_in_proxy_rub", "cs01_rub"):
        assert rub[key] == pytest.approx(rows_of(kbd, "RUB")[("ccy", None, None)][key]), key
    assert t["total"]["dv01_all_curves_rub"] == pytest.approx(kbd["total"]["dv01_all_curves_rub"])
    # the KBD view's RUB OFZ is the basis view's basis without the OFZ-PK plus the OFZ-IN proxy
    assert kbd["total"]["dv01_by_curve_rub"]["RUB OFZ"] == pytest.approx(
        t["total"]["ofz_basis_rub"] + 400.0 + t["total"]["dv01_ofz_in_proxy_rub"])
    with pytest.raises(ValueError):
        concepts.risk_table(KBD_RUN, FX, ofz_view="both")


def test_an_ofz_in_core_values_is_a_basis_again():
    # once core values a linker (TDM#38, quality exact) its KBD risk is no longer a nominal proxy
    exact = bond("IN", seg="Гос", ac="ОФЗ-ИН", cs01=0.0, risk={"RUB OFZ": {"5Y": -600.0}}, ofz_spr01=-550.0,
                 extra={"ofz_basis01": -600.0, "quality": "exact"})
    t = concepts.risk_table([exact], FX, ofz_view="basis")["total"]
    assert t["ofz_basis_rub"] == pytest.approx(-600.0) and t["dv01_ofz_in_proxy_rub"] == 0
    old = bond("IN", seg="Гос", ac="ОФЗ-ИН", cs01=0.0, ofz_spr01=-550.0, extra={"quality": "market_value"})
    assert concepts.risk_table([old], FX, ofz_view="basis")["total"]["ofz_basis_rub"] == pytest.approx(-550.0)


def test_ofz_view_kbd_of_a_run_before_0_2_6_is_approximate():
    # until 0.2.5 every OFZ was on RUONIA and ofz_spr01 was its basis: the «КБД» view moves it off RUONIA, says so
    t = concepts.risk_table(ITEMS, FX, ofz_view="kbd")
    assert t["ofz_view_note"] and "0.2.6" in t["ofz_view_note"]
    ofz = rows_of(t, "RUB")[("kind", "GOV", "OFZ_PD")]
    assert ofz["dv01_by_curve_rub"] == pytest.approx({"RUB OFZ": -2500.0, "RUB RUONIA": -500.0})
    assert ofz["dv01_rub"] == pytest.approx(-3000.0) and ofz["ofz_basis_rub"] == 0
    assert concepts.risk_table(ITEMS, FX, ofz_view="basis")["ofz_view_note"] is None


def test_the_run_summary_keeps_the_ofz_in_proxy_out_of_the_basis():
    from desk.publish import summary

    def row(pid, ac, ofz_basis, quality, risk, ofz=0.0):
        return {"position_id": pid, "book_id": "B", "pv_rub": 1e6, "risk": risk, "cs01": 0.0, "ofz": ofz,
                "instr_type": "BOND", "segment": "Гос", "asset_class": ac, "coupon_type": "fix", "ccy": "RUB",
                "notional": 1e6, "notional_rub": 1e6, "price": 100.0, "spread_bp": 0.0, "dv01": None, "dm_bp": None,
                "pnl": None, "ofz_basis": ofz_basis, "quality": quality}

    s = summary.build([row("PD", "ОФЗ-ПД", -3000.0, "exact", {"RUB OFZ": {"7Y": -3000.0}}, -2500.0),
                       row("IN", "ОФЗ-ИН", -600.0, "market_value", {"RUB OFZ": {"5Y": -600.0}}, -550.0)])
    assert s["basis"] == pytest.approx(-3000.0) and s["basisIn"] == pytest.approx(-600.0)
    assert s["dv01"] == pytest.approx({"RUB RUONIA": -3600.0}) and s["ofzSpr"] == pytest.approx(-3050.0)
    assert s["pos"]["IN"][3] == pytest.approx(-600.0)  # a position's basis_of as it is: the pages tell it by kind


# TDM#213 (0.2.7, core 0.39): an OFZ's RUONIA and basis risk are two computed risks. The row keeps them in the
# scenarios' coordinates (valuation.bonds.ofz_levels): RUB OFZ the basis (the KBD's par risk), RUB RUONIA RUONIA minus
# it, extra.ofz_ruonia01 the RUONIA risk. RUONIA -3050 (3Y -1030, 7Y -2020), basis -3000 (3Y -1000, 7Y -2000).
COMPUTED = bond("PD", seg="Гос", ac="ОФЗ-ПД", cs01=0.0, dv01=-3050.0, ofz_spr01=-2500.0,
                risk={"RUB OFZ": {"3Y": -1000.0, "7Y": -2000.0}, "RUB RUONIA": {"3Y": -30.0, "7Y": -20.0}},
                extra={"ofz_basis01": -3000.0, "ofz_ruonia01": -3050.0, "quality": "exact"})


def test_ofz_in_ruonia_basis_two_computed_risks():
    basis = concepts.risk_table([COMPUTED, bond("CORP")], FX, ofz_view="basis")
    kbd = concepts.risk_table([COMPUTED, bond("CORP")], FX, ofz_view="kbd")
    b, k = (rows_of(t, "RUB")[("kind", "GOV", "OFZ_PD")] for t in (basis, kbd))
    # «RUONIA + базис»: its RUONIA risk on RUONIA, the basis apart; «КБД»: its KBD par risk (the basis), no RUONIA
    assert b["dv01_by_curve_rub"] == pytest.approx({"RUB RUONIA": -3050.0}) and b["ofz_basis_rub"] == pytest.approx(-3000.0)
    assert k["dv01_by_curve_rub"] == pytest.approx({"RUB OFZ": -3000.0}) and k["ofz_basis_rub"] == 0
    assert b["dv01_rub"] == pytest.approx(-3050.0) and k["dv01_rub"] == pytest.approx(-3000.0)
    assert basis["ofz_view_note"] is None and kbd["ofz_view_note"] is None
    # a row of 0.2.6 (or an OFZ off core): its RUONIA is its basis again, the basis view says so
    old = concepts.risk_table(KBD_RUN, FX, ofz_view="basis")
    assert old["ofz_view_note"] and "1 OFZ" in old["ofz_view_note"] and "variant P" in old["ofz_view_note"]


def test_the_run_summary_keeps_the_ofz_ruonia_risk():
    from desk.publish import summary

    def row(pid, risk, ofz_basis, ofz_ruo):
        return {"position_id": pid, "book_id": "B", "pv_rub": 1e6, "risk": risk, "cs01": 0.0, "ofz": -2500.0,
                "instr_type": "BOND", "segment": "Гос", "asset_class": "ОФЗ-ПД", "coupon_type": "fix", "ccy": "RUB",
                "notional": 1e6, "notional_rub": 1e6, "price": 100.0, "spread_bp": 0.0, "dv01": None, "dm_bp": None,
                "pnl": None, "ofz_basis": ofz_basis, "quality": "exact", "ofz_ruo": ofz_ruo, "g_bp": -20.3,
                "z_kbd": -6.9 if ofz_ruo else None}

    f = COMPUTED["fact"]
    s = summary.build([row("NEW", f["risk"], -3000.0, -3050.0), row("OLD", {"RUB OFZ": {"7Y": -1000.0}}, -1000.0, None)])
    # the pages' RUONIA (the «RUONIA + базис» view): the new OFZ's RUONIA risk, the old one's KBD risk
    assert s["dv01"] == pytest.approx({"RUB RUONIA": -3050.0 - 1000.0}) and s["basis"] == pytest.approx(-4000.0)
    assert s["ofzRuo"] == pytest.approx(-3050.0 - 1000.0)  # what the «КБД» view takes off RUONIA
    assert s["pos"]["NEW"][11] == pytest.approx(-3050.0) and s["pos"]["OLD"][11] is None
    assert s["pos"]["NEW"][12:] == [-20.3, -6.9] and s["pos"]["OLD"][13] is None  # G and Z to the KBD (#232)
    assert s["pos"]["NEW"][1] == pytest.approx(-3050.0) and s["pos"]["NEW"][3] == pytest.approx(-3000.0)
    # a run before 0.2.6 has no OFZ on the KBD: ofzRuo is null, so the «КБД» history takes its basis off RUONIA as
    # before (0.0 would leave RUONIA whole and count the OFZ twice)
    early = summary.build([row("OLD", {"RUB RUONIA": {"7Y": -3000.0}}, None, None)])
    assert early["ofzRuo"] is None and early["dv01"] == pytest.approx({"RUB RUONIA": -3000.0})
