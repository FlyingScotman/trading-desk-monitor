"""Rates by factor (TDM#437) on synthetic history: RUONIA and the OFZ basis instead of the curves KBD and RUONIA, the
KBD-only rows apart; ``/curves`` by term and whole, ``/period`` ``ratesByFactor``; the sums equal those by curve."""
from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_pnl_period import CURVE, DAYS, get, holdings, seed_period

from desk.api import create_app
from desk.api.pnl_period import (
    F_BASIS,
    F_KBD,
    F_RUONIA,
    exact_factors,
    linear_factors,
    risk_factors,
)

OFZ = "RUB OFZ"
B = {"1Y": -2000.0, "5Y": -6000.0}
"""The OFZ's basis risk (the KBD's par risk), ``risk["RUB OFZ"]``."""
R = {"1M": -300.0, "1Y": -2500.0, "5Y": -5000.0}
"""Its RUONIA risk; ``risk["RUB RUONIA"]`` is R - B (the scenarios' coordinates)."""
KBD_DAY = DAYS[3]
"""A date valued on the KBD only (no RUONIA OIS): the KBD-only row."""


def moves(k: int) -> tuple[dict[str, float], dict[str, float]]:
    return {"1Y": -2.0 - k, "5Y": 1.0 + 0.5 * k}, {"1M": -1.0, "1Y": -3.0 + 0.25 * k, "5Y": 0.5}


def by_term(k: int) -> bool:
    """Odd runs lack ``prev.ofz_ruonia_risk``: their split is whole, without terms."""
    return k % 2 == 0


def ofz_pnl(k: int) -> dict:
    d_k, d_o = moves(k)
    terms = {b: B[b] * d_k[b] for b in B}
    lin = math.fsum(terms.values())
    ruo = math.fsum(R[b] * d_o[b] for b in R)
    prev = {"quantity": 10.0, "risk": {OFZ: B}}
    if by_term(k):
        prev["ofz_ruonia_risk"] = R
    return {"carry": 10.0, "rolldown": 0.0, "rates": lin + 7.0, "spread": 0.0, "convexity": 0.0, "other": 1.0,
            "funding": 0.0, "fx_gross": 0.0, "fx_funding": 0.0, "new_trades": 0.0,
            "extra": {"method": "desk-flash-exact-v2", "rates_linear": lin, "rates_linear_curves": {OFZ: terms},
                      "curve_moves_bp": {OFZ: d_k, CURVE: d_o}, "rates_linear_ruonia": ruo,
                      "rates_linear_basis": lin - ruo, "prev": prev}}


def more(con, rid, day):
    k = DAYS.index(day)
    rows = [("O1", {"spread_curve": OFZ, "risk": {OFZ: B, CURVE: {b: R[b] - B.get(b, 0.0) for b in R}},
                    "extra": {"ofz_ruonia01": math.fsum(R.values())}}, ofz_pnl(k))]
    if day == KBD_DAY:
        rows.append(("K1", {"spread_curve": OFZ, "risk": {OFZ: {"1Y": -100.0}}},
                     {"rates": 50.0, "other": 0.0, "extra": {"method": "desk-flash-exact-v2", "rates_linear": 40.0,
                                                             "rates_linear_curves": {OFZ: {"1Y": 40.0}}}}))
    for j, (pid, f, pnl) in enumerate(rows):
        con.execute(
            "INSERT INTO run_position(run_id,position_id,ord,book_id,secid,quantity,quality,pv_rub,dv01_rub,"
            "cs01_rub,fact,pnl,no_pnl) VALUES (%s,%s,%s,'DEMO-A','SYN-OFZ',10,'exact',1e7,-10,0,%s,%s,NULL)",
            (rid, pid, 50 + j, Jsonb({"name": "Synthetic " + pid, "ccy": "RUB", **f}), Jsonb(pnl)))
    con.execute("UPDATE run_reference SET data = data || %s WHERE run_id = %s AND kind = 'curves'", (Jsonb([
        {"curve_id": OFZ, "name": "Synthetic KBD", "ccy": "RUB", "kind": "bond",
         "points": [{"bucket": "1Y", "rate": 14.0 + 0.02 * k, "move_bp": 2.0},
                    {"bucket": "5Y", "rate": 13.0, "move_bp": 0.0}]}]), rid))


@pytest.fixture
def ofz_client(store_url):
    seed_period(store_url, more=more)
    return TestClient(create_app(store_url))


def fsum(values):
    vals = [v for v in values if v is not None]
    return math.fsum(vals) if vals else None


def test_curves_by_factor(ofz_client):
    k = get(ofz_client, "curves")
    ids = [f["id"] for f in k["factors"]]
    assert ids == [F_RUONIA, F_BASIS, F_KBD, "USD", ""]
    names = {f["id"]: f["name"] for f in k["factors"]}
    assert names[F_BASIS] == "Базис ОФЗ (КБД − RUONIA)" and names[F_KBD] == "КБД (нет RUONIA на дату)"
    assert names["USD"] == "USD: synthetic"
    even = [i for i in range(len(DAYS)) if by_term(i)]
    odd = [i for i in range(len(DAYS)) if not by_term(i)]
    lin_a1 = math.fsum(100.0 * i + 1 for i in range(len(DAYS)))  # A1's RUONIA terms: 1Y a quarter, 5Y the rest
    fl = k["factorLinear"]
    ruo_1y = math.fsum(R["1Y"] * moves(i)[1]["1Y"] for i in even)
    assert fl[F_RUONIA]["1Y"] == pytest.approx(lin_a1 * .25 + ruo_1y)
    assert fl[F_RUONIA]["1M"] == pytest.approx(math.fsum(R["1M"] * moves(i)[1]["1M"] for i in even))
    assert fl[F_BASIS]["1M"] == pytest.approx(-fl[F_RUONIA]["1M"])  # the KBD has no 1M term: the basis is -RUONIA
    assert fl[F_BASIS]["5Y"] == pytest.approx(math.fsum(B["5Y"] * moves(i)[0]["5Y"] - R["5Y"] * moves(i)[1]["5Y"]
                                                        for i in even))
    assert fl[F_KBD] == {**dict.fromkeys(k["buckets"]), "1Y": pytest.approx(40.0)}
    assert "1M" in k["buckets"] and k["buckets"][0] == "1M"
    rest = k["factorLinearRest"]
    pnl = [ofz_pnl(i)["extra"] for i in range(len(DAYS))]
    # whole: the OFZ's odd runs and the RUONIA rows without terms (all but A1)
    assert rest[F_RUONIA] == pytest.approx(fsum(pnl[i]["rates_linear_ruonia"] for i in odd)
                                           + k["linearTotal"][CURVE] - lin_a1)
    assert rest[F_BASIS] == pytest.approx(fsum(pnl[i]["rates_linear_basis"] for i in odd))
    assert rest[F_KBD] is None and rest[""] == pytest.approx(k["linearTotal"][""])
    # the split changes no total: the factors add up to the linear and exact totals by curve
    tot = k["factorLinearTotal"]
    assert fsum(tot.values()) == pytest.approx(fsum(k["linearTotal"].values()))
    assert tot[F_RUONIA] + tot[F_BASIS] == pytest.approx(
        k["linearTotal"][CURVE] + fsum(p["rates_linear"] for p in pnl))
    assert tot[F_KBD] == pytest.approx(40.0)
    assert fsum(k["factorExact"].values()) == pytest.approx(fsum(k["exact"].values()))
    assert k["factorExact"][F_BASIS] == pytest.approx(fsum(ofz_pnl(i)["rates"] - pnl[i]["rates_linear_ruonia"]
                                                           for i in range(len(DAYS))))
    assert k["factorExact"][F_KBD] == pytest.approx(50.0) and k["exact"][OFZ] == pytest.approx(
        fsum(ofz_pnl(i)["rates"] for i in range(len(DAYS))) + 50.0)
    cov = k["factorCoverage"]
    assert cov[F_KBD] == {"rows": 1, "linearRows": 1, "bucketRows": 1, "dates": 1}
    assert cov[F_BASIS] == {"rows": len(DAYS), "linearRows": len(DAYS), "bucketRows": len(even), "dates": len(DAYS)}
    # DV01 by factor: RUONIA = RUB OFZ + RUB RUONIA of the OFZ (its RUONIA risk), the basis RUB OFZ
    rows_on = sum(sum(1 for pid in holdings(d) if pid != "U1") for d in DAYS)
    dv = k["factorDv01"]
    assert dv[F_BASIS]["5Y"] == pytest.approx(B["5Y"])
    assert dv[F_RUONIA]["5Y"] == pytest.approx(R["5Y"] + -3000.0 * rows_on / len(DAYS))
    assert dv[F_KBD]["1Y"] == pytest.approx(-100.0 / len(DAYS)) and dv[F_KBD]["5Y"] is None
    mv = k["factorMove"]
    assert mv[F_RUONIA]["1Y"] == pytest.approx(k["move"][CURVE]["1Y"])
    assert mv[F_BASIS]["1Y"] == pytest.approx(k["move"][OFZ]["1Y"] - k["move"][CURVE]["1Y"])
    assert mv[F_KBD]["1Y"] == pytest.approx(k["move"][OFZ]["1Y"]) and mv[F_BASIS]["1M"] is None


def test_period_rates_by_factor(ofz_client):
    p = get(ofz_client)
    for d in p["days"]:
        assert fsum(d["ratesByFactor"].values()) == pytest.approx(fsum(d["ratesByCurve"].values()))
        assert d["ratesByFactor"].get(F_KBD) == (pytest.approx(50.0) if d["date"] == KBD_DAY else None)
    o1 = next(x for x in p["positions"] if x["positionId"] == "O1")
    assert set(o1["ratesByFactor"]) == {F_RUONIA, F_BASIS}
    assert fsum(o1["ratesByFactor"].values()) == pytest.approx(o1["c"]["rates"])
    assert o1["ratesByFactor"][F_RUONIA] == pytest.approx(fsum(ofz_pnl(i)["extra"]["rates_linear_ruonia"]
                                                               for i in range(len(DAYS))))
    a1 = next(x for x in p["positions"] if x["positionId"] == "A1" and x["book"] == "DEMO-A")
    assert a1["ratesByFactor"] == {F_RUONIA: pytest.approx(a1["c"]["rates"])}
    c1 = next(x for x in p["positions"] if x["positionId"] == "C1")
    assert c1["ratesByFactor"] == {"": pytest.approx(c1["c"]["rates"])}


def test_factor_rules():
    # an OFZ whose RUONIA terms do not add up to its saved RUONIA part is not split by term
    terms = {OFZ: {"1Y": -10.0}}
    by, whole = linear_factors(OFZ, -10.0, terms, -4.0, -6.0, {"1Y": 1.0}, {CURVE: {"1Y": 2.0}})
    assert by is None and whole == {F_RUONIA: -4.0, F_BASIS: -6.0}
    by, whole = linear_factors(OFZ, -10.0, terms, -4.0, -6.0, {"1Y": -2.0}, {CURVE: {"1Y": 2.0}})
    assert by == {F_RUONIA: {"1Y": -4.0}, F_BASIS: {"1Y": -6.0}} and whole == {}
    assert linear_factors(CURVE, 5.0, None, None, None, None, None) == (None, {F_RUONIA: 5.0})
    assert linear_factors(None, None, None, None, None, None, None) == (None, {})  # no linear: nothing, not 0
    assert linear_factors(OFZ, 3.0, {OFZ: {"1Y": 3.0}, "RUB KEY": {"1Y": 1.0}}, None, None, None, None) == (
        {F_KBD: {"1Y": 3.0}, "RUB KEY": {"1Y": 1.0}}, {})
    assert exact_factors(OFZ, 9.0, 4.0, 3.0) == {F_RUONIA: 4.0, F_BASIS: 5.0}
    assert exact_factors(OFZ, None, 4.0, 3.0) == {F_RUONIA: None, F_BASIS: None}
    assert exact_factors(OFZ, 9.0, None, None) == {F_KBD: 9.0} and exact_factors("USD", 1.0, None, None) == {"USD": 1.0}
    assert risk_factors({OFZ: {"1Y": -2.0}, CURVE: {"1Y": -1.0}}, True) == {F_RUONIA: {"1Y": -3.0},
                                                                            F_BASIS: {"1Y": -2.0}}
    assert risk_factors({OFZ: {"1Y": -2.0}}, False) == {F_KBD: {"1Y": -2.0}}


def test_kbd_floater_linear_factors_keep_the_discounting_in_the_basis():
    """TDM#467: a KBD floater's terms hold the KBD's B dK and RUONIA's (R - B) dO (the discounting): RUONIA's factor is
    R dO per bucket, the basis's B dK + (R - B) dO - R dO = B (dK - dO); together they are ``rates_linear``."""
    from desk.api.pnl_period import F_BASIS, F_RUONIA, RUONIA_CURVE, exact_factors, linear_factors
    b_, r_ = {"2Y": 300.0, "5Y": 22.0}, {"2Y": 10.0, "5Y": 3.0}  # the basis risk, the RUONIA risk (synthetic)
    d_k, d_o = {"2Y": 2.0, "5Y": 1.0}, {"2Y": 1.5, "5Y": 0.5}
    disc = {b: r_[b] - b_[b] for b in r_}
    terms = {OFZ: {b: b_[b] * d_k[b] for b in b_}, RUONIA_CURVE: {b: disc[b] * d_o[b] for b in b_}}
    lin = math.fsum(v for t in terms.values() for v in t.values())
    ruo = math.fsum(r_[b] * d_o[b] for b in r_)
    by_t, whole = linear_factors(OFZ, lin, terms, ruo, lin - ruo, r_, {OFZ: d_k, RUONIA_CURVE: d_o})
    assert whole == {} and set(by_t) == {F_RUONIA, F_BASIS}
    assert by_t[F_RUONIA] == pytest.approx({b: r_[b] * d_o[b] for b in r_})
    assert by_t[F_BASIS] == pytest.approx({b: b_[b] * (d_k[b] - d_o[b]) for b in b_})
    assert math.fsum(v for t in by_t.values() for v in t.values()) == pytest.approx(lin)
    assert exact_factors(OFZ, lin + 5.0, ruo, lin - ruo)[F_RUONIA] == pytest.approx(ruo)
