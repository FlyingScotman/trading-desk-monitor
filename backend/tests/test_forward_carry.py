"""TDM#66 / #72: the forward carry of a run (``scenario.carry``, ``GET /api/v1/concepts/carry``) on the golden fixtures
of 15.10.2026: core's horizon carry of the bonds (``valuation.bonds.horizon_bonds``, ``orchestrator.horizon``), the
funding of the future period by the funding engine with a flat or a stepped key rate, the rule sources (adapter,
settings, default), "not configured", the route's shape and the CBR meetings of window.DESK. Synthetic data only."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from rub_fixed_income import horizon_portfolio
from test_funding_run import DATE, SNAPS, _runs, _scan

from desk import funding_settings, store
from desk.api import create_app
from desk.marketdata import static_facts
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import bond_requests, horizon, prepare, value
from desk.rules import funding as F
from desk.rules.rate_index import FixingTable, HeldRates, KeyRatePath, follows_key_rate
from desk.scenario import carry, spread_curve
from desk.valuation import bonds

OFZ, CORP, KEY, OFZ_PK = "SU26238RMFS4", "RU000A0JXQ44", "RU000A109XR1", "SU29008RMFS8"
H1M = date(2026, 11, 15)


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(SNAPS), DATE.isoformat())


# ---- core's carry of the bonds -------------------------------------------------------------------------------------


def test_core_carry_is_horizon_portfolio_on_the_valuations_inputs(ctx):
    """``horizon`` gives exactly what core ``horizon_portfolio`` gives on the instruments, quotes, curves and policy
    ``value`` prices on; its start PV is the valuation's PV (the same calibrated spread)."""
    ids = (OFZ, CORP, KEY, OFZ_PK)
    reqs = [(s, 1000.0) for s in ids]
    outs, result = horizon(ctx, reqs, H1M, position_ids=list(ids))
    _, todo = bond_requests(ctx, reqs, list(ids))
    call = bonds.core_call(ctx.risk_market, todo, foreign={}, ofz_basis=ctx.ofz_basis)
    direct = horizon_portfolio(snapshot=call.snapshot, instruments=list(call.instruments.values()),
                               positions=call.positions, policy=call.policy, horizon_date=H1M)
    assert direct.coverage.status == "complete" and result is not None
    rows = {h.position_id: h for h in direct.positions}
    vals = {o.secid: o.valuation for o in value(ctx, reqs, position_ids=list(ids), fallback=False)}
    for s, o in zip(ids, outs):
        c, h = o.carry, rows[s]
        assert c is not None and o.reason is None, (s, o.reason, o.detail)
        assert c.carry == h.carry_rub and c.rolldown == h.rolldown_rub and c.total == h.horizon_pnl_rub
        assert c.coupon + c.pull + c.rolldown == pytest.approx(c.total, abs=1e-6)
        assert c.pv_start == pytest.approx(vals[s].pv_rub, abs=1e-6)
        assert c.spread_bp == pytest.approx(vals[s].z_spread_bp, abs=1e-6)


def test_a_fixed_bonds_one_month_carry_is_its_coupon_accrual_and_its_pull(ctx):
    """A fixed bond over a month without a coupon date: the coupon income is the accrual of its coupon rate
    (face x rate x days / 365 per bond), and its whole carry is close to its yield on the start PV; the price drift
    (``pull``) is the difference."""
    q = 1000.0
    (o,), _ = horizon(ctx, [(CORP, q)], H1M)
    v = value(ctx, [(CORP, q)], fallback=False)[0].valuation
    c = o.carry
    days = (H1M - DATE).days
    assert c.received == 0.0 and c.redemption_date > H1M
    assert c.coupon == pytest.approx(v.face_rub * q * v.current_coupon_pct / 100 * days / 365, rel=2e-3)
    assert c.total == pytest.approx(v.pv_rub * v.ytm_pct / 100 * days / 365, rel=0.15)
    assert c.pull == pytest.approx(c.total - c.coupon - c.rolldown, abs=1e-6)
    # a short position earns the opposite
    (s,), _ = horizon(ctx, [(CORP, -q)], H1M)
    assert s.carry.total == pytest.approx(-c.total) and s.carry.coupon == pytest.approx(-c.coupon)


def test_a_coupon_paid_before_the_horizon_is_in_the_coupon_income(ctx):
    (o,), _ = horizon(ctx, [(OFZ, 1000.0)], date(2027, 4, 15))  # a 6M horizon over the 26238's coupon of 2026-11
    c = o.carry
    assert c.received > 0 and c.coupon > 0
    assert c.coupon < c.received + 1000.0 * 1000 * 0.071 / 2  # the received coupon and at most a half-year's accrual


def test_a_bond_redeemed_before_the_horizon_is_carried_as_cash(ctx):
    """Core carries a bond only while it has flows after the horizon; one redeemed by then (here at its put) is the
    backend's: every flow to the horizon received against its dirty value at the start, no roll-down."""
    q = 1000.0
    v = value(ctx, [(CORP, q)], fallback=False)[0].valuation
    (o,), _ = horizon(ctx, [(CORP, q)], v.horizon_date + timedelta(days=30))
    c = o.carry
    assert o.reason is None and c is not None and c.method == "redeemed" and c.spread_bp is None
    assert c.pv_start == pytest.approx(v.pv_rub, abs=1e-6)  # the dirty value core prices the bond at
    assert c.received == pytest.approx(math.fsum(f.amount for f in v.cashflows) * q)
    assert c.total == pytest.approx(c.received - v.pv_rub) and c.rolldown == 0.0
    assert c.coupon + c.pull == pytest.approx(c.total)
    # the redemption at 100 % of the face is the rest of what it receives: coupons received = coupon + accrued bought
    assert c.received - (c.coupon + v.accrued_rub * q) == pytest.approx(v.face_rub * q, rel=1e-9)
    (core,), _ = horizon(ctx, [(CORP, q)], v.horizon_date - timedelta(days=30))
    assert core.carry.method == "core"


# ---- rate providers ------------------------------------------------------------------------------------------------


def test_flat_and_stepped_key_rate_paths():
    on = date(2026, 10, 15)
    t = FixingTable({"KEY_RATE": {on: 0.14, on + timedelta(days=1): 0.13}, "RUONIA": {on: 0.138},
                     "SOFR": {on: 0.04}})
    flat = HeldRates(t, on)
    assert flat.rate("KEY_RATE", date(2027, 1, 1)) == 0.14  # the run date's value, not a later row
    path = KeyRatePath(t, on, [(date(2026, 10, 27), 0.01), (date(2026, 12, 22), -0.02)])
    assert path.rate("KEY_RATE", date(2026, 10, 26)) == 0.14
    assert path.rate("KEY_RATE", date(2026, 10, 27)) == pytest.approx(0.15)
    assert path.rate("KEY_RATE", date(2027, 1, 1)) == pytest.approx(0.13)
    assert path.rate("RUONIA", date(2027, 1, 1)) == pytest.approx(0.138 - 0.01)  # today's spread to the key rate held
    assert path.rate("SOFR", date(2027, 1, 1)) == 0.04  # not a RUB index: held
    assert [k for _, k in path.path] == pytest.approx([0.15, 0.13])
    floor = KeyRatePath(t, on, [(date(2026, 10, 27), -0.5)])
    assert floor.rate("KEY_RATE", date(2026, 11, 1)) == 0.0  # never below 0
    assert follows_key_rate("TERM_RUONIA_3M") and follows_key_rate("RUSFAR") and not follows_key_rate("RUSFARCNY")


def test_meetings_take_effect_after_the_run_date():
    rows = [{"meeting_date": "2026-09-11", "effective_date": "2026-09-15", "is_estimated": False},
            {"meeting_date": "2026-10-23", "effective_date": None, "is_estimated": True},
            {"meeting_date": "2026-12-18", "effective_date": "2026-12-22", "is_estimated": False}]
    ms = carry.meetings(rows, DATE)
    assert [(m.date, m.effective, m.estimated) for m in ms] == [
        (date(2026, 10, 23), date(2026, 10, 26), True),  # no effective date: the next weekday
        (date(2026, 12, 18), date(2026, 12, 22), False)]


def test_horizon_and_steps_parsing():
    assert carry.horizon_date(DATE, "1M") == H1M and carry.horizon_date(DATE, "1y") == date(2027, 10, 15)
    assert carry.horizon_date(DATE, "2027-01-29") == date(2027, 1, 29)
    for bad in ("2W", "2026-10-15", "2036-01-01"):
        with pytest.raises(carry.CarryError):
            carry.horizon_date(DATE, bad)
    assert carry.parse_steps("25, -50,") == (25.0, -50.0) and carry.parse_steps(None) == ()
    with pytest.raises(carry.CarryError):
        carry.parse_steps("x")


# ---- the funding of the period, end to end on a stored run --------------------------------------------------------


NO_DEFAULT = {"default_rate": {"kind": "none"}, "default_rate_by_ccy": {}}
"""Funding settings with the market default rate off: only the adapter's rules (and the settings' own) fund."""


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """A settings folder of its own (the runs read the funding settings, #237): the defaults unless a test writes."""
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("DESK_HOME", str(tmp_path / "home"))


def _fs(**doc) -> funding_settings.FundingSettings:
    fs = funding_settings.from_doc({**NO_DEFAULT, **doc})
    assert fs.error is None, fs.error
    return fs


def _run(con, rid):
    run = con.execute("SELECT * FROM run WHERE run_id = %s", (rid,)).fetchone()
    names = [d.name for d in con.execute("SELECT * FROM run LIMIT 0").description]
    return dict(zip(names, run))


def _carry(con, rid, h="1M", ks="flat", steps=(), settings=None):
    return carry.forward_carry(con, SnapshotDir(SNAPS), _run(con, rid), h, ks, steps, settings=settings or _fs())


def _clean(con, rid, pid):
    fact = con.execute("SELECT fact FROM run_position WHERE run_id = %s AND position_id = %s", (rid, pid)).fetchone()[0]
    return fact["mv"] - fact["extra"]["quantity_bonds"] * fact["accrued"]


def test_forward_funding_flat_and_steps(store_url, ctx, tmp_path):
    """The adapter's rules on the run's positions held to the horizon: a market-value bond at KEY_RATE + 100 bp on its
    clean MV, the HTM lot at its contract rate, the cash of the derivatives book at RUONIA; a step of +100 bp at the
    first meeting moves the key-rate and RUONIA lines by step x base x days from its effective date / 365, not the
    lot. The carry is the bonds'; net = carry + funding."""
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        _, rid, _ = _runs(con, ctx)
        flat = _carry(con, rid)
        up = _carry(con, rid, ks="steps", steps=(100,))
        clean = _clean(con, rid, "T1|26238")
    assert flat["fundingStatus"] == "computed" and flat["horizonDate"] == "2026-11-15" and flat["days"] == 31
    pf = {p["positionId"]: p for p in flat["positions"]}
    pu = {p["positionId"]: p for p in up["positions"]}
    kr = ctx.market.latest_fixing("KEY_RATE")[1] / 100
    held = pf["T1|26238"]
    assert (held["fundingState"], held["fundingSource"], held["fundingRate"]) == ("funded", "adapter",
                                                                                 "KEY_RATE + 100 bp")
    assert held["funding"] == pytest.approx(-clean * (kr + 0.01) * 31 / 365)
    assert held["carry"] is not None and held["code"] is None
    assert held["net"] == pytest.approx(held["carry"] + held["funding"])
    assert pf["T1|XR1"]["fundingState"] == "funded"  # held, not new: funded over the whole period
    assert pf["H1|29008"]["fundingState"] == "funded" and pf["H1|29008"]["fundingRate"] == "14.25% contract"
    assert (pf["H1|29008"]["fundingSource"], pf["H1|29008"]["fundingRuleId"]) == ("adapter", "htm")  # rate_kind contract
    assert held["fundingRuleId"] == "trading_rub"
    assert pf["C1|26238"]["fundingState"] == "not_funded" and pf["C1|26238"]["funding"] == 0.0
    assert pf["D1|USD"]["carry"] is None and pf["D1|USD"]["code"] == "market_value"  # an FX row on its report value
    first = up["meetings"][0]
    assert first["stepBp"] == 100 and all(m["stepBp"] == 0 for m in up["meetings"][1:])
    # TDM#287: a year of meetings, the ones after the 1M horizon listed and not applied
    assert first["inHorizon"] and any(not m["inHorizon"] for m in up["meetings"])
    assert all(m["effective"] <= "2027-10-15" for m in up["meetings"])
    n = (H1M - date.fromisoformat(first["effective"])).days + 1
    assert pu["T1|26238"]["funding"] - held["funding"] == pytest.approx(-clean * 0.01 * n / 365)
    assert pu["H1|29008"]["funding"] == pytest.approx(pf["H1|29008"]["funding"])  # the contract rate
    assert pu["T1|26238"]["carry"] == held["carry"]  # the path moves the funding only
    cash = {(x["book"], x["kind"]): x["amount"] for x in flat["fundingBookLevel"]}
    cash_up = {(x["book"], x["kind"]): x["amount"] for x in up["fundingBookLevel"]}
    assert cash_up[("D1", "CASH")] - cash[("D1", "CASH")] == pytest.approx(-120_000_000 * 0.01 * n / 365)  # RUONIA
    assert cash_up[("C1", "ENTITY_CASH")] - cash[("C1", "ENTITY_CASH")] == pytest.approx(55_000_000 * 0.01 * n / 365)
    assert up["keyRatePath"][1]["keyRatePct"] == pytest.approx(kr * 100 + 1)
    assert any("сегодняшним спредом" in x for x in up["notes"])
    t = flat["totals"]
    assert t["nPositions"] == 5 and t["nCovered"] + t["nNotCovered"] == 5
    assert t["carry"] == pytest.approx(math.fsum(p["carry"] for p in flat["positions"] if p["carry"] is not None))
    covered = [p for p in flat["positions"] if p["carry"] is not None]
    assert t["funding"] == pytest.approx(math.fsum(p["funding"] for p in covered))
    assert t["fundingNoCarry"] == pytest.approx(math.fsum(p["funding"] or 0.0 for p in flat["positions"]
                                                          if p["carry"] is None))
    assert t["fundingBookLevel"] == pytest.approx(sum(cash.values()))
    assert t["netKnown"] == pytest.approx(math.fsum(p["net"] for p in covered))
    for g in [t, *(v for k in ("byBook", "byEntity", "byCcy", "byKind") for v in flat[k].values())]:
        # TDM#287: the identity of every group, the null parts 0
        if g["nNoCarryNoFunding"]:  # strict B1: an unknown funding of a position without a forecast
            assert g["fundingNoCarry"] is None and g["fundingTotal"] is None and g["net"] is None
            continue
        parts = (g["funding"] or 0.0) + (g["fundingNoCarry"] or 0.0) + g["fundingBookLevel"]
        assert g["fundingTotal"] == pytest.approx(parts)
        if g["net"] is not None:
            assert g["net"] == pytest.approx(g["carry"] + parts) and g["nCoveredNoFunding"] == 0
        else:
            assert g["carry"] is None  # every covered position is funded here: only a group without a forecast
    assert t["net"] is not None and t["dv01"] == pytest.approx(math.fsum(p["dv01"] or 0.0 for p in flat["positions"]))
    ofz = pf["T1|26238"]  # the monitor's DV01, the OFZ spread risk as cs01; a RUB position in RUB
    with store.connect(store_url) as con:
        fact = con.execute("SELECT fact FROM run_position WHERE run_id = %s AND position_id = 'T1|26238'",
                           (rid,)).fetchone()[0]
    assert ofz["dv01"] == fact["dv01"] and ofz["cs01"] == fact["ofz_spr01"] != 0 and ofz["fxRate"] == 1.0
    assert ofz["inCcy"] == pytest.approx({k: ofz[k] for k in ("carry", "coupon", "pull", "rolldown", "funding", "net")}
                                         | {"spreadRoll": None})
    usd = pf["D1|USD"]
    assert usd["ccy"] == "USD" and usd["fxRate"] > 1 and usd["inCcy"]["funding"] == 0.0
    assert flat["byBook"]["D1"]["fundingBookLevel"] == pytest.approx(cash[("D1", "CASH")])
    assert flat["byEntity"]["ENT1"]["nPositions"] == 4 and flat["byEntity"]["ENT2"]["nPositions"] == 1
    assert flat["byBook"]["T1"]["carry"] == pytest.approx(pf["T1|26238"]["carry"] + (pf["T1|XR1"]["carry"] or 0.0))


def test_no_funding_rule_anywhere_is_not_configured_and_null(store_url, ctx, tmp_path):
    with store.connect(store_url) as con:
        _scan(con, tmp_path, policy=False, rules=False)
        _, rid, _ = _runs(con, ctx)
        out = _carry(con, rid, "3M")  # the settings' default rate off (NO_DEFAULT)
    assert out["fundingStatus"] == "not_configured" and "не настроено" in out["fundingText"]
    assert all(p["funding"] is None and p["net"] is None and p["fundingState"] == "not_configured"
               and p["fundingSource"] == "none" for p in out["positions"])
    assert out["totals"]["funding"] is None and out["totals"]["net"] is None and out["totals"]["carry"] is not None
    assert out["fundingBookLevel"] == [] and all(g["funding"] is None for g in out["byBook"].values())
    with store.connect(store_url) as con:
        year = _carry(con, rid, "1Y")  # the key-rate floater matures on 2027-10-14, before the horizon
    p = {x["positionId"]: x for x in year["positions"]}
    xr1 = p["T1|XR1"]
    assert xr1["carryMethod"] == "redeemed" and xr1["code"] is None and xr1["rolldown"] == 0.0
    assert xr1["carry"] == pytest.approx(xr1["coupon"] + xr1["pull"])
    assert p["T1|26238"]["carryMethod"] == "core" and any("carryMethod redeemed" in n for n in year["notes"])


def test_the_settings_default_rate_funds_at_dec01s_clean_base(store_url, ctx, tmp_path):
    """No adapter rule (TDM#237): the settings' market default rate funds a bond book on its clean MV (DEC01), the
    line named «рыночная ставка по умолчанию»; the source is ``default``."""
    fs = _fs(default_rate={"kind": "ruonia", "spread_bp": 50})
    with store.connect(store_url) as con:
        _scan(con, tmp_path, policy=False, rules=False)
        _, rid, _ = _runs(con, ctx)
        out = _carry(con, rid, settings=fs)
        clean = _clean(con, rid, "T1|26238")
    p = {x["positionId"]: x for x in out["positions"]}
    assert out["fundingStatus"] == "computed"
    held = p["T1|26238"]
    ruo = ctx.market.latest_fixing("RUONIA")[1] / 100
    assert (held["fundingState"], held["fundingSource"], held["fundingNote"]) == (
        "funded", "default", F.DEFAULT_RATE_LABEL)
    assert held["fundingRuleId"] == "default:RUB" and F.DEFAULT_RATE_LABEL in held["fundingRate"]
    assert held["funding"] == pytest.approx(-clean * (ruo + 0.005) * 31 / 365)  # the clean MV: DEC01
    assert any(F.DEFAULT_RATE_LABEL in n for n in out["notes"])


def test_tiers_override_adapter_settings(store_url, ctx, tmp_path):
    """The adapter's rule wins over a settings rule and the default; a settings rule marked override wins over it."""
    rule = {"rule_id": "s-rub", "book_type": "TRADING", "ccy": "RUB", "rate_kind": "fixed", "fixed_rate_pct": 1}
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        _, rid, _ = _runs(con, ctx)
        plain = _carry(con, rid, settings=_fs(rules=[rule], default_rate={"kind": "fixed", "fixed_rate_pct": 2}))
        over = _carry(con, rid, settings=_fs(rules=[{**rule, "override": True}]))
        clean = _clean(con, rid, "T1|26238")
    p = {x["positionId"]: x for x in plain["positions"]}
    assert (p["T1|26238"]["fundingSource"], p["T1|26238"]["fundingRate"]) == ("adapter", "KEY_RATE + 100 bp")
    o = {x["positionId"]: x for x in over["positions"]}["T1|26238"]
    assert (o["fundingSource"], o["fundingRuleId"]) == ("settings_override", "s-rub")
    assert o["funding"] == pytest.approx(-clean * 0.01 * 31 / 365)


# ---- the route, window.DESK ----------------------------------------------------------------------------------------


def test_route_shape_cache_and_errors(store_url, ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DESK_SNAPSHOT_CACHE", str(SNAPS))
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        _, rid, _ = _runs(con, ctx)
        static_facts.load_cbr(con, ctx.market.snapshot)
    c = TestClient(create_app(store_url))
    r = c.get(f"/api/v1/concepts/carry?run={rid}&horizon=3M&ks=steps&steps=25,-50")
    assert r.status_code == 200, r.text
    j = r.json()
    assert {"horizonDate", "ks", "meetings", "positions", "totals", "byBook", "byEntity", "byCcy", "byKind",
            "fundingStatus", "notes"} <= set(j)
    assert j["ks"] == "steps" and j["horizonDate"] == "2027-01-15" and j["fundingStatus"] == "computed", j["fundingText"]
    assert [m["stepBp"] for m in j["meetings"]][:2] == [25, -50] and all(m["effective"] > "2026-10-15"
                                                                       for m in j["meetings"])
    p = j["positions"][0]
    assert {"carry", "coupon", "pull", "rolldown", "funding", "net", "fundingState", "fundingSource", "code"} <= set(p)
    assert set(j["byCcy"]) >= {"RUB"} and set(j["byKind"]) and j["byEntity"]
    tag = r.headers["etag"]
    assert c.get(f"/api/v1/concepts/carry?run={rid}&horizon=3M&ks=steps&steps=25,-50",
                 headers={"If-None-Match": tag}).status_code == 304
    default = c.get("/api/v1/concepts/carry").json()  # the run shown, 3M, flat
    assert default["runId"] == rid and default["ks"] == "flat" and all(m["stepBp"] == 0 for m in default["meetings"])
    assert c.get("/api/v1/concepts/carry?horizon=2W").status_code == 422
    assert c.get("/api/v1/concepts/carry?ks=implied").status_code == 422
    assert c.get("/api/v1/concepts/carry?spread=sector").status_code == 422
    sc = c.get(f"/api/v1/concepts/carry?run={rid}&spread=curve").json()
    assert sc["spread"] == "curve" and {"spreadRoll", "spreadRollBp", "spreadCurve"} <= set(sc["positions"][0])
    assert "spreadRoll" in sc["totals"] and sc["runId"] == rid
    assert c.get("/api/v1/concepts/carry?run=999999").status_code == 404
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()
    ms = desk["CBR_MEETINGS"]
    assert ms and ms[0] == {"date": "2026-10-23", "effective": ms[0]["effective"], "estimated": True}
    assert all(m["effective"] > "2026-10-15" for m in ms)



# ---- the spread rolled along its peers' curve (spread=curve) -------------------------------------------------------


def test_peer_curve_from_buckets_and_its_roll():
    """A grade's curve: a point (median tenor, median spread) per C09 tenor bucket of at least MIN_BUCKET peers, linear
    between them, flat beyond; a bucket of two issues is no point, a grade with one point no curve."""
    peers = [("A", 0.5, 100), ("A", 0.6, 110), ("A", 0.7, 120),        # bucket <= 1Y: (0.6, 110)
             ("A", 4.0, 200), ("A", 4.5, 210), ("A", 5.0, 220),        # bucket 3-5Y: (4.5, 210)
             ("A", 2.5, 900), ("A", 2.6, 900),                         # two issues: not a point
             ("BBB", 1.5, 300), ("BBB", 1.6, 300), ("BBB", 1.7, 300)]  # one point: no curve
    curves = spread_curve.build_curves(peers)
    assert set(curves) == {"A"} and curves["A"].points == ((0.6, 110), (4.5, 210)) and curves["A"].issues == 6
    c = curves["A"]
    assert c.at(0.1) == 110 and c.at(9) == 210 and c.at(2.55) == pytest.approx(110 + 100 * 1.95 / 3.9)
    assert spread_curve.roll(c, 4.5, 1.0) == pytest.approx(c.at(3.5) - 210)
    assert spread_curve.curve_for(curves, "A+") == (c, "A (для A+)")  # a neighbouring grade
    assert spread_curve.curve_for(curves, None) == (None, "no_rating")
    assert spread_curve.curve_for(curves, "BBB") == (None, "no_peer_curve")


def test_spread_roll_on_a_synthetic_peer_curve(ctx):
    """``spread_rolls``: a fixed corporate rolls along its grade's curve, spreadRoll = CS01 x Δ added to its carry; an
    OFZ and a floater keep their spread; without a rating (the fixture's static has none) the spread is held."""
    curve = spread_curve.PeerCurve("A", ((0.5, 80.0), (3.0, 180.0)), 9)
    cs01 = -40.0

    def pc(pid, secid):
        return carry.PositionCarry(pid, "B", secid, "exact", {"cs01": cs01}, carry=1000.0, method="core")

    corp, ofz, flt = pc("c", CORP), pc("o", OFZ), pc("f", KEY)
    curves = carry.spread_rolls(ctx, [corp, ofz, flt], H1M, {"A": curve}, grade_of=lambda t: "A")
    terms = ctx.static.bonds[CORP]
    ten = spread_curve.tenor(terms, ctx.market.quote(terms), DATE)
    delta = curve.at(ten - (H1M - DATE).days / 365.25) - curve.at(ten)
    assert delta < 0 and corp.spread_roll_bp == pytest.approx(delta)  # shorter: tighter on an upward curve
    assert corp.spread_roll == pytest.approx(cs01 * delta) and corp.carry == pytest.approx(1000.0 + cs01 * delta)
    assert corp.spread_curve == "A" and curves == {"A": curve}
    assert (ofz.spread_curve, ofz.spread_roll, ofz.carry) == ("ofz_held", None, 1000.0)
    assert (flt.spread_curve, flt.spread_roll) == ("floater_held", None)
    unrated = pc("u", CORP)
    carry.spread_rolls(ctx, [unrated], H1M, {"A": curve})  # the desk rating: none in the fixture
    assert (unrated.spread_curve, unrated.spread_roll, unrated.carry) == ("no_rating", None, 1000.0)


def test_spread_modes_on_a_stored_run(store_url, ctx, tmp_path):
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        _, rid, _ = _runs(con, ctx)
        held = _carry(con, rid)
        curve = carry.forward_carry(con, SnapshotDir(SNAPS), _run(con, rid), "1M", spread="curve", settings=_fs())
    assert held["spread"] == "held" and held["spreadCurves"] == []
    assert any("кэрри = купон + pull + roll-down + spreadRoll" in n for n in curve["notes"])
    assert not any("spreadRoll (roll" in n for n in held["notes"])
    assert all(p["spreadRoll"] is None and p["spreadCurve"] is None for p in held["positions"])
    p = {x["positionId"]: x for x in curve["positions"]}
    assert p["T1|26238"]["spreadCurve"] == "ofz_held" and p["T1|XR1"]["spreadCurve"] == "floater_held"
    assert p["D1|USD"]["spreadCurve"] == "not_covered" and curve["totals"]["spreadRoll"] is None
    assert curve["totals"]["carry"] == pytest.approx(held["totals"]["carry"])  # nothing rolled: the same carry
    assert any("spread=curve" in n for n in curve["notes"])
    assert any("переносится на Z-спред через CS01, первое приближение" in n for n in curve["notes"])  # TDM#281


def test_group_net_with_partial_funding_and_amounts_in_ccy():
    """Rule B1 (TDM#287): a group whose covered position has no funding has no net, netKnown sums the positions with
    both; a position's amounts in its currency at the run's rate (mv_rub / mv)."""
    run = {"run_id": 1, "business_date": DATE}
    usd = {"ccy": "USD", "mv": 1000.0, "mv_rub": 90000.0, "dv01": -5.0, "cs01": -2.0}
    rub = {"ccy": "RUB", "mv": 5000.0, "mv_rub": 5000.0, "dv01": -1.0, "cs01": 0.0, "ofz_spr01": -0.5}
    cs = [carry.PositionCarry("a", "B", "X", "exact", usd, carry=900.0, coupon=800.0, pull=100.0, rolldown=0.0),
          carry.PositionCarry("b", "B", "Y", "exact", rub, carry=50.0, coupon=40.0, pull=5.0, rolldown=5.0),
          carry.PositionCarry("c", "C", "Z", "exact", rub, carry=30.0, coupon=30.0, pull=0.0, rolldown=0.0)]
    funding = carry.ForwardFunding("computed", "", {
        "a": {"state": "funded", "amount": -450.0, "amountCcy": -5.0, "source": "adapter"},
        "b": {"state": "problem", "amount": None, "source": "none", "reason": "no rule"},
        "c": {"state": "funded", "amount": -10.0, "amountCcy": -10.0, "source": "adapter"}})
    out = carry.assemble(run, H1M, "flat", [], cs, funding, {"B": "E", "C": "E"}, lambda f: None, [], [])
    b = out["byBook"]["B"]
    assert b["net"] is None and b["nCoveredNoFunding"] == 1 and b["netKnown"] == pytest.approx(900.0 - 450.0)
    assert out["byBook"]["C"]["net"] == pytest.approx(20.0) and out["totals"]["net"] is None
    assert out["totals"]["netKnown"] == pytest.approx(450.0 + 20.0)
    assert b["dv01"] == -6.0 and b["cs01"] == -2.5  # the OFZ spread risk where cs01 is 0
    a = out["positions"][0]
    assert a["fxRate"] == 90.0 and a["inCcy"] == pytest.approx({"carry": 10.0, "coupon": 800 / 90, "pull": 100 / 90,
                                                               "rolldown": 0.0, "spreadRoll": None,
                                                               "funding": -5.0, "net": 5.0})


def test_group_net_is_null_when_a_position_without_carry_has_no_funding():
    """Rule B1 applied strictly (coordinator, 26.10, #297): a position without a carry forecast whose funding is unknown
    makes its group's fundingNoCarry, fundingTotal and net null (``nNoCarryNoFunding`` counts them); netKnown still
    sums the positions with both; a position not funded by the rules (amount 0) is no unknown."""
    run = {"run_id": 1, "business_date": DATE}
    rub = {"ccy": "RUB", "mv": 1000.0, "mv_rub": 1000.0, "dv01": -1.0, "cs01": -1.0}
    cs = [carry.PositionCarry("a", "B", "X", "exact", rub, carry=100.0, coupon=100.0, pull=0.0, rolldown=0.0),
          carry.PositionCarry("b", "B", "Y", "market_value", rub, code="market_value"),
          carry.PositionCarry("c", "C", "Z", "market_value", rub, code="market_value")]
    funding = carry.ForwardFunding("computed", "", {
        "a": {"state": "funded", "amount": -40.0, "amountCcy": -40.0, "source": "adapter"},
        "b": {"state": "problem", "amount": None, "source": "none", "reason": "no rule"},
        "c": {"state": "not_funded", "amount": 0.0, "amountCcy": 0.0, "source": "none", "reason": "not on MV"}})
    out = carry.assemble(run, H1M, "flat", [], cs, funding, {"B": "E", "C": "E"}, lambda f: None, [], [])
    b, c = out["byBook"]["B"], out["byBook"]["C"]
    assert b["nNoCarryNoFunding"] == 1 and b["nCoveredNoFunding"] == 0
    assert b["fundingNoCarry"] is None and b["fundingTotal"] is None and b["net"] is None
    assert b["funding"] == pytest.approx(-40.0) and b["netKnown"] == pytest.approx(60.0)
    assert c["nNoCarryNoFunding"] == 0 and c["fundingNoCarry"] == 0.0 and c["net"] is None  # nothing covered
    assert out["totals"]["net"] is None and out["totals"]["nNoCarryNoFunding"] == 1
